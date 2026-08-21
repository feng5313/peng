// ==================== 视图标签页切换 ====================

function initViewTabs() {
  const viewTabs = document.getElementById('view-tabs');
  if (!viewTabs) {
    console.warn('view-tabs container not found');
    return;
  }

  // 获取所有标签页按钮
  const tabs = viewTabs.querySelectorAll('.view-tab');

  tabs.forEach(tab => {
    tab.addEventListener('click', () => {
      const view = tab.dataset.view;
      switchViewTab(view);
    });
  });

  // 绑定编辑器打开文件按钮 - 复用消息输入框上方的文件浏览器功能
  const openFileBtn = document.getElementById('editor-open-file');
  if (openFileBtn) {
    openFileBtn.addEventListener('click', () => {
      // 直接复用消息输入框上方的文件浏览器按钮功能
      const fileBrowserBtn = document.getElementById('file-browser-btn');
      if (fileBrowserBtn) {
        // 触发文件浏览器按钮的点击事件
        state.isFileBrowserFromEditor = true;
        // 隐藏"设为工作目录"按钮
        const fileBrowserSetWorkdir = document.getElementById('file-browser-set-workdir');
        if (fileBrowserSetWorkdir) {
          fileBrowserSetWorkdir.style.display = 'none';
        }
        fileBrowserBtn.click();
      } else {
        console.error('file-browser-btn 不存在');
      }
    });
  }

  // 绑定编辑器另存为按钮
  const saveAsBtn = document.getElementById('editor-save-as');
  if (saveAsBtn) {
    saveAsBtn.addEventListener('click', () => {
      saveAsFile();
    });
  }

  // 绑定编辑器保存按钮
  const saveBtn = document.getElementById('editor-save');
  if (saveBtn) {
    saveBtn.addEventListener('click', (e) => {
      e.preventDefault();
      e.stopPropagation();
      saveEditorContent();
    });
  }

  // 绑定默认布局编辑器分拆按钮
  const splitBtnDefault = document.getElementById('editor-split-btn-default');
  if (splitBtnDefault) {
    splitBtnDefault.addEventListener('click', () => splitEditorGroup());
  }
  // 绑定默认布局恢复已关闭标签页按钮
  const restoreBtnDefault = document.getElementById('editor-restore-tab-btn-default');
  if (restoreBtnDefault) {
    restoreBtnDefault.addEventListener('click', () => showClosedTabHistoryPopup());
  }
  // 绑定默认布局编辑器满页面显示按钮
  const editorFullscreenBtnDefault = document.getElementById('editor-fullscreen-btn-default');
  if (editorFullscreenBtnDefault) {
    editorFullscreenBtnDefault.addEventListener('click', () => toggleEditorFullscreen());
  }

  // 🔧 Task#184：初始化恢复按钮的灰色状态
  updateRestoreBtnState();
}

// ==================== 视图标签页恢复 ====================

async function restoreViewTab() {
  try {
    // 🔧 Bug2修复：非默认布局不通过restoreViewTab恢复编辑器，由initLayoutEditorPanel处理
    // 🔧 Task#327：通过统一管理器读取布局
    const layoutData = loadEditorLayout();
    const savedLayout = layoutData.layout;
    const savedViewTab = layoutData.viewTab;
    console.log('[restoreViewTab] savedLayout:', savedLayout, 'savedViewTab:', savedViewTab);
    if (savedLayout && savedLayout !== 'default') return;

    if (savedViewTab === 'editor') {
      // 编辑器视图：检查是否有保存的编辑器标签页
      // 如果没有标签页，则保持默认的output视图，避免显示空白编辑器
      const savedTabs = localStorage.getItem('peng-ed-tabs');
      if (savedTabs) {
        const tabs = JSON.parse(savedTabs);
        if (tabs && tabs.length > 0) {
          // 有标签页，切换到编辑器视图
          // 🔧 Task#331修复：恢复阶段不保存layout，避免用未恢复的_lastActiveGroupId覆盖正确值
          await switchViewTab('editor', { skipSave: true });
        }
      }
    } else if (savedViewTab === 'terminal') {
      // 🔧 Task#412修复：恢复terminal视图（之前只恢复editor，terminal/output被忽略）
      // output是默认初始视图不需要恢复，terminal需要主动切换
      await switchViewTab('terminal', { skipSave: true });
    }
    // savedViewTab === 'output' 或 null：保持默认的output视图，无需操作
  } catch (error) {
    console.warn('恢复视图状态失败:', error);
  }
}

