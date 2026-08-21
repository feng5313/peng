// ==================== 文件编辑器 ====================

let monacoEditor = null;

// 🔧 Task#416：缓存文件路径 -> 项目目录的映射，避免重复查找
window._gitRootCache = window._gitRootCache || {};

/**
 * 🔧 Task#416：查找文件所在的项目目录（.git所在目录）
 * 从文件目录开始向上查找 .git/config 文件（文件而非目录，/api/files/exists 可检查）
 * 本地和远程统一使用 /api/files/exists?nest= 参数
 * 如果没有.git则返回文件所在目录
 * @param {string} filePath - 文件绝对路径
 * @param {string} tabNest - 文件来源nest（null/'local'为本地）
 * @returns {Promise<string>} 项目目录路径
 */
async function _findGitRootForFile(filePath, tabNest) {
  if (!filePath) return null;

  // 检查缓存
  const cacheKey = (tabNest || 'local') + ':' + filePath;
  if (window._gitRootCache[cacheKey] !== undefined) {
    return window._gitRootCache[cacheKey];
  }

  const lastSlash = Math.max(filePath.lastIndexOf('/'), filePath.lastIndexOf('\\'));
  if (lastSlash <= 0) return null;
  const fileDir = filePath.substring(0, lastSlash);

  // nest参数：远程文件传nest名，本地不传
  const nestParam = tabNest && tabNest !== 'local' ? '&nest=' + encodeURIComponent(tabNest) : '';

  try {
    // 从文件目录开始，逐级向上查找 .git/config
    const pathParts = fileDir.replace(/\\/g, '/').split('/');
    while (pathParts.length > 0) {
      const candidate = pathParts.join('/') || '/';
      const gitConfigPath = candidate + '/.git/config';
      try {
        const resp = await fetch(`/api/files/exists?path=${encodeURIComponent(gitConfigPath)}${nestParam}`);
        const data = await resp.json();
        if (data.exists) {
          window._gitRootCache[cacheKey] = candidate;
          return candidate;
        }
      } catch (e) { /* 忽略单次请求失败 */ }
      pathParts.pop();
    }
  } catch (e) {
    console.warn('[Task#416] _findGitRootForFile error:', e);
  }

  // 没有找到.git，回退到文件所在目录
  window._gitRootCache[cacheKey] = fileDir;
  return fileDir;
}

/**
 * 🔧 Task#416/820：激活标签页时同步nest下拉框和工作目录
 * 更新nest下拉框为文件所在nest
 * 🔧 Task#820：工作目录优先使用标签页打开时记录的 workdir，而非查找.git工程目录
 * @param {object} tab - 标签页对象 { path, nest, workdir, isNewFile, isVirtual, isContentEditor }
 */
async function _syncNestAndWorkdirForTab(tab) {
  if (!tab || tab.isContentEditor || tab.isVirtual) return;

  const tabNest = tab.nest || null;

  // 同步 nest 下拉框
  if (typeof _prepareNestState === 'function') _prepareNestState(tabNest);
  if (typeof _syncNestDropdown === 'function') _syncNestDropdown(tabNest);

  // 🔧 Task#820：优先使用标签页打开时记录的工作目录
  if (tab.workdir) {
    const workdirInput = document.getElementById('workdir-input');
    if (workdirInput) workdirInput.value = toNativeDisplayPath(tab.workdir);
    if (typeof validateAndSetWorkdir === 'function') validateAndSetWorkdir(tab.workdir);
    return;
  }

  // 🔧 向后兼容：旧标签页没有记录workdir，使用原逻辑
  // 新文档：工作目录同步到文档路径的父目录
  if (tab.isNewFile) {
    if (tab.path) {
      const parts = tab.path.split('/');
      parts.pop(); // 移除文件名
      const dir = parts.join('/') || (state.workdir || '/');
      const workdirInput = document.getElementById('workdir-input');
      if (workdirInput) workdirInput.value = toNativeDisplayPath(dir);
      if (typeof validateAndSetWorkdir === 'function') validateAndSetWorkdir(dir);
    }
    return;
  }

  if (!tab.path) return;

  // 查找 .git 项目目录并更新工作目录
  const projectDir = await _findGitRootForFile(tab.path, tabNest);
  if (projectDir) {
    const workdirInput = document.getElementById('workdir-input');
    if (workdirInput) {
      workdirInput.value = toNativeDisplayPath(projectDir);
    }
    if (typeof validateAndSetWorkdir === 'function') {
      validateAndSetWorkdir(projectDir);
    }
  }
}

// 🔧 Task#348：Ctrl+Alt+W 关闭当前编辑器标签页
// Ctrl+W（关闭标签页）和Ctrl+Shift+W（关闭窗口）都被浏览器进程拦截，JS无法拦截
// 改用 Ctrl+Alt+W，浏览器不拦截此组合键
(function registerCloseTabHandler() {
  function closeTabHandler(e) {
    if ((e.ctrlKey || e.metaKey) && e.altKey && (e.key === 'w' || e.key === 'W')) {
      const editorView = document.getElementById('editor-view');
      const editorVisible = editorView && editorView.style.display !== 'none';
      if (editorVisible) {
        e.preventDefault();
        e.stopPropagation();
        const activeTabId = getActiveEditorTabId();
        if (activeTabId) {
          closeTab(activeTabId);
        }
      }
    }
  }
  document.addEventListener('keydown', closeTabHandler, true);
})();

/**
 * 格式化编辑器头部文件路径显示
 * 格式：<nest图标><nest名字><文件路径><文件类型图标按钮>
 * 文件类型图标按钮紧跟在路径文字后面，点击弹出LSP状态面板
 * @param {string} path - 文件路径
 * @param {string} tabNest - 文件来源nest（'local' 或远程nest名称）
 * @param {string} [suffix] - 可选后缀文本（如"（只读）"）
 * @returns {string} HTML字符串
 */
function _formatHeaderPathHTML(path, tabNest, suffix) {
  const nests = window._latestNests || {};
  const isLocal = !tabNest || tabNest === 'local';

  let iconHTML;
  let nestName;
  if (isLocal) {
    iconHTML = '🪹';
    nestName = 'local';
  } else {
    const nestInfo = nests[tabNest];
    const platform = nestInfo?.platform || '';
    const linuxId = nestInfo?.linux_id || '';
    iconHTML = (typeof getPlatformIconHTML === 'function') ? (getPlatformIconHTML(platform, linuxId) || '💻') : '💻';
    nestName = tabNest;
  }

  const pathDisplay = path || '';
  const suffixText = suffix || '';

  // 文件类型图标（从路径推导）
  var fileIcon = (typeof getFileIcon === 'function') ? getFileIcon(path || '') : '📄';

  // 截断优先级：文件路径先截断 > nest名收缩 > nest图标保留
  // 文件类型图标按钮紧跟路径文字（作为inline-flex的一部分，不受flex:1影响）
  return '<span style="display:inline-flex;align-items:center;gap:4px;white-space:nowrap;min-width:0;">' +
    '<span class="header-path-nest-icon" style="flex-shrink:0;line-height:1;">' + iconHTML + '</span>' +
    '<span class="header-path-nest-name" style="font-size:11px;color:var(--text-dim);flex-shrink:10;overflow:hidden;min-width:0;">' + nestName + '</span>' +
    '<span class="header-path-file-text" style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="' + (path || '') + '">' + pathDisplay + suffixText + '</span>' +
    '<button class="lsp-lang-btn" title="查看LSP服务器状态">' +
    '<span class="lang-icon">' + fileIcon + '</span>' +
    '</button>' +
    '</span>';
}

/**
 * 🔧 Task#248：更新编辑器头部文件路径元素（同时更新两个布局中的路径label）
 * @param {string} path - 文件路径
 * @param {string} tabNest - 文件来源nest
 * @param {string} [suffix] - 可选后缀
 */
function _setEditorFilePathHTML(path, tabNest, suffix) {
  const html = _formatHeaderPathHTML(path, tabNest, suffix);
  const ids = ['editor-file-path-2', 'editor-file-path'];
  ids.forEach(id => {
    const el = document.getElementById(id);
    if (el) el.innerHTML = html;
  });
}

/**
 * 🔧 Task#248：设置编辑器头部文件路径为纯文本（用于非文件类型的标签页）
 * @param {string} text - 显示文本
 */
function _setEditorFilePathText(text) {
  const ids = ['editor-file-path-2', 'editor-file-path'];
  ids.forEach(id => {
    const el = document.getElementById(id);
    if (el) el.textContent = text;
  });
}

// 🔧 修复：完全销毁 Monaco Editor 实例
function destroyMonacoEditor() {
  if (monacoEditor) {
    try {
      monacoEditor.dispose();
      monacoEditor = null;
      // 🔧 修复（Task #10）：重置事件绑定标志，确保重新创建编辑器时能重新绑定Ctrl+C拦截事件
      // 否则新编辑器实例不会绑定Ctrl+C拦截，导致大文件复制时被浏览器截断
      window.editorEventsBound = false;
    } catch (error) {
      console.warn('[destroyMonacoEditor] 销毁失败:', error);
      monacoEditor = null;
      window.editorEventsBound = false;
    }
  }
}

/**
 * 🔧 Task#418：修复Monaco Editor中emoji字符宽度与中文字符不一致的问题
 *
 * 根因分析：
 *   ZyfunHuge字体的全角字符宽度(typicalFullwidthCharacterWidth)=13px，
 *   半角字符宽度(typicalHalfwidthCharacterWidth)=7.83px，全角/半角比≈1.66（非标准2.0）。
 *   Emoji由系统emoji字体渲染，像素宽度≈2×7.83=16px，而中文字符=13px，导致emoji比中文宽25%。
 *
 * 修复方案：
 *   Monkey-patch Monaco渲染层的 _actualReadPixelOffset 方法，
 *   当检测到光标位于emoji之后时，使用typicalFullwidthCharacterWidth作为emoji的像素宽度，
 *   而非DOM实际测量的2×halfWidth。
 *
 * 调用链：getScrolledVisiblePosition → getOffsetForColumn → visibleRangeForPosition
 *   → _readPixelOffset → _actualReadPixelOffset → DOM测量
 */
