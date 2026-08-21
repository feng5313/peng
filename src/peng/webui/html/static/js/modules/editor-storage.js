// ==================== 编辑器状态持久化管理器 ====================
//
// 集中管理所有编辑器相关的 localStorage 操作。
// 提供统一的序列化/反序列化、版本管理、错误处理、容量控制。
//
// 存储结构（按职责分离为独立的key，避免单个key超出5MB限制）：
//   peng-ed-layout       - 编辑器全局布局（布局模式、当前视图标签、活跃group）
//   peng-ed-groups       - 分拆列信息（每列的宽度、tabs引用ID、currentTabId）
//   peng-ed-tabs         - 所有标签页元数据（path/name/language/editType等，不含content）
//   peng-ed-content      - 小文件标签页内容（key=tabId，大文件不存）
//   peng-ed-cursor       - 编辑器状态（光标位置、滚动位置、选中范围）
//   peng-ed-edits         - 🔧 Task#421：content快照序列（用于重建undo/redo栈）
//   peng-ed-history      - 关闭的标签页历史 + 搜索记录
//   peng-closed-tab-history - 关闭的标签页历史（保留旧key兼容）
//
// 不负责管理的localStorage key（由各自模块独立管理）：
//   peng-theme, peng-font-settings, sidebar-*-width, peng-token,
//   peng-remembered-username, peng_enable_drag, peng-path-history-*,
//   peng-collapsed-*, peng-expanded-*, peng_undo_stack（输入框undo栈）

const EDITOR_STORAGE_VERSION = 1;

// ==================== 工具函数 ====================

/** 安全地从 localStorage 读取并解析 JSON */
function _edStore_get(key, defaultValue) {
  try {
    const raw = localStorage.getItem(key);
    if (raw === null) return defaultValue;
    return JSON.parse(raw);
  } catch (e) {
    console.warn(`[EditorStorage] 读取 ${key} 失败:`, e);
    return defaultValue;
  }
}

/** 安全地写入 JSON 到 localStorage，超出容量时自动降级 */
function _edStore_set(key, value) {
  try {
    localStorage.setItem(key, JSON.stringify(value));
    return true;
  } catch (e) {
    console.warn(`[EditorStorage] 写入 ${key} 失败（可能超出容量）:`, e);
    return false;
  }
}

/** 安全移除 localStorage key */
function _edStore_remove(key) {
  try {
    localStorage.removeItem(key);
  } catch (e) {
    console.warn(`[EditorStorage] 移除 ${key} 失败:`, e);
  }
}

/** 判断文件是否为大文件（基于 config.webui.large_file_threshold） */
function _isLargeFile(content, tab) {
  const threshold = state.webuiConfig.large_file_threshold || (3 * 1024 * 1024);
  if (tab && tab._isLoading) return true;
  if (content && content.length > threshold) return true;
  return false;
}

// ==================== 布局状态保存/恢复 ====================

/** 保存编辑器全局布局信息 */
function saveEditorLayout() {
  const layoutData = {
    v: EDITOR_STORAGE_VERSION,
    layout: state.currentLayout || 'default',
    viewTab: state.currentViewTab || 'output',
    activeGroupId: state._lastActiveGroupId ?? 0,
  };
  _edStore_set('peng-ed-layout', layoutData);
}

/** 读取编辑器全局布局信息 */
function loadEditorLayout() {
  const data = _edStore_get('peng-ed-layout', null);
  if (data && data.v === EDITOR_STORAGE_VERSION) {
    return data;
  }
  // 无有效数据，返回默认值
  return {
    v: EDITOR_STORAGE_VERSION,
    layout: 'default',
    viewTab: 'output',
    activeGroupId: 0,
  };
}

// ==================== 分拆列（Group）保存/恢复 ====================

/**
 * 保存所有编辑器分组的结构信息。
 * 每个group保存：id, tabIds[], currentTabId, width, containerId
 * 标签页详细数据由 saveEditorTabStates / saveEditorTabContent 管理。
 */
