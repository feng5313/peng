/**
 * Monaco Editor LSP Provider 模块
 *
 * 将后端LSP能力桥接到前端Monaco Editor。
 * 为所有后端支持的语言（Python, C/C++, Rust, Go, Zig, Lua等）注册Monaco Language Provider，
 * 提供：跳转定义、查找引用、悬停信息、文档符号、查找实现、代码补全等功能。
 *
 * 注意：TypeScript/JavaScript已有Monaco内置语言服务，无需重复注册。
 */

// ==================== LSP Provider 注册 ====================

// 记录已注册provider的语言，避免重复注册
const _registeredLanguages = new Set();

// LSP请求缓存（避免短时间内重复请求）
const _requestCache = new Map();
const CACHE_TTL = 3000; // 3秒缓存

// 防抖：避免频繁触发hover等请求
var _pendingRequests = {};

// LSP错误提示节流：同语言+N分钟内只提示一次
var _lspErrorShown = {};
var LSP_ERROR_INTERVAL = 120000; // 2分钟内不重复提示同一语言的错误

function _debouncedRequest(key, fn, delay) {
  if (_pendingRequests[key]) {
    clearTimeout(_pendingRequests[key].timer);
  }
  return new Promise(function(resolve) {
    _pendingRequests[key] = {
      timer: setTimeout(function() {
        delete _pendingRequests[key];
        fn().then(resolve);
      }, delay || 300),
      resolve: resolve,
    };
  });
}

/**
 * 通过Monaco model反查文件路径
 * _sharedModels的key可能是相对路径或绝对路径。
 */
function _getFilePathFromModel(model) {
  if (window._sharedModels) {
    for (var filePath in window._sharedModels) {
      if (window._sharedModels[filePath] && window._sharedModels[filePath].model === model) {
        return filePath;
      }
    }
  }
  var uri = model.uri.toString();
  if (uri.indexOf('file://') === 0) {
    return uri.substring(7);
  }
  return '';
}

/**
 * 将文件路径转为绝对路径
 * 相对路径如 "src/peng/task/database.py" 需要加上工作目录前缀
 */
function _resolveFilePath(filePath) {
  if (!filePath) return filePath;
  // 已经是绝对路径（Linux /开头 或 Windows 盘符 X:开头）
  if (filePath.charAt(0) === '/' || (filePath.length > 1 && filePath.charAt(1) === ':')) {
    return filePath;
  }
  // 相对路径：加上当前工作目录
  var workdir = state.workdir || state.currentWorkdir || '';
  if (workdir) {
    var sep = (workdir.indexOf('\\') >= 0) ? '\\' : '/';
    return workdir.replace(/[\\\/]+$/, '') + sep + filePath;
  }
  return filePath;
}

/**
 * 根据文件路径查找对应的nest（从editor tabs中匹配）
 */
function _getNestForFile(filePath) {
  if (!filePath || !state.editorGroups) return null;
  // 遍历所有group的所有tab找匹配
  for (var i = 0; i < state.editorGroups.length; i++) {
    var group = state.editorGroups[i];
    if (group.tabs) {
      for (var j = 0; j < group.tabs.length; j++) {
        var tab = group.tabs[j];
        if (tab.path === filePath || _pathsMatch(tab.path, filePath)) {
          return tab.nest || null;
        }
      }
    }
  }
  return null;
}

/**
 * 检查两个文件路径是否匹配（处理相对路径/绝对路径差异）
 */
function _pathsMatch(path1, path2) {
  if (path1 === path2) return true;
  if (path1.length > path2.length && path1.lastIndexOf(path2) === path1.length - path2.length) return true;
  if (path2.length > path1.length && path2.lastIndexOf(path1) === path2.length - path1.length) return true;
  return false;
}

/**
 * 通过文件路径查找对应的Monaco model URI
 * 处理_sharedModels中key（相对路径）与后端返回路径（绝对路径）不匹配的问题
 */
function _getMonacoUriForPath(filePath) {
  if (window._sharedModels) {
    // 精确匹配
    if (window._sharedModels[filePath] && window._sharedModels[filePath].model) {
      return window._sharedModels[filePath].model.uri;
    }
    // 模糊匹配（相对路径 vs 绝对路径）
    for (var key in window._sharedModels) {
      if (window._sharedModels[key] && window._sharedModels[key].model && _pathsMatch(key, filePath)) {
        return window._sharedModels[key].model.uri;
      }
    }
  }
  return monaco.Uri.parse('file://' + filePath);
}

/**
 * 获取当前活动tab的nest来源
 */
function _getActiveTabNest() {
  if (typeof state === 'undefined') return null;
  if (state.editorGroups) {
    for (var i = 0; i < state.editorGroups.length; i++) {
      var group = state.editorGroups[i];
      if (group.currentTabId && group.tabs) {
        for (var j = 0; j < group.tabs.length; j++) {
          if (group.tabs[j].id === group.currentTabId) {
            return group.tabs[j].nest || null;
          }
        }
      }
    }
  }
  var activeTab = state.tabs ? state.tabs[state.activeTab] : null;
  return activeTab ? (activeTab.nest || null) : null;
}

/**
 * 向后端LSP API发送请求，自动附带nest参数
 */
