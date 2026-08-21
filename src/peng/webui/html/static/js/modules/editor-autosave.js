// ==================== 编辑器自动保存 ====================

// Debounce函数(防抖)
function debounce(func, wait) {
  let timeout;
  return function executedFunction(...args) {
    const later = () => {
      clearTimeout(timeout);
      func(...args);
    };
    clearTimeout(timeout);
    timeout = setTimeout(later, wait);
  };
}

// 自动保存到临时文件(使用debounce,300ms)
const autoSaveToTemp = debounce(async function (tab) {
  if (!tab || !tab.id) return;

  // 避免重复保存
  if (state.isSaving) return;
  state.isSaving = true;

  try {
    const response = await fetch('/api/editor/auto-save', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        tabId: tab.id,
        path: tab.path || null,
        content: tab.content || '',
        editType: tab.editType || null,
        rootMsgId: tab.rootMsgId || null,
        isContentEditor: tab.isContentEditor || null,
        editId: tab.editId || null
      })
    });

    const result = await response.json();
    if (!result.success) {
      console.warn(`⚠️ 自动保存失败: ${tab.name}`);
      showSimpleToast(`⚠️ 自动保存失败: ${tab.name}`, 'warning');
    }
  } catch (error) {
    console.warn(`自动保存到临时文件失败: ${error.message}`);
    showSimpleToast(`⚠️ 自动保存到临时文件失败: ${tab.name}`, 'warning');
  } finally {
    state.isSaving = false;
  }
}, 300);

// 从临时文件加载内容（只返回content字符串）
async function loadFromTemp(tabId) {
  try {
    const response = await fetch(`/api/editor/auto-save?tabId=${encodeURIComponent(tabId)}`);
    const result = await response.json();

    if (result.success && result.content) {
      return result.content;
    }
  } catch (error) {
    console.warn(`从临时文件加载失败: ${error.message}`);
  }
  return null;
}

// 保存编辑器标签页状态到localStorage（包括分拆状态）
// 🔧 Task#327：委托给 editor-storage.js 的统一管理器
function saveEditorTabs() {
  // 先缓存当前非活跃tab的viewState（光标/滚动/选中/undo）
  cacheTabViewStates();
  // 统一保存所有编辑器状态
  saveAllEditorState();
}