function saveEditorGroups() {
  if (!state.editorGroups || state.editorGroups.length === 0) {
    _edStore_remove('peng-ed-groups');
    return;
  }

  // 🔧 Task#412：按 groups 内元素数量统一处理（单/多group都保存）
  // 必须保存 currentTabId，否则刷新后无法恢复正确的激活标签页
  const validGroups = state.editorGroups.filter(g => g.tabs && g.tabs.length > 0);
  if (validGroups.length === 0) {
    _edStore_remove('peng-ed-groups');
    return;
  }

  const groupsData = validGroups.map(group => {
    // 获取group DOM元素的实际宽度
    const groupEl = document.querySelector('[data-group-id="' + group.id + '"]');
    const width = groupEl ? groupEl.offsetWidth : null;
    return {
      id: group.id,
      tabIds: group.tabs.map(t => t.id),
      currentTabId: group.currentTabId,
      containerId: group.containerId,
      width: width,
    };
  });

  _edStore_set('peng-ed-groups', groupsData);
}

/** 读取分拆列结构 */
function loadEditorGroups() {
  return _edStore_get('peng-ed-groups', null);
}

// ==================== 标签页元数据保存/恢复 ====================

/**
 * 保存所有标签页的元数据（不含content，content单独管理）。
 * 包括：id, path, name, language, editType, editId, nest, isNewFile, isVirtual 等。
 */
function saveEditorTabMeta() {
  // 🔧 Task#327修复：收集所有group中的所有tab（包括split副本），不仅仅是group0
  const allTabs = [];
  const seenIds = new Set();
  if (state.editorGroups) {
    for (const group of state.editorGroups) {
      for (const tab of group.tabs) {
        if (!seenIds.has(tab.id)) {
          seenIds.add(tab.id);
          allTabs.push(tab);
        }
      }
    }
  }
  // 🔧 Task#340：数据源统一从groups获取，allTabs已包含所有group的tabs
  // 🔧 Task#360：排除"输出全文.md"，不保存到localStorage（内容是动态的任务输出，恢复后已过期）
  const tabsSource = allTabs.filter(tab => tab.id !== 'tab-output-full-text');

  // 保存标签页元数据（不含content）
  const metaList = tabsSource.map(tab => ({
    id: tab.id,
    path: tab.path,
    name: tab.name,
    language: tab.language,
    isNewFile: tab.isNewFile,
    isVirtual: tab.isVirtual,
    editType: tab.editType,
    rootMsgId: tab.rootMsgId,
    isContentEditor: tab.isContentEditor,
    editId: tab.editId,
    nest: tab.nest || null,
    workdir: tab.workdir || null,  // 🔧 Task#820：持久化打开时的工作目录
    useLocal: tab.useLocal || false,
    isLarge: _isLargeFile(tab.content, tab),  // 标记大文件用于粗体显示
  }));

  _edStore_set('peng-ed-tabs', metaList);
}

/** 读取标签页元数据 */
function loadEditorTabMeta() {
  return _edStore_get('peng-ed-tabs', null);
}

// ==================== 标签页内容保存/恢复 ====================

/**
 * 保存小文件标签页的编辑内容。
 * 🔧 重构：内容恢复走 per-tab 的 loadFromTemp API（/api/editor/auto-save），
 *    peng-ed-content 从未被读取过，直接跳过，不再浪费 localStorage 空间。
 */
function saveEditorTabContents() {
  // 不再写入 localStorage，内容恢复由 loadFromTemp 负责
}

/** 读取保存的标签页内容（已废弃，保留接口兼容） */
function loadEditorTabContents() {
  return {};
}

/** 🔧 Task#421：读取保存的原始内容（用于重建undo/redo栈） */
// ==================== 编辑器状态保存/恢复 ====================

/**
 * 保存每个标签页的编辑器状态：
 * - 光标位置（lineNumber, column）
 * - 滚动位置（scrollTop, scrollLeft）
 * - 选中范围（selectionStart, selectionEnd）或 Monaco Selection
 * - Monaco viewState（光标、滚动、折叠状态等，不含 undo/redo 栈）
 *
 * 🔧 Task#421注：Monaco 0.45.0 的 saveViewState() 不包含 undo/redo 栈。
 * undo/redo 栈通过保存 original/content 重建（见 saveEditorTabContents）。
 */