function _patchMonacoEmojiWidth() {
  try {
    var strings = require('vs/base/common/strings');
    if (!strings || !strings.isEmojiImprecise) {
      console.warn('[Monaco] emoji宽度修复：找不到strings模块');
      return;
    }

    // 存储原始函数引用（闭包级别，避免每次渲染重复patch）
    var _origActualReadPixelOffset = null;

    // 监听编辑器创建，在渲染层原型上注入patch
    // 使用MutationObserver检测Monaco编辑器DOM创建
    var _patchApplied = false;

    function applyPatch(editor) {
      if (_patchApplied) return;
      try {
        var vl = editor._modelData.view._viewLines._visibleLines;
        var r1 = vl.getVisibleLine(1)._renderedViewLine;
        // 找到渲染层的基类原型（Monaco内部类'u'）
        var baseProto = Object.getPrototypeOf(Object.getPrototypeOf(r1));

        if (!baseProto || !baseProto._actualReadPixelOffset) {
          console.warn('[Monaco] emoji宽度修复：找不到_actualReadPixelOffset');
          return;
        }

        // 保存原始函数
        _origActualReadPixelOffset = baseProto._actualReadPixelOffset;

        var fi = editor._configuration.options._values[50];
        // fullW缓存值，字体设置变更时需要更新
        var _cachedFullW = fi.typicalFullwidthCharacterWidth;

        // Patch：当column位于emoji之后时，用全角宽度替代DOM测量宽度
        baseProto._actualReadPixelOffset = function(g, h, m, C) {
          var origResult = _origActualReadPixelOffset.call(this, g, h, m, C);
          if (origResult === -1 || m <= 2) return origResult;

          var content = this.input.lineContent;
          if (!content || content.length < 2) return origResult;

          // 检查column m之前的2个UTF-16单元是否构成emoji surrogate pair
          // 对于col m，最后两个UTF-16单元在 indices [m-3, m-2]
          // 例如 "🀄xyz"(len=4): col5 → indices [2,3] = emoji surrogate pair
          var idx1 = m - 3;
          var idx2 = m - 2;

          if (idx1 >= 0 && idx2 < content.length) {
            var cp1 = content.charCodeAt(idx1);
            var cp2 = content.charCodeAt(idx2);

            if (cp1 >= 0xD800 && cp1 <= 0xDBFF && cp2 >= 0xDC00 && cp2 <= 0xDFFF) {
              var fullCp = ((cp1 - 0xD800) << 10) + (cp2 - 0xDC00) + 0x10000;

              if (strings.isEmojiImprecise(fullCp)) {
                // 获取emoji之前的像素偏移量
                var beforeCol = idx1 + 1;
                var beforeOffset = _origActualReadPixelOffset.call(this, g, h, beforeCol, C);

                if (beforeOffset >= 0) {
                  // 使用全角字符宽度（与中文字符一致）
                  // 动态获取fullW，以支持字体设置热更新
                  var currentFullW = _cachedFullW;
                  try {
                    // 从任意活跃编辑器获取当前fontInfo
                    for (var gi = 0; gi < state.editorGroups.length; gi++) {
                      var ge = state.editorGroups[gi].monacoEditor;
                      if (ge) {
                        var cfi = ge._configuration.options._values[50];
                        if (cfi) {
                          currentFullW = cfi.typicalFullwidthCharacterWidth;
                          _cachedFullW = currentFullW;  // 更新缓存
                        }
                        break;
                      }
                    }
                  } catch(ex) { /* 使用缓存值 */ }
                  return beforeOffset + currentFullW;
                }
              }
            }
          }

          return origResult;
        };

        _patchApplied = true;
        console.log('[Monaco] emoji宽度修复已应用 (fullW=' + _cachedFullW + 'px)');
      } catch (e) {
        console.warn('[Monaco] emoji宽度修复失败:', e);
      }
    }

    // 全局hook：在编辑器创建后自动应用patch
    var origCreate = monaco.editor.create;
    monaco.editor.create = function(container, options) {
      var editor = origCreate.apply(this, arguments);
      // 延迟应用patch，等待渲染层初始化完成
      setTimeout(function() { applyPatch(editor); }, 100);
      return editor;
    };

  } catch (e) {
    console.warn('[Monaco] emoji宽度修复初始化失败:', e);
  }
}

/**
 * 🔧 Task#426：修复Monaco Editor内置LSP相关action的快捷键绑定
 * Monaco standalone默认keybinding不符合VS Code标准：
 * - "转到定义" 默认 Ctrl+F12 → 应为 F12
 * - "转到实现" 保持 Ctrl+F12（不变）
 * - "转到符号" 无keybinding显示 → 应显示 Ctrl+Shift+O
 * 方案：定期检查Shadow DOM中右键菜单，按label精确替换/添加keybinding文字
 * 同时通过addCommand确保F12和Ctrl+Shift+O快捷键实际生效
 */
// 按菜单项label精确替换keybinding（避免"转到实现"也被误改）
var _labelKeybindingReplacements = {
  '转到定义': {from: 'Ctrl+F12', to: 'F12'},
  'Go to Definition': {from: 'Ctrl+F12', to: 'F12'},
  '转到符号...': {from: 'Ctrl+Shift+O', to: 'F2'},
  'Go to Symbol...': {from: 'Ctrl+Shift+O', to: 'F2'}
};

// 菜单项label到应显示快捷键的映射（用于无keybinding的菜单项）
var _keybindingAdditions = {
  '转到符号...': 'F2',
  '转到符号': 'F2',
  'Go to Symbol...': 'F2',
  '命令面板': 'F1',
  'Command Palette': 'F1'
};

function _patchContextMenuKeybindings() {
  function _applyKeybindingPatch(root) {
    // 按label精确替换keybinding
    var actionItems = root.querySelectorAll('.action-menu-item');
    for (var i = 0; i < actionItems.length; i++) {
      var item = actionItems[i];
      var label = item.querySelector('.action-label');
      if (!label) continue;
      var labelText = label.textContent.trim();
      
      // 检查是否需要替换keybinding
      var replacement = _labelKeybindingReplacements[labelText];
      if (replacement) {
        var kb = item.querySelector('.keybinding');
        if (kb && kb.textContent.trim() === replacement.from) {
          kb.textContent = replacement.to;
        }
      }
      
      // 检查是否需要添加keybinding
      if (_keybindingAdditions[labelText]) {
        if (!item.querySelector('.keybinding')) {
          var kbSpan = document.createElement('span');
          kbSpan.className = 'keybinding';
          kbSpan.textContent = _keybindingAdditions[labelText];
          item.appendChild(kbSpan);
        }
      }
    }
  }

  // 定期检查shadow DOM中的菜单并应用patch
  // 比MutationObserver更可靠，因为Monaco可能直接修改DOM节点的textContent
  setInterval(function() {
    var host = document.querySelector('.shadow-root-host');
    if (host && host.shadowRoot) {
      var menuContainer = host.shadowRoot.querySelector('.context-view');
      // 只在菜单可见时才patch（避免无效操作）
      if (menuContainer && getComputedStyle(menuContainer).display !== 'none') {
        _applyKeybindingPatch(host.shadowRoot);
      }
    }
  }, 50);

  console.log('[Monaco] Context menu keybinding patch initialized (interval-based)');
}

async function loadMonacoEditor() {
  // 如果已经加载，直接返回
  if (window.monaco) {
    return;
  }

  // 加载Monaco Editor
  const loaderScript = document.createElement('script');
  loaderScript.src = '/static/vendor/monaco/vs/loader.js';
  document.head.appendChild(loaderScript);

  return new Promise((resolve, reject) => {
    loaderScript.onload = () => {
      // 配置Monaco Editor - 使用本地资源（零延迟，不依赖CDN）
      require.config({
        paths: { vs: '/static/vendor/monaco/vs' },
        'vs/nls': {
          availableLanguages: { '*': 'zh-cn' }
        }
      });

      require(['vs/editor/editor.main'], function () {
        window.monaco = monaco;
        // 🔧 Task#418：修复emoji字符宽度与中文字符不一致的问题
        // 根因：ZyfunHuge字体的全角/半角比≈1.66（非标准2.0），导致emoji由系统字体渲染为2×半角宽度
        // 而中文字符使用全角宽度（typicalFullwidthCharacterWidth），两者不一致
        // 修复：monkey-patch渲染层_actualReadPixelOffset，让emoji像素偏移使用全角宽度
        _patchMonacoEmojiWidth();
        // 🔧 Task#426：修复LSP右键菜单快捷键显示
        _patchContextMenuKeybindings();
        // 初始化LSP Providers（桥接后端LSP能力到Monaco Editor）
        if (typeof initLspProviders === 'function') {
          try {
            initLspProviders();
          } catch (e) {
            console.warn('[Monaco] LSP Providers初始化失败:', e);
          }
        }
        resolve();
      }, function (error) {
        console.error('Monaco Editor 加载失败:', error);
        reject(error);
      });
    };

    loaderScript.onerror = () => {
      reject(new Error('无法加载Monaco Editor loader'));
    };
  });
}

/**
 * 🔧 Task#341修复：为所有editor-group容器注册mousedown监听器，更新_lastActiveGroupId
 * 统一管理所有group的活动状态追踪，不再依赖分散在各处的mousedown guard
 * 调用时机：ensureGroup0之后、splitEditorGroup之后
 */
function initGroupMousedownTrackers() {
  if (!state.editorGroups) return;
  state.editorGroups.forEach(function (group) {
    var container = document.getElementById(group.containerId);
    if (!container) return;
    var groupEl = container.closest('.editor-group');
    if (!groupEl) return;
    // 防止重复注册：用dataset标记
    if (groupEl.dataset.mousedownTracker === 'true') return;
    groupEl.dataset.mousedownTracker = 'true';
    groupEl.addEventListener('mousedown', function () {
      state._lastActiveGroupId = group.id;
    }, true);
  });
}

/** 根据分组ID查找分组对象（使用find而非数组索引，因为splice删除后索引会错位） */

/**
 * 🔧 Task#262：并发分块加载大文件
 * 使用并发池模式同时请求多个chunk，大幅提升大文件加载速度
 * @param {string} filePath - 文件路径
 * @param {number} fileSize - 文件总大小（字节）
 * @param {string} nestParam - nest参数
 * @param {string} nestName - nest名称（用于进度显示）
 * @param {string} sizeStr - 格式化的文件大小字符串
 * @param {number} estimatedSeconds - 预估加载秒数
 * @param {number} startTime - 开始时间戳
 * @param {object} tab - 标签页对象（用于更新进度和检查取消）
 * @param {string} tabId - 标签页ID
 * @param {number} groupId - 分组ID
 * @param {object} group - 分组对象
 * @returns {Promise<string>} 文件完整内容
 */
