// ==================== 编辑器标签页关闭历史记录 ====================

const CLOSED_TAB_HISTORY_KEY = 'peng-closed-tab-history';
const MAX_CLOSED_TAB_HISTORY = 50;
const DELETED_FILE_HISTORY_KEY = 'peng-deleted-file-history';
const MAX_DELETED_FILE_HISTORY = 50;

// ==================== localStorage 安全写入 ====================

/**
 * 安全写入 localStorage：quota满时自动清理占用大的key再重试。
 * 优先清理已知的调试/缓存key（不影响用户数据），然后尝试移除unsavedContent。
 * 🔧 修复：原saveClosedTabHistory的catch块在quota满时会逐步pop直到空数组，
 *   导致最近文件历史总是被清空。此函数确保至少保留当前条目。
 */
function _safeSetItem(key, value) {
  try {
    localStorage.setItem(key, value);
    return true;
  } catch(e) {
    // 第1步：清理已知的调试/缓存key（不影响用户数据）
    const disposableKeys = [
      '__peng_layout_calls',     // 调试布局调用栈（可丢弃）
      '__peng_self_test_result', // 自测试结果（可丢弃）
    ];
    for (const dk of disposableKeys) {
      const val = localStorage.getItem(dk);
      if (val && val.length > 100) {
        localStorage.removeItem(dk);
        try {
          localStorage.setItem(key, value);
          console.warn('[_safeSetItem] quota满，已清理 ' + dk + ' 后重试成功');
          return true;
        } catch(e2) { /* 继续清理下一个 */ }
      }
    }

    // 第2步：如果值含unsavedContent，移除后重试（适用于关闭历史）
    if (value.includes('"unsavedContent"')) {
      const trimmed = JSON.parse(value);
      let changed = false;
      for (const entry of trimmed) {
        if (entry.unsavedContent) { delete entry.unsavedContent; changed = true; }
      }
      if (changed) {
        try {
          localStorage.setItem(key, JSON.stringify(trimmed));
          return true;
        } catch(e3) { /* 继续下一步 */ }
      }
    }

    // 第3步：如果仍然失败，清理peng-ed-content（文件内容缓存，可从服务器重新加载）
    const contentCache = localStorage.getItem('peng-ed-content');
    if (contentCache && contentCache.length > 500) {
      localStorage.removeItem('peng-ed-content');
      try {
        localStorage.setItem(key, value);
        console.warn('[_safeSetItem] quota满，已清理 peng-ed-content 后重试成功');
        return true;
      } catch(e4) { /* 继续下一步 */ }
    }

    // 第4步：清理peng-ed-cursor（编辑器状态缓存，可丢弃）
    const cursorCache = localStorage.getItem('peng-ed-cursor');
    if (cursorCache && cursorCache.length > 500) {
      localStorage.removeItem('peng-ed-cursor');
      try {
        localStorage.setItem(key, value);
        console.warn('[_safeSetItem] quota满，已清理 peng-ed-cursor 后重试成功');
        return true;
      } catch(e5) { /* 最终失败 */ }
    }

    console.error('[_safeSetItem] 所有清理策略均失败，无法写入 ' + key);
    return false;
  }
}

// ==================== 删除文件记录管理 ====================

/** 记录已删除的文件/目录（供"最近打开的文件"列表恢复使用） */
function _recordDeletedFile(info) {
  try {
    const history = JSON.parse(localStorage.getItem(DELETED_FILE_HISTORY_KEY) || '[]');
    // 去重：同nest同路径的旧记录删除
    const deduped = history.filter(h => !(h.originalPath === info.originalPath && h.nest === info.nest));
    deduped.unshift(info);
    while (deduped.length > MAX_DELETED_FILE_HISTORY) deduped.pop();
    localStorage.setItem(DELETED_FILE_HISTORY_KEY, JSON.stringify(deduped));
  } catch(e) {
    console.warn('记录删除文件失败:', e);
  }
}

/** 获取已删除文件历史列表 */
function _getDeletedFileHistory() {
  try {
    const rawStr = localStorage.getItem(DELETED_FILE_HISTORY_KEY);
    const raw = rawStr ? JSON.parse(rawStr) : [];
    return Array.isArray(raw) ? raw.filter(h => h && h.originalPath) : [];
  } catch(e) {
    return [];
  }
}

/** 从已删除文件历史中移除指定项 */
function _removeDeletedFileHistory(index) {
  try {
    const history = _getDeletedFileHistory();
    if (index >= 0 && index < history.length) {
      history.splice(index, 1);
      localStorage.setItem(DELETED_FILE_HISTORY_KEY, JSON.stringify(history));
    }
  } catch(e) {
    console.warn('移除删除文件记录失败:', e);
  }
}