// 从localStorage恢复编辑器标签页状态
async function restoreEditorTabs() {
  // 🔧 Bug2修复：防止restoreEditorTabs被并发调用两次（restoreViewTab和initLayoutEditorPanel）
  if (window._restoreEditorTabsRunning) {
    console.log('[restoreEditorTabs] 跳过：已在运行中');
    return;
  }
  window._restoreEditorTabsRunning = true;
  try {
    // 确保group0始终存在
    ensureGroup0();

    const savedTabs = localStorage.getItem('peng-ed-tabs');
    // 🔧 Task#412：统一从 peng-ed-groups 读取 currentTabId（单/多group都保存）
    const savedGroups = loadEditorGroups();
    const savedCurrentTab = (savedGroups && savedGroups.length > 0 && savedGroups[0].currentTabId)
      || null;
    console.log('[restoreEditorTabs] 开始恢复, savedTabs存在:', !!savedTabs, 'savedCurrentTab:', savedCurrentTab, 'layout:', state.currentLayout);

    if (savedTabs) {
      const tabs = JSON.parse(savedTabs);
      // 🔧 Task#360：过滤掉"输出全文.md"，不恢复（内容是动态的任务输出）
      const filteredTabs = tabs.filter(tab => tab.id !== 'tab-output-full-text');

      // 恢复标签页并加载内容
      const restoredTabs = await Promise.all(filteredTabs.map(async (tab) => {
        // 先从临时文件加载内容（只返回content字符串）
        const tempContent = await loadFromTemp(tab.id);

        // 🔧 修复：使用 undefined 表示"未加载"，空字符串表示"文件为空"
        // 这样 switchToTab() 可以正确判断是否需要重新加载
        let finalContent = undefined;

        // 如果有临时文件内容,使用临时文件内容
        // 否则尝试从服务器加载
        // 🔧 修复：空字符串 "" 也是有效内容，不应触发服务器加载
        if (tempContent !== undefined && tempContent !== null) {
          finalContent = tempContent;
        } else if (tab.isContentEditor && tab.editType && tab.editId) {
          // 🔧 Task#360修复：内容编辑器（消息/分支消息/事务）优先从API加载
          // 必须在文件路径分支之前处理，因为内容编辑器的path字段存的是标题而非真实路径
          try {
            let url;
            if (tab.editType === 'message') {
              url = `/api/messages/${tab.editId}`;
            } else if (tab.editType === 'branch-message') {
              url = `/api/messages/${tab.editId}?branch=true`;
            } else if (tab.editType === 'task') {
              url = `/api/tasks/${tab.editId}`;
            }

            if (url) {
              const response = await fetch(url);
              const result = await response.json();
              const content = result.content || '';
              if (content || Object.keys(result).length > 0) {
                finalContent = content;
                showSimpleToast(`✅ 从服务器加载${tab.editType}: ${tab.name}`);
              } else {
                console.warn(`从服务器加载${tab.editType}失败: ${tab.name}, 空结果`);
              }
            }
          } catch (error) {
            console.warn(`从服务器加载${tab.editType}失败: ${tab.name}`, error);
          }
        } else if (tab.path && !tab.isNewFile && !tab.isVirtual) {
          try {
            // 🔧 Task#72：使用标签记住的nest而非全局nest-select
            const reloadNestParam = tab.useLocal ? '' : (tab.nest ? `&nest=${encodeURIComponent(tab.nest)}` : '');

            // 🔧 Task#262：先检查文件大小，大文件走并发分块加载
            const LARGE_FILE_THRESHOLD = state.webuiConfig.large_file_threshold || (3 * 1024 * 1024);
            try {
              const existsResp = await fetch(`/api/files/exists?path=${encodeURIComponent(tab.path)}${reloadNestParam}`);
              const existsData = await existsResp.json();
              if (existsResp.ok && existsData.success && existsData.exists) {
                const fileSize = existsData.size || 0;
                const isImage = existsData.is_image || false;
                if (!isImage && fileSize > LARGE_FILE_THRESHOLD) {
                  // 大文件：用并发分块加载
                  const nestName = reloadNestParam ? reloadNestParam.replace('&nest=', '') : 'local';
                  const sizeStr = (typeof formatSizeHuman === 'function') ? formatSizeHuman(fileSize) : fileSize + ' B';
                  const estimatedSeconds = Math.max(1, Math.round(fileSize / (10 * 1024 * 1024)));
                  const dummyTab = { _cancelled: false, content: '' };
                  finalContent = await _loadFileChunksParallel(
                    tab.path, fileSize, reloadNestParam, nestName, sizeStr,
                    estimatedSeconds, Date.now(), dummyTab, tab.id, 0, { monacoEditor: null }
                  );
                  // 成功加载，跳过下面的普通加载
                  if (finalContent !== undefined) {
                    // content已加载
                  } else {
                    finalContent = undefined;
                  }
                } else {
                  // 非大文件，走普通加载
                  finalContent = undefined; // 继续走下面的普通路径
                }
              }
            } catch (e) {
              // 大小检查失败，走普通路径
              finalContent = undefined;
            }

            // 普通路径加载（非大文件，或大文件检查失败时）
            if (finalContent === undefined) {
              const response = await fetch(`/api/files/read?path=${encodeURIComponent(tab.path)}${reloadNestParam}`);
              const result = await response.json();
              if (response.ok && result.content !== undefined) {
                finalContent = result.content;
              } else {
                console.warn(`从服务器加载文件失败: ${tab.name}, status=${response.status}`);
              }
            }
          } catch (error) {
            console.warn(`从服务器加载文件失败: ${tab.name}`, error);
          }
        }

        return {
          ...tab,
          // 使用localStorage中保存的editType和editId，不从临时文件获取
          editType: tab.editType,
          isContentEditor: tab.isContentEditor,
          editId: tab.editId,
          rootMsgId: tab.rootMsgId,
          // 🔧 Task#327修复：空文档(isNewFile)的content为undefined时，设为空字符串
          // 确保空文档标签页也能正确恢复
          content: tab.isNewFile && finalContent === undefined ? '' : finalContent,
          // 🔧 修复：记录原始内容，用于判断文件是否被编辑，以及文件变化时的自动合并
          original: (tab.isNewFile && finalContent === undefined) ? '' : finalContent,
          cursorPosition: null,
          scrollTop: null
        };
      }));

      // 🔧 Task#340：直接赋给group0.tabs（唯一数据源），不再使用state.editorTabs
      ensureGroup0();
      // 🔧 Task#499：恢复时重新计算tab的language（避免旧缓存中错误的language值）
      for (const rt of restoredTabs) {
        if (rt.path && typeof getMonacoLanguage === 'function') {
          const ext = rt.path.split('.').pop().toLowerCase();
          const correctLang = getMonacoLanguage(ext);
          if (correctLang && correctLang !== 'plaintext' && rt.language !== correctLang) {
            console.log('[restoreEditorTabs] 修正语言: ' + rt.name + ' ' + rt.language + ' -> ' + correctLang);
            rt.language = correctLang;
          }
        }
      }
      state.editorGroups[0].tabs = restoredTabs;

      // 🔧 修复：设置恢复后的活跃标签页
      // savedCurrentTab来自初始化时的group0.currentTabId，可能为null
      // 无论如何都必须从恢复的tabs中确定一个有效的currentTabId
      if (savedCurrentTab) {
        const tabExists = restoredTabs.some(tab => tab.id === savedCurrentTab);
        if (tabExists) {
          setActiveEditorTabId(savedCurrentTab);  // 🔧 Task#334
        } else {
          // 保存的标签不存在，使用第一个标签
          if (restoredTabs.length > 0) {
            setActiveEditorTabId(restoredTabs[0].id);
          }
        }
      } else {
        // 🔧 Task#326修复：savedCurrentTab为null时（初始化group0时currentTabId未设置）
        // 必须从恢复的tabs中取第一个作为活跃标签，否则后续switchToTab被跳过
        // 导致group0没有currentTabId也没有Monaco编辑器
        if (restoredTabs.length > 0) {
          setActiveEditorTabId(restoredTabs[0].id);
        }
      }

      renderTabs();

      // 恢复分拆状态（editorGroups）——从新格式读取
      const savedGroups = loadEditorGroups();
      if (savedGroups && savedGroups.length > 0) {
        try {
          // 🔧 Task#412：统一单/多group恢复逻辑（不再区分 length > 1）
          // 找到id=0的group作为group0的数据源；如果不存在，则用第一个group作为替身
          const g0Real = savedGroups.find(g => g.id === 0);
          const g0Data = g0Real || savedGroups[0];
          const g0SurrogateId = g0Real ? null : savedGroups[0].id;
          if (g0Data && g0Data.tabIds) {
            // group0只包含g0Data.tabIds中列出的tab
            const g0Tabs = [];
            for (const tab of restoredTabs) {
              if (g0Data.tabIds.includes(tab.id)) {
                g0Tabs.push(tab);
              }
            }
            state.editorGroups[0].tabs = g0Tabs;
            setActiveEditorTabId(g0Data.currentTabId || getActiveEditorTabId());
          }
          // 多列分拆结构，需要重建（从group1开始）
          for (let i = 0; i < savedGroups.length; i++) {
            const sg = savedGroups[i];
            if (sg.id === 0) continue;
            if (sg.id === g0SurrogateId) continue;
            await splitEditorGroupRestore(sg, restoredTabs);
          }
          // group0确认currentTabId
          if (state.editorGroups && state.editorGroups.length > 0 && state.editorGroups[0].id === 0) {
            state.editorGroups[0].currentTabId = g0Data.currentTabId || state.editorGroups[0].currentTabId;
          }
          renderTabs();
          // 恢复activeGroupId
          const layoutData = loadEditorLayout();
          state._lastActiveGroupId = layoutData.activeGroupId ?? 0;
          try { localStorage.setItem('__peng_debug_restore1', JSON.stringify({activeGroupId: state._lastActiveGroupId, from: layoutData.activeGroupId, ts: Date.now()})); } catch(e) {}

          // 延迟恢复列宽，等DOM渲染完成
          requestAnimationFrame(() => {
            _restoreSplitWidths(savedGroups);
            layoutAllMonacoEditors();
          });
        } catch (e) {
          console.warn('恢复分拆状态失败:', e);
        }
      }

      // 🔧 修复：恢复标签页后主动切换到当前标签，从而加载编辑器内容
      // 🔧 Task#331修复：多group场景下，switchToTab内部通过getActiveEditorGroupId()
      // 选择目标编辑器。恢复group0时必须临时将_lastActiveGroupId设为0，
      // 否则updateEditorContent会把内容写到group1的monaco上，group0不会被创建。
      const savedActiveGroupId = state._lastActiveGroupId;
      // 🔧 Task#412：统一单/多group切换逻辑
      // 多group时临时切到group0初始化monacoEditor，单group直接switchToTab
      if (state.editorGroups && state.editorGroups.length > 1) {
        state._lastActiveGroupId = 0;
      }
      const activeTabId = getActiveEditorTabId();
      if (activeTabId) {
        await switchToTab(activeTabId);
      }
      // 🔧 Task#327修复：多group场景下，确保每个group都切换到其保存的currentTabId
      // splitEditorGroupRestore只创建了monaco编辑器但未显式切换tab
      if (state.editorGroups && state.editorGroups.length > 1) {
        for (const group of state.editorGroups) {
          if (group.id === 0) continue;  // group0已由switchToTab处理
          if (group.currentTabId && group.monacoEditor) {
            const tab = group.tabs.find(t => t.id === group.currentTabId);
            if (tab) {
              // 设置共享model并恢复内容
              if (tab.path && window.monaco) {
                const sharedModel = getOrCreateSharedModel(tab.path, tab.content, tab.language);
                if (sharedModel) {
                  group.monacoEditor.setModel(sharedModel);
                }
              } else {
                group.monacoEditor.setValue(tab.content || '');
              }
            }
          }
        }
      }
      // 🔧 修复：恢复后确保group0的monacoEditor指向全局monacoEditor
      if (state.editorGroups && state.editorGroups.length > 0 && state.editorGroups[0].id === 0) {
        // 🔧 Task#327：如果monacoEditor为null说明恢复流程有bug，不要静默fallback掩盖问题
        if (!monacoEditor) {
          const g0Tabs = getG0Tabs();
          console.error('[restoreEditorTabs] BUG: monacoEditor为null！group0没有创建monaco编辑器。' +
            ' tabs=' + g0Tabs.length + ' groups=' + state.editorGroups.length +
            ' currentLayout=' + state.currentLayout);
        }
        if (monacoEditor) {
          state.editorGroups[0].monacoEditor = monacoEditor;
        }
        // 🔧 Task#327/340修复：单group时group0.tabs已在上面设置，无需再同步
        // 🔧 Task#327修复：只在group0没有currentTabId时才用全局值补充
        // 避免覆盖前面已正确设置的值
        if (!state.editorGroups[0].currentTabId) {
          state.editorGroups[0].currentTabId = getActiveEditorTabId();  // 🔧 Task#334：从group获取
        }
        // 🔧 Bug8修复：为group0的当前tab设置共享model
        const g0CurrentTabId = state.editorGroups[0].currentTabId;
        if (monacoEditor && g0CurrentTabId) {
          const currentTab = findTabById(g0CurrentTabId);
          if (currentTab && currentTab.path && window.monaco) {
            const sharedModel = getOrCreateSharedModel(currentTab.path, currentTab.content, currentTab.language);
            if (sharedModel) {
              monacoEditor.setModel(sharedModel);
            }
          }
        }
      }
      // 🔧 Bug3修复：非默认布局下恢复group0的标签页渲染
      if (state.currentLayout !== 'default' && state.editorGroups && state.editorGroups.length > 0) {
        state.editorGroups.forEach(g => renderGroupTabs(g.id));
      }

      // 🔧 Task#327：恢复编辑器状态（光标位置、滚动位置、选中文字、undo/redo）
      await _restoreEditorStatesAfterLoad();

      // 🔧 Task#331：恢复完成后，将_lastActiveGroupId恢复为正确保存的值
      // 之前临时设为0是为了让updateEditorContent操作group0
      if (state.editorGroups && state.editorGroups.length > 1) {
        state._lastActiveGroupId = savedActiveGroupId;
      }
    }
  } catch (error) {
    console.warn('恢复编辑器标签页状态失败:', error);
    // 🔧 Task#340：清空group0的tabs
    ensureGroup0();
    state.editorGroups[0].tabs = [];
    setActiveEditorTabId(null);  // 🔧 Task#334
  } finally {
    window._restoreEditorTabsRunning = false;

    // 🔧 Task#292修复：移除此处的空文档创建逻辑
    // 非默认布局由 initLayoutEditorPanel() 中的 hasAnyTabs 检查负责创建
    // 默认布局由 switchViewTab() 中的 group0.tabs.length === 0 检查负责创建
    // 此处不再重复创建，避免创建2个空文档

    // 🔧 恢复后进行一致性检查，确保标签和编辑器一一对应
    validateEditorGroupConsistency();
  }
}