async function _loadFileChunksParallel(filePath, fileSize, nestParam, nestName, sizeStr, estimatedSeconds, startTime, tab, tabId, groupId, group) {
  const CHUNK_SIZE = state.webuiConfig.large_file_chunk_size || (1024 * 1024);
  const CONCURRENCY = state.webuiConfig.large_file_parallel_concurrency || 4;

  // 计算所有chunk的offset列表
  const offsets = [];
  for (let off = 0; off < fileSize; off += CHUNK_SIZE) {
    offsets.push(off);
  }
  const totalChunks = offsets.length;

  // 按offset顺序存储结果
  const chunks = new Array(totalChunks).fill('');
  let completedChunks = 0;
  let nextIndex = 0;  // 下一个待分配的chunk索引

  // 进度更新辅助函数
  function updateProgress() {
    const loadedBytes = completedChunks * CHUNK_SIZE;
    const pct = Math.min(100, Math.round((completedChunks / totalChunks) * 100));
    const elapsed = ((Date.now() - startTime) / 1000).toFixed(1);
    const remaining = pct > 0 ? Math.round((elapsed / pct) * (100 - pct)) : estimatedSeconds;
    tab.content = _buildProgressText(nestName, filePath, sizeStr, loadedBytes, pct, remaining, elapsed, completedChunks, totalChunks);
    if (group.monacoEditor && group.currentTabId === tabId) {
      window._suppressContentChangeTracking = true;
      try { group.monacoEditor.setValue(tab.content); } finally { window._suppressContentChangeTracking = false; }
    }
  }

  // 单个chunk加载函数
  async function loadChunk(index) {
    const offset = offsets[index];
    const chunkUrl = `/api/files/read-chunk?path=${encodeURIComponent(filePath)}&offset=${offset}&chunk_size=${CHUNK_SIZE}${nestParam}`;
    const resp = await fetch(chunkUrl);
    const data = await resp.json();
    if (!resp.ok || !data.success) {
      throw new Error(data.detail || `分块读取失败 (offset=${offset})`);
    }
    return { index, content: data.content || '', chunkSize: data.chunk_size };
  }

  // 并发池加载
  async function runPool() {
    const running = new Set();

    while (nextIndex < totalChunks || running.size > 0) {
      if (tab._cancelled) break;

      // 填充并发池
      while (running.size < CONCURRENCY && nextIndex < totalChunks) {
        const idx = nextIndex++;
        const promise = loadChunk(idx).then(result => {
          running.delete(promise);
          chunks[result.index] = result.content;
          completedChunks++;
          updateProgress();
        }).catch(err => {
          running.delete(promise);
          console.warn(`[chunk ${idx}] 加载失败:`, err);
          // 单个chunk失败不中断，保留空字符串
          completedChunks++;
          updateProgress();
        });
        running.add(promise);
      }

      // 等待任意一个完成
      if (running.size > 0) {
        await Promise.race(running);
      }
    }
  }

  await runPool();

  // 按顺序拼接所有chunk
  return chunks.join('');
}

/** 🔧 Task#343：动态创建一个group的DOM元素（替代HTML硬编码） */
function createGroupDom(groupId) {
  const groupEl = document.createElement('div');
  groupEl.className = 'editor-group';
  groupEl.dataset.groupId = groupId;
  groupEl.style.fontSize = 'inherit';
  groupEl.style.fontFamily = 'inherit';

  const tabsEl = document.createElement('div');
  tabsEl.className = 'editor-tabs';
  tabsEl.id = 'editor-tabs-' + groupId;

  const editorContainerEl = document.createElement('div');
  editorContainerEl.id = 'monaco-editor-container-' + groupId;
  editorContainerEl.className = 'editor-container';

  groupEl.appendChild(tabsEl);
  groupEl.appendChild(editorContainerEl);
  return groupEl;
}

/**
 * 🔧 Task#343：确保至少有一个编辑器分组存在。
 * 替代旧的ensureGroup0()——不再强制id=0，改为动态分配ID。
 * 同时确保DOM中有对应的group元素。
 */
function ensureGroup0() {
  const groupsContainer = document.getElementById('editor-groups-container');
  if (!state.editorGroups || state.editorGroups.length === 0) {
    // 没有任何group，创建第一个（id动态分配，通常从0开始）
    const newId = 0;
    const newGroup = {
      id: newId,
      tabs: [],
      currentTabId: null,
      monacoEditor: monacoEditor,
      containerId: 'monaco-editor-container-' + newId
    };
    state.editorGroups = [newGroup];
    // 🔧 Task#343：同步创建DOM元素（不再依赖HTML硬编码）
    if (groupsContainer && !groupsContainer.querySelector('[data-group-id="' + newId + '"]')) {
      groupsContainer.appendChild(createGroupDom(newId));
    }
  } else {
    const g = state.editorGroups[0];
    // 🔧 Task#343：确保DOM中有对应的group元素
    if (groupsContainer && !groupsContainer.querySelector('[data-group-id="' + g.id + '"]')) {
      // 第一个group没有DOM，需要创建
      groupsContainer.insertBefore(createGroupDom(g.id), groupsContainer.firstChild);
    }
    // 更新运行时引用
    if (monacoEditor && g.id === state.editorGroups[0].id) {
      state.editorGroups[0].monacoEditor = monacoEditor;
    }
  }
}

/** 根据分组ID查找分组对象（使用find而非数组索引，因为splice删除后索引会错位） */
function findGroupById(groupId) {
  if (!state.editorGroups) return null;
  return state.editorGroups.find(g => g.id === groupId) || null;
}

/** 获取当前活跃的编辑器分组ID（用于文件打开等操作） */
function getActiveEditorGroupId() {
  // 尝试找到有焦点的组
  if (state.editorGroups && state.editorGroups.length > 0) {
    for (const group of state.editorGroups) {
      if (group.monacoEditor && group.monacoEditor.hasTextFocus()) {
        return group.id;
      }
    }
    // 🔧 Bug5修复：即使只有1个group也要检查_lastActiveGroupId
    if (state._lastActiveGroupId != null && findGroupById(state._lastActiveGroupId)) {
      return state._lastActiveGroupId;
    }
  }
  // 返回第一个组的id（可能是0或非0）
  return state.editorGroups?.[0]?.id ?? 0;
}

/**
 * 🔧 Task#334：获取当前活跃编辑器分组的当前标签页ID
 * 替代直接读取 state.currentTabId 全局变量（多group场景下全局变量无法正确区分）
 */
function getActiveEditorTabId() {
  const groupId = getActiveEditorGroupId();
  const group = findGroupById(groupId);
  return group?.currentTabId || null;
}

/**
 * 🔧 Task#334/340：获取当前活跃编辑器分组的当前标签页对象
 * 替代 state.editorTabs.find(t => t.id === state.currentTabId) 模式
 */
function getActiveEditorTab() {
  const groupId = getActiveEditorGroupId();
  const group = findGroupById(groupId);
  if (!group || !group.currentTabId) return null;
  return group.tabs.find(t => t.id === group.currentTabId) || null;
}

/**
 * 🔧 Task#334/340：从所有group中查找指定tabId的标签页对象
 * 用于需要跨group查找的场景（如文件变更检测、文件浏览器等）
 */
function findTabById(tabId) {
  if (!tabId) return null;
  if (state.editorGroups) {
    for (const group of state.editorGroups) {
      const found = group.tabs.find(t => t.id === tabId);
      if (found) return found;
    }
  }
  return null;
}

/**
 * 🔧 Task#334：设置当前活跃编辑器分组的当前标签页ID
 * 替代直接赋值 state.currentTabId = xxx（已删除该全局变量）
 * 唯一真实来源是 group.currentTabId
 */
function setActiveEditorTabId(tabId) {
  const groupId = getActiveEditorGroupId();
  const group = findGroupById(groupId);
  if (group) {
    group.currentTabId = tabId;
  }
}

/** 在指定分组打开文件标签页 */
async function openFileInGroup(groupId, filePath, useLocal = false, tabNestForCreate = null) {
  const tabId = `tab-${btoa(encodeURIComponent(filePath)).replace(/[/+=]/g, '-')}`;

  // 🔧 Bug2修复：只在真正没有分组结构时才返回false
  if (!state.editorGroups || state.editorGroups.length === 0) {
    return false;
  }

  // 确保分组存在（用find而非索引）
  const group = findGroupById(groupId);
  if (!group) return false;

  // 检查该组是否已有此标签
  const existingTab = group.tabs.find(t => t.id === tabId);
  if (existingTab) {
    switchTabInGroup(groupId, tabId);
    return true;
  }

  // 创建新标签页
  const fileName = filePath.split('/').pop().split('\\').pop();
  const ext = '.' + filePath.split('.').pop().toLowerCase();
  // CMakeLists.txt 特殊处理：文件名匹配而非扩展名
  const fileNameLower = filePath.split('/').pop().split('\\').pop().toLowerCase();
  const langMap = {
    '.py': 'python', '.kn': 'python', '.js': 'javascript', '.ts': 'typescript',
    '.html': 'html', '.css': 'css', '.json': 'json',
    '.md': 'markdown', '.sh': 'shell', '.yaml': 'yaml', '.yml': 'yaml',
    '.toml': 'toml', '.rs': 'rust', '.go': 'go', '.java': 'java',
    '.cpp': 'cpp', '.c': 'c', '.h': 'cpp', '.hpp': 'cpp',
    '.sql': 'sql', '.xml': 'xml', '.vue': 'html', '.jsx': 'javascript',
    '.tsx': 'typescript', '.lua': 'lua', '.rb': 'ruby', '.php': 'php',
    '.zig': 'zig', '.cs': 'csharp', '.kt': 'kotlin', '.swift': 'swift',
    '.dart': 'dart', '.scala': 'scala', '.cmake': 'cmake',
  };
  let language = langMap[ext] || 'markdown';  // 🔧 Task#866：未匹配扩展名默认markdown高亮
  // CMakeLists.txt 文件名匹配
  if (fileNameLower === 'cmakelists.txt') {
    language = 'cmake';
  }

  const newTab = {
    id: tabId,
    name: fileName,
    path: filePath,
    content: '',
    language: language,
    scrollTop: null,
    isNewFile: false,
    isVirtual: false,
    isLocal: useLocal || false,
    nest: tabNestForCreate,
    workdir: state.workdir || null,  // 🔧 Task#820：记录打开时的工作目录
    groupId: groupId
  };

  // 加载文件内容
  let nestParam = '';
  if (tabNestForCreate) {
    nestParam = '&nest=' + encodeURIComponent(tabNestForCreate);
  } else if (!useLocal) {
    nestParam = getNestParam();
  }

  // 🔧 先检查文件大小，大文件走分块加载
  try {
    const existsResponse = await fetch(`/api/files/exists?path=${encodeURIComponent(filePath)}${nestParam}`);
    const existsResult = await existsResponse.json();
    if (existsResponse.ok && existsResult.success && existsResult.exists) {
      const LARGE_FILE_THRESHOLD = state.webuiConfig.large_file_threshold || (3 * 1024 * 1024);
      const fileSize = existsResult.size || 0;
      const isImage = existsResult.is_image || false;
      if (!isImage && fileSize > LARGE_FILE_THRESHOLD) {
        // 🔧 Task#866：超大文件不使用markdown默认高亮，回退plaintext
        if (!langMap[ext]) newTab.language = 'plaintext';
        // 大文件：在group内并发分块加载
        const nestName = nestParam ? nestParam.replace('&nest=', '') : 'local';
        const sizeStr = formatSizeHuman(fileSize);
        const estimatedSeconds = Math.max(1, Math.round(fileSize / (10 * 1024 * 1024)));
        const startTime = Date.now();

        newTab._isLoading = true;
        newTab._cancelled = false;
        newTab.content = _buildProgressText(nestName, filePath, sizeStr, 0, 0, estimatedSeconds, 0);

        group.tabs.push(newTab);
        group.currentTabId = tabId;
        setActiveEditorTabId(tabId);
        renderGroupTabs(groupId);
        await createGroupEditor(groupId);
        await switchTabInGroup(groupId, tabId);

        // 🔧 Task#262：并发分块加载
        const fullContent = await _loadFileChunksParallel(filePath, fileSize, nestParam, nestName, sizeStr, estimatedSeconds, startTime, newTab, tabId, groupId, group);

        // 加载完成
        newTab.content = fullContent;
        newTab._isLoading = false;
        newTab.original = fullContent;
        newTab._isLargeFile = true;  // 🔧 Task#810：标记大文件
        if (!newTab._cancelled && group.monacoEditor && group.currentTabId === tabId) {
          window._suppressContentChangeTracking = true;
          try {
            // 🔧 Task#810：setModel前禁用昂贵的换行（minimap保留，Task#866）
            group.monacoEditor.updateOptions({
              readOnly: true,  // 🔧 Task#866：超大文件只读，禁止修改
              wordWrap: 'off',
              wrappingStrategy: 'simple',
              scrollbar: { horizontal: 'visible', horizontalScrollbarSize: 10 },  // 🔧 Task#866：大文件始终显示横向滚动条
            });
            if (newTab.path && window.monaco) {
              const sharedModel = getOrCreateSharedModel(newTab.path, fullContent, newTab.language);
              if (sharedModel) group.monacoEditor.setModel(sharedModel);
              else group.monacoEditor.setValue(fullContent);
            } else {
              group.monacoEditor.setValue(fullContent);
            }
            group.monacoEditor.layout();
          } finally { window._suppressContentChangeTracking = false; }
        }
        renderGroupTabs(groupId);
        showSimpleToast(`📄 ${fileName} 已加载 (${formatSizeHuman(fileSize)})，超大文件为只读模式`, 'info');
        return true;
      }
    }
  } catch (e) {
    // 检查失败则走正常路径
  }

  // 正常文件：先加载内容，再用原子操作创建标签+编辑器
  try {
    const response = await fetch('/api/files/read?path=' + encodeURIComponent(filePath) + nestParam);
    const result = await response.json();
    if (response.ok && result.content !== undefined) {
      newTab.content = result.content || '';
      // 🔧 修复：记录原始内容，用于判断文件是否被编辑，以及文件变化时的自动合并
      newTab.original = newTab.content;
      if (result.language) newTab.language = result.language;
      // 🔧 修复：使用后端返回的绝对路径，确保与 file_watcher 广播的路径一致
      if (result.path) newTab.path = result.path;
    }
  } catch (e) {
    // 静默处理
  }

  group.tabs.push(newTab);
  group.currentTabId = tabId;
  setActiveEditorTabId(tabId);
  renderGroupTabs(groupId);
  await createGroupEditor(groupId);
  await switchTabInGroup(groupId, tabId);

  return true;
}