function saveEditorStates() {
  const statesMap = {};

  if (state.editorGroups) {
    for (const group of state.editorGroups) {
      const editor = group.monacoEditor;
      for (const tab of group.tabs) {
        const tabState = {};

        // 如果当前tab正显示在这个group的编辑器中，保存实时状态
        if (editor && group.currentTabId === tab.id) {
          const pos = editor.getPosition();
          if (pos) {
            tabState.cursorLine = pos.lineNumber;
            tabState.cursorCol = pos.column;
          }
          const sel = editor.getSelection();
          if (sel && !sel.isEmpty()) {
            tabState.selStartLine = sel.startLineNumber;
            tabState.selStartCol = sel.startColumn;
            tabState.selEndLine = sel.endLineNumber;
            tabState.selEndCol = sel.endColumn;
          }
          tabState.scrollTop = editor.getScrollTop();
          tabState.scrollLeft = editor.getScrollLeft();
          // 🔧 Task#421：保存 Monaco viewState（光标、滚动、折叠状态，不含 undo/redo 栈）
          // undo/redo 通过保存 original/content 差异来重建
          try {
            tabState.viewState = editor.saveViewState();
          } catch (e) { /* 忽略 */ }
        } else {
          // 非当前tab：从tab对象上保存的缓存状态读取
          if (tab._savedViewState) {
            tabState.viewState = tab._savedViewState;
          }
          if (tab._savedCursorLine != null) {
            tabState.cursorLine = tab._savedCursorLine;
            tabState.cursorCol = tab._savedCursorCol;
          }
          if (tab._savedScrollTop != null) {
            tabState.scrollTop = tab._savedScrollTop;
            tabState.scrollLeft = tab._savedScrollLeft || 0;
          }
        }

        statesMap[tab.id] = tabState;
      }
    }
  }

  _edStore_set('peng-ed-cursor', statesMap);
}

/** 读取保存的编辑器状态 */
function loadEditorStates() {
  return _edStore_get('peng-ed-cursor', {});
}

// ==================== 搜索记录保存/恢复 ====================

/** 保存搜索记录 */
function saveEditorSearchHistory(searchTerm) {
  if (!searchTerm || !searchTerm.trim()) return;
  const history = _edStore_get('peng-ed-search-history', []);
  // 去重
  const idx = history.indexOf(searchTerm);
  if (idx !== -1) history.splice(idx, 1);
  history.unshift(searchTerm);
  // 最多保存50条
  if (history.length > 50) history.length = 50;
  _edStore_set('peng-ed-search-history', history);
}

/** 读取搜索记录 */
function loadEditorSearchHistory() {
  return _edStore_get('peng-ed-search-history', []);
}

// ==================== 🔧 Task#421：Find/Replace 状态保存/恢复 ====================

/**
 * 保存当前活跃编辑器的 find/replace 搜索词到 localStorage。
 * 从 Monaco findController 的 _state 中读取当前搜索/替换字符串。
 */
function _saveFindReplaceState() {
  try {
    // 找到当前活跃group的编辑器
    const activeGroupId = getActiveEditorGroupId();
    const group = findGroupById(activeGroupId);
    const editor = group && group.monacoEditor ? group.monacoEditor : monacoEditor;
    if (!editor) return;

    const fc = editor.getContribution('editor.contrib.findController');
    if (!fc || !fc._state) return;

    const searchString = fc._state.searchString || '';

    const findState = {
      searchString: searchString,
      replaceString: fc._state.replaceString || '',
      isRegex: !!fc._state.isRegex,
      wholeWord: !!fc._state.wholeWord,
      matchCase: !!fc._state.matchCase,
    };

    // 保存 find state（即使 searchString 为空也保存选项）
    _edStore_set('peng-ed-find-state', findState);
    // 有搜索词时更新搜索历史
    if (searchString) {
      saveEditorSearchHistory(searchString);
    }
  } catch (e) {
    // findController 可能不可用（Monaco未加载等），静默忽略
  }
}

/**
 * 🔧 Task#421：恢复 find/replace 搜索词到当前活跃编辑器。
 * 在编辑器状态恢复完成后调用。
 */
// 🔧 Task#421：待注入的搜索历史（find widget 延迟创建时使用）
window._pendingFindHistory = null;