// 清空编辑器标签页状态
// 🔧 Task#327：委托给 editor-storage.js 的统一管理器
function clearEditorTabs() {
  clearAllEditorState();
}

/** 完全重置编辑器状态（清除所有持久化数据，包括分拆状态） */
function resetEditorState() {
  clearEditorTabs();
  // 重置内存中的编辑器数据
  if (state.editorGroups && state.editorGroups.length > 0) {
    state.editorGroups[0].tabs = [];
  }
  state.editorGroups = null;
  state._lastActiveGroupId = null;
  console.log('[Editor] 编辑器状态已完全重置');
}

// 初始化编辑器


// 显示保存提示
function showSimpleToast(message, type = 'success') {
  showToast(type, message, '', 3000);
}

/**
 * 显示通用Toast通知
 * @param {string} toastType - toast类型（success/error/info/warning）
 * @param {string} title - 标题
 * @param {string} message - 消息内容
 * @param {number} duration - 显示时长（毫秒），默认3000ms
 */
function showToast(toastType, title, message, duration = 3000) {
  const container = document.getElementById('toast-container');
  if (!container) {
    console.warn('Toast容器不存在');
    return;
  }

  // 创建toast元素
  const toast = document.createElement('div');
  toast.className = `toast ${toastType}`;

  // 创建标题和消息
  const titleEl = document.createElement('div');
  titleEl.style.fontWeight = 'bold';
  titleEl.style.marginBottom = '4px';
  titleEl.textContent = title;

  const messageEl = document.createElement('div');
  messageEl.style.fontSize = '13px';
  messageEl.style.opacity = '0.9';
  messageEl.textContent = message;

  toast.appendChild(titleEl);
  toast.appendChild(messageEl);

  // 添加到容器
  container.appendChild(toast);

  // 自动移除
  setTimeout(() => {
    toast.style.animation = 'slideOut 0.3s ease';
    setTimeout(() => {
      if (toast.parentNode) {
        toast.parentNode.removeChild(toast);
      }
    }, 80);
  }, duration);
}