async function _lspRequest(action, params) {
  if (!params.file_path) return { success: false, error: '无文件路径' };

  // 确保file_path是绝对路径
  params.file_path = _resolveFilePath(params.file_path);

  // 确定nest：优先使用params中指定的，否则从文件路径匹配tab推断，最后用活跃tab
  var nest = params.nest || _getNestForFile(params.file_path) || _getActiveTabNest();

  // 检查缓存（completion不缓存，每次输入都需要最新结果）
  var cacheKey = action + ':' + params.file_path + ':' + params.line + ':' + params.column + ':' + (nest || '');
  if (action !== 'completion') {
    var cached = _requestCache.get(cacheKey);
    if (cached && Date.now() - cached.time < CACHE_TTL) {
      return cached.result;
    }
  }

  try {
    var body = {
      file_path: params.file_path,
      line: params.line || 1,
      column: params.column || 1,
    };
    if (nest) body.nest = nest;

    // 传文件内容给后端LSP daemon（包括completion，否则后端读磁盘旧文件看不到用户正在输入的代码）
    // 大文件（超过large_file_threshold）不发content，让daemon读磁盘文件，避免TCP JSON行协议传输超限
    var editor = _getActiveEditor();
    if (editor) {
      var content = editor.getModel().getValue();
      var threshold = (state.webuiConfig && state.webuiConfig.large_file_threshold) || (3 * 1024 * 1024);
      if (content.length < threshold) {
        body.content = content;
      }
    }

    var response = await fetch('/api/lsp/' + action, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });

    if (!response.ok) {
      var errMsg = 'HTTP ' + response.status;
      _showLspError(action, errMsg);
      return { success: false, error: errMsg };
    }

    var result = await response.json();
    
    // 检查LSP不可用的错误，弹出提示
    // 但跳过临时性错误（daemon启动中、连接断开等），这些是正常现象不应打扰用户
    if (result.success === false && result.error) {
      var err = result.error;
      var isTransient = err.indexOf('Connection') >= 0 || err.indexOf('daemon') >= 0 
        || err.indexOf('timeout') >= 0 || err.indexOf('超时') >= 0
        || err.indexOf('启动') >= 0 || err.indexOf('AST') >= 0;
      // completion失败完全静默（用户不会注意到单个completion失败）
      if (!isTransient && action !== 'completion') {
        _showLspError(action, result.error);
      }
    }
    
    // 缓存结果（completion不缓存）
    if (action !== 'completion') {
      _requestCache.set(cacheKey, { result: result, time: Date.now() });
    }
    return result;
  } catch (error) {
    console.warn('[LSP] ' + action + ' 请求失败:', error);
    _showLspError(action, error.message);
    return { success: false, error: error.message };
  }
}

/**
 * 显示LSP错误提示（带节流，同语言2分钟内只提示一次）
 */
function _showLspError(action, errorMsg) {
  // 从当前编辑器获取语言
  var lang = 'unknown';
  var editor = _getActiveEditor();
  if (editor) {
    lang = editor.getModel().getLanguageId();
  }
  
  var errorKey = lang + ':' + (errorMsg || '').substring(0, 50);
  var now = Date.now();
  if (_lspErrorShown[errorKey] && now - _lspErrorShown[errorKey] < LSP_ERROR_INTERVAL) {
    return; // 节流中，不重复提示
  }
  _lspErrorShown[errorKey] = now;

  // 构造用户友好的提示
  var hint = _getLspInstallHint(lang, errorMsg);
  if (typeof showToast === 'function') {
    showToast('warning', 'LSP不可用', hint, 5000);
  } else if (typeof window !== 'undefined' && window._showToastFallback) {
    window._showToastFallback(hint);
  }
}

/**
 * 根据语言和错误信息生成安装提示
 */
function _getLspInstallHint(languageId, errorMsg) {
  var langName = {
    'python': 'Python', 'c': 'C', 'cpp': 'C++', 'rust': 'Rust',
    'go': 'Go', 'zig': 'Zig', 'lua': 'Lua'
  }[languageId] || languageId;
  
  var serverHints = {
    'python': 'pylsp (pip install python-lsp-server)',
    'c': 'clangd (apt install clangd)',
    'cpp': 'clangd (apt install clangd)',
    'rust': 'rust-analyzer (rustup component add rust-analyzer)',
    'go': 'gopls (go install golang.org/x/tools/gopls@latest)',
    'zig': 'zls',
    'lua': 'lua-language-server'
  };
  
  var serverHint = serverHints[languageId] || 'LSP服务器';
  
  if (errorMsg && errorMsg.indexOf('未找到') >= 0) {
    return langName + ' LSP服务器未安装。请安装: ' + serverHint;
  }
  if (errorMsg && errorMsg.indexOf('未连接') >= 0) {
    return 'Nest未连接，请检查连接状态';
  }
  if (errorMsg && errorMsg.indexOf('lsp-query') >= 0) {
    return '远程nest上未找到peng项目，请确保peng已部署到该nest';
  }
  return langName + ' LSP暂不可用。请确认已安装: ' + serverHint;
}

/**
 * 将后端返回的location转换为Monaco Definition对象
 * 同文件：使用当前model的URI，Monaco可直接跳转
 * 跨文件：调用openEditorForFile打开目标文件并定位，返回空数组让Monaco不再处理
 */
function _toMonacoLocation(loc, currentFilePath) {
  if (!loc || !loc.path) return null;

  // 判断是否是同文件跳转
  var isSameFile = _pathsMatch(loc.path, currentFilePath);

  if (!isSameFile) {
    // 跨文件跳转：用openEditorForFile打开目标文件
    _openFileAtLocation(loc);
    return null; // 返回null，让filter过滤掉，最终返回空数组让Monaco不做跳转
  }

  // 同文件跳转：返回当前model的URI，Monaco可直接跳转
  var uri = _getMonacoUriForPath(loc.path);
  return {
    uri: uri,
    range: new monaco.Range(
      loc.startLine || 1, loc.startColumn || 1,
      loc.endLine || 1, loc.endColumn || 1
    ),
  };
}

/**
 * 跨文件跳转：打开目标文件并定位到指定行
 */
async function _openFileAtLocation(loc) {
  try {
    var filePath = loc.path;
    var line = loc.startLine || 1;
    var column = loc.startColumn || 1;

    if (typeof openEditorForFile === 'function') {
      await openEditorForFile(filePath);
    }

    // 等待文件加载后定位光标
    setTimeout(function () {
      var editor = _getActiveEditor();
      if (editor) {
        editor.setPosition({ lineNumber: line, column: column });
        editor.revealLineInCenter(line);
        editor.focus();
      }
    }, 500);
  } catch (e) {
    console.warn('[LSP] 跨文件跳转失败:', e);
  }
}

/**
 * 获取当前活动的Monaco编辑器实例
 */
function _getActiveEditor() {
  if (typeof monacoEditor !== 'undefined' && monacoEditor) return monacoEditor;
  if (typeof state !== 'undefined' && state.editorGroups) {
    // 找到当前活动分组的编辑器
    for (var i = 0; i < state.editorGroups.length; i++) {
      var group = state.editorGroups[i];
      if (group.monacoEditor) return group.monacoEditor;
    }
  }
  return null;
}