function _restoreFindReplaceState() {
  try {
    const saved = _edStore_get('peng-ed-find-state', null);
    const searchHistory = loadEditorSearchHistory();

    const activeGroupId = getActiveEditorGroupId();
    const group = findGroupById(activeGroupId);
    const editor = group && group.monacoEditor ? group.monacoEditor : monacoEditor;
    if (!editor) return;

    const fc = editor.getContribution('editor.contrib.findController');
    if (!fc) return;

    // 恢复最后一次搜索词（不自动打开find widget，下次Ctrl+F时会自动填入）
    if (saved && saved.searchString && fc._state) {
      if (typeof fc.setSearchString === 'function') {
        fc.setSearchString(saved.searchString);
      }
      if (saved.isRegex !== undefined) fc._state.isRegex = saved.isRegex;
      if (saved.wholeWord !== undefined) fc._state.wholeWord = saved.wholeWord;
      if (saved.matchCase !== undefined) fc._state.matchCase = saved.matchCase;
      if (saved.replaceString !== undefined) fc._state.replaceString = saved.replaceString;
    }

    // 🔧 Task#421：注入搜索历史到 find widget 的 inputBox
    if (searchHistory.length > 0) {
      if (fc._widget && fc._widget._findInput && fc._widget._findInput.inputBox) {
        // widget 已创建，直接注入
        _injectSearchHistory(fc._widget, searchHistory);
      } else {
        // widget 未创建，保存待注入列表，在首次 Ctrl+F 时注入
        window._pendingFindHistory = searchHistory;
        _hookFindWidgetCreation(editor, fc);
      }
    }
  } catch (e) {
    // 静默忽略
  }
}

/**
 * 🔧 Task#421：注入搜索历史到 find widget。
 * 通过 inputBox.addToHistory() 注入每个历史条目，
 * 用户在 find widget 中按上下箭头就能浏览搜索历史。
 */
function _injectSearchHistory(widget, searchHistory) {
  try {
    if (!widget || !widget._findInput || !widget._findInput.inputBox) return;
    const ib = widget._findInput.inputBox;
    // 从旧到新注入（addToHistory 会将新项放在列表末尾）
    for (let i = searchHistory.length - 1; i >= 0; i--) {
      try {
        ib.input.value = searchHistory[i];
        ib.addToHistory();
      } catch (e) { /* 静默 */ }
    }
  } catch (e) { /* 静默 */ }
}

/**
 * 🔧 Task#421：hook find widget 的首次创建，注入搜索历史。
 * 通过拦截 start() 方法来检测 widget 首次显示。
 */
function _hookFindWidgetCreation(editor, fc) {
  try {
    if (!fc || fc._widget) return;
    // 🔧 不覆盖 fc.start（会导致Ctrl+F失效），改用 editor.addAction 延迟注入
    // 在下次 find action 触发后，通过 setTimeout 注入历史
    // 已在 _hookFindStateChanges 的 onKeyDown 中处理历史注入
  } catch (e) { /* 静默 */ }
}

// ==================== 统一保存/恢复入口 ====================

/**
 * 统一保存所有编辑器状态到 localStorage。
 * 替代原有的 saveEditorTabs() 函数。
 * 在标签页切换、关闭、内容变更等时机调用。
 */
function saveAllEditorState() {
  try {
    saveEditorLayout();
    saveEditorGroups();
    saveEditorTabMeta();
    saveEditorTabContents();
    saveEditorStates();
    // 🔧 Task#421：保存 find/replace 搜索词
    _saveFindReplaceState();
    // 🔧 Task#421：保存编辑快照历史
    saveEditHistory();
  } catch (e) {
    console.warn('[EditorStorage] saveAllEditorState 失败:', e);
  }
}

/**
 * 统一保存（防抖版本）。
 * 避免高频操作（如输入内容变更）导致频繁写入 localStorage。
 */
const saveAllEditorStateDebounced = (function() {
  let timer = null;
  return function() {
    if (timer) clearTimeout(timer);
    timer = setTimeout(() => {
      saveAllEditorState();
      timer = null;
    }, 500);
  };
})();

/**
 * 清除所有编辑器相关的 localStorage 数据。
 */