// 根据id递归查找消息（支持消息树结构：msg.tree.root.children）
function findMessageByIdRecursive(messages, msgId) {
  // 统一转换为数字类型进行比较（避免字符串和数字类型不一致的问题）
  const targetId = typeof msgId === 'string' ? parseInt(msgId, 10) : msgId;
  // 遍历根消息数组
  for (const msg of messages) {

    // 检查当前根消息（使用宽松比较 == 或统一类型比较）
    if (msg.id === targetId || msg.id == targetId) {
      return msg;
    }

    // 如果消息有树结构，递归遍历树节点
    if (msg.tree && msg.tree.root) {
      const found = findInMessageTree(msg.tree.root, targetId);
      if (found) {
        return found;
      }
    }
  }
  return null;
}

// 在消息树中递归查找（树节点有message和children属性）
function findInMessageTree(node, msgId) {
  // 统一转换为数字类型进行比较
  const targetId = typeof msgId === 'string' ? parseInt(msgId, 10) : msgId;

  // 检查当前节点的消息
  if (node.message) {
    if (node.message.id === targetId || node.message.id == targetId) {
      return node.message;
    }
  }

  // 递归遍历子节点
  if (node.children && node.children.length > 0) {
    for (const child of node.children) {
      const found = findInMessageTree(child, targetId);
      if (found) return found;
    }
  }

  return null;
}