// ==================== 各 Provider 注册 ====================

function _registerDefinitionProvider(languageId) {
  monaco.languages.registerDefinitionProvider(languageId, {
    provideDefinition: async function (model, position) {
      var filePath = _getFilePathFromModel(model);
      if (!filePath) return [];

      try {
        var result = await _lspRequest('definition', {
          file_path: filePath, line: position.lineNumber, column: position.column,
        });
        if (!result.success || !result.locations || result.locations.length === 0) return [];
        return result.locations.map(function(loc) { return _toMonacoLocation(loc, filePath); }).filter(Boolean);
      } catch (e) {
        console.warn('[LSP] definition error:', e);
        return [];
      }
    },
  });
}

function _registerReferenceProvider(languageId) {
  monaco.languages.registerReferenceProvider(languageId, {
    provideReferences: async function (model, position, context) {
      var filePath = _getFilePathFromModel(model);
      if (!filePath) return [];

      try {
        var result = await _lspRequest('references', {
          file_path: filePath, line: position.lineNumber, column: position.column,
        });
        if (!result.success || !result.locations || result.locations.length === 0) return [];
        return result.locations.map(function(loc) { return _toMonacoLocation(loc, filePath); }).filter(Boolean);
      } catch (e) {
        console.warn('[LSP] references error:', e);
        return [];
      }
    },
  });
}

function _registerHoverProvider(languageId) {
  var _lastHoverTime = 0;
  monaco.languages.registerHoverProvider(languageId, {
    provideHover: async function (model, position) {
      var filePath = _getFilePathFromModel(model);
      if (!filePath) return null;

      // 防抖：500ms内不重复请求
      var now = Date.now();
      if (now - _lastHoverTime < 500) return null;
      _lastHoverTime = now;

      try {
        var result = await _lspRequest('hover', {
          file_path: filePath, line: position.lineNumber, column: position.column,
        });
        if (!result.success || !result.contents || result.contents.length === 0) return null;

        var contents = result.contents.map(function (c) { return { value: c }; });
        var range = undefined;
        if (result.range) {
          range = new monaco.Range(
            result.range.startLine, result.range.startColumn,
            result.range.endLine, result.range.endColumn
          );
        }
        return { contents: contents, range: range };
      } catch (e) {
        console.warn('[LSP] hover error:', e);
        return null;
      }
    },
  });
}

function _registerDocumentSymbolProvider(languageId) {
  monaco.languages.registerDocumentSymbolProvider(languageId, {
    provideDocumentSymbols: async function (model) {
      var filePath = _getFilePathFromModel(model);
      if (!filePath) return [];

      try {
        var result = await _lspRequest('symbols', { file_path: filePath });
        if (!result.success || !result.symbols || result.symbols.length === 0) return [];
        return _convertSymbols(result.symbols);
      } catch (e) {
        console.warn('[LSP] symbols error:', e);
        return [];
      }
    },
  });
}

function _convertSymbols(symbols) {
  if (!symbols) return [];
  var result = [];
  for (var i = 0; i < symbols.length; i++) {
    var sym = symbols[i];
    var kind = _monacoSymbolKind(sym.kind);
    var s = {
      name: sym.name, kind: kind, detail: sym.detail || '',
      range: new monaco.Range(sym.startLine || 1, sym.startColumn || 1, sym.endLine || 1, sym.endColumn || 1),
      selectionRange: new monaco.Range(sym.startLine || 1, sym.startColumn || 1, sym.startLine || 1, Math.max(sym.startColumn || 1, (sym.startColumn || 1) + sym.name.length)),
      tags: [],
    };
    if (sym.children && sym.children.length > 0) s.children = _convertSymbols(sym.children);
    result.push(s);
  }
  return result;
}

function _monacoSymbolKind(kindStr) {
  var map = {
    'File': 0, 'Module': 1, 'Namespace': 2, 'Package': 3,
    'Class': 4, 'Method': 5, 'Property': 6, 'Field': 7,
    'Constructor': 8, 'Enum': 9, 'Interface': 10, 'Function': 11,
    'Variable': 12, 'Constant': 13, 'String': 14, 'Number': 15,
    'Boolean': 16, 'Array': 17, 'Object': 18, 'Key': 19,
    'Null': 20, 'EnumMember': 21, 'Struct': 22, 'Event': 23,
    'Operator': 24, 'TypeParameter': 25,
  };
  return map[kindStr] !== undefined ? map[kindStr] : 12;
}

function _registerImplementationProvider(languageId) {
  monaco.languages.registerImplementationProvider(languageId, {
    provideImplementation: async function (model, position) {
      var filePath = _getFilePathFromModel(model);
      if (!filePath) return [];

      try {
        var result = await _lspRequest('implementation', {
          file_path: filePath, line: position.lineNumber, column: position.column,
        });
        if (!result.success || !result.locations || result.locations.length === 0) return [];
        return result.locations.map(function(loc) { return _toMonacoLocation(loc, filePath); }).filter(Boolean);
      } catch (e) {
        console.warn('[LSP] implementation error:', e);
        return [];
      }
    },
  });
}

/**
 * 注册CompletionItemProvider - 代码补全
 * 这是用户反馈的"成员变量、成员函数补全"功能
 */