// 格式化文件大小为人类可读格式
function formatSizeHuman(bytes) {
  if (bytes === 0) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  const i = Math.floor(Math.log(bytes) / Math.log(1024));
  return (bytes / Math.pow(1024, i)).toFixed(i === 0 ? 0 : 1) + ' ' + units[i];
}

// 大文件加载确认对话框
function showLargeFileDialog(filePath, fileSize) {
  return new Promise((resolve) => {
    const fileName = filePath.split('/').pop().split('\\').pop();
    const sizeStr = formatSizeHuman(fileSize);
    // 预估加载时间（基于10MB/s网络速度）
    const estimatedSeconds = Math.max(1, Math.round(fileSize / (10 * 1024 * 1024)));
    const now = new Date();
    const endTime = new Date(now.getTime() + estimatedSeconds * 1000);
    const endTimeStr = endTime.toLocaleTimeString();

    const overlay = document.createElement('div');
    overlay.style.cssText = 'position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,0.5);z-index:30000;display:flex;align-items:center;justify-content:center;';

    const dialog = document.createElement('div');
    dialog.style.cssText = 'background:var(--bg-secondary, #2d2d2d);border:1px solid var(--border-color, #444);border-radius:8px;padding:24px;max-width:420px;width:90%;color:var(--text-primary, #e0e0e0);';
    dialog.innerHTML = `
      <div style="font-size:16px;font-weight:600;margin-bottom:16px;">📄 大文件加载确认</div>
      <div style="margin-bottom:8px;"><strong>文件名：</strong>${fileName}</div>
      <div style="margin-bottom:8px;"><strong>文件大小：</strong>${sizeStr}</div>
      <div style="margin-bottom:8px;"><strong>预计加载时间：</strong>约 ${estimatedSeconds} 秒</div>
      <div style="margin-bottom:16px;"><strong>预计完成时间：</strong>${endTimeStr}</div>
      <div style="display:flex;gap:12px;justify-content:flex-end;">
        <button id="large-file-cancel" style="padding:8px 20px;border:1px solid var(--border-color, #555);background:transparent;color:var(--text-primary, #e0e0e0);border-radius:4px;cursor:pointer;">取消</button>
        <button id="large-file-confirm" style="padding:8px 20px;background:var(--accent-color, #4a9eff);color:#fff;border:none;border-radius:4px;cursor:pointer;">加载</button>
      </div>
    `;
    overlay.appendChild(dialog);
    document.body.appendChild(overlay);

    dialog.querySelector('#large-file-confirm').onclick = () => {
      document.body.removeChild(overlay);
      resolve(true);
    };
    dialog.querySelector('#large-file-cancel').onclick = () => {
      document.body.removeChild(overlay);
      resolve(false);
    };
    overlay.onclick = (e) => {
      if (e.target === overlay) {
        document.body.removeChild(overlay);
        resolve(false);
      }
    };
  });
}
// 构建加载进度文本（含ASCII进度条）
function _buildProgressText(nestName, filePath, sizeStr, loadedBytes, pct, remainingSec, elapsedSec, completedChunks, totalChunks) {
  const barWidth = 40;
  const filled = Math.round(barWidth * pct / 100);
  const empty = barWidth - filled;
  const bar = '█'.repeat(filled) + '░'.repeat(empty);
  const loadedStr = loadedBytes > 0 ? formatSizeHuman(loadedBytes) : '0 B';
  const shortPath = filePath.length > 60 ? '...' + filePath.slice(-57) : filePath;
  const chunkInfo = (completedChunks !== undefined && totalChunks !== undefined)
    ? `  分块: ${completedChunks}/${totalChunks}    并发: ${state.webuiConfig.large_file_parallel_concurrency || 4}\n`
    : '';
  return [
    '',
    `  📁 [${nestName}] ${shortPath}`,
    `  📦 ${sizeStr}`,
    '',
    `  ${bar}  ${pct}%`,
    '',
    `  已加载: ${loadedStr} / ${sizeStr}`,
    chunkInfo +
    `  已用: ${elapsedSec}s    剩余: ~${remainingSec}s`,
    '',
    '  💡 关闭标签页可取消加载',
    '',
  ].join('\n');
}

// 异步分块加载大文件：先创建标签页显示进度，循环分块读取完成后替换内容
async function loadLargeFileAsync(filePath, tabId, fileSize, nestParam, tabNest, targetGroupId) {
  const fileName = filePath.split('/').pop().split('\\').pop();
  const nestName = nestParam ? nestParam.replace('&nest=', '') : 'local';
  const sizeStr = formatSizeHuman(fileSize);
  const estimatedSeconds = Math.max(1, Math.round(fileSize / (10 * 1024 * 1024)));

  // 切换到编辑器视图
  // 🔧 Task#253修复：使用skipRestore避免restoreEditorTabs恢复旧标签页（如"新文档"），
  // 导致与即将创建的进度标签页冲突，显示错误的标签页
  if (state.currentViewTab !== 'editor') {
    await switchViewTab('editor', { skipAutoCreate: true, skipRestore: true });
  }

  // 先创建标签页，内容为加载进度提示
  let progressTab = {
    id: tabId,
    path: filePath,
    name: fileName,
    language: 'plaintext',
    content: _buildProgressText(nestName, filePath, sizeStr, 0, 0, estimatedSeconds, 0),
    original: '',
    cursorPosition: null,
    scrollTop: null,
    isContentEditor: false,
    isVirtual: false,
    isNewFile: false,
    nest: tabNest,
    workdir: state.workdir || null,  // 🔧 Task#820：记录打开时的工作目录
    _isLoading: true,
    _cancelled: false,
  };

  // 🔧 Task#255：统一走group路径，无group时先确保group结构存在（默认1个group）
  let groupId = targetGroupId;
  if (!state.editorGroups || state.editorGroups.length === 0) {
    ensureGroupStructure();
    groupId = getActiveEditorGroupId();
  }
  if (groupId == null) groupId = getActiveEditorGroupId();

  const group = findGroupById(groupId);
  if (group) {
    // 检查tab是否已存在（openFileInGroup可能已创建）
    const existingInGroup = group.tabs.find(t => t.id === tabId);
    if (existingInGroup) {
      existingInGroup.content = progressTab.content;
      existingInGroup._isLoading = true;
      existingInGroup._cancelled = false;
      progressTab = existingInGroup;
    } else {
      group.tabs.push(progressTab);
    }
    group.currentTabId = tabId;
    // 🔧 Task#326：统一使用group路径，不再调用updateEditorContent（它会走全局路径可能写入错误编辑器）
    // 直接走 renderGroupTabs + createGroupEditor + switchTabInGroup
    renderGroupTabs(groupId);
    await createGroupEditor(groupId);
    await switchTabInGroup(groupId, tabId);
  }

  // 🔧 Task#248：使用和"最近的文件"一致的格式显示路径
  _setEditorFilePathHTML(filePath, tabNest);
  if (typeof setLanguageBadge === 'function') { setLanguageBadge('加载中...'); } else { document.getElementById('editor-language').textContent = '加载中...'; }

  const startTime = Date.now();
  let language = 'plaintext';
  let allContent = '';

  try {
    // 🔧 Task#262：并发分块加载
    const g0 = findGroupById(groupId);
    allContent = await _loadFileChunksParallel(filePath, fileSize, nestParam, nestName, sizeStr, estimatedSeconds, startTime, progressTab, tabId, groupId, g0 || group);

    if (progressTab._cancelled) return;

    // 检查标签页是否还存在（从所有group中查找）
    let finalTabExists = false;
    if (state.editorGroups) {
      for (const g of state.editorGroups) {
        if (g.tabs.find(t => t.id === tabId)) { finalTabExists = true; break; }
      }
    }
    if (!finalTabExists || progressTab._cancelled) return;

    // 更新标签页为真实文件内容
    progressTab.path = filePath;
    progressTab.language = language;
    progressTab.content = allContent;
    progressTab.original = allContent;
    progressTab._isLoading = false;
    progressTab._isLargeFile = true;  // 🔧 Task#810：标记大文件，switchTabInGroup据此优化编辑器选项
    delete progressTab._cancelled;

    // 🔧 Task#255：统一走group路径更新编辑器显示
    const g = findGroupById(groupId);
    if (g) {
      renderGroupTabs(groupId);
      await createGroupEditor(groupId);
      await switchTabInGroup(groupId, tabId);
    }
    const totalTime = Math.round((Date.now() - startTime) / 1000);
    showSimpleToast(`✅ ${fileName} 加载完成 (${sizeStr}, ${totalTime}秒)，超大文件为只读模式`, 'info');

  } catch (error) {
    if (progressTab._cancelled) return;
    console.error('[loadLargeFileAsync] 加载失败:', error);
    showSimpleToast(`❌ 加载失败: ${error.message}`, 'error');
    // 移除标签页（从所有group中查找并移除）
    if (state.editorGroups) {
      for (const g of state.editorGroups) {
        const idx = g.tabs.findIndex(t => t.id === tabId);
        if (idx >= 0) { g.tabs.splice(idx, 1); break; }
      }
    }
    if (groupId != null) renderGroupTabs(groupId);
    else renderTabs();
  }
}