function clearAllEditorState() {
  _edStore_remove('peng-ed-layout');
  _edStore_remove('peng-ed-groups');
  _edStore_remove('peng-ed-tabs');
  _edStore_remove('peng-ed-cursor');
  _edStore_remove('peng-ed-find-state');  // 🔧 Task#421
  _edStore_remove('peng-ed-search-history');
  // 🔧 迁移到服务端：清除残留的旧localStorage数据
  _edStore_remove('peng-ed-content');
  _edStore_remove('peng-ed-edits');
  // 清除服务端KV数据
  fetch('/api/editor/kv/edits', { method: 'DELETE' }).catch(() => {});
  // 一次性清除所有旧key（开发期不再维护旧key）
  _edStore_remove('peng-editor-tabs');
  _edStore_remove('peng-editor-current-tab');
  _edStore_remove('peng-editor-groups');
  _edStore_remove('peng-active-group-id');
  _edStore_remove('peng-layout');
  _edStore_remove('peng-current-view-tab');
}

// ==================== 辅助函数 ====================

/**
 * 判断标签页是否为大文件标签。
 * 用于UI显示粗体等视觉区分。
 */
function isTabLargeFile(tab) {
  if (!tab) return false;
  // 如果有显式标记
  if (tab._isLoading || tab._needsReload) return true;
  // 基于内容长度判断
  return _isLargeFile(tab.content, tab);
}

/**
 * 在保存编辑器状态前，缓存当前非活跃tab的viewState。
 * 应在tab切换前调用。
 */
function cacheTabViewStates() {
  if (!state.editorGroups) return;
  for (const group of state.editorGroups) {
    const editor = group.monacoEditor;
    if (!editor) continue;
    for (const tab of group.tabs) {
      if (group.currentTabId === tab.id) {
        // 当前活跃tab：从编辑器直接获取
        try {
          tab._savedViewState = editor.saveViewState();
        } catch (e) { /* 忽略 */ }
        const pos = editor.getPosition();
        if (pos) {
          tab._savedCursorLine = pos.lineNumber;
          tab._savedCursorCol = pos.column;
        }
        tab._savedScrollTop = editor.getScrollTop();
        tab._savedScrollLeft = editor.getScrollLeft();
      }
      // 非活跃tab的缓存已在之前切换时保存
    }
  }
}

// ==================== 🔧 Task#421：Find 搜索历史 hook ====================

/**
 * hook Monaco 编辑器的 findController，监听搜索词变化并保存到搜索历史。
 * 在编辑器创建后调用一次。
 * @param {object} editor - Monaco 编辑器实例
 */
function _hookFindStateChanges(editor) {
  try {
    const fc = editor.getContribution('editor.contrib.findController');
    if (!fc || !fc._state) return;

    // 监听 state 的 searchString 变化
    // Monaco 的 FindOptionValue 使用事件机制，我们通过 onDidChange 替代
    // 使用定期检查方式（Monaco的state对象没有暴露直接的事件）
    let _lastSearchString = '';
    let _lastReplaceString = '';

    // 使用 editor 的 onDidFocusEditorWidget 事件来检查搜索词变化
    editor.onDidFocusEditorWidget(() => {
      try {
        if (!fc._state) return;
        const currentSearch = fc._state.searchString || '';
        const currentReplace = fc._state.replaceString || '';
        if (currentSearch && currentSearch !== _lastSearchString) {
          _lastSearchString = currentSearch;
          saveEditorSearchHistory(currentSearch);
        }
        _lastReplaceString = currentReplace;
      } catch (e) { /* 静默忽略 */ }
    });

    // 同时在 Ctrl+F / Ctrl+H 触发时也检查并注入历史
    editor.onKeyDown((e) => {
      if ((e.ctrlKey || e.metaKey) && (e.keyCode === 36 /* KeyF */ || e.keyCode === 33 /* KeyH */)) {
        // 延迟执行（等Monaco处理完快捷键、创建widget后再注入）
        setTimeout(() => {
          try {
            // 🔧 Task#421：注入待处理的搜索历史（find widget 延迟创建场景）
            if (window._pendingFindHistory) {
              // widget 可能还没创建，用重试机制
              const tryInject = (attempts) => {
                if (!window._pendingFindHistory) return;
                if (fc._widget && fc._widget._findInput && fc._widget._findInput.inputBox) {
                  _injectSearchHistory(fc._widget, window._pendingFindHistory);
                  window._pendingFindHistory = null;
                } else if (attempts > 0) {
                  setTimeout(() => tryInject(attempts - 1), 100);
                }
              };
              tryInject(10); // 最多重试10次（1秒内）
            }
            // 检查当前搜索词并更新历史
            if (fc._state && fc._state.searchString) {
              const searchStr = fc._state.searchString;
              if (searchStr !== _lastSearchString) {
                _lastSearchString = searchStr;
                saveEditorSearchHistory(searchStr);
              }
            }
          } catch (ex) { /* 静默 */ }
        }, 200);
      }
    });
  } catch (e) {
    console.warn('[Task#421] hookFindStateChanges 失败:', e);
  }
}