function _registerCompletionProvider(languageId) {
  monaco.languages.registerCompletionItemProvider(languageId, {
    triggerCharacters: ['.', '(', ' ', ':', '>', 'a', 'b', 'c', 'd', 'e', 'f', 'g', 'h', 'i', 'j', 'k', 'l', 'm', 'n', 'o', 'p', 'q', 'r', 's', 't', 'u', 'v', 'w', 'x', 'y', 'z', 'A', 'B', 'C', 'D', 'E', 'F', 'G', 'H', 'I', 'J', 'K', 'L', 'M', 'N', 'O', 'P', 'Q', 'R', 'S', 'T', 'U', 'V', 'W', 'X', 'Y', 'Z', '0', '1', '2', '3', '4', '5', '6', '7', '8', '9', '_'],
    provideCompletionItems: async function (model, position) {
      var filePath = _getFilePathFromModel(model);
      if (!filePath) return { suggestions: [] };

      try {
        var result = await _lspRequest('completion', {
          file_path: filePath,
          line: position.lineNumber,
          column: position.column,
        });

        if (!result.success || !result.items || result.items.length === 0) return { suggestions: [] };

        // 计算补全range：需要确定替换范围的起始列
        var lineContent = model.getLineContent(position.lineNumber);
        var textBeforeCursor = lineContent.substring(0, position.column - 1);
        
        // 找到最后一个触发字符的位置来确定替换范围的起始
        var dotPos = textBeforeCursor.lastIndexOf('.');
        var parenPos = textBeforeCursor.lastIndexOf('(');
        var colonPos = textBeforeCursor.lastIndexOf('::');
        var arrowPos = textBeforeCursor.lastIndexOf('->');
        var lastTrigger = Math.max(dotPos, parenPos, colonPos, arrowPos);
        
        var startCol;
        if (lastTrigger >= 0) {
          // 有触发字符：从触发字符后面开始替换
          if (colonPos === lastTrigger) {
            startCol = lastTrigger + 3;  // ::后面
          } else if (arrowPos === lastTrigger) {
            startCol = lastTrigger + 3;  // ->后面
          } else {
            startCol = lastTrigger + 2;  // .或(后面
          }
        } else {
          // 无触发字符（普通名称补全如"hui"）：从当前标识符开头开始替换
          // 向左扫描找到标识符起始位置（字母/数字/下划线）
          var wordStart = textBeforeCursor.length;
          while (wordStart > 0) {
            var ch = textBeforeCursor.charCodeAt(wordStart - 1);
            if ((ch >= 65 && ch <= 90) || (ch >= 97 && ch <= 122) || (ch >= 48 && ch <= 57) || ch === 95) {
              wordStart--;
            } else {
              break;
            }
          }
          startCol = wordStart + 1;  // +1因为column是1-based
        }
        
        // 提取用户已输入的标识符文本（用于过滤不相关的补全项）
        var typedText = '';
        if (lastTrigger < 0 && textBeforeCursor.length > 0) {
          var wordStart2 = textBeforeCursor.length;
          while (wordStart2 > 0) {
            var ch2 = textBeforeCursor.charCodeAt(wordStart2 - 1);
            if ((ch2 >= 65 && ch2 <= 90) || (ch2 >= 97 && ch2 <= 122) || (ch2 >= 48 && ch2 <= 57) || ch2 === 95) {
              wordStart2--;
            } else {
              break;
            }
          }
          typedText = textBeforeCursor.substring(wordStart2).toLowerCase();
        }
        
        var range = {
          startLineNumber: position.lineNumber,
          endLineNumber: position.lineNumber,
          startColumn: startCol,
          endColumn: position.column,
        };

        var suggestions = result.items.map(function (item) {
          var insertText = item.insertText || item.label || '';
          // 如果insertText包含参数签名，只取函数名部分
          var parenIdx = insertText.indexOf('(');
          if (parenIdx > 0) {
            insertText = insertText.substring(0, parenIdx);
          }
          return {
            label: item.label || item.name,
            kind: _monacoCompletionKind(item.kind),
            detail: item.detail || '',
            documentation: item.documentation || '',
            insertText: insertText,
            range: range,
            sortText: String.fromCharCode(0) + item.label, // LSP结果优先
          };
        });
        
        // 过滤：只保留包含用户已输入文本的补全项
        if (typedText) {
          suggestions = suggestions.filter(function(s) {
            return s.label.toLowerCase().indexOf(typedText) >= 0;
          });
        }
        
        // 排序：完全匹配的优先，前缀匹配的次之
        suggestions.sort(function(a, b) {
          var aLabel = a.label.toLowerCase();
          var bLabel = b.label.toLowerCase();
          var aExact = (aLabel === typedText) ? 0 : 
                        (aLabel.indexOf(typedText) === 0) ? 1 : 2;
          var bExact = (bLabel === typedText) ? 0 : 
                        (bLabel.indexOf(typedText) === 0) ? 1 : 2;
          if (aExact !== bExact) return aExact - bExact;
          return a.label.localeCompare(b.label);
        });

        return { suggestions: suggestions };
      } catch (e) {
        console.warn('[LSP] completion error:', e);
        return { suggestions: [] };
      }
    },
  });
}

function _monacoCompletionKind(kindStr) {
  var map = {
    'text': 1, 'method': 2, 'function': 3, 'constructor': 4,
    'field': 5, 'variable': 6, 'class': 7, 'interface': 8,
    'module': 9, 'property': 10, 'unit': 11, 'value': 12,
    'enum': 13, 'keyword': 14, 'snippet': 15, 'color': 16,
    'file': 17, 'reference': 18, 'folder': 19, 'enumMember': 20,
    'constant': 21, 'struct': 22, 'event': 23, 'operator': 24,
    'typeParameter': 25,
  };
  if (typeof kindStr === 'number') return kindStr;
  return map[kindStr] !== undefined ? map[kindStr] : 6; // 默认variable
}

/**
 * 为指定语言注册所有LSP Provider
 */
function registerLspProviders(languageId) {
  if (!languageId || _registeredLanguages.has(languageId)) return;
  if (typeof monaco === 'undefined' || !monaco.languages) return;

  console.log('[LSP] Registering providers for: ' + languageId);
  _registeredLanguages.add(languageId);

  try {
    _registerDefinitionProvider(languageId);
    _registerReferenceProvider(languageId);
    _registerHoverProvider(languageId);
    _registerDocumentSymbolProvider(languageId);
    _registerImplementationProvider(languageId);
    _registerCompletionProvider(languageId);
    console.log('[LSP] ' + languageId + ' providers registered');
  } catch (e) {
    console.error('[LSP] ' + languageId + ' registration failed:', e);
    _registeredLanguages.delete(languageId);
  }
}

/**
 * 注册Monaco不内置的语言定义（Monarch tokenizer + 语言配置）
 * Monaco内置: python, c, cpp, rust, go, javascript, typescript, java, etc.
 * 需要手动注册: zig, lua
 */