async function openEditorForFile(filePath, useLocal = false, foundNest = null) {
  // 🔧 非文本文件（图片/视频/音频/压缩包/可执行文件等）不支持编辑
  const nonTextExtensions = new Set([
    '.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.svg', '.ico', '.tiff', '.tif', '.avif', '.heic', '.heif',
    '.mp3', '.mp4', '.avi', '.mov', '.wav', '.flac', '.mkv', '.wmv', '.webm',
    '.zip', '.tar', '.gz', '.bz2', '.xz', '.7z', '.rar',
    '.exe', '.dll', '.so', '.dylib', '.bin', '.dat', '.iso', '.dmg',
    '.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx',
    '.odt', '.ods', '.odp',
  ]);
  const ext = '.' + filePath.split('.').pop().toLowerCase();
  if (nonTextExtensions.has(ext)) {
    showSimpleToast('⚠️ 不支持编辑此类文件', 'warning');
    return;
  }

  // 🔧 Task#261修复：防止并发调用导致竞态条件（如快速连续点击文件链接）
  if (window._openEditorForFileRunning) {
    console.log('[openEditorForFile] 跳过：已在运行中, filePath:', filePath);
    return;
  }
  window._openEditorForFileRunning = true;

  try {
    // 🔧 Task#280修复：不在打开文件前清理新文档标签——新文档标签应保留，文件在旁边创建新标签
    // _cleanupEmptyNewDocTabs();
    // 生成标签ID（基于文件路径）
    const tabId = `tab-${btoa(encodeURIComponent(filePath)).replace(/[/+=]/g, '-')}`;

    // useLocal时，不带nest参数（用于远程文件不存在时回退到本地）
    // 🔧 foundNest：当checkFileExistsWithLocalFallback在其他nest找到文件时传入
    let nestParamToUse;
    let tabNestForCreate;
    if (useLocal) {
      nestParamToUse = '';
      tabNestForCreate = null;
    } else if (foundNest) {
      // 文件在其他nest上找到的，使用该nest
      nestParamToUse = `&nest=${encodeURIComponent(foundNest)}`;
      tabNestForCreate = foundNest;
    } else {
      nestParamToUse = getNestParam();
      tabNestForCreate = getSelectedNestName() || null;
    }

    // 🔧 Task#260修复：统一在所有分组中查找已有标签，找到则切换焦点而非重复加载
    let existingInGroup = null;
    let existingGroupId = -1;
    if (state.editorGroups) {
      for (const group of state.editorGroups) {
        const found = group.tabs.find(t => t.id === tabId);
        if (found) { existingInGroup = found; existingGroupId = group.id; break; }
      }
    }

    if (existingInGroup && existingGroupId >= 0) {
      // 🔧 Task#260：同nest同路径文件已在任意分组中打开，直接切换焦点到已有标签页
      if (state.currentViewTab !== 'editor') {
        await switchViewTab('editor', { skipAutoCreate: true, skipRestore: true });
      }
      // 切换到该标签所在的分组并激活
      state._lastActiveGroupId = existingGroupId;
      setActiveEditorTabId(tabId);  // 🔧 Task#334：使用工具函数
      switchTabInGroup(existingGroupId, tabId);
      renderTabs();
    } else {
      // 如果不存在，先检查文件是否存在
      try {
        const response = await fetch(`/api/files/exists?path=${encodeURIComponent(filePath)}${nestParamToUse}`);
        const result = await response.json();
        if (!result.success || !result.exists) {
          return;
        }

        // 🔧 大文件检测：图片>30MB拒绝加载，非图片>3MB异步加载
        const LARGE_FILE_THRESHOLD = state.webuiConfig.large_file_threshold || (3 * 1024 * 1024);  // 🔧 Task#262：从配置读取
        const LARGE_IMAGE_THRESHOLD = 30 * 1024 * 1024;  // 30MB
        const fileSize = result.size || 0;
        const isImage = result.is_image || false;

        if (isImage && fileSize > LARGE_IMAGE_THRESHOLD) {
          showSimpleToast(`⚠️ 图片文件过大 (${formatSizeHuman(fileSize)})，超过30MB限制，无法加载`, 'warning');
          return;
        }

        if (!isImage && fileSize > LARGE_FILE_THRESHOLD) {
          // 大文件：异步加载，先创建标签页显示进度
          // 🔧 Task#255：统一走group路径，loadLargeFileAsync内部会确保group结构
          const targetGroupId = getActiveEditorGroupId();
          await loadLargeFileAsync(filePath, tabId, fileSize, nestParamToUse, tabNestForCreate, targetGroupId);
          return;
        }

        // 切换到编辑器视图
        // 🔧 Task#253修复：skipRestore避免恢复旧标签页导致与新标签冲突
        if (state.currentViewTab !== 'editor') {
          await switchViewTab('editor', { skipAutoCreate: true, skipRestore: true });
        }

        // 加载Monaco Editor（正常路径才需要，大文件路径不需要）
        await loadMonacoEditor();

        // 🔧 Task#255：统一走group路径，无group时先确保group结构存在
        if (!state.editorGroups || state.editorGroups.length === 0) {
          ensureGroupStructure();
        }
        const activeGroupId = getActiveEditorGroupId();
        await openFileInGroup(activeGroupId, filePath, useLocal, tabNestForCreate);
      } catch (error) {
        return;
      }
    }

  } catch (error) {
    // 只有非文件不存在的错误才显示提示
    if (error.message !== '无法读取文件') {
      console.error('[openEditorForFile] 打开编辑器失败:', error);
      showSimpleToast(`❌  打开编辑器失败: ${error.message}`, 'error');
    }
  } finally {
    // 🔧 Task#261修复：释放防重入锁
    window._openEditorForFileRunning = false;
  }
}

/**
 * 🔧 Task#261修复：清理所有空的、未修改的"新文档"标签页
 * 这些空白标签页在点击文件超链接时会干扰文件标签页的显示
 * 只清理满足以下所有条件的标签页：
 * 1. isNewFile === true
 * 2. 内容为空（空字符串）
 * 3. 不在大文件加载中（_isLoading 不为 true）
 */
function _cleanupEmptyNewDocTabs() {
  const newDocRegex = /^新文档\d+$/;
  let cleaned = 0;

  // 清理所有分组中的空新文档标签页
  if (state.editorGroups) {
    state.editorGroups.forEach(group => {
      for (let i = group.tabs.length - 1; i >= 0; i--) {
        const tab = group.tabs[i];
        if (tab.isNewFile && !tab.content && !tab._isLoading && newDocRegex.test(tab.name)) {
          // 释放共享model
          releaseSharedModel(tab.path);
          group.tabs.splice(i, 1);
          cleaned++;
        }
      }
      // 如果清理后 currentTabId 指向已删除的标签页，更新它
      if (group.tabs.length > 0 && (!group.currentTabId || !group.tabs.find(t => t.id === group.currentTabId))) {
        group.currentTabId = group.tabs[0].id;
      } else if (group.tabs.length === 0) {
        group.currentTabId = null;
      }
    });
  }

  if (cleaned > 0) {
    console.log(`[_cleanupEmptyNewDocTabs] 清理了 ${cleaned} 个空白新文档标签页`);
    // 重新渲染标签页
    if (state.editorGroups && state.editorGroups.length > 0) {
      state.editorGroups.forEach(g => renderGroupTabs(g.id));
    }
    renderTabs();
  }
}