function textToArgs(text) {
  // 先去除 \r（处理 Windows/WSL 的 CRLF 换行符），再将真正的换行符转为 \n 供后端识别
  // 去除 \r 是关键：如果保留 \r，写入临时文件后 Python 文本模式读取时
  // 会将 \r 转为 \n，导致 \r\n 变成 \n\n（每行多出一个空行）
  return text.replaceAll("\r", "").replaceAll("\n", "\\n")
}

/**
 * 🔧 Task#218优化：通过REST API异步通知事务内容变更，不阻塞保存流程
 * 之前: processBuiltinCommand + executeBuiltinCommand → subprocess.run(bin/task info) ~700ms
 * 现在: POST /api/cli/task/{id}/info → ~40ms, 且用catch静默处理错误
 */
async function _notifyTaskContentChange(taskId, infoMessage) {
  try {
    await fetch(`/api/cli/task/${taskId}/info`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ info: infoMessage })
    });
  } catch (e) {
    console.warn(`通知事务#${taskId}失败:`, e);
  }
}

async function saveEditorContent() {

  // 🔧 修复：支持分拆编辑器保存 - 使用当前活跃的编辑器
  const activeGroupId = getActiveEditorGroupId();
  let activeEditor = monacoEditor;
  let currentTab = findTabById(getActiveEditorTabId());  // 🔧 Task#334
  
  // 如果有分组结构，优先使用活跃分组的编辑器和标签页
  if (state.editorGroups?.length > 0 && activeGroupId != null) {
    const activeGroup = findGroupById(activeGroupId);
    if (activeGroup?.monacoEditor) {
      activeEditor = activeGroup.monacoEditor;
    }
    if (activeGroup?.currentTabId) {
      const groupTab = activeGroup.tabs.find(t => t.id === activeGroup.currentTabId);
      if (groupTab) currentTab = groupTab;
    }
  }

  if (!activeEditor) {
    console.error('编辑器不存在');
    return;
  }

  let content = activeEditor.getValue();
  const saveBtn = document.getElementById('editor-save');

  // 禁用保存按钮，显示保存中状态
  if (saveBtn) {
    saveBtn.disabled = true;
    setSaveBtnContent(saveBtn, 'saving');
  }
  try {
    // 检查当前是否为文本编辑器
    if (currentTab && currentTab.isContentEditor) {

      // 文本编辑器：保存消息或事务内容
      // 从当前标签中获取编辑类型和ID
      const editType = currentTab.editType;
      const targetId = currentTab.editId;
      if (!targetId) {
        showSimpleToast(`无法获取${editType === 'task' ? '事务' : '消息'}ID`, 'error');
        return;
      }

      // 🔧 Task#218优化：使用直接REST API替代subprocess调用，从~1500ms降至~80ms
      // 之前: fetch('/api/command') → subprocess.run(bin/msg或bin/task) → 每次约700ms
      // 现在: 直接调用 PUT /api/cli/msg/{id}/field/content 或 PUT /api/cli/task/{id}/field/content → 约40ms
      // 🔧 Task#484修复：REST API用JSON body传输，JSON本身能正确编码换行符，不需要textToArgs转义
      // 只需去除 \r（处理Windows/WSL的CRLF），保留真正的 \n 换行符
      const saveArgs = content.replace(/\r/g, '');
      let apiUrl, method;
      if (editType === 'message' || editType === 'branch-message') {
        apiUrl = `/api/cli/msg/${targetId}/field/content`;
        method = 'PUT';
      } else if (editType === 'task') {
        apiUrl = `/api/cli/task/${targetId}/field/content`;
        method = 'PUT';
      }

      const response = await fetch(apiUrl, {
        method: method,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ value: saveArgs })
      });

      if (!response.ok) {
        const errorData = await response.json().catch(() => ({}));
        const errMsg = errorData.detail || '保存失败';
        throw new Error(typeof errMsg === 'string' ? errMsg : JSON.stringify(errMsg));
      }

      const result = await response.json();

      // 显示保存成功提示 & 异步通知相关事务（不阻塞保存流程）
      if (editType === 'task') {
        showSimpleToast(`✅ 已保存事务#${targetId}`, 'success');
        // 🔧 Task#218优化：通知也改用REST API，不再走subprocess
        _notifyTaskContentChange(targetId, `事务#${targetId}的需求描述已变更：\n${saveArgs}`);
      } else if (editType === 'message' || editType === 'branch-message') {
        const msgId = targetId;
        const msg = findMessageByIdRecursive(state.messages, msgId);
        const tab = currentTab;
        const savedRootMsgId = tab && tab.rootMsgId ? parseInt(tab.rootMsgId, 10) : null;
        const msgRootMsgId = msg ? (msg.chain_head_id || msg.id) : null;
        const rootMsgId = savedRootMsgId || msgRootMsgId;
        const relatedTask = findTaskByMessageId(rootMsgId);
        showSimpleToast(`✅ 已保存消息 ${msgId}，关联事务: ${relatedTask ? relatedTask.id : '无'}`, "success");
        if (relatedTask && relatedTask.id && msg && msg.receiver === state.agentName) {
          // 🔧 Task#218优化：通知也改用REST API
          _notifyTaskContentChange(relatedTask.id, `消息#${msgId}的需求描述已变更：\n${saveArgs}`);
        }
      }

      // 保存后关闭当前标签页
      // 🔧 修复：支持分拆列中的标签页关闭
      if (state.editorGroups?.length > 0 && activeGroupId != null) {
        closeTabInGroup(activeGroupId, getActiveEditorTabId());  // 🔧 Task#334
      } else {
        await closeTab(getActiveEditorTabId());  // 🔧 Task#334
      }

      // 刷新消息/事务显示
      if (editType === 'message' || editType === 'branch-message') {
        // 刷新消息列表
        loadMessages(false, true);  // 编辑后：完整加载
      } else if (editType === 'task') {
        // 刷新事务列表
        loadTasks();
      }

      // 🔧 Task#364：默认布局下，编辑消息/分支消息/事务content保存成功后，自动切换到输出xterm界面
      if (state.currentLayout === 'default') {
        switchViewTab('output');
      }

    } else {
      // 普通文件保存或新文件另存为
      // 🔧 Task#134修复：直接从currentTab对象获取文件状态，避免多标签页时editorView.dataset冲突
      const isVirtual = currentTab ? !!currentTab.isVirtual : false;
      const isNewFile = currentTab ? !!currentTab.isNewFile : false;

      // 检查是否为虚拟文件（只读）
      if (isVirtual) {
        showSimpleToast('⚠️ 虚拟文件不可保存', 'warning');
        return;
      }

      // 检查是否为新文件（需要另存为）
      if (isNewFile) {
        // 调用另存为功能
        await saveAsFile();
        return;
      }

      // 🔧 Task#134修复：从currentTab.path获取文件路径，而非editorView.dataset.currentFilePath
      // editorView.dataset是全局共享的DOM属性，多标签页切换时可能与实际活动标签不同步
      const filePath = currentTab ? currentTab.path : null;
      if (!filePath) {
        showSimpleToast('❌ 无法获取文件路径', 'error');
        return;
      }

      // 🔧 Task#72：使用标签记住的nest而非全局nest-select
      const saveNestParam = (currentTab && currentTab.useLocal) ? '' : (currentTab && currentTab.nest ? `&nest=${encodeURIComponent(currentTab.nest)}` : '');
      const response = await fetch(`/api/files/write${saveNestParam ? '?' + saveNestParam.slice(1) : ''}`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          path: filePath,
          content: content
        })
      });

      if (!response.ok) {
        const errorData = await response.json().catch(() => ({}));
        throw new Error(errorData.detail || '保存失败');
      }

      const result = await response.json();

      // 显示保存成功提示
      showSimpleToast(`✅ 文件已保存: ${filePath}`, 'success');
      showInOutput(`✅ 文件已保存: ${filePath}`, 'success');
    }

  } catch (error) {
    console.error('保存失败:', error);
    showSimpleToast(`❌ 保存失败: ${error.message}`, 'error');
    showInOutput(`❌ 保存失败: ${error.message}`, 'error');
  } finally {
    // 恢复保存按钮
    if (saveBtn) {
      saveBtn.disabled = false;
      setSaveBtnContent(saveBtn, 'save');
    }
  }
}