// ==================== 🔧 Task#421：完整 undo/redo 编辑历史追踪 ====================

/**
 * 编辑操作追踪系统。
 *
 * 原理：监听 Monaco model 的 onDidChangeContent 事件，捕获每次编辑的
 * {rangeOffset, rangeLength, text}，每次事件立即创建一组。
 * 恢复时：从 original 内容开始，依次 executeEdits 每组操作 + pushUndoStop，
 * 完整重建多步 undo/redo 栈。
 *
 * 存储结构（peng-ed-edits）：
 *   { tabId1: [[editGroup1], [editGroup2], ...], tabId2: ... }
 *   每个 editGroup = [{offset, len, text}, ...]
 *
 * 🔧 注意：为节省 localStorage 空间，单个 tab 最多保存 200 组编辑操作。
 */

// 全局编辑追踪状态
window._pengEditTrackers = window._pengEditTrackers || {};
const _MAX_EDIT_GROUPS = 200; // 每个 tab 最多保存的编辑组数

/**
 * 🔧 Task#421：直接hook到model的编辑操作追踪。
 * 保存 content 快照序列，恢复时逐步 executeEdits 产生多步 undo。
 * 不依赖 original，不受保存/刷新影响。
 */
function _hookModelEditTracker(model, filePath) {
  if (!model || !filePath) return;
  // 🔧 Task#421：如果已有tracker但model已被dispose（如setModel替换），删除旧tracker重建
  const existing = window._pengEditTrackers[filePath];
  if (existing) {
    if (!existing.model || existing.model.isDisposed()) {
      if (existing.listener) existing.listener.dispose();
      if (existing._timer) clearTimeout(existing._timer);
      delete window._pengEditTrackers[filePath];
    } else {
      return; // tracker仍然有效，跳过
    }
  }

  try {
    const tracker = {
      tabId: null,
      filePath: filePath,
      snapshots: [],  // content 快照序列
      model: model,
      listener: null,
      _timer: null,
    };

    // 记录初始 content 快照
    tracker.snapshots.push(model.getValue());

    tracker.listener = model.onDidChangeContent((e) => {
      if (window._suppressContentChangeTracking) return;
      if (e.isFlush) return;
      // 🔧 Task#421：跳过 undo/redo 操作（不作为新编辑快照）
      if (window._pengUndoRedoing) return;

      // 防抖：100ms 内的连续变更合并为一个快照
      if (tracker._timer) clearTimeout(tracker._timer);
      tracker._timer = setTimeout(() => {
        // 使用 tracker.model 而非闭包中的 model（闭包中的 model 可能被 dispose）
        const currentModel = tracker.model;
        if (!currentModel || currentModel.isDisposed()) return;
        const content = currentModel.getValue();
        // 只在内容真正变化时记录
        if (tracker.snapshots.length === 0 || tracker.snapshots[tracker.snapshots.length - 1] !== content) {
          tracker.snapshots.push(content);
          // 限制快照数量
          if (tracker.snapshots.length > _MAX_EDIT_GROUPS + 1) {
            tracker.snapshots.shift();
          }
        }
      }, 100);
    });

    window._pengEditTrackers[filePath] = tracker;
  } catch (e) {
    console.warn('[Task#421] _hookModelEditTracker failed:', e);
  }
}