// 打开编辑器编辑消息/事务文本
async function openEditorForContent(editType, editId, rawText, rootMsgId = null) {
  // 🔧 Task#177修复：防止快速双击编辑图标导致创建重复标签页
  const lockKey = `text-edit-${editType}-${editId}`;
  if (window._openEditorForContentRunning === lockKey) return;
  window._openEditorForContentRunning = lockKey;
  try {
    // 🔧 Task#261修复：清理空的"新文档"标签页，避免干扰
    _cleanupEmptyNewDocTabs();

    // 🔧 优化：并行加载Monaco Editor和API获取内容，减少等待时间
    const [_monacoResult, contentResult] = await Promise.all([
      loadMonacoEditor(),
      (async () => {
        try {
          let url;
          if (editType === 'message') {
            url = '/api/messages/' + editId;
          } else if (editType === 'branch-message') {
            url = '/api/messages/' + editId + '?branch=true';
          } else if (editType === 'task') {
            url = '/api/tasks/' + editId;
          }
          if (url) {
            const response = await fetch(url);
            const result = await response.json();
            return result.content || '';
          }
        } catch (apiError) {
          console.warn('[openEditorForContent] API获取内容失败，使用备用方案:', apiError);
        }
        // 备用方案：使用HTML attribute中的rawText
        const textarea = document.createElement('textarea');
        textarea.innerHTML = rawText;
        return textarea.value;
      })()
    ]);

    // 切换到编辑器视图（skipAutoCreate: 不自动创建新文档，由本函数自己创建标签页）
    // 🔧 Task#253修复：skipRestore避免恢复旧标签页导致与内容标签冲突
    await switchViewTab('editor', { skipAutoCreate: true, skipRestore: true });

    // 根据editType生成标题
    const titleMap = {
      'message': `消息 #${editId}`,
      'branch-message': `分支消息 #${editId}`,
      'task': `事务 #${editId}`
    };
    const title = titleMap[editType] || `${editType} #${editId}`;

    // 生成标签ID
    const tabId = `text-edit-${editType}-${editId}`;

    // 检查是否已存在该标签（在所有分组中查找）
    let existingTabInGroup = null;
    let existingGroupId = -1;
    if (state.editorGroups) {
      for (const group of state.editorGroups) {
        const found = group.tabs.find(t => t.id === tabId);
        if (found) { existingTabInGroup = found; existingGroupId = group.id; break; }
      }
    }

    if (existingTabInGroup && existingGroupId >= 0) {
      // 在分组中已存在，切换到该分组和标签
      switchTabInGroup(existingGroupId, tabId);
    } else {
      // 🔧 修复 Bug：创建新标签页前保存当前标签页状态
      if (getActiveEditorTabId()) {
        saveCurrentTabState();
      }

      // 使用已并行获取的内容
      const decodedText = contentResult;

      // 创建新标签
      const newTab = {
        id: tabId,
        path: title,
        name: title,
        language: 'markdown',
        content: decodedText,
        original: decodedText,
        cursorPosition: null,
        scrollTop: null,
        isContentEditor: true,
        isVirtual: false,
        isNewFile: false,
        editType: editType,
        editId: editId,
        rootMsgId: rootMsgId
      };

      const activeGroupId = getActiveEditorGroupId();

      // 确保group结构存在
      if (!state.editorGroups || state.editorGroups.length === 0) {
        ensureGroup0();
      }

      const targetGroup = findGroupById(activeGroupId);
      if (targetGroup) {
        // 删除同editType+editId的旧标签页，防止重复
        const dupIdx = targetGroup.tabs.findIndex(t =>
          t.editType === editType && t.editId === editId && t.id !== tabId
        );
        if (dupIdx >= 0) {
          targetGroup.tabs.splice(dupIdx, 1);
        }
        targetGroup.tabs.push(newTab);
        targetGroup.currentTabId = tabId;
      }
      setActiveEditorTabId(tabId);
      saveEditorTabs();
      renderGroupTabs(activeGroupId);
      await createGroupEditor(activeGroupId);
      await switchTabInGroup(activeGroupId, tabId);

      // 更新路径显示（统一用 updateEditorPathDisplay）
      updateEditorPathDisplay(newTab);

      // 恢复保存按钮
      const saveBtn = document.getElementById('editor-save');
      if (saveBtn) {
        saveBtn.disabled = false;
        setSaveBtnContent(saveBtn, 'save');
      }
    }

    // 更新标签页 UI
    renderTabs();
    if (state.currentLayout !== 'default' && state.editorGroups?.length > 0) {
      state.editorGroups.forEach(g => renderGroupTabs(g.id));
    }

  } catch (error) {
    console.error('打开编辑器失败:', error);
    showSimpleToast(`❌  打开编辑器失败: ${error.message}`, 'error');
  } finally {
    // 🔧 Task#177修复：释放防重入锁
    if (window._openEditorForContentRunning === lockKey) {
      window._openEditorForContentRunning = null;
    }
  }
}

// 创建新标签
async function createNewTab(filePath, tabId, useLocal = false, overrideNest = null) {
  // useLocal时，不带nest参数（用于远程文件不存在时回退到本地）
  // 🔧 overrideNest：当文件在其他nest上找到时，使用该nest读取文件和标记标签
  let nestParamToUse;
  let tabNest;
  if (useLocal) {
    nestParamToUse = '';
    tabNest = null;
  } else if (overrideNest) {
    nestParamToUse = `&nest=${encodeURIComponent(overrideNest)}`;
    tabNest = overrideNest;
  } else {
    nestParamToUse = getNestParam();
    tabNest = getSelectedNestName() || null;
  }
  // 从后端获取文件内容
  const response = await fetch(`/api/files/read?path=${encodeURIComponent(filePath)}${nestParamToUse}`);
  if (!response.ok) {
    throw new Error('无法读取文件');
  }

  const { content, language, path: absolutePath } = await response.json();

  // 🔧 修复：使用后端返回的绝对路径，确保与 file_watcher 广播的路径一致
  const actualPath = absolutePath || filePath;

  // 获取文件名
  const fileName = actualPath.split('/').pop().split('\\').pop();

  // 保存当前标签的状态（如果存在）
  if (getActiveEditorTabId()) {
    saveCurrentTabState();
  }

  // 创建新标签

  const newTab = {
    id: tabId,
    path: actualPath,  // 🔧 修复：使用绝对路径
    name: fileName,
    language: language,
    content: content,
    original: content,  // 🔧 新增：原始内容，用于判断是否已编辑（content != original）
    cursorPosition: null,
    scrollTop: null,
    isContentEditor: false,
    isVirtual: false,
    isNewFile: false,
    useLocal: useLocal,  // 🔧 标记是否使用本地模式（远程文件不存在时回退到本地）
    nest: tabNest,  // 🔧 Task#72：记住打开文件时的nest来源
    workdir: state.workdir || null,  // 🔧 Task#820：记录打开时的工作目录
  };

  // 🔧 Task#340：添加到group0（唯一数据源）
  ensureGroup0();
  state.editorGroups[0].tabs.push(newTab);
  setActiveEditorTabId(tabId);  // 🔧 Task#334：使用工具函数

  // 保存标签页状态
  saveEditorTabs();

  // 更新编辑器内容
  updateEditorContent(newTab);

  // 更新标题和保存按钮状态（真实文件可保存）
  // 🔧 Task#248：使用和"最近的文件"一致的格式显示路径
  _setEditorFilePathHTML(filePath, tabNest);
  if (typeof setLanguageBadge === 'function') { setLanguageBadge(language); } else { document.getElementById('editor-language').textContent = language; }

  // 存储编辑器状态到编辑器视图容器
  const editorView = document.getElementById('editor-view');
  if (editorView) {
    editorView.dataset.currentFilePath = filePath;
    editorView.dataset.isVirtual = 'false';
  }

  // 恢复保存按钮
  const saveBtn = document.getElementById('editor-save');
  if (saveBtn) {
    saveBtn.disabled = false;
    setSaveBtnContent(saveBtn, 'save');
  }

  // 🔧 Task#416：创建新标签页后同步nest下拉框和工作目录（异步查找.git工程目录）
  _syncNestAndWorkdirForTab({ path: actualPath, nest: tabNest });
}

// 切换到指定标签
async function switchToTab(tabId) {
  const tab = findTabById(tabId);
  if (!tab) {
    return;
  }

  // 保存当前标签的状态
  if (getActiveEditorTabId()) {  // 🔧 Task#334
    saveCurrentTabState();
  }

  // 切换到新标签
  setActiveEditorTabId(tabId);  // 🔧 Task#334：使用工具函数

  // 🔧 修复：优先使用已有内容，不重新加载
  // 只有在内容为空时才尝试从服务器加载
  // 这样可以保留从临时文件恢复的内容
  if (tab.content === undefined || tab.content === null) {
    if (!tab.isVirtual && !tab.isNewFile && tab.path) {
      // 重新加载文件内容（确保是最新的）
      // 🔧 如果标签标记为useLocal，则不带nest参数读取本地文件
      const reloadNestParam = tab.useLocal ? '' : getNestParam();
      try {
        const response = await fetch(`/api/files/read?path=${encodeURIComponent(tab.path)}${reloadNestParam}`);
        if (response.ok) {
          const { content, language, path: absolutePath } = await response.json();
          tab.content = content;
          tab.language = language;
          // 🔧 修复：记录原始内容，用于判断文件是否被编辑，以及文件变化时的自动合并
          tab.original = content;
          // 🔧 Task#421：文件保存/重新加载后清除编辑历史（original变了，offset基准变了）
          clearEditTrackerByPath(tab.path);
          // 🔧 修复：使用后端返回的绝对路径，确保与 file_watcher 广播的路径一致
          if (absolutePath) {
            tab.path = absolutePath;
          }
        }
      } catch (error) {
        console.error('重新加载文件失败:', error);
      }
    }
  }

  // 更新编辑器内容
  updateEditorContent(tab);

  // 🔧 修复：切换标签后聚焦到编辑器（覆盖 initFocusManagement 的10ms延迟聚焦）
  // 🔧 Task#311修复：优先使用当前活跃group的编辑器聚焦，而非全局monacoEditor
  const focusGroupId = getActiveEditorGroupId();
  const focusGroup = (focusGroupId != null && state.editorGroups) ? findGroupById(focusGroupId) : null;
  const focusEditor = (focusGroup && focusGroup.monacoEditor) ? focusGroup.monacoEditor : monacoEditor;
  if (focusEditor && typeof focusEditor.focus === 'function') {
    // 延迟20ms聚焦，覆盖 initFocusManagement 的10ms延迟
    setTimeout(() => {
      if (focusEditor && typeof focusEditor.focus === 'function') {
        focusEditor.focus();
      }
    }, 20);
  }

  // 更新标题和保存按钮状态
  const saveBtn = document.getElementById('editor-save');

  // 获取编辑器视图容器
  const editorView = document.getElementById('editor-view');

  // 🔧 修复：确保属性有明确的布尔值，避免 undefined 导致判断错误
  const isContentEditor = !!tab.isContentEditor;
  const isVirtual = !!tab.isVirtual;
  const isNewFile = !!tab.isNewFile;

  if (isContentEditor) {
    // 文本编辑器（消息/分支消息/事务）
    _setEditorFilePathText(tab.name);
    if (editorView) {
      editorView.dataset.isVirtual = 'true';
    }
    if (saveBtn) {
      saveBtn.disabled = false;
      setSaveBtnContent(saveBtn, 'save');
    }
  } else if (isVirtual) {
    // 虚拟文件（只读）
    _setEditorFilePathText(tab.name + '（只读）');
    if (editorView) {
      editorView.dataset.isVirtual = 'true';
    }
    if (saveBtn) {
      saveBtn.disabled = true;
      setSaveBtnContent(saveBtn, 'readonly');
    }
  } else if (isNewFile) {
    // 新文件（需要保存时另存为）
    _setEditorFilePathText(tab.name + '（未保存）');
    if (editorView) {
      editorView.dataset.isVirtual = 'false';
      editorView.dataset.isNewFile = 'true';
    }
    if (saveBtn) {
      saveBtn.disabled = false;
      setSaveBtnContent(saveBtn, 'save');
    }
  } else {
    // 普通文件（可编辑） - 🔧 Task#248：使用和"最近的文件"一致的格式显示路径
    _setEditorFilePathHTML(tab.path, tab.nest);
    if (editorView) {
      editorView.dataset.isVirtual = 'false';
      editorView.dataset.isNewFile = 'false';
    }
    if (saveBtn) {
      saveBtn.disabled = false;
      setSaveBtnContent(saveBtn, 'save');
    }
  }
  if (typeof setLanguageBadge === 'function') { setLanguageBadge(tab.language); } else { document.getElementById('editor-language').textContent = tab.language; }
  // 触发LSP状态检查（更新语言按钮上的状态点）
  if (typeof checkLspStatus === 'function' && tab.path) {
    checkLspStatus(tab.path, tab.nest);
  }
  if (editorView) {
    editorView.dataset.currentFilePath = tab.path || tab.name;
  }

  // 🔧 Task#416：切换编辑器标签页时，根据文件所在nest同步nest下拉框和工作目录
  if (!isContentEditor && !isVirtual && tab.path) {
    _syncNestAndWorkdirForTab(tab);
  }
}