// ==================== 编辑器状态恢复辅助函数 ====================

/**
 * 🔧 Task#327：在标签页内容恢复后，恢复编辑器的光标位置、滚动位置、选中文字和undo/redo状态。
 * 🔧 Task#421：增加 undo/redo 栈重建（通过 original→content 差异）和 find/replace 状态恢复。
 * 此函数在 restoreEditorTabs() 中标签页内容和group结构恢复完成后调用。
 */
async function _restoreEditorStatesAfterLoad() {
  const savedStates = loadEditorStates();
  // 🔧 Task#421：加载保存的编辑快照历史（从服务端KV存储加载）
  const savedSnapshots = await loadEditHistory();

  // 🔧 Task#327修复：延迟恢复viewState，确保switchToTab→updateEditorContent→setModel/setValue完成后
  // requestAnimationFrame不够，因为switchToTab是async的，可能在下一个帧还没完成
  // 使用setTimeout 100ms确保所有异步操作完成后再恢复viewState
  setTimeout(() => {
    if (state.editorGroups) {
      for (const group of state.editorGroups) {
        const editor = group.monacoEditor;
        if (!editor) continue;

        // 🔧 Task#421：用快照重建 undo/redo 栈
        _rebuildUndoRedoForGroup(group, savedSnapshots);

        // 注意：不在此处hook tracker！replay后用户做undo/redo会触发onDidChangeContent
        // 导致tracker捕获undo/redo操作作为新快照，污染编辑历史。
        // tracker会在以下时机自动hook：
        // 1. getOrCreateSharedModel 创建或复用model时
        // 2. updateEditorContent 中设置model时
        // 3. _rebuildUndoRedoForGroup 结束后清除所有tracker

        // 恢复当前活跃tab的编辑器状态
        if (group.currentTabId) {
          const tabState = savedStates[group.currentTabId];
          if (tabState && tabState.viewState) {
            try {
              editor.restoreViewState(tabState.viewState);
            } catch (e) {
              // viewState 恢复失败时，尝试用基本字段恢复
              _restoreBasicEditorState(editor, tabState);
            }
          } else if (tabState) {
            _restoreBasicEditorState(editor, tabState);
          }
        }

        // 缓存非活跃tab的状态（切换时使用）
        for (const tab of group.tabs) {
          const tabState = savedStates[tab.id];
          if (tabState) {
            if (tab.id !== group.currentTabId) {
              tab._savedViewState = tabState.viewState;
            }
            tab._savedCursorLine = tabState.cursorLine;
            tab._savedCursorCol = tabState.cursorCol;
            tab._savedScrollTop = tabState.scrollTop;
            tab._savedScrollLeft = tabState.scrollLeft;
          }
        }
      }
    }

    // 标记大文件标签页的粗体样式
    _updateLargeFileTabStyles();

    // 🔧 Task#421：恢复 find/replace 搜索词
    _restoreFindReplaceState();

    // 🔧 Task#327修复：恢复焦点到刷新前的活跃group
    // 使用双阶段延迟：先恢复viewState(500ms)，再focus到正确group(800ms)
    const layoutData = loadEditorLayout();
    const savedActiveGroupId = layoutData.activeGroupId ?? 0;
    setTimeout(() => {
      console.log('[restoreFocus] 尝试恢复焦点到 group', savedActiveGroupId, '当前activeGroupId=', state._lastActiveGroupId);
      if (state.editorGroups) {
        const activeGroup = state.editorGroups.find(g => g.id === savedActiveGroupId);
        if (activeGroup && activeGroup.monacoEditor) {
          activeGroup.monacoEditor.focus();
          state._lastActiveGroupId = savedActiveGroupId;
          console.log('[restoreFocus] focus完成, activeGroupId=', state._lastActiveGroupId);
        }
      }
    }, 800);
  });
}