function _registerMonacoLanguageDefinitions() {
  // Zig 语言
  if (!monaco.languages.getLanguages().some(function(l) { return l.id === 'zig'; })) {
    monaco.languages.register({ id: 'zig' });
    monaco.languages.setMonarchTokensProvider('zig', {
      keywords: [
        'const', 'var', 'fn', 'pub', 'return', 'if', 'else', 'while', 'for',
        'break', 'continue', 'switch', 'try', 'catch', 'error', 'defer',
        'errdefer', 'async', 'await', 'suspend', 'resume', 'struct', 'enum',
        'union', 'opaque', 'usingnamespace', 'test', 'comptime', 'noreturn',
        'linksection', 'callconv', 'volatile', 'allowzero', 'align', 'packed',
        'export', 'extern', 'inline', 'noalias', 'anytype', 'anyframe',
      ],
      typeKeywords: [
        'bool', 'void', 'type', 'anyerror', 'anyopaque', 'usize', 'isize',
        'u8', 'u16', 'u32', 'u64', 'u128', 'i8', 'i16', 'i32', 'i64', 'i128',
        'f16', 'f32', 'f64', 'f80', 'f128', 'c_short', 'c_ushort', 'c_int',
        'c_uint', 'c_long', 'c_ulong', 'c_longlong', 'c_ulonglong', 'c_longdouble',
      ],
      operators: [
        '=', '==', '!=', '<', '>', '<=', '>=', '+', '-', '*', '/', '%',
        '++', '**', '<<', '>>', '&', '|', '^', '~', '!', '&&', '||',
        '+=', '-=', '*=', '/=', '%=', '<<=', '>>=', '&=', '|=', '^=',
      ],
      symbols: /[=><!~?:&|+\-*/^%]+/,
      tokenizer: {
        root: [
          // identifiers and keywords
          [/[a-zA-Z_]\w*/, {
            cases: {
              '@keywords': 'keyword',
              '@typeKeywords': 'type',
              '@default': 'identifier',
            },
          }],
          // whitespace
          { include: '@whitespace' },
          // numbers
          [/\d*\.\d+([eE][\-+]?\d+)?/, 'number.float'],
          [/0[xX][0-9a-fA-F_]+/, 'number.hex'],
          [/\d+/, 'number'],
          // strings
          [/"([^"\\]|\\.)*$/, 'string.invalid'],
          [/"/, 'string', '@string'],
          // characters
          [/'[^'\\]'/, 'string'],
          [/\\./, 'string.escape'],
          [/'/, 'string.invalid'],
          // delimiters and operators
          [/[{}()\[\]]/, '@brackets'],
          [/[<>](?!@symbols)/, '@brackets'],
          [/@symbols/, {
            cases: {
              '@operators': 'operator',
              '@default': '',
            },
          }],
          // semicolon
          [/;/, 'delimiter'],
          // comments
          [/\/\/.*$/, 'comment'],
          [/\d+_/, 'number'],
        ],
        whitespace: [
          [/[ \t\r\n]+/, 'white'],
          [/\/\*/, 'comment', '@comment'],
          [/\/\/.*$/, 'comment'],
        ],
        comment: [
          [/[^\/*]+/, 'comment'],
          [/\*\//, 'comment', '@pop'],
          [/[\/*]/, 'comment'],
        ],
        string: [
          [/[^\\"]+/, 'string'],
          [/\\./, 'string.escape'],
          [/"/, 'string', '@pop'],
        ],
      },
    });
    // Zig 语言配置（括号匹配、注释切换等）
    monaco.languages.setLanguageConfiguration('zig', {
      comments: {
        lineComment: '//',
        blockComment: ['/*', '*/'],
      },
      brackets: [
        ['{', '}'],
        ['[', ']'],
        ['(', ')'],
      ],
      autoClosingPairs: [
        { open: '{', close: '}' },
        { open: '[', close: ']' },
        { open: '(', close: ')' },
        { open: '"', close: '"', notIn: ['string'] },
        { open: '\'', close: '\'', notIn: ['string', 'comment'] },
      ],
      surroundingPairs: [
        { open: '{', close: '}' },
        { open: '[', close: ']' },
        { open: '(', close: ')' },
        { open: '"', close: '"' },
        { open: '\'', close: '\'' },
      ],
      folding: {
        markers: {
          start: /^\s*\/\/\s*#?region\b/,
          end: /^\s*\/\/\s*#?endregion\b/,
        },
      },
    });
    console.log('[LSP] Zig language definition registered');
  }

  // Lua 语言
  if (!monaco.languages.getLanguages().some(function(l) { return l.id === 'lua'; })) {
    monaco.languages.register({ id: 'lua' });
    monaco.languages.setMonarchTokensProvider('lua', {
      keywords: [
        'and', 'break', 'do', 'else', 'elseif', 'end', 'false', 'for',
        'function', 'goto', 'if', 'in', 'local', 'nil', 'not', 'or',
        'repeat', 'return', 'then', 'true', 'until', 'while',
      ],
      typeKeywords: [
        'self', 'super',
      ],
      operators: [
        '=', '==', '~=', '<', '>', '<=', '>=', '+', '-', '*', '/', '%',
        '//', '^', '#', '&', '~', '|', '<<', '>>', '..', '...',
      ],
      symbols: /[=><!~?:&|+\-*/^%#]+/,
      tokenizer: {
        root: [
          [/[a-zA-Z_]\w*/, {
            cases: {
              '@keywords': 'keyword',
              '@typeKeywords': 'type',
              '@default': 'identifier',
            },
          }],
          { include: '@whitespace' },
          [/\d*\.\d+([eE][\-+]?\d+)?/, 'number.float'],
          [/0[xX][0-9a-fA-F_]+/, 'number.hex'],
          [/\d+/, 'number'],
          [/[\"']([^\"\\]|\\.)*$/, 'string.invalid'],
          [/"/, 'string', '@string_dq'],
          [/'/, 'string', '@string_sq'],
          [/\[\[/, 'string', '@string_long'],
          [/[{}()\[\]]/, '@brackets'],
          [/@symbols/, {
            cases: {
              '@operators': 'operator',
              '@default': '',
            },
          }],
          [/;/, 'delimiter'],
        ],
        whitespace: [
          [/[ \t\r\n]+/, 'white'],
          [/--\[\[/, 'comment', '@comment_long'],
          [/--.*$/, 'comment'],
        ],
        comment_long: [
          [/[^\]]+/, 'comment'],
          [/\]\]/, 'comment', '@pop'],
          [/\]/, 'comment'],
        ],
        string_dq: [
          [/[^\\"]+/, 'string'],
          [/\\./, 'string.escape'],
          [/"/, 'string', '@pop'],
        ],
        string_sq: [
          [/[^\\']+/, 'string'],
          [/\\./, 'string.escape'],
          [/'/, 'string', '@pop'],
        ],
        string_long: [
          [/[^\]]+/, 'string'],
          [/\]\]/, 'string', '@pop'],
          [/\]/, 'string'],
        ],
      },
    });
    monaco.languages.setLanguageConfiguration('lua', {
      comments: {
        lineComment: '--',
        blockComment: ['--[[', ']]'],
      },
      brackets: [
        ['{', '}'],
        ['[', ']'],
        ['(', ')'],
      ],
      autoClosingPairs: [
        { open: '{', close: '}' },
        { open: '[', close: ']' },
        { open: '(', close: ')' },
        { open: '"', close: '"' },
        { open: "'", close: "'" },
      ],
      surroundingPairs: [
        { open: '{', close: '}' },
        { open: '[', close: ']' },
        { open: '(', close: ')' },
        { open: '"', close: '"' },
        { open: "'", close: "'" },
      ],
    });
    console.log('[LSP] Lua language definition registered');
  }

  // CMake 语言
  if (!monaco.languages.getLanguages().some(function(l) { return l.id === 'cmake'; })) {
    monaco.languages.register({
      id: 'cmake',
      extensions: ['.cmake'],
      filenames: ['CMakeLists.txt', 'cmakelists.txt'],
      aliases: ['CMake', 'cmake'],
    });
    monaco.languages.setMonarchTokensProvider('cmake', {
      // CMake commands (https://cmake.org/cmake/help/latest/manual/cmake-commands.7.html)
      keywords: [
        // Scripting commands
        'message', 'macro', 'endmacro', 'function', 'endfunction', 'cmake_minimum_required',
        'include', 'find_package', 'include_guard', 'cmake_policy', 'return', 'block', 'endblock',
        'break', 'continue', 'configure_file', 'file', 'foreach', 'endforeach', 'if', 'elseif',
        'else', 'endif', 'math', 'execute_process', 'get_filename_component', 'get_property',
        'set_property', 'unset', 'string', 'list', 'option', 'separate_arguments', 'set',
        'site_name', 'site_name', 'unset', 'variable_watch', 'while', 'endwhile',
        // Project commands
        'project', 'add_custom_command', 'add_custom_target', 'add_definitions', 'add_dependencies',
        'add_executable', 'add_library', 'add_subdirectory', 'add_test', 'aux_source_directory',
        'build_command', 'create_test_sourcelist', 'define_property', 'enable_language',
        'enable_testing', 'export', 'fltk_wrap_ui', 'get_source_file_property',
        'get_target_property', 'get_test_property', 'include_directories',
        'include_external_msproject', 'include_external_project', 'include_regular_expression',
        'install', 'link_directories', 'load_cache', 'output_required_files',
        'qt_wrap_cpp', 'qt_wrap_ui', 'remove_definitions', 'set_source_files_properties',
        'set_target_properties', 'set_tests_properties', 'source_group', 'subdirs',
        'target_compile_definitions', 'target_compile_features', 'target_compile_options',
        'target_include_directories', 'target_link_directories', 'target_link_libraries',
        'target_link_options', 'target_precompile_headers', 'target_sources',
        'try_compile', 'try_run',
        // CTest commands
        'ctest_configure', 'ctest_build', 'ctest_test', 'ctest_coverage',
        'ctest_memcheck', 'ctest_submit', 'ctest_update', 'ctest_upload',
        // Deprecated / rarely used
        'install_files', 'install_programs', 'install_targets', 'make_directory',
        'mark_as_advanced', 'remove', 'subdir_depends', 'use_mangled_mesa',
        'utility_source', 'variable_requires', 'write_file',
        // CMake language keywords
        'AND', 'OR', 'NOT', 'MATCHES', 'EXISTS', 'IS_DIRECTORY', 'IS_ABSOLUTE',
        'EQUAL', 'LESS', 'GREATER', 'LESS_EQUAL', 'GREATER_EQUAL',
        'STREQUAL', 'STRLESS', 'STRGREATER', 'STRLESS_EQUAL', 'STRGREATER_EQUAL',
        'VERSION_EQUAL', 'VERSION_LESS', 'VERSION_GREATER',
        'VERSION_LESS_EQUAL', 'VERSION_GREATER_EQUAL',
        'DEFINED', 'POLICY', 'TARGET', 'TEST', 'COMMAND', 'CACHE', 'ENV', 'PARENT_SCOPE',
        'IN', 'WITH', 'ON', 'OFF', 'TRUE', 'FALSE', 'YES', 'NO', 'Y', 'N',
        'IGNORE', 'NOTFOUND', 'OPTIONAL', 'REQUIRED', 'QUIET', 'MODULE', 'SHARED',
        'STATIC', 'OBJECT', 'INTERFACE', 'IMPORTED', 'ALIAS', 'GLOBAL',
        'EXCLUDE_FROM_ALL', 'SYSTEM', 'BEFORE', 'AFTER', 'PRIVATE', 'PUBLIC',
        'PROPERTY', 'PROPERTIES', 'DIRECTORY', 'SOURCE', 'INSTALL', 'NAME',
        'NAMES', 'PATHS', 'HINTS', 'PATH_SUFFIXES', 'DOC', 'NO_DEFAULT_PATH',
        'NO_CMAKE_PATH', 'NO_CMAKE_ENVIRONMENT_PATH', 'NO_SYSTEM_ENVIRONMENT_PATH',
        'NO_CMAKE_SYSTEM_PATH', 'CMAKE_FIND_ROOT_PATH_BOTH', 'ONLY_CMAKE_FIND_ROOT_PATH',
        'NO_CMAKE_FIND_ROOT_PATH', 'COMPONENTS', 'OPTIONAL_COMPONENTS', 'CONFIG',
        'MODULES', 'REQUIRED_VARS',
      ],
      operators: [
        'AND', 'OR', 'NOT',
      ],
      symbols: /[=><!~?:&|+\-*/^%]+/,
      escapes: /\\(?:[abfnrtv\\"']|x[0-9A-Fa-f]{1,4}|u[0-9A-Fa-f]{4}|U[0-9A-Fa-f]{8})/,
      tokenizer: {
        root: [
          // CMake commands are case-insensitive, match at start of line (after whitespace)
          // Commands are function-call style: command(args...)
          [/^[ \t]*(\w+)(?=\s*\()/, {
            cases: {
              '@keywords': 'keyword',
              '@default': 'keyword',
            },
          }],
          // Variables: ${VAR}, $ENV{VAR}, $<...>
          [/\$\{[\w./+-]*\}/, 'variable.predefined'],
          [/\$ENV\{[\w./+-]*\}/, 'variable.predefined'],
          [/\$<[^>]*>/, 'variable.predefined'],
          // Comments
          { include: '@whitespace' },
          // Numbers
          [/\d*\.\d+([eE][\-+]?\d+)?/, 'number.float'],
          [/0[xX][0-9a-fA-F]+/, 'number.hex'],
          [/\d+/, 'number'],
          // Strings
          [/"/, 'string', '@string_dq'],
          [/'/, 'string', '@string_sq'],
          // Brackets and delimiters
          [/[{}()\[\]]/, '@brackets'],
          [/[;]/, 'delimiter'],
          // Keywords used inside conditions (AND, OR, NOT, etc.)
          [/[A-Z][A-Z_]*/, {
            cases: {
              '@keywords': 'keyword',
              '@default': 'identifier',
            },
          }],
          // Regular identifiers
          [/[a-zA-Z_]\w*/, 'identifier'],
          // Operators
          [/@symbols/, {
            cases: {
              '@operators': 'operator',
              '@default': '',
            },
          }],
        ],
        whitespace: [
          [/[ \t\r\n]+/, 'white'],
          [/#\[(?:(?!\]\])[\s\S])*/, 'comment', '@bracket_comment'],
          [/#.*$/, 'comment'],
        ],
        bracket_comment: [
          [/[^\]]+/, 'comment'],
          [/\]\]/, 'comment', '@pop'],
          [/\]/, 'comment'],
        ],
        string_dq: [
          [/[^\\"]+/, 'string'],
          [/@escapes/, 'string.escape'],
          [/\\./, 'string.escape.invalid'],
          [/"/, 'string', '@pop'],
        ],
        string_sq: [
          [/[^\\']+/, 'string'],
          [/@escapes/, 'string.escape.invalid'],
          [/'/, 'string', '@pop'],
        ],
      },
    });
    monaco.languages.setLanguageConfiguration('cmake', {
      comments: {
        lineComment: '#',
      },
      brackets: [
        ['{', '}'],
        ['[', ']'],
        ['(', ')'],
      ],
      autoClosingPairs: [
        { open: '{', close: '}' },
        { open: '[', close: ']' },
        { open: '(', close: ')' },
        { open: '"', close: '"' },
        { open: "'", close: "'" },
      ],
      surroundingPairs: [
        { open: '{', close: '}' },
        { open: '[', close: ']' },
        { open: '(', close: ')' },
        { open: '"', close: '"' },
        { open: "'", close: "'" },
      ],
    });
    console.log('[LSP] CMake language definition registered');
  }
}

/**
 * 初始化LSP Provider - 为所有后端支持的语言注册provider
 */
function initLspProviders() {
  // 先注册Monaco不内置的语言定义（Zig, Lua等），用try-catch防止影响后续注册
  try {
    _registerMonacoLanguageDefinitions();
  } catch(e) {
    console.error('[LSP] _registerMonacoLanguageDefinitions FAILED:', e);
  }
  // 排除TS/JS，Monaco已内置
  var languages = ['python', 'c', 'cpp', 'rust', 'go', 'zig', 'lua'];
  for (var i = 0; i < languages.length; i++) {
    try {
      registerLspProviders(languages[i]);
    } catch(e) {
      console.error('[LSP] registerLspProviders(' + languages[i] + ') FAILED:', e);
    }
  }
  // 初始化LSP面板点击事件
  try {
    initLspPanelToggle();
  } catch(e) {
    console.error('[LSP] initLspPanelToggle FAILED:', e);
  }
  console.log('[LSP] All providers initialized, registered:', Array.from(_registeredLanguages).join(','));
}

function getRegisteredLspLanguages() {
  return Array.from(_registeredLanguages);
}

// ==================== LSP 状态面板 ====================

// 缓存LSP状态
var _lspStatusCache = {};
var _lspPanelVisible = false;

/**
 * 检查当前文件的LSP状态并更新语言按钮上的状态点
 */
async function checkLspStatus(filePath, nest) {
  if (!filePath) return;

  // 确保filePath是绝对路径
  filePath = _resolveFilePath(filePath);

  var cacheKey = filePath + ':' + (nest || 'local');
  var cached = _lspStatusCache[cacheKey];
  if (cached && Date.now() - cached.time < 30000) {
    _updateLangBtnDot(cached.status);
    return;
  }

  try {
    var body = { file_path: filePath };
    if (nest) body.nest = nest;

    var response = await fetch('/api/lsp/status', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });

    if (!response.ok) return;
    var result = await response.json();

    _lspStatusCache[cacheKey] = { status: result, time: Date.now() };
    _updateLangBtnDot(result);
  } catch (e) {
    console.warn('[LSP] status check failed:', e);
  }
}

/**
 * 更新语言按钮上的状态（已移除lsp-dot，保留图标更新逻辑）
 * 按钮嵌入在文件路径中，通过class选择器定位
 */
function _updateLangBtnDot(status) {
  var btns = document.querySelectorAll('.lsp-lang-btn');
  for (var i = 0; i < btns.length; i++) {
    btns[i].title = '查看LSP服务器状态';
  }
}

/**
 * 设置语言按钮的图标（供editor-core.js等模块调用）
 * 按钮现在嵌入在文件路径中，通过class选择器定位
 */
function setLanguageBadge(lang) {
  var btns = document.querySelectorAll('.lsp-lang-btn');
  var icon = (typeof getLangIcon === 'function') ? getLangIcon(lang) : '📄';
  for (var i = 0; i < btns.length; i++) {
    var btn = btns[i];
    btn.setAttribute('data-lang', lang);
    btn.innerHTML = '<span class="lang-icon">' + icon + '</span>';
    btn.title = '查看LSP服务器状态';
  }
}

/**
 * 切换LSP状态面板的显示
 */
function toggleLspPanel() {
  var panel = document.getElementById('lsp-panel-global');
  if (!panel) {
    panel = document.createElement('div');
    panel.id = 'lsp-panel-global';
    panel.className = 'lsp-panel';
    panel.style.display = 'none';
    document.body.appendChild(panel);
  }

  if (_lspPanelVisible) {
    panel.style.display = 'none';
    _lspPanelVisible = false;
    return;
  }

  _lspPanelVisible = true;
  _renderLspPanel(panel);
  _loadAllLspStatus(panel);

  // 定位面板到按钮下方
  var btn = document.querySelector('.lsp-lang-btn:hover') || document.querySelector('.lsp-lang-btn');
  if (btn) {
    var rect = btn.getBoundingClientRect();
    panel.style.top = (rect.bottom + 4) + 'px';
    panel.style.left = Math.max(4, Math.min(rect.left, window.innerWidth - 340)) + 'px';
  }
  panel.style.display = 'block';
}

/**
 * 渲染LSP面板框架
 */
function _renderLspPanel(panel) {
  var languages = [
    { id: 'python', name: 'Python', server: 'pylsp', hint: 'pip install python-lsp-server' },
    { id: 'c', name: 'C/C++', server: 'clangd', hint: 'apt install clangd' },
    { id: 'cpp', name: 'C/C++', server: 'clangd', hint: 'apt install clangd' },
    { id: 'rust', name: 'Rust', server: 'rust-analyzer', hint: 'rustup component add rust-analyzer' },
    { id: 'go', name: 'Go', server: 'gopls', hint: 'go install golang.org/x/tools/gopls@latest' },
    { id: 'zig', name: 'Zig', server: 'zls', hint: '安装zls' },
    { id: 'lua', name: 'Lua', server: 'lua-language-server', hint: '安装lua-language-server' },
    { id: 'typescript', name: 'TypeScript/JS', server: '内置', hint: 'Monaco内置支持' },
  ];

  // 去重（c和cpp合并）
  var seen = {};
  var uniqueLangs = [];
  for (var i = 0; i < languages.length; i++) {
    var key = languages[i].server;
    if (!seen[key]) {
      seen[key] = true;
      uniqueLangs.push(languages[i]);
    }
  }

  var html = '<div class="lsp-panel-header"><strong>LSP 服务器状态</strong><button class="lsp-panel-close" onclick="toggleLspPanel()">✕</button></div>';

  for (var j = 0; j < uniqueLangs.length; j++) {
    var lang = uniqueLangs[j];
    html += '<div class="lsp-panel-item" data-lsp-lang="' + lang.id + '">';
    html += '<span class="status-icon unknown" id="lsp-panel-icon-' + lang.id + '">◌</span>';
    html += '<div class="lang-info">';
    html += '<div class="lang-name">' + lang.name + '</div>';
    html += '<div class="lang-server" id="lsp-panel-server-' + lang.id + '">' + lang.server + '</div>';
    html += '<div class="lang-hint" id="lsp-panel-hint-' + lang.id + '" style="display:none;"></div>';
    html += '</div></div>';
  }

  panel.innerHTML = html;
}

/**
 * 加载所有语言的LSP状态
 */
async function _loadAllLspStatus(panel) {
  try {
    var response = await fetch('/api/lsp/dependencies');
    if (!response.ok) return;
    var result = await response.json();
    if (!result.success || !result.dependencies) return;

    var deps = result.dependencies;
    // 映射：server_name -> status
    var serverStatus = {};
    for (var key in deps) {
      var dep = deps[key];
      serverStatus[dep.server_name] = dep.status;
    }

    // 更新面板中各语言的状态
    var langServerMap = {
      'python': 'python-lsp-server',
      'c': 'clangd',
      'cpp': 'clangd',
      'rust': 'rust-analyzer',
      'go': 'gopls',
      'zig': 'zls',
      'lua': 'lua-language-server',
      'typescript': 'builtin',
    };

    var items = panel.querySelectorAll('.lsp-panel-item');
    for (var i = 0; i < items.length; i++) {
      var langId = items[i].getAttribute('data-lsp-lang');
      var serverName = langServerMap[langId] || '';
      var icon = document.getElementById('lsp-panel-icon-' + langId);
      var hint = document.getElementById('lsp-panel-hint-' + langId);

      if (langId === 'typescript') {
        // Monaco内置
        if (icon) { icon.className = 'status-icon ok'; icon.textContent = '✓'; }
      } else if (serverStatus[serverName] === 'installed') {
        if (icon) { icon.className = 'status-icon ok'; icon.textContent = '✓'; }
      } else {
        if (icon) { icon.className = 'status-icon unavailable'; icon.textContent = '✗'; }
        if (hint) {
          var hints = {
            'python': 'pip install python-lsp-server',
            'c': 'sudo apt install clangd',
            'cpp': 'sudo apt install clangd',
            'rust': 'rustup component add rust-analyzer',
            'go': 'go install golang.org/x/tools/gopls@latest',
            'zig': '安装 zls',
            'lua': '安装 lua-language-server',
          };
          hint.textContent = hints[langId] || '未安装';
          hint.style.display = 'block';
          hint.style.color = '#f85149';
        }
      }
    }
  } catch (e) {
    console.warn('[LSP] load status failed:', e);
  }
}

/**
 * 初始化LSP语言按钮的点击事件
 * 按钮现在嵌入在文件路径中，通过事件委托处理（因为按钮会随路径更新重建）
 */
function initLspPanelToggle() {
  // 使用事件委托，因为按钮在editor-file-path内会被重建
  document.addEventListener('click', function(e) {
    var btn = e.target.closest('.lsp-lang-btn');
    if (btn) {
      e.stopPropagation();
      toggleLspPanel();
      return;
    }
    // 点击面板外关闭
    if (_lspPanelVisible) {
      var panel = document.getElementById('lsp-panel-global');
      if (panel && panel.contains(e.target)) return;
      if (panel) { panel.style.display = 'none'; }
      _lspPanelVisible = false;
    }
  });
}