async function switchViewTab(view, options = {}) {
  // 🔧 修复：如果已经是当前视图，直接返回，避免重复执行（特别是restoreEditorTabs）
  if (state.currentViewTab === view) {
    return;
  }

  // 更新状态
  state.currentViewTab = view;

  // 🔧 修复：保存当前视图状态到 localStorage（修复刷新后编辑器显示空白的问题）
  // 🔧 Task#331修复：恢复阶段跳过保存，避免用未恢复的_lastActiveGroupId覆盖localStorage中正确的值
  // 🔧 Task#327：通过统一管理器保存
  if (!options.skipSave) {
    try {
      saveEditorLayout();
    } catch (error) {
      console.warn('保存视图状态失败:', error);
    }
  }

  // 更新标签页按钮的 active 状态
  const tabs = document.querySelectorAll('.view-tab');
  tabs.forEach(tab => {
    if (tab.dataset.view === view) {
      tab.classList.add('active');
    } else {
      tab.classList.remove('active');
    }
  });

  // 切换视图显示
  const outputView = document.getElementById('output');
  const editorView = document.getElementById('editor-view');
  const terminalView = document.getElementById('terminal-view');
  const outputFilters = document.getElementById('output-filters');
  const outputHeader = document.getElementById('output-xterm-header');
  const editorHeader = document.getElementById('editor-header');

  // 🔧 修复：先移除所有视图的active类
  if (terminalView) terminalView.classList.remove('active');

  if (view === 'output') {
    outputView.style.display = 'block';
    editorView.style.display = 'none';
    terminalView.style.display = 'none';
    // 显示xterm相关元素
    if (outputFilters) outputFilters.style.display = 'block';
    // 显示终端头部，隐藏编辑器头部
    if (outputHeader) outputHeader.style.display = 'flex';
    if (editorHeader) editorHeader.style.display = 'none';

    // 🔧 修复：使用**双 requestAnimationFrame** 延迟 fit 调用
    // 当容器刚从 display:none 变为可见时，单次 rAF 不足以完成嵌套 flex 布局的尺寸计算。
    // 使用双 rAF 确保：第一帧完成重排(reflow)，第二帧拿到正确的容器尺寸。
    if (state.output && state.outputFit) {
      requestAnimationFrame(() => {
        requestAnimationFrame(() => {
          try {
            if (state.output && state.outputFit) {
              state.outputFit.fit();
            }
          } catch (e) {
            console.warn('[view-tabs] fit 失败:', e);
          }
        });
      });
    }
  } else if (view === 'editor') {
    outputView.style.display = 'none';
    editorView.style.display = 'flex';
    terminalView.style.display = 'none';
    // 隐藏xterm相关元素（过滤勾选框）
    if (outputFilters) outputFilters.style.display = 'none';
    // 隐藏终端头部，显示编辑器头部
    if (outputHeader) outputHeader.style.display = 'none';
    if (editorHeader) editorHeader.style.display = 'flex';
    // 确保group0数据结构存在
    ensureGroup0();
    // 🔧 Task#341修复：为所有group容器注册mousedown追踪器
    initGroupMousedownTrackers();
    // 先加载Monaco
    await loadMonacoEditor();
    // 🔧 Task#320修复：不要每次切到编辑器都调用restoreEditorTabs！
    // restoreEditorTabs从localStorage重新加载，覆盖当前内存中的tab
    // 只在首次加载页面时restore（页面初始化时已调用）
    // 如果tab数据丢失（如刷新后），restoreEditorTabs会在页面加载时由initLayoutEditorPanel调用
    // 这里只需要渲染当前内存中的tab
    const g0Tabs = getG0Tabs();
    if (g0Tabs.length > 0) {
      // 有tab数据，直接渲染
      state.editorGroups.forEach(function(g) { renderGroupTabs(g.id); });
      // 确保group0有Monaco编辑器实例
      const g0 = state.editorGroups[0];
      if (g0 && g0.currentTabId && !g0.monacoEditor) {
        await createGroupEditor(0);
      }
      if (g0 && g0.currentTabId && g0.monacoEditor) {
        const tab = g0.tabs.find(t => t.id === g0.currentTabId);
        if (tab) {
          updateEditorContent(tab);
        }
      }
    } else if (!options.skipRestore) {
      // 只有没有任何tab数据时才从localStorage恢复（首次加载场景）
      await restoreEditorTabs();
    }

    // 延迟触发layout()，确保minimap正确
    if (monacoEditor) {
      requestAnimationFrame(() => {
        monacoEditor.layout();
      });
    }

    // 如果没有标签页，根据配置决定是否自动创建空文档（默认否）
    if (getG0Tabs().length === 0 && !options.skipAutoCreate) {
      const autoCreateEmptyDoc = state.webuiConfig?.editor?.auto_create_empty_doc ?? false;
      if (autoCreateEmptyDoc) {
        if (monacoEditor) {
          monacoEditor.setValue('');
        }
        ensureGroup0();
        await createNewFileInGroup(0);
      }
    }

    // 渲染group0的标签页
    renderGroupTabs(0);

    // 更新编辑器路径和光标信息显示
    const currentTab = findTabById(getActiveEditorTabId());  // 🔧 Task#334
    if (currentTab) {
      updateEditorPathDisplay(currentTab);
    }
    updateEditorCursorInfo();

    // 延迟聚焦到编辑器
    setTimeout(() => {
      const activeGroupId = getActiveEditorGroupId();
      const activeGroup = findGroupById(activeGroupId);
      if (activeGroup && activeGroup.monacoEditor) {
        activeGroup.monacoEditor.focus();
      } else if (monacoEditor) {
        monacoEditor.focus();
      }
    }, 50);
  } else if (view === 'terminal') {
    outputView.style.display = 'none';
    editorView.style.display = 'none';
    // 🔧 修复：使用CSS的.active类而不是直接设置style.display
    terminalView.style.display = 'flex';
    terminalView.classList.add('active');
    // 隐藏xterm相关元素（过滤勾选框）
    if (outputFilters) outputFilters.style.display = 'none';
    // 隐藏输出头部，隐藏编辑器头部，显示终端头部
    if (outputHeader) outputHeader.style.display = 'none';
    if (editorHeader) editorHeader.style.display = 'none';

    const terminalHeader = document.getElementById('terminal-header');
    if (terminalHeader) terminalHeader.style.display = 'flex';

    // 🔧 Task#84：切换到终端视图时，同步nest下拉框和工作目录到当前活动标签页
    const activeTab = state.terminalTabs.find(tab => tab.id === state.currentTerminalTabId);
    if (activeTab && activeTab.nest) {
      const nestName = activeTab.nest;
      _prepareNestState(nestName);
      _syncNestDropdown(nestName);
      const nests = window._latestNests || {};
      if (nestName === 'local') {
        const workdirInput = document.getElementById('workdir-input');
        if (workdirInput) {
          workdirInput.value = toNativeDisplayPath(state.projectRoot || '');
        }
        state.remoteNest = null;
        state.remoteNestRoot = null;
      } else if (nests[nestName]) {
        _updateWorkdirForNest(nestName, nests);
      }
    }

    // 🔧 修复：点击终端按钮时，自动将输入焦点切换到当前激活的终端
    // 参考 switchTerminalTab 的两次聚焦策略：第一次立即聚焦，第二次延迟20ms覆盖 initFocusManagement 的10ms延迟
    if (activeTab && activeTab.xterm && typeof activeTab.xterm.focus === 'function') {
      try {
        // 立即聚焦
        activeTab.xterm.focus();
        // 延迟20ms再次聚焦，覆盖 initFocusManagement 的自动聚焦机制（10ms）
        setTimeout(() => {
          if (activeTab.xterm && typeof activeTab.xterm.focus === 'function') {
            activeTab.xterm.focus();
          }
        }, 20);
      } catch (error) {
        console.warn('[switchViewTab] 聚焦终端失败:', error);
      }
    }

  }
}