/**
 * 🔧 Task#421：用 content 快照重建 undo/redo 栈。
 *
 * 方案：从 localStorage 读取保存的 content 快照序列（peng-ed-edits），
 * 从第一个快照开始，逐步 executeEdits 到下一个快照，
 * 每步之间用 pushUndoStop 分隔，产生完整多步 undo/redo 栈。
 *
 * 不依赖 original！快照序列自包含。
 */
function _rebuildUndoRedoForGroup(group, savedSnapshots) {
  if (!savedSnapshots || Object.keys(savedSnapshots).length === 0) return;

  const editor = group.monacoEditor;
  if (!editor) return;

  for (const tab of group.tabs) {
    const snapshots = savedSnapshots[tab.id];
    if (!snapshots || snapshots.length < 2) continue;

    // 只处理当前活跃的 tab
    if (group.currentTabId !== tab.id) continue;

    // 安全检查：快照最后一个必须等于当前 tab.content
    // 否则说明快照数据过期，不能用来 replay（会回到旧版本！）
    if (snapshots[snapshots.length - 1] !== tab.content) {
      console.warn(`[Task#421] 跳过 ${tab.name}: 快照末尾不匹配当前content`);
      continue;
    }

    try {
      const model = editor.getModel();
      if (!model) continue;

      const savedPosition = editor.getPosition();

      window._suppressContentChangeTracking = true;
      try {
        // 步骤1：设为第一个快照（最早的内容状态）
        model.setValue(snapshots[0]);

        // 步骤2：逐步 executeEdits 到每个后续快照
        for (let i = 1; i < snapshots.length; i++) {
          const targetContent = snapshots[i];
          const currentContent = model.getValue();
          if (currentContent === targetContent) continue;

          // 用全量替换实现每一步
          editor.executeEdits('restore-snap-' + i, [{
            range: model.getFullModelRange(),
            text: targetContent,
          }]);
          editor.pushUndoStop();
        }
        console.log(`[Task#421] 已为 tab ${tab.name} 通过 ${snapshots.length - 1} 个快照重建 undo/redo 栈`);
      } finally {
        window._suppressContentChangeTracking = false;
      }

      // 恢复光标位置
      if (savedPosition) {
        const lineCount = model.getLineCount();
        const savedLine = Math.min(savedPosition.lineNumber, lineCount);
        const maxCol = model.getLineMaxColumn(savedLine);
        editor.setPosition({ lineNumber: savedLine, column: Math.min(savedPosition.column, maxCol) });
      }
    } catch (e) {
      console.warn(`[Task#421] 重建 undo/redo 失败 (${tab.name}):`, e);
    }
  }

  // 🔧 Task#421：replay完成后清除所有相关tracker。
  // replay过程中的setValue会触发onDidChangeContent（虽然被_suppressContentChangeTracking阻止），
  // 但replay后的undo/redo操作会触发onDidChangeContent，tracker会误捕获。
  // 清除后，用户下次真正编辑时通过getOrCreateSharedModel重新hook。
  for (const tab of group.tabs) {
    if (tab.path) {
      const tracker = window._pengEditTrackers && window._pengEditTrackers[tab.path];
      if (tracker) {
        if (tracker.listener) tracker.listener.dispose();
        if (tracker._timer) clearTimeout(tracker._timer);
        delete window._pengEditTrackers[tab.path];
      }
    }
  }
}