// 保存当前标签的状态
// 🔧 Task#311修复：使用当前活跃group的monacoEditor而非全局monacoEditor
// 防止在group模式下保存错误编辑器的内容到错误标签，导致标签页内容互相覆盖
function saveCurrentTabState() {
  const activeTabId = getActiveEditorTabId();  // 🔧 Task#334
  if (!activeTabId) return;

  // 🔧 Task#311：优先使用当前活跃group的monacoEditor
  const activeGroupId = getActiveEditorGroupId();
  const activeGroup = (activeGroupId != null && state.editorGroups) ? findGroupById(activeGroupId) : null;
  const editor = (activeGroup && activeGroup.monacoEditor) ? activeGroup.monacoEditor : monacoEditor;
  if (!editor) return;

  // 🔧 Task#311/340：在所有group中查找当前标签页
  let currentTab = null;
  if (activeGroup) {
    currentTab = activeGroup.tabs.find(t => t.id === activeTabId);
  }
  if (!currentTab) {
    currentTab = findTabById(activeTabId);
  }
  if (!currentTab) return;

  // 保存内容
  const newContent = editor.getValue();
  const oldContent = currentTab.content;

  // 只有在内容真正变化时才更新和保存
  if (newContent !== oldContent) {
    currentTab.content = newContent;
    // 🔧 关键修复：切换标签页时也要自动保存到临时文件
    autoSaveToTemp(currentTab);
  }

  // 保存光标位置
  const position = editor.getPosition();
  if (position) {
    currentTab.cursorPosition = { line: position.lineNumber, column: position.column };
  }

  // 保存滚动位置
  currentTab.scrollTop = editor.getScrollTop();

  // 🔧 Task#327：缓存完整的viewState（含光标、选中、undo/redo），用于刷新恢复
  try {
    currentTab._savedViewState = editor.saveViewState();
  } catch (e) { /* 忽略 */ }
}

// 更新编辑器内容
function updateEditorContent(tab) {
  // 🔧 Task#280修复：优先使用当前group的Monaco编辑器，而非全局monacoEditor
  // 默认布局中group.monacoEditor和全局monacoEditor可能是不同的实例
  const activeGroupId = getActiveEditorGroupId();
  const groupEditor = (activeGroupId != null && state.editorGroups) ?
    (findGroupById(activeGroupId) || {}).monacoEditor : null;
  const editor = groupEditor || monacoEditor;

  if (editor) {

    // 🔧 Task#134修复：程序化更新编辑器内容时，抑制 onDidChangeModelContent 回调
    // 防止中间状态（如空字符串）污染当前tab的content属性
    window._suppressContentChangeTracking = true;
    try {
      // 🔧 Bug8修复：如果有path，使用共享model而非setValue
      if (tab.path && window.monaco) {
        const sharedModel = getOrCreateSharedModel(tab.path, tab.content || '', tab.language);
        if (sharedModel) {
          editor.setModel(sharedModel);
        } else {
          editor.setValue(tab.content || '');
        }
      } else {
        const newValue = tab.content || '';
        editor.setValue(newValue);
      }
      // 强制触发视图更新，确保内容完全刷新
      editor.layout();

      if (tab.language) {
        monaco.editor.setModelLanguage(editor.getModel(), tab.language);
      }
      // 使用自定义主题（与创建时保持一致）
      const customThemeName = `peng-${state.theme}`;
      monaco.editor.setTheme(customThemeName);

      // 🔧 修复：更新滚动条宽度（确保始终应用）
      // 🔧 Task#866：大文件需要横向滚动条（wordWrap=off），小文件隐藏横向滚动条
      const _isLarge = isTabLargeFile(tab) || (tab.content && tab.content.length > 500 * 1024);
      if (_isLarge) {
        editor.updateOptions({
          readOnly: true,  // 🔧 Task#866：超大文件只读
          verticalScrollbarSize: 10,
          wordWrap: 'off',
          wrappingStrategy: 'simple',
          scrollbar: { horizontal: 'visible', horizontalScrollbarSize: 10 },
        });
      } else {
        editor.updateOptions({
          readOnly: false,
          verticalScrollbarSize: 10,
          wordWrap: 'on',
          wrappingStrategy: 'advanced',
          scrollbar: { horizontal: 'hidden', horizontalScrollbarSize: 0 },
        });
      }

      // 恢复光标位置
      if (tab.cursorPosition) {
        editor.setPosition({
          lineNumber: tab.cursorPosition.line,
          column: tab.cursorPosition.column
        });
      }

      // 恢复滚动位置
      if (tab.scrollTop !== null) {
        editor.setScrollTop(tab.scrollTop);
      }

      // 聚焦到编辑器
      editor.focus();

      // 🔧 Task#421：hook 编辑操作追踪（用于保存完整 undo/redo 历史）
      hookEditTracker(editor, tab);
    } finally {
      window._suppressContentChangeTracking = false;
    }
  } else {
    // 🔧 Task#343：动态获取第一个group的editor容器（不再硬编码monaco-editor-container-0）
    const firstGroup = state.editorGroups && state.editorGroups.length > 0 ? state.editorGroups[0] : null;
    const containerId = firstGroup ? firstGroup.containerId : null;
    const editorContainer = containerId ? document.getElementById(containerId) : null;

    if (!editorContainer) {
      console.error('[updateEditorContent] 找不到Monaco编辑器容器');
      return;
    }

    // 先定义并应用自定义主题
    applyMonacoTheme(state.theme);

    // 创建编辑器时使用自定义主题
    const customThemeName = `peng-${state.theme}`;

    monacoEditor = monaco.editor.create(editorContainer, {
      value: tab.content,
      language: tab.language,
      theme: customThemeName,
      automaticLayout: false,  // 🔧 Task#383：改用自定义ResizeObserver，传入减去minimap偏移的宽度
      fontSize: FONT_SETTINGS.fontSizeEditor,  // 🔧 Task#1035：编辑器字号独立设置
      fontFamily: FONT_SETTINGS.fontFamily,  // 🔧 修复：使用字体设置
      minimap: { enabled: true, maxColumn: 50 },
      lineNumbers: 'on',
      scrollBeyondLastLine: false,
      scrollBeyondLastColumn: 0,  // 🔧 Task#383：禁止右侧额外滚动空间
      wordWrap: 'on',  // 默认开启自动换行
      wrappingStrategy: 'advanced',  // 🔧 Task#383：高级换行策略，精确处理CJK字符
      wrappingIndent: 'same',  // 🔧 修复（Task #31）：换行缩进与原始缩进一致，避免额外宽度需求
      formatOnPaste: true,
      formatOnType: true,
      largeFileOptimizations: false,  // 🔧 修复：禁用大文件优化，确保Ctrl+A/Ctrl+C对长文本也能正确选中全部内容
      scrollbar: {
        vertical: 'hidden',       // 🔧 Task#411：隐藏垂直滚动条（minimap提供导航）
        verticalScrollbarSize: 0,
        horizontal: 'hidden',     // 🔧 Task#866：默认隐藏横向滚动条，大文件时动态切换为visible
        horizontalScrollbarSize: 0,
      }
    });

    // 绑定编辑器事件（只绑定一次）
    if (!window.editorEventsBound) {
      // 🔧 Task#134：程序化更新编辑器内容时的抑制标志
      // 在 updateEditorContent 中设置为true，防止 onDidChangeModelContent
      // 将中间状态（如 setValue('')）写入当前tab的content，导致多标签页内容互相污染
      window._suppressContentChangeTracking = false;

      // 监听内容变化
      // 🔧 Task#311修复：捕获创建时的编辑器实例引用，避免后续monacoEditor全局变量被覆盖后读取错误的编辑器内容
      const globalEditorInstance = monacoEditor;
      monacoEditor.onDidChangeModelContent(() => {
        // 🔧 Task#134：程序化更新期间跳过内容同步
        if (window._suppressContentChangeTracking) return;

        // 🔧 Task#421：auto-hook — 仅在用户真实编辑时创建tracker
        // undo/redo时不创建（_pengUndoRedoing），否则初始快照是undo后内容会污染数据
        const curModel = globalEditorInstance.getModel();
        const currentTab = findTabById(getActiveEditorTabId());
        if (curModel && !window._pengUndoRedoing &&
            currentTab && currentTab.path &&
            (!window._pengEditTrackers || !window._pengEditTrackers[currentTab.path])) {
          hookEditTracker(globalEditorInstance, currentTab);
        }

        if (currentTab) {
          // 🔧 Task#311修复：使用捕获的编辑器实例，而非全局monacoEditor变量
          const newContent = globalEditorInstance.getValue();
          // 🔧 只在内容真正变化时才保存
          if (newContent !== currentTab.content) {
            currentTab.content = newContent;
            autoSaveToTemp(currentTab);
            // 🔧 Bug5修复：group0的全局monacoEditor编辑时同步内容到其他分拆列中同路径文件
            syncContentToOtherGroups(0, currentTab);
          }
        } else {
          console.warn('[onDidChangeModelContent] 找不到当前tab');
        }
      });

      // 光标位置变化时更新信息显示
      monacoEditor.onDidChangeCursorPosition(() => {
        updateEditorCursorInfo();
      });

      // 🔧 Task#383：自定义ResizeObserver替代automaticLayout + MutationObserver修正minimap位置
      // Monaco的automaticLayout用容器原始宽度layout，不感知minimap的14px right偏移，
      // 导致contentWidth超出minimap左边缘遮挡文字。
      // 方案：ResizeObserver触发layout，MutationObserver持续修正minimap.style.left
      if (typeof _safeLayoutEditor === 'function') {
        var _editorResizeObs = new ResizeObserver(function () { _safeLayoutEditor(monacoEditor); });
        _editorResizeObs.observe(editorContainer);
        _safeLayoutEditor(monacoEditor);
      } else {
        monacoEditor.layout();
      }
      if (typeof _setupMinimapFix === 'function') {
        _setupMinimapFix(monacoEditor);
      }


      // 添加 Ctrl+S 快捷键支持
      monacoEditor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS, function () {
        saveEditorContent();
      });

      // 添加 Alt+Z 快捷键：切换自动换行
      monacoEditor.addCommand(monaco.KeyMod.Alt | monaco.KeyCode.KeyZ, function () {
        const options = monacoEditor.getOptions();
        const currentWordWrap = options.get(monaco.editor.EditorOption.wordWrap);

        // 切换逻辑：如果当前是自动换行，则关闭自动换行
        // 如果当前不是自动换行，则开启自动换行
        if (currentWordWrap === 'on') {
          // 关闭自动换行
          monacoEditor.updateOptions({
            wordWrap: 'off'
          });
          showSimpleToast('📄 自动换行已关闭', 'info');
        } else {
          // 开启自动换行
          monacoEditor.updateOptions({
            wordWrap: 'on'
          });
          showSimpleToast('📄 自动换行已开启', 'success');
        }
      });

      // 🔧 修复（Task #10）：拦截 Ctrl+C，使用 Monaco API 获取完整选中文本写入剪贴板
      // Monaco Editor 依赖浏览器原生 Selection API 复制文本，当选择的文本跨越多行且超出可视区域时，
      // Chrome 的剪贴板操作会在中间用 "..." 截断（参考 GitHub Issue #1540, #3471）
      // 解决方案：用 addCommand 拦截 Ctrl+C，通过 getModel().getValueInRange() 获取完整文本，
      // 再用 navigator.clipboard.writeText() 写入剪贴板，绕过浏览器原生复制机制
      // 🔧 修复（Task #10 + #146）：拦截 Ctrl+C
      // Task #10: Monaco依赖浏览器原生Selection API复制，长文本会被截断，用API绕过
      // Task #146: clipboard操作可能触发focusin导致焦点被抢到输入框
      //           解决：设置 _suppressFocusManagement 标志抑制焦点管理，操作完成后恢复
      monacoEditor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyC, function () {
        const selection = monacoEditor.getSelection();
        if (!selection || selection.isEmpty()) {
          return; // 没有选中文本，不执行任何操作
        }
        const selectedText = monacoEditor.getModel().getValueInRange(selection);
        // 🔧 Task #146：抑制焦点管理器，防止clipboard操作期间焦点被抢走
        window._suppressFocusManagement = true;
        const restoreFocus = () => {
          setTimeout(() => {
            window._suppressFocusManagement = false;
            monacoEditor.focus();
          }, 50);
        };
        if (navigator.clipboard && typeof navigator.clipboard.writeText === 'function') {
          navigator.clipboard.writeText(selectedText).then(() => {
            // 复制成功，恢复焦点管理
            window._suppressFocusManagement = false;
          }).catch(err => {
            console.error('[peng-ctrl-c] Clipboard API 失败:', err);
            // 备用方案：使用 textarea + execCommand
            fallbackCopy(selectedText);
            restoreFocus();
          });
        } else {
          // 不支持 Clipboard API，使用备用方案
          fallbackCopy(selectedText);
          restoreFocus();
        }
      });

      // 🔧 修复（Task #10, #30, #146）：拦截 Ctrl+X（剪切）
      // Task #10, #30: 先同步删除选中文本，再异步写入剪贴板
      // Task #146: 设置 _suppressFocusManagement 防止焦点被抢
      monacoEditor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyX, function () {
        const selection = monacoEditor.getSelection();
        if (!selection || selection.isEmpty()) {
          return;
        }
        const selectedText = monacoEditor.getModel().getValueInRange(selection);
        // 同步删除选中文本（selection 对象在异步回调中可能失效）
        monacoEditor.executeEdits('peng-cut-ctrl', [{
          range: selection,
          text: '',
          forceMoveMarkers: true
        }]);
        // 🔧 Task #146：抑制焦点管理器
        window._suppressFocusManagement = true;
        const restoreFocus = () => {
          setTimeout(() => {
            window._suppressFocusManagement = false;
            monacoEditor.focus();
          }, 50);
        };
        if (navigator.clipboard && typeof navigator.clipboard.writeText === 'function') {
          navigator.clipboard.writeText(selectedText).then(() => {
            window._suppressFocusManagement = false;
          }).catch(err => {
            console.error('[peng-ctrl-x] Clipboard API 失败:', err);
            fallbackCopy(selectedText);
            restoreFocus();
          });
        } else {
          fallbackCopy(selectedText);
          restoreFocus();
        }
      });

      // 🔧 Task#341修复：mousedown追踪已统一到initGroupMousedownTrackers()中管理
      // onDidFocusEditorWidget保留路径显示更新功能
      // 🔧 Task#416：焦点切换时同步nest下拉框和工作目录
      monacoEditor.onDidFocusEditorWidget(() => {
        updateEditorPathDisplay();
        var focusTab = findTabById(getActiveEditorTabId());
        // 🔧 Task#820：isNewFile标签页也同步工作目录（使用记录的workdir）
        if (focusTab && !focusTab.isContentEditor && !focusTab.isVirtual && focusTab.path) {
          _syncNestAndWorkdirForTab(focusTab);
        }
      });

      // 🔧 F1 打开命令面板（Monaco默认F1可能被浏览器拦截，显式绑定）
      monacoEditor.addCommand(monaco.KeyCode.F1, function () {
        monacoEditor.trigger('keyboard', 'editor.action.quickCommand');
      });
      // Ctrl+Shift+P 也打开命令面板
      monacoEditor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyMod.Shift | monaco.KeyCode.KeyP, function () {
        monacoEditor.trigger('keyboard', 'editor.action.quickCommand');
      });

      // 🔧 Task#426：修复LSP右键菜单快捷键绑定
      // F12 转到定义（Monaco内置默认Ctrl+F12，覆盖为F12匹配VS Code标准）
      monacoEditor.addCommand(monaco.KeyCode.F12, function () {
        monacoEditor.trigger('peng', 'editor.action.revealDefinition');
      });
      // F2 转到符号
      monacoEditor.addCommand(monaco.KeyCode.F2, function () {
        monacoEditor.trigger('peng', 'editor.action.quickOutline');
      });

      // 🔧 Task#421：hook find/replace 搜索词变化，自动保存搜索历史
      _hookFindStateChanges(monacoEditor);

      // 🔧 Task#421：hook 编辑操作追踪（编辑器首次创建时）
      hookEditTracker(monacoEditor, tab);

      window.editorEventsBound = true;
    }
    // 🔧 Task#280修复：同步更新group.monacoEditor，防止后续createGroupEditor重复创建
    const activeGid = getActiveEditorGroupId();
    if (activeGid != null && state.editorGroups) {
      const g = findGroupById(activeGid);
      if (g && !g.monacoEditor) g.monacoEditor = monacoEditor;
    }
  }
}