/** 静默恢复已删除的文件/目录 */
async function _restoreDeletedFile(entry, nestInfo) {
  const nest = entry.nest || 'local';
  const nestParam = nest !== 'local' ? `?nest=${encodeURIComponent(nest)}` : '';

  const response = await fetch(`/api/files/restore${nestParam}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      backup_path: entry.backupPath,
      original_path: entry.originalPath || entry.path,
    }),
  });

  if (!response.ok) {
    const errData = await response.json().catch(() => ({}));
    throw new Error(errData.detail || '恢复失败');
  }

  return await response.json();
}

/** 判断tab是否是文件（排除消息/分支消息/事务/xterm输出/未保存新文档） */
function isFileTab(tab) {
  // 排除消息/分支消息编辑器
  if (tab.editType === 'message' || tab.editType === 'branch-message' || tab.editType === 'task') return false;
  if (tab.isContentEditor) return false;
  // 排除xterm输出内容
  if (tab.name && (tab.name.includes('xterm') || tab.name.includes('终端'))) return false;
  // 必须有路径
  if (!tab.path) return false;
  // 🔧 修复：未实际保存且无内容的新文档，不应出现在最近文件列表
  if (tab.isNewFile && (!tab.content || tab.content.trim() === '')) return false;
  return true;
}

/** 保存关闭的标签页到历史记录（只保存文件类型） */
function saveClosedTabHistory(tab) {
  if (!tab || !isFileTab(tab)) return;

  const history = JSON.parse(localStorage.getItem(CLOSED_TAB_HISTORY_KEY) || '[]');
  
  // 去重：同nest同路径的旧记录删除
  const newPath = tab.path || '';
  // 🔧 修复：tab.nest 为 null/undefined 时说明文件是本地文件，不应回退到 state.remoteNest
  // （state.remoteNest 反映的是当前 UI 上下文，而非文件实际的 nest）
  const newNest = tab.nest || 'local';
  const deduped = history.filter(h => !(h.path === newPath && h.nest === newNest));
  
  // 🔧 修复：保存未存盘的编辑内容（如果有修改）
  const hasUnsavedEdits = tab.content !== undefined && tab.original !== undefined && tab.content !== tab.original;
  const historyEntry = {
    nest: newNest,
    path: newPath,
    name: tab.name || '',
    closedAt: Date.now(),
    workdir: tab.workdir || null,  // 🔧 Task#820：记录打开时的工作目录
  };
  if (hasUnsavedEdits && tab.content) {
    historyEntry.unsavedContent = tab.content;
  }
  
  deduped.unshift(historyEntry);
  
  // 限制历史记录数量
  while (deduped.length > MAX_CLOSED_TAB_HISTORY) {
    deduped.pop();
  }
  
  _safeSetItem(CLOSED_TAB_HISTORY_KEY, JSON.stringify(deduped));
  // 🔧 Task#184：更新恢复按钮的灰色状态
  updateRestoreBtnState();
}

/** 获取关闭的标签页历史列表 */
function _getClosedTabFileHistory() {
  try {
    const rawStr = localStorage.getItem(CLOSED_TAB_HISTORY_KEY);
    const raw = rawStr ? JSON.parse(rawStr) : [];
    // 🔧 Task#184：过滤掉null/undefined等无效项
    return Array.isArray(raw) ? raw.filter(h => h && h.path) : [];
  } catch(e) {
    return [];
  }
}

/** 🔧 Task#609：异步检查并清理历史中已删除的文件，返回清理后的有效列表 */
async function _cleanupDeletedFilesFromHistory() {
  const history = _getClosedTabFileHistory();
  if (history.length === 0) return history;

  // 并发检查所有文件是否存在
  const checkPromises = history.map(async (item) => {
    try {
      const nest = item.nest || 'local';
      let url = `/api/files/exists?path=${encodeURIComponent(item.path)}`;
      if (nest !== 'local') {
        url += `&nest=${encodeURIComponent(nest)}`;
      }
      const resp = await fetch(url);
      if (!resp.ok) return { item, exists: false };
      const data = await resp.json();
      return { item, exists: !!(data.success && data.exists) };
    } catch (e) {
      // 网络错误等，保守保留
      return { item, exists: true };
    }
  });

  const results = await Promise.all(checkPromises);
  const validItems = results.filter(r => r.exists).map(r => r.item);

  // 如果有删除项，更新localStorage
  if (validItems.length < history.length) {
    localStorage.setItem(CLOSED_TAB_HISTORY_KEY, JSON.stringify(validItems));
    updateRestoreBtnState();
  }

  return validItems;
}

/** 从历史记录中移除指定项 */
function removeClosedTabHistory(index) {
  const history = _getClosedTabFileHistory();
  if (index >= 0 && index < history.length) {
    history.splice(index, 1);
    localStorage.setItem(CLOSED_TAB_HISTORY_KEY, JSON.stringify(history));
  }
  // 🔧 Task#184：更新恢复按钮的灰色状态
  updateRestoreBtnState();
}

/** 恢复关闭的标签页（在当前活跃分拆列打开文件）
 *  🔧 Bug修复：接受item对象或index。当传入index时，从_raw_history取（兼容旧调用）。
 *  推荐传item对象，避免显示列表和原始历史列表排序不一致导致打开错误文件。
 */
async function restoreClosedTab(indexOrItem) {
  // 如果传入的是对象，直接使用；否则从历史记录取
  let item;
  if (typeof indexOrItem === 'object' && indexOrItem !== null) {
    item = indexOrItem;
    // 从localStorage历史中按path+nest匹配并移除
    const history = _getClosedTabFileHistory();
    const histIdx = history.findIndex(h => h.path === item.path && (h.nest || 'local') === (item.nest || 'local'));
    if (histIdx >= 0) {
      removeClosedTabHistory(histIdx);
    }
  } else {
    // 兼容：通过index从原始历史取（不推荐，仅用于未被showClosedTabHistoryPopup覆盖的场景）
    const history = _getClosedTabFileHistory();
    const index = indexOrItem;
    if (index < 0 || index >= history.length) return;
    item = history[index];
    removeClosedTabHistory(index);
  }

  if (!item.path) return;

  // 设置nest
  const nest = item.nest || 'local';
  if (nest !== 'local') {
    _prepareNestState(nest);
    _syncNestDropdown(nest);
  }

  // 🔧 修复：恢复未存盘的编辑内容
  const unsavedContent = item.unsavedContent || null;

  // 🔧 Task#184修复+Task#255统一：先尝试在当前活跃分拆列打开文件
  // openFileInGroup失败（group结构不存在）时，openEditorForFile内部会自动确保group结构
  const groupId = getActiveEditorGroupId();
  const ok = await openFileInGroup(groupId, item.path, nest === 'local', nest);
  if (!ok) {
    await openEditorForFile(item.path, nest === 'local', nest);
  }

  // 🔧 Task#820：恢复工作目录到标签页信息
  if (item.workdir) {
    setTimeout(() => {
      if (state.editorGroups) {
        for (const group of state.editorGroups) {
          const found = group.tabs.find(t => t.path === item.path);
          if (found) {
            found.workdir = item.workdir;
            // 如果当前活跃标签是刚恢复的，同步工作目录输入框
            if (group.currentTabId === found.id) {
              const workdirInput = document.getElementById('workdir-input');
              if (workdirInput) workdirInput.value = toNativeDisplayPath(item.workdir);
              if (typeof validateAndSetWorkdir === 'function') validateAndSetWorkdir(item.workdir);
            }
            break;
          }
        }
      }
    }, 200);
  }

  // 🔧 修复：如果有未存盘的编辑内容，恢复到标签页
  if (unsavedContent) {
    setTimeout(() => {
      // 查找包含该路径的标签页（在所有分组和全局tabs中）
      let targetTab = null;
      let targetEditor = null;
      if (state.editorGroups) {
        for (const group of state.editorGroups) {
          const found = group.tabs.find(t => t.path === item.path);
          if (found) {
            targetTab = found;
            if (group.monacoEditor) targetEditor = group.monacoEditor;
            break;
          }
        }
      }
      if (!targetTab) {
        targetTab = findTabById(
          [...getAllEditorTabs()].find(t => t.path === item.path)?.id
        );
      }
      if (targetTab) {
        targetTab.content = unsavedContent;
        targetTab.original = targetTab.original || '';
        // 更新编辑器内容
        if (!targetEditor) targetEditor = monacoEditor;
        if (targetEditor && getActiveEditorTabId() === targetTab.id) {  // 🔧 Task#334
          targetEditor.setValue(unsavedContent);
        }
      }
    }, 200);
  }
}

// 历史列表弹窗状态
let _fileHistoryPopup = null;
let _fileHistorySelectedIndex = -1;
let _fileHistoryItems = [];
// 🔧 Task#809：外部点击关闭处理器引用（用于正确移除监听器）
let _fileHistoryOutsideClick = null;

/** 关闭文件历史弹窗 */
function closeFileHistoryPopup() {
  if (_fileHistoryPopup) {
    _fileHistoryPopup.remove();
    _fileHistoryPopup = null;
  }
  _fileHistorySelectedIndex = -1;
  _fileHistoryItems = [];
  // 🔧 Task#809修复：removeEventListener 的 capture 参数必须与 addEventListener 一致
  document.removeEventListener('keydown', _fileHistoryKeyHandler, true);
  // 🔧 Task#809修复：清理外部点击监听器，防止泄漏累积
  if (_fileHistoryOutsideClick) {
    document.removeEventListener('mousedown', _fileHistoryOutsideClick, true);
    _fileHistoryOutsideClick = null;
  }
}

/** 文件历史列表键盘事件处理 */
function _fileHistoryKeyHandler(e) {
  if (!_fileHistoryPopup || !_fileHistoryPopup.parentNode) {
    closeFileHistoryPopup();
    return;
  }
  
  if (e.key === 'Escape') {
    e.preventDefault();
    e.stopPropagation();
    closeFileHistoryPopup();
    return;
  }
  
  if (e.key === 'ArrowDown') {
    e.preventDefault();
    _fileHistorySelectedIndex = Math.min(_fileHistorySelectedIndex + 1, _fileHistoryItems.length - 1);
    _updateFileHistorySelection();
    return;
  }
  
  if (e.key === 'ArrowUp') {
    e.preventDefault();
    _fileHistorySelectedIndex = Math.max(_fileHistorySelectedIndex - 1, 0);
    _updateFileHistorySelection();
    return;
  }
  
  if (e.key === 'Delete') {
    e.preventDefault();
    if (_fileHistorySelectedIndex >= 0 && _fileHistorySelectedIndex < _fileHistoryItems.length) {
      removeClosedTabHistory(_fileHistorySelectedIndex);
      _fileHistoryItems.splice(_fileHistorySelectedIndex, 1);
      if (_fileHistorySelectedIndex >= _fileHistoryItems.length) {
        _fileHistorySelectedIndex = Math.max(0, _fileHistoryItems.length - 1);
      }
      _renderFileHistoryList();
    }
    return;
  }
  
  if (e.key === 'Enter' || e.key === 'Tab') {
    e.preventDefault();
    if (_fileHistorySelectedIndex >= 0 && _fileHistorySelectedIndex < _fileHistoryItems.length) {
      // 🔧 Bug修复：传递实际item对象而非index，避免显示列表和原始历史排序不一致
      const selectedItem = _fileHistoryItems[_fileHistorySelectedIndex];
      closeFileHistoryPopup();
      restoreClosedTab(selectedItem);
    }
    return;
  }
}

/** 更新历史列表选中状态 */
function _updateFileHistorySelection() {
  if (!_fileHistoryPopup) return;
  const items = _fileHistoryPopup.querySelectorAll('.history-item');
  items.forEach((el, i) => {
    el.classList.toggle('selected', i === _fileHistorySelectedIndex);
  });
  // 滚动到可见
  const selected = _fileHistoryPopup.querySelector('.history-item.selected');
  if (selected) selected.scrollIntoView({ block: 'nearest' });
}

/** 渲染文件历史列表内容 */
function _renderFileHistoryList() {
  if (!_fileHistoryPopup) return;
  const listEl = _fileHistoryPopup.querySelector('.file-history-list-content');
  if (!listEl) return;
  
  if (_fileHistoryItems.length === 0) {
    listEl.innerHTML = '<div class="history-empty">暂无文件历史记录</div>';
    return;
  }
  
  const fragment = document.createDocumentFragment();
  const nests = window._latestNests || {};

  _fileHistoryItems.forEach(function(item, idx) {
    const isSelected = idx === _fileHistorySelectedIndex;
    const div = document.createElement('div');
    div.className = 'history-item' + (isSelected ? ' selected' : '');
    div.dataset.index = idx;
    div.style.cssText = 'display:flex;align-items:center;white-space:nowrap;';

    // 🔧 Task#214：格式 <nest图标><nest名字><文件类型图标><文件路径>
    const isLocal = item.nest === 'local';
    let iconHTML;
    let nestName;
    if (isLocal) {
      iconHTML = '🪹';
      nestName = 'local';
    } else {
      const nestInfo = nests[item.nest];
      const platform = nestInfo?.platform || '';
      const linuxId = nestInfo?.linux_id || '';
      iconHTML = getPlatformIconHTML(platform, linuxId) || '💻';
      nestName = item.nest;
    }

    const pathDisplay = item.path || '未知';
    var fileIcon = (typeof getFileIcon === 'function') ? getFileIcon(item.name || item.path || '') : '📄';
    var isDeleted = !!item.isDeleted;
    var pathStyle = 'flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:1rem;';
    pathStyle += isDeleted ? 'color:var(--text-dim);text-decoration:line-through;' : 'color:var(--text-primary);';

    div.innerHTML =
      '<span class="history-nest-label" style="display:inline-flex;align-items:center;flex-shrink:0;margin-right:4px;overflow:hidden;">' +
        '<span style="flex-shrink:0;margin-right:4px;">' + iconHTML + '</span>' +
        '<span style="flex:1;min-width:0;font-size:11px;color:var(--text-dim);white-space:nowrap;" title="' + (item.nest || '') + '">' + nestName + '</span>' +
      '</span>' +
      '<span style="flex-shrink:0;margin-right:4px;">' + fileIcon + '</span>' +
      '<span class="history-file-path" style="' + pathStyle + '" title="' + (item.path || '') + '">' + pathDisplay + '</span>' +
      '<span style="font-size:10px;color:var(--text-dim);flex-shrink:0;margin-left:8px;">' + _formatTimeAgo(item.closedAt) + '</span>' +
      (isDeleted ? '<span class="history-restore-btn" data-idx="' + idx + '" title="恢复" style="flex-shrink:0;margin-left:8px;cursor:pointer;font-size:1rem;">♻️</span>' : '');

    if (isDeleted) {
      // 已删除文件：点击恢复图标恢复，点击行不执行
      var restoreBtn = div.querySelector('.history-restore-btn');
      if (restoreBtn) {
        restoreBtn.addEventListener('click', function(ev) {
          ev.preventDefault();
          ev.stopPropagation();
          var i = parseInt(this.dataset.idx);
          _handleRestoreFromHistory(i);
        });
      }
      div.addEventListener('click', function(ev) {
        ev.preventDefault();
        ev.stopPropagation();
        // 已删除文件点击行也尝试恢复
        var i = parseInt(div.dataset.index);
        _handleRestoreFromHistory(i);
      });
    } else {
      // 🔧 Bug修复：传递实际item对象而非index，避免显示列表和原始历史排序不一致
      var clickItem = item;
      div.addEventListener('click', function(ev) {
        ev.preventDefault();
        ev.stopPropagation();
        closeFileHistoryPopup();
        restoreClosedTab(clickItem);
      });
    }

    fragment.appendChild(div);
  });
  
  listEl.innerHTML = '';
  listEl.appendChild(fragment);

  // 🔧 Task#214：自动计算nest标签区域最大宽度，统一设置使列对齐
  const nestLabels = listEl.querySelectorAll('.history-nest-label');
  if (nestLabels.length > 0) {
    let maxWidth = 0;
    nestLabels.forEach(function(el) {
      const w = el.scrollWidth;
      if (w > maxWidth) maxWidth = w;
    });
    nestLabels.forEach(function(el) {
      el.style.width = maxWidth + 'px';
      el.querySelector('span:last-child').style.overflow = 'hidden';
      el.querySelector('span:last-child').style.textOverflow = 'ellipsis';
    });
  }

  // 🔧 Task#351：根据最长项目自动调整弹窗宽度，不超过页面右边界
  const items = listEl.querySelectorAll('.history-item');
  if (items.length > 0 && _fileHistoryPopup) {
    let maxItemWidth = 0;
    items.forEach(function(el) { if (el.scrollWidth > maxItemWidth) maxItemWidth = el.scrollWidth; });
    // 计算弹窗实际左侧位置，确保右侧不超出页面
    const popupRect = _fileHistoryPopup.getBoundingClientRect();
    const availableWidth = window.innerWidth - popupRect.left - 8; // 右边留8px间距
    const targetWidth = Math.min(maxItemWidth + 28, availableWidth);
    _fileHistoryPopup.style.width = Math.max(300, targetWidth) + 'px';
  }
}

/** 静默恢复已删除的文件/目录，然后刷新列表 */
async function _handleRestoreFromHistory(index) {
  if (index < 0 || index >= _fileHistoryItems.length) return;
  var item = _fileHistoryItems[index];
  if (!item.isDeleted) return;

  try {
    await _restoreDeletedFile(item);
    // 从已删除历史中移除
    var deletedHistory = _getDeletedFileHistory();
    var delIdx = deletedHistory.findIndex(function(df) {
      return df.originalPath === item.path && (df.nest || 'local') === (item.nest || 'local');
    });
    if (delIdx >= 0) _removeDeletedFileHistory(delIdx);

    // 从当前列表中移除该项
    _fileHistoryItems.splice(index, 1);
    if (_fileHistorySelectedIndex >= _fileHistoryItems.length) {
      _fileHistorySelectedIndex = Math.max(0, _fileHistoryItems.length - 1);
    }
    _renderFileHistoryList();
    showSimpleToast('已恢复: ' + item.path, 'success');
  } catch(error) {
    console.error('恢复文件失败:', error);
    showSimpleToast('恢复失败: ' + error.message, 'error');
  }
}

/** 显示最近打开的文件历史列表（参照输入框历史列表样式） */
async function showClosedTabHistoryPopup() {
  // 先关闭已有的
  closeFileHistoryPopup();
  
  // 🔧 Task#609：清理已删除的文件，只保留仍然存在的文件
  _fileHistoryItems = await _cleanupDeletedFilesFromHistory();
  
  // 加载已删除文件历史记录
  const deletedFiles = _getDeletedFileHistory();
  // 将已删除文件合并到列表中（标记 isDeleted）
  _fileHistoryItems = _fileHistoryItems.concat(
    deletedFiles.map(function(df) {
      return {
        nest: df.nest || 'local',
        path: df.originalPath,
        name: df.name || df.originalPath.split('/').pop() || df.originalPath.split('\\').pop() || '',
        closedAt: df.deletedAt || Date.now(),
        isDeleted: true,
        backupPath: df.backupPath,
        deletedType: df.type,
      };
    })
  );
  
  // 统一按时间倒序排列（最新的在前）
  _fileHistoryItems.sort(function(a, b) {
    return (b.closedAt || 0) - (a.closedAt || 0);
  });
  
  _fileHistorySelectedIndex = _fileHistoryItems.length > 0 ? 0 : -1;
  
  // 创建弹窗（参照 input-history-list 样式）
  const popup = document.createElement('div');
  popup.id = 'closed-tab-history-popup';
  popup.className = 'input-history-list';
  popup.style.cssText = 'position:fixed;bottom:auto;top:50%;left:50%;transform:translate(-50%,-50%);' +
    'min-width:300px;max-width:calc(100vw - 16px);max-height:60vh;display:flex;flex-direction:column;' +
    'z-index:10000;padding:0;';
  
  // 🔧 修复：尝试定位到可见按钮下方，找不到则保持居中
  const btns = [
    document.getElementById('editor-restore-tab-btn'),
    document.getElementById('editor-restore-tab-btn-default')
  ];
  for (const btn of btns) {
    if (!btn) continue;
    const rect = btn.getBoundingClientRect();
    if (rect.width > 0 && rect.height > 0) {
      // 按钮可见，定位到按钮下方
      const popupTop = rect.bottom + 4;
      let popupLeft = rect.left + rect.width / 2 - 200;
      popupLeft = Math.max(8, Math.min(popupLeft, window.innerWidth - 420));
      popup.style.top = popupTop + 'px';
      popup.style.left = popupLeft + 'px';
      popup.style.transform = 'none';
      break;
    }
  }
  
  // 标题
  const header = document.createElement('div');
  header.style.cssText = 'padding:8px 12px;font-weight:bold;font-size:13px;border-bottom:1px solid var(--border-color);' +
    'display:flex;justify-content:space-between;align-items:center;flex-shrink:0;';
  header.innerHTML = '<span>最近打开的文件</span>' +
    '<span style="font-size:11px;color:var(--text-dim);font-weight:normal;">↑↓选择 Del删除 Enter打开 Esc关闭</span>';
  popup.appendChild(header);
  
  // 列表容器
  const listEl = document.createElement('div');
  listEl.className = 'file-history-list-content';
  listEl.style.cssText = 'overflow-y:auto;flex:1;';
  popup.appendChild(listEl);
  
  document.body.appendChild(popup);
  _fileHistoryPopup = popup;
  
  // 渲染列表
  _renderFileHistoryList();
  
  // 绑定键盘事件
  document.addEventListener('keydown', _fileHistoryKeyHandler, true);
  
  // 🔧 Task#809修复：点击外部关闭 — 使用capture阶段确保Monaco等组件的stopPropagation不会阻止关闭
  // 原Bug：冒泡阶段的mousedown会被Monaco编辑器内部stopPropagation拦截，导致点击编辑器区域无法关闭弹窗
  setTimeout(function() {
    _fileHistoryOutsideClick = function(e) {
      if (_fileHistoryPopup && !_fileHistoryPopup.contains(e.target)) {
        closeFileHistoryPopup();
      }
    };
    document.addEventListener('mousedown', _fileHistoryOutsideClick, true);
  }, 50);
}

/** 更新恢复按钮的灰色状态（历史列表为空时灰色） */
function updateRestoreBtnState() {
  const history = _getClosedTabFileHistory();
  const empty = !history || history.length === 0;
  const btns = document.querySelectorAll('.editor-restore-btn');
  btns.forEach(function(btn) {
    if (empty) {
      btn.classList.add('disabled');
    } else {
      btn.classList.remove('disabled');
    }
  });
}

/** 🔧 Task#214：根据文件名获取文件类型图标 */
/** 格式化时间差 */
function _formatTimeAgo(timestamp) {
  if (!timestamp) return '';
  const diff = Date.now() - timestamp;
  if (diff < 60000) return '刚刚';
  if (diff < 3600000) return Math.floor(diff / 60000) + '分钟前';
  if (diff < 86400000) return Math.floor(diff / 3600000) + '小时前';
  return Math.floor(diff / 86400000) + '天前';
}

/** 关闭分组中的标签页 */
async function closeTabInGroup(groupId, tabId) {
  const group = findGroupById(groupId);
  if (!group) return;

  const tabIndex = group.tabs.findIndex(t => t.id === tabId);
  if (tabIndex === -1) return;

  // 🔧 Bug3修复：删除临时文件
  fetch(`/api/editor/auto-save?tabId=${encodeURIComponent(tabId)}`, { method: 'DELETE' }).catch(() => {});

  // 🔧 Bug4修复：从共享model注册表中释放（如果没有其他标签使用）
  const tabToClose = group.tabs[tabIndex];
  if (tabToClose) {
    // 🔧 Task#253修复：大文件异步加载中，标记取消以停止分块读取循环
    if (tabToClose._isLoading) {
      tabToClose._cancelled = true;
    }
    // 保存关闭的标签页到历史记录（在释放和移除前）
    saveClosedTabHistory(tabToClose);
    releaseSharedModel(tabToClose.path);
  }

  group.tabs.splice(tabIndex, 1);

  // 🔧 Task#340：不再需要同步state.editorTabs，group.tabs是唯一数据源

  // 🔧 Task#326 Bug2根因修复：关闭tab后，如果有剩余tab，必须切换编辑器内容
  // 
  // 根因：当关闭的是当前活跃tab时，上面的第454-462行会把currentTabId改为剩余tab的id，
  // 然后这里检查 group.currentTabId === tabId（被关闭的tabId）→ false → 不切换编辑器！
  // 导致标签页显示新文档1但编辑器内容仍是README.md
  //
  // 修复：统一逻辑——先确定新的currentTabId，然后总是切换到新tab
  if (group.tabs.length === 0) {
    // 没有剩余tab：断开Monaco编辑器与共享model的关联
    if (group.monacoEditor) {
      try {
        window._suppressContentChangeTracking = true;
        group.monacoEditor.setModel(null);
      } catch(e) {}
      finally { window._suppressContentChangeTracking = false; }
    }
    // 如果只剩最后一列，根据配置决定是否创建新文件（默认否）
    if (state.editorGroups.length <= 1) {
      const autoCreateEmptyDoc = state.webuiConfig?.editor?.auto_create_empty_doc ?? false;
      if (autoCreateEmptyDoc) {
        await createNewFileInGroup(groupId);
      } else {
        // 不自动创建空文档：刷新标签栏，清除路径显示
        renderGroupTabs(groupId);
        _clearEditorPathDisplay();
        // 自动切换到 xterm 输出界面
        if (state.currentLayout === 'default') {
          switchViewTab('output');
        }
      }
    } else {
      // 有多列时，删除此列
      removeEditorGroup(groupId);
    }
  } else {
    // 还有剩余tab：确定要切换到的tab并切换编辑器内容
    let targetTabId = group.currentTabId;
    // 如果currentTabId仍指向被关闭的tab，选择相邻tab
    if (!targetTabId || !group.tabs.some(t => t.id === targetTabId)) {
      const nextTab = group.tabs[Math.min(tabIndex, group.tabs.length - 1)];
      targetTabId = nextTab.id;
    }
    group.currentTabId = targetTabId;
    setActiveEditorTabId(targetTabId);
    switchTabInGroup(groupId, targetTabId);
    renderGroupTabs(groupId);
  }

  // 🔧 修复：关闭标签后进行一致性检查，确保标签和编辑器一一对应
  validateEditorGroupConsistency();
  // 保存状态到localStorage
  saveEditorTabs();
}

/** 关闭所有分拆列的所有标签页 */
async function closeAllTabsInGroup() {
  if (!state.editorGroups || state.editorGroups.length === 0) return;

  // 逐个group、逐个tab清理：删除临时文件、保存关闭历史、释放共享model
  for (const group of state.editorGroups) {
    for (const tab of group.tabs) {
      fetch(`/api/editor/auto-save?tabId=${encodeURIComponent(tab.id)}`, { method: 'DELETE' }).catch(() => {});
      if (tab._isLoading) {
        tab._cancelled = true;
      }
      saveClosedTabHistory(tab);
      releaseSharedModel(tab.path);
    }
    // 断开Monaco编辑器
    if (group.monacoEditor) {
      try {
        window._suppressContentChangeTracking = true;
        group.monacoEditor.setModel(null);
      } catch(e) {}
      finally { window._suppressContentChangeTracking = false; }
    }
    group.tabs = [];
    group.currentTabId = null;
  }
  setActiveEditorTabId(null);

  // 清理多余的分组（保留一个）
  while (state.editorGroups.length > 1) {
    removeEditorGroup(state.editorGroups[state.editorGroups.length - 1].id);
  }

  // 唯一保留的group处理
  const lastGroup = state.editorGroups[0];
  if (lastGroup) {
    const autoCreateEmptyDoc = state.webuiConfig?.editor?.auto_create_empty_doc ?? false;
    if (autoCreateEmptyDoc) {
      await createNewFileInGroup(lastGroup.id);
    } else {
      renderGroupTabs(lastGroup.id);
      _clearEditorPathDisplay();
      if (state.currentLayout === 'default') {
        switchViewTab('output');
      }
    }
  }

  validateEditorGroupConsistency();
  // 🔧 Task#971：重置c按钮标记，全部关闭后不再显示
  state._closeAllButtonShown = false;
  saveEditorTabs();
}

/** 删除编辑器分组（保留至少一列） */
function removeEditorGroup(groupId) {
  if (state.editorGroups.length <= 1) return;

  const groupIndex = state.editorGroups.findIndex(g => g.id === groupId);
  if (groupIndex === -1) return;

  const group = state.editorGroups[groupIndex];

  // 切换到相邻分组（先计算，splice前）
  const nextGroupIndex = groupIndex < state.editorGroups.length - 1 ? groupIndex + 1 : groupIndex - 1;
  const nextGroup = state.editorGroups[nextGroupIndex];

  // 🔧 修复：先销毁monaco编辑器，避免后续DOM操作影响编辑器（在DOM清理前操作）
  if (group.monacoEditor) {
    try { window._suppressContentChangeTracking = true; group.monacoEditor.setModel(null); } catch(e) {}
    finally { window._suppressContentChangeTracking = false; }
    try { group.monacoEditor.dispose(); } catch(e) {}
    group.monacoEditor = null;
  }

  // 从DOM中移除分拆手柄和group元素
  const groupsContainer = getEditorGroupsContainer();
  if (groupsContainer) {
    // 找到对应的DOM元素并移除（用多种选择器确保找到）
    let groupEl = groupsContainer.querySelector(`[data-group-id="${groupId}"]`);
    if (!groupEl) {
      // 回退：用containerId查找
      groupEl = group.containerId ? document.getElementById(group.containerId)?.closest('.editor-group') : null;
    }
    if (!groupEl) {
      // 回退：遍历所有group元素，通过索引查找（groupIndex对应的DOM位置）
      const allGroupEls = groupsContainer.querySelectorAll('.editor-group');
      // DOM中group元素数量应等于当前state.editorGroups数量（删除前）
      if (allGroupEls.length === state.editorGroups.length && groupIndex < allGroupEls.length) {
        groupEl = allGroupEls[groupIndex];
      }
    }
    if (groupEl) {
      const prevSibling = groupEl.previousElementSibling;
      const nextSibling = groupEl.nextElementSibling;
      
      const prevIsHandle = prevSibling?.classList.contains('editor-split-handle');
      const nextIsHandle = nextSibling?.classList.contains('editor-split-handle');
      
      if (prevIsHandle && nextIsHandle) {
        prevSibling.remove();
      } else if (prevIsHandle) {
        prevSibling.remove();
      } else if (nextIsHandle) {
        nextSibling.remove();
      }
      
      groupEl.remove();
    } else {
      console.warn(`[removeEditorGroup] 未找到groupId=${groupId}的DOM元素，尝试清理所有空group`);
      // 最后的回退：清理所有tabs为空的editor-group DOM元素
      const allGroupEls = groupsContainer.querySelectorAll('.editor-group');
      allGroupEls.forEach(el => {
        const tabContainer = el.querySelector('.editor-tabs');
        if (tabContainer && tabContainer.children.length === 0) {
          // 移除相邻的分拆手柄
          const prev = el.previousElementSibling;
          const next = el.nextElementSibling;
          if (prev?.classList.contains('editor-split-handle')) prev.remove();
          else if (next?.classList.contains('editor-split-handle')) next.remove();
          el.remove();
        }
      });
    }
  }

  // 从数据中移除
  state.editorGroups.splice(groupIndex, 1);

  // 切换到相邻分组
  if (nextGroup) {
    state._lastActiveGroupId = nextGroup.id;
    setActiveEditorTabId(nextGroup.currentTabId);  // 🔧 Task#334
    if (nextGroup.currentTabId) {
      switchTabInGroup(nextGroup.id, nextGroup.currentTabId);
    }
    updateEditorPathDisplay();
  }

  layoutAllMonacoEditors();

  // 🔧 Bug修复：删除分组后，重新分配剩余分组的宽度为 flex:1 均分
  if (groupsContainer) {
    const remainingGroups = groupsContainer.querySelectorAll('.editor-group');
    remainingGroups.forEach(g => {
      g.style.flex = '1';
      g.style.width = '';
    });
  }

  // 🔧 修复：删除分组后保存状态到localStorage，防止刷新后残留旧分组数据
  saveEditorTabs();
}

/** 🔧 一致性检查：确保所有group的标签和编辑器状态一一对应
 *  修复：空tabs但编辑器有内容、DOM中有残留空group等不一致状态
 */
function validateEditorGroupConsistency() {
  if (!state.editorGroups || state.editorGroups.length === 0) return;

  // 检查1：每个group的currentTabId必须指向tabs中的有效标签
  state.editorGroups.forEach(group => {
    if (group.currentTabId && !group.tabs.some(t => t.id === group.currentTabId)) {
      group.currentTabId = group.tabs.length > 0 ? group.tabs[0].id : null;
    }
    // 空tabs的group不应该有编辑器内容
    if (group.tabs.length === 0 && group.monacoEditor) {
      try { group.monacoEditor.setModel(null); } catch(e) {}
    }
  });

  // 检查2：过滤掉空的group（除最后一个外）
  if (state.editorGroups.length > 1) {
    const groupsToRemove = [];
    for (let i = state.editorGroups.length - 1; i >= 0; i--) {
      if (state.editorGroups[i].tabs.length === 0) {
        groupsToRemove.push(i);
      }
    }
    // 至少保留1个group
    if (groupsToRemove.length >= state.editorGroups.length) {
      groupsToRemove.pop(); // 保留最后一个
    }
    // 从后往前移除空的group
    for (const idx of groupsToRemove) {
      if (state.editorGroups.length <= 1) break;
      const group = state.editorGroups[idx];
      // 销毁编辑器
      if (group.monacoEditor) {
        try { group.monacoEditor.setModel(null); } catch(e) {}
        try { group.monacoEditor.dispose(); } catch(e) {}
      }
      // 移除DOM
      const container = getEditorGroupsContainer();
      if (container) {
        const el = container.querySelector(`[data-group-id="${group.id}"]`);
        if (el) {
          const prev = el.previousElementSibling;
          const next = el.nextElementSibling;
          if (prev?.classList.contains('editor-split-handle')) prev.remove();
          else if (next?.classList.contains('editor-split-handle')) next.remove();
          el.remove();
        }
      }
      state.editorGroups.splice(idx, 1);
    }
    // 重新均分宽度
    const container = getEditorGroupsContainer();
    if (container) {
      container.querySelectorAll('.editor-group').forEach(g => {
        g.style.flex = '1';
        g.style.width = '';
      });
    }
  }

  // 检查3：只剩1个空group时，根据配置决定是否创建新文件（默认否）
  if (state.editorGroups.length === 1 && state.editorGroups[0].tabs.length === 0) {
    const autoCreateEmptyDoc = state.webuiConfig?.editor?.auto_create_empty_doc ?? false;
    if (autoCreateEmptyDoc) {
      createNewFileInGroup(state.editorGroups[0].id);
    }
  }

  // 检查4：清理DOM中不属于任何state.editorGroups的残留.editor-group元素
  const container = getEditorGroupsContainer();
  if (container && state.editorGroups.length > 0) {
    const validGroupIds = new Set(state.editorGroups.map(g => String(g.id)));
    container.querySelectorAll('.editor-group').forEach(el => {
      if (!validGroupIds.has(el.dataset.groupId)) {
        console.warn(`[一致性检查] 发现残留的DOM group: ${el.dataset.groupId}，清理中`);
        const prev = el.previousElementSibling;
        const next = el.nextElementSibling;
        if (prev?.classList.contains('editor-split-handle')) prev.remove();
        else if (next?.classList.contains('editor-split-handle')) next.remove();
        el.remove();
      }
    });
  }
}

/** 获取编辑器分组的容器元素 */
function getEditorGroupsContainer() {
  // 🔧 Task#320统一方案：全局唯一的editor-groups-container
  return document.getElementById('editor-groups-container');
}

/** 清空编辑器文件路径显示（关闭所有标签后） */
function _clearEditorPathDisplay() {
  const ids = ['editor-file-path', 'editor-file-path-2'];
  ids.forEach(id => {
    const el = document.getElementById(id);
    if (el) el.innerHTML = '';
  });
}

/** 更新编辑器文件路径显示（根据当前活跃group更新所有路径显示元素） */
function updateEditorPathDisplay(tab) {
  if (!tab) {
    // 如果没有传入tab，尝试从当前活跃group获取
    const activeGroupId = getActiveEditorGroupId();
    const group = findGroupById(activeGroupId);
    if (group && group.currentTabId) {
      tab = group.tabs.find(t => t.id === group.currentTabId);
    }
    if (!tab) {
      tab = findTabById(getActiveEditorTabId());  // 🔧 Task#334
    }
  }
  if (!tab) return;

  const isContentEditor = !!tab.isContentEditor;
  const isVirtual = !!tab.isVirtual;
  const isNewFile = !!tab.isNewFile;

  // 🔧 Task#248：普通文件使用和"最近的文件"列表一致的格式（nest图标+nest名+文件图标+路径）
  if (!isContentEditor && !isVirtual && !isNewFile && tab.path) {
    if (typeof _setEditorFilePathHTML === 'function') {
      _setEditorFilePathHTML(tab.path, tab.nest);
    }
  } else {
    // 非文件类型（消息/虚拟/新文件）使用纯文本
    const displayPath = tab.path || tab.name || '';
    const suffix = isVirtual ? '（只读）' : (isNewFile ? '（未保存）' : '');
    const ids = ['editor-file-path-2', 'editor-file-path'];
    ids.forEach(id => {
      const el = document.getElementById(id);
      if (el) el.textContent = (tab.name || displayPath) + suffix;
    });
  }

  // 更新语言标签（按钮现在嵌入在文件路径中，通过setLanguageBadge更新）
  if (tab.language && tab.language !== 'plaintext') {
    if (typeof setLanguageBadge === 'function') setLanguageBadge(tab.language);
  }
}

/** 更新编辑器光标位置信息 */
function updateEditorCursorInfo() {
  const editor = state.editorGroups?.length > 0
    ? state.editorGroups.find(g => g.currentTabId === getActiveEditorTabId())?.monacoEditor || monacoEditor  // 🔧 Task#334
    : monacoEditor;
  if (!editor) return;

  const position = editor.getPosition();
  if (!position) return;

  const cursorText = `行 ${position.lineNumber}, 列 ${position.column}`;
  const infoSelectors = ['editor-cursor-info-2', 'editor-cursor-info'];
  infoSelectors.forEach(id => {
    const el = document.getElementById(id);
    if (el) el.textContent = cursorText;
  });
}