/** 🔧 Task#421：hook 编辑器模型的编辑操作追踪（通过editor+tab） */
function hookEditTracker(editor, tab) {
  if (!editor || !tab || !tab.path) return;
  // 委托给 _hookModelEditTracker（如果已存在会自动跳过）
  const model = editor.getModel();
  if (model) _hookModelEditTracker(model, tab.path);
  // 更新 tabId（延迟设置）
  const tracker = window._pengEditTrackers[tab.path];
  if (tracker && !tracker.tabId) tracker.tabId = tab.id;
  // 🔧 Task#421：拦截undo/redo，设置全局标志防止tracker捕获
  _interceptUndoRedo(editor);
}

/**
 * 🔧 Task#421：拦截editor的undo/redo操作，设置全局标志。
 * 拦截 trigger('source','undo') 和 trigger('source','redo') 调用。
 * 在undo/redo期间 _pengUndoRedoing=true，tracker和auto-hook都跳过。
 */
const _interceptedEditors = new WeakSet();
function _interceptUndoRedo(editor) {
  if (!editor || _interceptedEditors.has(editor)) return;
  _interceptedEditors.add(editor);
  
  const origTrigger = editor.trigger.bind(editor);
  editor.trigger = function(source, handlerId, payload) {
    const id = typeof handlerId === 'string' ? handlerId : (handlerId ? handlerId.id : '');
    if (id === 'undo' || id === 'redo') {
      window._pengUndoRedoing = true;
      try { return origTrigger(source, handlerId, payload); }
      finally { window._pengUndoRedoing = false; }
    }
    return origTrigger(source, handlerId, payload);
  };
}

/**
 * 🔧 Task#421：刷新追踪器（不再需要，保持接口兼容）。
 */
/**
 * 🔧 Task#421：保存编辑快照历史到服务端（KV存储，不占localStorage）。
 * 只更新有有效快照（>=2）的tab。
 */
function saveEditHistory() {
  if (!window._pengEditTrackers) return;

  const snapshotsMap = {};
  const allTabs = getAllEditorTabs();

  for (const key in window._pengEditTrackers) {
    const tracker = window._pengEditTrackers[key];
    const snapshots = tracker.snapshots;

    let tabId = tracker.tabId;
    if (!tabId && tracker.filePath) {
      const found = allTabs.find(t => t.path === tracker.filePath);
      if (found) { tabId = found.id; tracker.tabId = tabId; }
    }
    if (!tabId) continue;

    if (snapshots && snapshots.length >= 2) {
      snapshotsMap[tabId] = snapshots;
    }
  }

  const keys = Object.keys(snapshotsMap);
  if (keys.length > 0) {
    // 🔧 迁移到服务端KV存储，不再写入localStorage
    fetch('/api/editor/kv/edits', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(snapshotsMap),
    }).catch(e => console.warn('[saveEditHistory] 服务端保存失败:', e));
  }
}

/** 🔧 Task#421：从服务端加载保存的编辑操作历史 */
async function loadEditHistory() {
  try {
    const resp = await fetch('/api/editor/kv/edits');
    const data = await resp.json();
    if (data.success && data.data) return data.data;
  } catch (e) {
    console.warn('[loadEditHistory] 服务端加载失败:', e);
  }
  return {};
}

/**
 * 🔧 Task#421：按文件路径清除编辑追踪器。
 * 在文件保存/重新加载时调用（original变了，offset基准变了）。
 */
function clearEditTrackerByPath(filePath) {
  if (!filePath || !window._pengEditTrackers) return;
  const tracker = window._pengEditTrackers[filePath];
  if (tracker) {
    if (tracker.listener) tracker.listener.dispose();
    delete window._pengEditTrackers[filePath];
  }
  // 服务端KV存储的编辑历史会在下次saveEditHistory时自动覆盖更新
}

/**
 * 🔧 Task#421：清除指定 tab 的编辑追踪器。
 * 在 tab 关闭时调用。
 */
function clearEditTracker(tabId) {
  if (!window._pengEditTrackers) return;
  for (const key in window._pengEditTrackers) {
    if (window._pengEditTrackers[key].tabId === tabId) {
      const tracker = window._pengEditTrackers[key];
      if (tracker.listener) tracker.listener.dispose();
      delete window._pengEditTrackers[key];
      break;
    }
  }
}