// 渲染标签页 - 统一委托给renderGroupTabs(firstGroup.id)
function renderTabs() {
  // 🔧 Task#343：动态获取第一个group的tabs容器（不再硬编码editor-tabs-0）
  if (!state.editorGroups || state.editorGroups.length === 0) return;
  const firstGroup = state.editorGroups[0];
  const tabsContainer = document.getElementById('editor-tabs-' + firstGroup.id);
  if (!tabsContainer) {
    return;
  }

  tabsContainer.innerHTML = '';

  // 🔧 检测同名标签，为同名标签添加父目录后缀区分（类似VS Code）
  const g0Tabs = getG0Tabs();
  const nameMap = {};
  g0Tabs.forEach(tab => {
    if (!nameMap[tab.name]) nameMap[tab.name] = [];
    nameMap[tab.name].push(tab);
  });
  // 对同名标签计算最小区分路径
  const displayNameMap = {};
  Object.entries(nameMap).forEach(([name, tabs]) => {
    if (tabs.length <= 1) {
      displayNameMap[tabs[0].id] = name;
    } else {
      // 同名文件：找最短不同部分区分
      tabs.forEach(tab => {
        const parts = (tab.path || '').split(/[/\\]/).filter(Boolean);
        // 从倒数第2级（父目录）开始往上找，找到能区分的最短后缀
        for (let depth = 1; depth < parts.length; depth++) {
          const mySuffix = parts.slice(-(depth + 1)).join('/');
          const unique = !tabs.some(other => other.id !== tab.id &&
            (other.path || '').split(/[/\\]/).filter(Boolean).slice(-(depth + 1)).join('/') === mySuffix);
          if (unique) {
            const suffix = parts.slice(-depth - 1, -1).join('/');
            displayNameMap[tab.id] = name + (suffix ? ` (${suffix})` : '');
            return;
          }
        }
        displayNameMap[tab.id] = name;
      });
    }
  });

  g0Tabs.forEach(tab => {
    const tabElement = document.createElement('button');
    // 🔧 Task#327：大文件标签页加粗体样式
    const largeFileClass = (typeof isTabLargeFile === 'function' && isTabLargeFile(tab)) ? ' large-file-tab' : '';
    tabElement.className = `editor-tab ${tab.id === getActiveEditorTabId() ? 'active' : ''}${largeFileClass}`;  // 🔧 Task#334
    tabElement.dataset.tabId = tab.id;

    // hover hint：nest + 绝对路径
    var tabTitle = tab.path || '';
    if (tab.nest && tab.nest !== 'local') tabTitle = '[' + tab.nest + '] ' + tabTitle;
    if (tab.isNewFile && !tabTitle) tabTitle = tab.name;
    // 标签内容
    tabElement.innerHTML = `
      <span class="tab-name" title="${tabTitle}">${displayNameMap[tab.id] || tab.name}</span>
      <button class="tab-close" title="关闭">&times;</button>
    `;

    // 点击标签切换
    tabElement.addEventListener('click', (e) => {
      if (e.target.classList.contains('tab-close')) {
        e.stopPropagation();
        closeTab(tab.id);
      } else {
        switchToTab(tab.id);
        renderTabs();
        // 🔧 修复：点击编辑器标签后，焦点应该切换到编辑器，而不是消息输入框
        // 使用两次聚焦策略：第一次立即聚焦，第二次延迟20ms覆盖 initFocusManagement 的10ms延迟
        if (monacoEditor) {
          monacoEditor.focus();
          setTimeout(() => {
            if (monacoEditor) {
              monacoEditor.focus();
            }
          }, 20);
        }
      }
    });

    // 关闭按钮事件
    const closeBtn = tabElement.querySelector('.tab-close');
    if (closeBtn) {
      closeBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        closeTab(tab.id);
      });
    }

    tabsContainer.appendChild(tabElement);
  });

  // 新建文件按钮（与终端标签页的"+"按钮样式一致）
  const newTabBtn = document.createElement('button');
  newTabBtn.className = 'editor-tab-new';
  newTabBtn.textContent = '+';
  newTabBtn.title = '新建文档';
  newTabBtn.addEventListener('click', () => {
    createNewFile();
  });
  tabsContainer.appendChild(newTabBtn);
}

// 关闭标签
async function closeTab(tabId) {
  // 🔧 Bug4修复：在group模式下，委托给closeTabInGroup处理
  // 找到该标签页所在的group
  if (state.editorGroups && state.editorGroups.length > 0) {
    for (const group of state.editorGroups) {
      if (group.tabs.some(t => t.id === tabId)) {
        closeTabInGroup(group.id, tabId);
        return;
      }
    }
  }
  // 🔧 Task#340：无group结构时不应该到达这里（ensureGroup0保证group存在）
  console.warn('[closeTab] 未找到tab所在的group:', tabId);
}

// 关闭编辑器
function closeEditor() {
  // 切换回终端视图
  // 注意：不清空group tabs，因为调用closeEditor的closeTab已经清空并保存过了
  switchViewTab('output');
}

// 保存编辑器标签页状态到localStorage