/** 用基本字段恢复编辑器状态（viewState恢复失败时的回退方案） */
function _restoreBasicEditorState(editor, tabState) {
  try {
    if (tabState.cursorLine && tabState.cursorCol) {
      editor.setPosition({ lineNumber: tabState.cursorLine, column: tabState.cursorCol });
    }
    if (tabState.scrollTop != null) {
      editor.setScrollTop(tabState.scrollTop);
    }
    if (tabState.scrollLeft != null) {
      editor.setScrollLeft(tabState.scrollLeft);
    }
    if (tabState.selStartLine && tabState.selEndLine) {
      editor.setSelection({
        startLineNumber: tabState.selStartLine,
        startColumn: tabState.selStartCol || 1,
        endLineNumber: tabState.selEndLine,
        endColumn: tabState.selEndCol || 1,
      });
    }
  } catch (e) {
    console.warn('[EditorStorage] 恢复基本编辑器状态失败:', e);
  }
}

/**
 * 🔧 Task#327：更新大文件标签页的粗体样式。
 * 大文件标签页文字用粗体区分。
 */
function _updateLargeFileTabStyles() {
  if (!state.editorGroups) return;
  for (const group of state.editorGroups) {
    const tabsEl = document.getElementById('editor-tabs-' + group.id);
    if (!tabsEl) continue;
    for (const tab of group.tabs) {
      if (isTabLargeFile(tab)) {
        const tabEl = tabsEl.querySelector('[data-tab-id="' + tab.id + '"]');
        if (tabEl) tabEl.classList.add('large-file-tab');
      }
    }
  }
}
