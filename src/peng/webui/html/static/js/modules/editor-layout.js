// ==================== 布局管理 ====================

/**
 * 布局模式：
 * - 'default': 默认布局（输出/终端/编辑器 标签页切换）
 * - 'editor-output-terminal': 上编辑器；下左输出；下右终端
 * - 'editor-tabbed': 上编辑器；下输出/终端共用标签页
 * - 'editor-output-terminal-rows': 上编辑器；中输出；下终端
 */

function initLayoutSelector() {
  const selector = document.getElementById('layout-selector');
  if (!selector) return;

  // 🔧 Task#327：通过统一管理器恢复布局
  const layoutData = loadEditorLayout();
  const savedLayout = layoutData.layout || 'default';
  state.currentLayout = savedLayout;

  // 激活对应按钮
  selector.querySelectorAll('.layout-btn').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.layout === savedLayout);
    btn.addEventListener('click', () => switchLayout(btn.dataset.layout));
  });

  // 应用布局
  applyLayout(savedLayout);
}

function switchLayout(layout) {
  if (state.currentLayout === layout) return;
  state.currentLayout = layout;
  // 🔧 Task#327：通过统一管理器保存布局
  saveEditorLayout();

  // 更新按钮状态
  document.querySelectorAll('.layout-btn').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.layout === layout);
  });

  applyLayout(layout);
}

function applyLayout(layout) {
  const mainContent = document.getElementById('main-content');
  if (!mainContent) return;

  // 清除所有布局class
  mainContent.classList.remove('layout-default', 'layout-editor-output-terminal', 'layout-editor-tabbed', 'layout-editor-output-terminal-rows');
  mainContent.classList.add('layout-' + layout);

  // 获取核心DOM节点（需要在不同布局间移动的元素）
  const defaultContainer = document.getElementById('default-output-container');
  const editorView = document.getElementById('editor-view');
  const outputEl = document.getElementById('output');
  const outputFilters = document.getElementById('output-filters');
  const terminalView = document.getElementById('terminal-view');

  // 🔧 核心原则：第一个group的DOM由JS动态创建（createGroupDom），ID动态分配
  // 确保第一个group数据结构存在
  ensureGroup0();

  if (layout === 'default') {
    // ===== 恢复默认：把DOM节点移回default-output-container =====
    hideEl('layout-editor-panel');
    hideEl('layout-split-handle-1');
    hideEl('layout-bottom-panel');
    hideEl('layout-split-handle-2');
    hideEl('layout-terminal-panel');

    showEl('default-output-container');

    // 恢复 view-tabs 显示
    const viewTabs = document.getElementById('view-tabs');
    if (viewTabs) viewTabs.style.display = '';

    // 把output-filters和output移回default容器
    if (outputFilters && outputFilters.parentElement !== defaultContainer) {
      defaultContainer.insertBefore(outputFilters, defaultContainer.querySelector('#output'));
    }
    if (outputEl && outputEl.parentElement !== defaultContainer) {
      defaultContainer.appendChild(outputEl);
    }

    // 🔧 Task#320统一方案：editor-groups-container是全局唯一容器
    // 默认布局：把editor-groups-container整体移到editor-view中
    const groupsContainer = document.getElementById('editor-groups-container');
    if (groupsContainer && editorView) {
      editorView.appendChild(groupsContainer);
    }

    // editor-view移回default容器
    if (editorView && editorView.parentElement !== defaultContainer) {
      defaultContainer.appendChild(editorView);
    }
    if (terminalView && terminalView.parentElement !== defaultContainer) {
      defaultContainer.appendChild(terminalView);
    }

    // 根据当前视图状态恢复显示
    editorView.style.display = state.currentViewTab === 'editor' ? 'flex' : 'none';
    terminalView.style.display = state.currentViewTab === 'terminal' ? 'flex' : 'none';
    outputEl.style.display = state.currentViewTab === 'output' ? 'block' : 'none';
    outputFilters.style.display = state.currentViewTab === 'output' ? 'block' : 'none';

  } else {
    // ===== 非默认布局 =====
    hideEl('default-output-container');
    showEl('layout-editor-panel');

    // 先隐藏所有子面板
    hideEl('layout-bottom-left');
    hideEl('layout-bottom-split');
    hideEl('layout-bottom-right');
    hideEl('layout-bottom-tabbed');
    hideEl('layout-split-handle-1');
    hideEl('layout-bottom-panel');
    hideEl('layout-split-handle-2');
    hideEl('layout-terminal-panel');

    // 隐藏 view-tabs（非默认布局不显示）
    const viewTabs = document.getElementById('view-tabs');
    if (viewTabs) viewTabs.style.display = 'none';

    // ===== 移动编辑器group结构到面板容器 =====
    // 🔧 Task#320统一方案：editor-groups-container是全局唯一容器
    // 非默认布局：把editor-groups-container整体移到layout-editor-panel中
    const layoutEditorPanel = document.getElementById('layout-editor-panel');
    const groupsContainer = document.getElementById('editor-groups-container');
    if (layoutEditorPanel && groupsContainer) {
      // 插到panel header之后
      const panelHeader = layoutEditorPanel.querySelector('.panel-header');
      if (panelHeader && panelHeader.nextSibling) {
        layoutEditorPanel.insertBefore(groupsContainer, panelHeader.nextSibling);
      } else {
        layoutEditorPanel.appendChild(groupsContainer);
      }
    }
    // editorView现在为空，确保隐藏
    if (editorView) editorView.style.display = 'none';

    if (layout === 'editor-output-terminal') {
      // 布局2：上编辑器；下左输出；下右终端
      showEl('layout-split-handle-1');
      showEl('layout-bottom-panel');
      showEl('layout-bottom-left');
      showEl('layout-bottom-split');
      showEl('layout-bottom-right');

      const outputHost = document.getElementById('layout-output-host-left');
      const terminalHost = document.getElementById('layout-terminal-host-right');
      if (outputHost) {
        if (outputFilters) { outputFilters.style.display = 'block'; outputHost.appendChild(outputFilters); }
        if (outputEl) { outputEl.style.display = 'block'; outputHost.appendChild(outputEl); }
      }
      if (terminalHost) {
        terminalView.style.display = 'flex';
        terminalHost.appendChild(terminalView);
      }

    } else if (layout === 'editor-tabbed') {
      // 布局3：上编辑器；下输出/终端共用标签页
      showEl('layout-split-handle-1');
      showEl('layout-bottom-panel');
      showEl('layout-bottom-tabbed');

      const outputHost = document.getElementById('layout-output-host-tabbed');
      const terminalHost = document.getElementById('layout-terminal-host-tabbed');
      if (outputHost) {
        if (outputFilters) { outputFilters.style.display = 'block'; outputHost.appendChild(outputFilters); }
        if (outputEl) { outputEl.style.display = 'block'; outputHost.appendChild(outputEl); }
      }
      if (terminalHost) {
        terminalView.style.display = 'none'; // 默认先隐藏终端
        terminalHost.appendChild(terminalView);
      }
      initPanelTabs();

    } else if (layout === 'editor-output-terminal-rows') {
      // 布局4：上编辑器；中输出；下终端
      showEl('layout-split-handle-1');
      showEl('layout-bottom-panel');
      showEl('layout-split-handle-2');
      showEl('layout-terminal-panel');
      showEl('layout-bottom-left');

      const outputHost = document.getElementById('layout-output-host-left');
      const terminalHost = document.getElementById('layout-terminal-host-rows');
      if (outputHost) {
        if (outputFilters) { outputFilters.style.display = 'block'; outputHost.appendChild(outputFilters); }
        if (outputEl) { outputEl.style.display = 'block'; outputHost.appendChild(outputEl); }
      }
      if (terminalHost) {
        terminalView.style.display = 'flex';
        terminalHost.appendChild(terminalView);
      }
    }

    // 绑定编辑器面板的按钮事件
    initLayoutEditorPanel();
  }

  // 初始化分割条拖拽
  initLayoutSplitHandles();

  // 🔧 Task#178 Bug7修复：多次延迟fit确保xterm充满容器
  // 非默认布局中output被移动到新容器，需要多次尝试确保fit成功
  setTimeout(() => {
    window.dispatchEvent(new Event('resize'));
    layoutAllMonacoEditors();
    fitAllTerminals();
  }, 200);
  // 二次fit：等DOM完全渲染和flex布局计算完成
  setTimeout(() => {
    fitAllTerminals();
    layoutAllMonacoEditors();
  }, 500);
}

function hideEl(id) {
  const el = document.getElementById(id);
  if (el) el.style.display = 'none';
}

function showEl(id) {
  const el = document.getElementById(id);
  if (el) el.style.display = '';
}

/** 初始化布局面板中的编辑器区域 */
async function initLayoutEditorPanel() {
  // 绑定编辑器面板的按钮事件
  const openFileBtn2 = document.getElementById('editor-open-file-2');
  if (openFileBtn2 && !openFileBtn2._bound) {
    openFileBtn2._bound = true;
    openFileBtn2.addEventListener('click', () => {
      state.isFileBrowserFromEditor = true;
      const fileBrowserSetWorkdir = document.getElementById('file-browser-set-workdir');
      if (fileBrowserSetWorkdir) fileBrowserSetWorkdir.style.display = 'none';
      document.getElementById('file-browser-btn')?.click();
    });
  }
  const saveAsBtn2 = document.getElementById('editor-save-as-2');
  if (saveAsBtn2 && !saveAsBtn2._bound) {
    saveAsBtn2._bound = true;
    saveAsBtn2.addEventListener('click', () => saveAsFile());
  }
  const saveBtn2 = document.getElementById('editor-save-2');
  if (saveBtn2 && !saveBtn2._bound) {
    saveBtn2._bound = true;
    saveBtn2.addEventListener('click', (e) => { e.preventDefault(); e.stopPropagation(); saveEditorContent(); });
  }
  const splitBtn = document.getElementById('editor-split-btn');
  if (splitBtn && !splitBtn._bound) {
    splitBtn._bound = true;
    splitBtn.addEventListener('click', () => splitEditorGroup());
  }
  // 恢复已关闭标签页按钮（非默认布局）
  const restoreBtn = document.getElementById('editor-restore-tab-btn');
  if (restoreBtn && !restoreBtn._bound) {
    restoreBtn._bound = true;
    restoreBtn.addEventListener('click', () => showClosedTabHistoryPopup());
  }
  const editorFullscreenBtn = document.getElementById('editor-fullscreen-btn');
  if (editorFullscreenBtn && !editorFullscreenBtn._bound) {
    editorFullscreenBtn._bound = true;
    editorFullscreenBtn.addEventListener('click', () => toggleEditorFullscreen());
  }

  // 绑定非默认布局输出面板的按钮
  ['2', '3'].forEach(suffix => {
    const clearBtn = document.getElementById('btn-clear-output-' + suffix);
    if (clearBtn && !clearBtn._bound) {
      clearBtn._bound = true;
      clearBtn.addEventListener('click', () => {
        const btn = document.getElementById('btn-clear-output');
        if (btn) btn.click();
      });
    }
    const copyBtn = document.getElementById('btn-copy-output-' + suffix);
    if (copyBtn && !copyBtn._bound) {
      copyBtn._bound = true;
      copyBtn.addEventListener('click', () => {
        const btn = document.getElementById('btn-copy-output');
        if (btn) btn.click();
      });
    }
    const prevBtn = document.getElementById('btn-prev-page-' + suffix);
    if (prevBtn && !prevBtn._bound) {
      prevBtn._bound = true;
      prevBtn.addEventListener('click', () => {
        const btn = document.getElementById('btn-prev-page');
        if (btn) btn.click();
      });
    }
    const nextBtn = document.getElementById('btn-next-page-' + suffix);
    if (nextBtn && !nextBtn._bound) {
      nextBtn._bound = true;
      nextBtn.addEventListener('click', () => {
        const btn = document.getElementById('btn-next-page');
        if (btn) btn.click();
      });
    }
    const scrollBtn = document.getElementById('btn-scroll-bottom-' + suffix);
    if (scrollBtn && !scrollBtn._bound) {
      scrollBtn._bound = true;
      scrollBtn.addEventListener('click', () => {
        const btn = document.getElementById('btn-scroll-bottom');
        if (btn) btn.click();
      });
    }
  });

  // 渲染编辑器标签页到布局面板
  renderLayoutEditorTabs();

  // 🔧 Task#320修复：切换布局时不要调用restoreEditorTabs()！
  // restoreEditorTabs会从localStorage重新加载，覆盖当前内存中的tab内容
  // 切换布局只是移动DOM元素，应该保留当前内存中的tab状态
  //
  // 🔧 Task#417修复：但如果是页面刷新后的首次加载（内存中没有tab数据），
  // 必须调用 restoreEditorTabs() 从 localStorage 恢复标签页。
  // 否则非default布局刷新后标签页丢失，只剩空白文档。
  console.log('[initLayoutEditorPanel] 开始, layout:', state.currentLayout, 'tabs:', getG0Tabs().length);
  ensureGroup0();
  await loadMonacoEditor();

  const hasAnyTabs = (state.editorGroups && state.editorGroups.some(g => g.tabs.length > 0));
  if (hasAnyTabs) {
    // 内存中有tab数据（布局切换场景）：直接渲染，不调用restoreEditorTabs
    state.editorGroups.forEach(g => renderGroupTabs(g.id));
    // 切到当前活跃tab
    const g0 = state.editorGroups[0];
    if (g0 && g0.currentTabId && g0.monacoEditor) {
      const tab = g0.tabs.find(t => t.id === g0.currentTabId);
      if (tab) {
        updateEditorContent(tab);
      }
    }
  } else {
    // 内存中没有tab数据（页面刷新场景）：从localStorage恢复
    const savedTabs = localStorage.getItem('peng-ed-tabs');
    if (savedTabs) {
      try {
        await restoreEditorTabs();
      } catch (e) {
        console.warn('[initLayoutEditorPanel] restoreEditorTabs失败:', e);
      }
    }
    // 恢复后仍然没有标签页，根据配置决定是否创建空白文档（默认否）
    if (!state.editorGroups || !state.editorGroups.some(g => g.tabs.length > 0)) {
      const autoCreateEmptyDoc = state.webuiConfig?.editor?.auto_create_empty_doc ?? false;
      if (autoCreateEmptyDoc) {
        ensureGroup0();
        await createNewFileInGroup(0);
      }
    }
  }
  console.log('[initLayoutEditorPanel] 完成, tabs:', getG0Tabs().length, 'groups:', state.editorGroups ? state.editorGroups.length : 0);
}

/** 渲染布局面板中的编辑器标签页 */
function renderLayoutEditorTabs() {
  // 统一使用group结构渲染
  if (state.currentLayout === 'default') return;
  if (state.editorGroups?.length > 0) {
    state.editorGroups.forEach(g => renderGroupTabs(g.id));
    return;
  }
}

/** 初始化下方面板标签页切换（布局3） */
function initPanelTabs() {
  const tabs = document.querySelectorAll('#panel-tabs-bottom .panel-tab');
  tabs.forEach(tab => {
    if (tab._bound) return;
    tab._bound = true;
    tab.addEventListener('click', () => {
      tabs.forEach(t => t.classList.remove('active'));
      tab.classList.add('active');
      const panel = tab.dataset.panel;
      const outputHost = document.getElementById('layout-output-host-tabbed');
      const terminalHost = document.getElementById('layout-terminal-host-tabbed');
      if (panel === 'output') {
        if (outputHost) outputHost.style.display = '';
        if (terminalHost) terminalHost.style.display = 'none';
        // 显示输出头部信息和操作按钮
        const status3 = document.getElementById('output-msg-info-3');
        const actions3 = document.getElementById('output-actions-3');
        if (status3) status3.style.display = '';
        if (actions3) actions3.style.display = '';
      } else {
        if (outputHost) outputHost.style.display = 'none';
        if (terminalHost) terminalHost.style.display = '';
        // 隐藏输出头部信息和操作按钮
        const status3 = document.getElementById('output-msg-info-3');
        const actions3 = document.getElementById('output-actions-3');
        if (status3) status3.style.display = 'none';
        if (actions3) actions3.style.display = 'none';
        // terminal-view需显示
        const tv = document.getElementById('terminal-view');
        if (tv) tv.style.display = 'flex';
      }
      setTimeout(() => fitAllTerminals(), 100);
    });
  });
}

/** 初始化布局分割条拖拽 */
function initLayoutSplitHandles() {
  // 垂直分割条（上下）
  ['layout-split-handle-1', 'layout-split-handle-2'].forEach(id => {
    const handle = document.getElementById(id);
    if (!handle || handle._dragBound) return;
    handle._dragBound = true;

    handle.addEventListener('mousedown', (e) => {
      e.preventDefault();
      handle.classList.add('dragging');
      const mainContent = document.getElementById('main-content');
      const startY = e.clientY;
      const panelBefore = handle.previousElementSibling;
      const panelAfter = handle.nextElementSibling;
      const startHeightBefore = panelBefore.offsetHeight;
      const startHeightAfter = panelAfter.offsetHeight;

      // 🔧 Task#414：拖拽时实时调整编辑器和终端大小
      let _layoutRafPending = false;
      const onMouseMove = (e) => {
        const delta = e.clientY - startY;
        const newHeightBefore = Math.max(60, startHeightBefore + delta);
        const newHeightAfter = Math.max(60, startHeightAfter - delta);
        panelBefore.style.flex = 'none';
        panelBefore.style.height = newHeightBefore + 'px';
        panelAfter.style.flex = 'none';
        panelAfter.style.height = newHeightAfter + 'px';
        // 用rAF节流，避免每帧多次layout造成卡顿
        if (!_layoutRafPending) {
          _layoutRafPending = true;
          requestAnimationFrame(() => {
            _layoutRafPending = false;
            layoutAllMonacoEditors();
            fitAllTerminals();
          });
        }
      };

      const onMouseUp = () => {
        handle.classList.remove('dragging');
        document.removeEventListener('mousemove', onMouseMove);
        document.removeEventListener('mouseup', onMouseUp);
        layoutAllMonacoEditors();
        fitAllTerminals();
      };

      document.addEventListener('mousemove', onMouseMove);
      document.addEventListener('mouseup', onMouseUp);
    });
  });

  // 水平分割条（左右 - layout-bottom-split）
  const hSplit = document.getElementById('layout-bottom-split');
  if (hSplit && !hSplit._dragBound) {
    hSplit._dragBound = true;
    hSplit.addEventListener('mousedown', (e) => {
      e.preventDefault();
      hSplit.classList.add('dragging');
      const parent = hSplit.parentElement;
      const leftPanel = document.getElementById('layout-bottom-left');
      const rightPanel = document.getElementById('layout-bottom-right');
      const startX = e.clientX;
      const startWidthLeft = leftPanel.offsetWidth;
      const startWidthRight = rightPanel.offsetWidth;

      const onMouseMove = (e) => {
        const delta = e.clientX - startX;
        const newWidthLeft = Math.max(60, startWidthLeft + delta);
        const newWidthRight = Math.max(60, startWidthRight - delta);
        leftPanel.style.flex = 'none';
        leftPanel.style.width = newWidthLeft + 'px';
        rightPanel.style.flex = 'none';
        rightPanel.style.width = newWidthRight + 'px';
      };

      const onMouseUp = () => {
        hSplit.classList.remove('dragging');
        document.removeEventListener('mousemove', onMouseMove);
        document.removeEventListener('mouseup', onMouseUp);
        fitAllTerminals();
      };

      document.addEventListener('mousemove', onMouseMove);
      document.addEventListener('mouseup', onMouseUp);
    });
  }
}

/** 🔧 Task#383：修正Monaco minimap遮挡右侧文字
 * Monaco内部hardcode了minimap的right间距(~14px)，导致 minimap.left = contentRight - 14，
 * 即文字右边缘被minimap遮住14px（约2-3个中文字符）。
 * 通过MutationObserver + rAF双保险持续修正minimap.style.left = contentLeft + contentWidth。 */
const _MINIMAP_RIGHT_MARGIN = 14;

function _fixMinimapPosition(editor) {
  try {
    var dom = editor.getDomNode();
    if (!dom) return;
    var minimap = dom.querySelector('.minimap');
    if (!minimap) return;
    var li = editor.getLayoutInfo();
    var correctLeft = li.contentLeft + li.contentWidth;
    var currentLeft = parseInt(minimap.style.left) || 0;
    if (currentLeft !== correctLeft) {
      minimap.style.left = correctLeft + 'px';
    }
    // 🔧 Task#411：去除minimap内部左侧空白
    // Monaco minimap将编辑器的contentLeft（行号+装饰区域）也映射到minimap中，
    // 导致minimap canvas左侧有与contentLeft成比例的空白区域。
    // 通过CSS变量传递偏移量，由CSS规则完成实际裁切。
    // 注意：Monaco layout会重写minimap.style.cssText覆盖CSS变量，所以每次都需重新设置。
    var blankPx = Math.ceil(li.contentLeft * li.minimap.minimapWidth / li.width);
    if (blankPx > 0 && minimap.style.getPropertyValue('--minimap-blank-px') !== '-' + blankPx + 'px') {
      minimap.style.setProperty('--minimap-blank-px', '-' + blankPx + 'px');
    }
  } catch (e) { }
}

/** 持续修正循环：用rAF确保在Monaco每帧layout后都修正minimap位置 */
function _startMinimapFixLoop(editor) {
  if (editor._minimapFixLoopRunning) return;
  editor._minimapFixLoopRunning = true;
  var stopped = false;

  function tick() {
    if (stopped) return;
    _fixMinimapPosition(editor);
    requestAnimationFrame(tick);
  }
  requestAnimationFrame(tick);

  // 清理方法（editor dispose时调用）
  editor._stopMinimapFixLoop = function () { stopped = true; };
}

/** 为编辑器设置minimap位置修正 */
function _setupMinimapFix(editor) {
  try {
    if (editor._minimapFixReady) return;
    var dom = editor.getDomNode();
    if (!dom) return;
    var minimap = dom.querySelector('.minimap');
    if (!minimap) {
      // minimap DOM可能还没创建，设置轮询检查
      if (!editor._minimapFixPollTimer) {
        var pollCount = 0;
        editor._minimapFixPollTimer = setInterval(function () {
          pollCount++;
          var mm = dom.querySelector('.minimap');
          if (mm || pollCount > 50) {
            clearInterval(editor._minimapFixPollTimer);
            editor._minimapFixPollTimer = null;
            if (mm) {
              _fixMinimapPosition(editor);
              _startMinimapFixLoop(editor);
              editor._minimapFixReady = true;
            }
          }
        }, 100);
      }
      return;
    }
    _fixMinimapPosition(editor);
    _startMinimapFixLoop(editor);
    editor._minimapFixReady = true;
  } catch (e) { }
}

/** 安全layout编辑器 */
function _safeLayoutEditor(editor) {
  try {
    var dom = editor.getDomNode();
    if (!dom) return;
    var parent = dom.parentElement;
    // 🔧 Task#414：使用父容器的尺寸而非Monaco自身DOM尺寸
    // 当外层面板高度变化时，父容器高度会跟随flex布局更新，
    // 但Monaco DOM节点仍保持上一次layout()的尺寸，导致读取旧值
    var w = parent ? parent.clientWidth : dom.offsetWidth;
    var h = parent ? parent.clientHeight : dom.offsetHeight;
    if (w > 0 && h > 0) {
      editor.layout({ width: w, height: h });
    }
  } catch (e) { }
}

/** 重新布局所有Monaco编辑器 */
function layoutAllMonacoEditors() {
  // 默认布局的主编辑器
  if (monacoEditor) {
    _safeLayoutEditor(monacoEditor);
    _fixMinimapPosition(monacoEditor);
    _setupMinimapFix(monacoEditor);
  }
  // 分拆编辑器组
  if (state.editorGroups) {
    state.editorGroups.forEach(group => {
      if (group.monacoEditor) {
        _safeLayoutEditor(group.monacoEditor);
        _fixMinimapPosition(group.monacoEditor);
        _setupMinimapFix(group.monacoEditor);
      }
    });
  }
}

/** 重新fit所有终端 */
function fitAllTerminals() {
  // 🔧 修复：使用**双 requestAnimationFrame** 延迟 fit 调用
  // 嵌套 flex 布局可能需要多帧才能完成尺寸计算，单次 rAF 不够
  requestAnimationFrame(() => {
    requestAnimationFrame(() => {
      // 默认布局的输出终端
      if (state.output && state.outputFit) {
        try { state.outputFit.fit(); } catch (e) { }
      }
      // 所有终端标签页
      if (state.terminalTabs) {
        state.terminalTabs.forEach(tab => {
          if (tab.xterm && tab.fitAddon) {
            try { tab.fitAddon.fit(); } catch (e) { }
          }
        });
      }
    });
  });
}

// 🔧 全局resize handler：确保xterm和monaco在窗口大小变化时重新计算
let _resizeTimer = null;
window.addEventListener('resize', () => {
  if (_resizeTimer) clearTimeout(_resizeTimer);
  _resizeTimer = setTimeout(() => {
    fitAllTerminals();
    layoutAllMonacoEditors();
  }, 150);
});

/** 已废弃：group结构现在由ensureGroup0()动态创建，无需手动处理 */
function ensureGroupStructure(editorView) {
  // ensureGroup0()已负责创建DOM和数据结构，无需额外操作
}

/** 已废弃：不再撤销group结构，始终保持group模式 */
function undoGroupStructure(editorView) {
  // 不再需要，group结构始终保持
}

/** 编辑器分拆（左右并列） */
async function splitEditorGroup() {
  // 🔧 Task#178 Bug4修复：防止重复点击（async函数在执行期间可能被再次触发）
  if (window._splitEditorGroupRunning) {
    showSimpleToast('正在分拆中...', 'info');
    return;
  }
  window._splitEditorGroupRunning = true;
  try {
    return await _splitEditorGroupImpl();
  } finally {
    window._splitEditorGroupRunning = false;
  }
}

/** 分拆实现 */
async function _splitEditorGroupImpl() {
  // 确保group0存在
  ensureGroup0();

  // 🔧 Task#326：获取活跃group，验证它有完整的 tab + editor
  const activeGroupId = getActiveEditorGroupId();
  const activeGroup = findGroupById(activeGroupId);
  if (!activeGroup) {
    showSimpleToast('找不到活跃编辑器分组', 'warning');
    return;
  }

  // 🔧 Task#326：验证活跃group有有效的标签页和Monaco编辑器
  const currentTab = activeGroup.currentTabId
    ? activeGroup.tabs.find(t => t.id === activeGroup.currentTabId)
    : null;
  if (!currentTab) {
    showSimpleToast('当前没有打开的文件，不可分拆', 'warning');
    return;
  }

  // 🔧 Task#320统一方案：始终使用全局唯一的editor-groups-container
  const groupsContainer = document.getElementById('editor-groups-container');
  if (!groupsContainer) return;

  // 最多支持4个分组
  const existingGroups = groupsContainer.querySelectorAll('.editor-group');
  if (existingGroups.length >= 4) {
    showSimpleToast('最多支持4个编辑器分组', 'info');
    return;
  }

  // 创建新分组 - 使用唯一ID（不能用数组长度，因为splice删除后可能重复）
  const newGroupId = state.editorGroups.reduce(function (max, g) { return Math.max(max, g.id); }, -1) + 1;
  const newGroupEl = document.createElement('div');
  newGroupEl.className = 'editor-group';
  newGroupEl.dataset.groupId = newGroupId;

  const tabsEl = document.createElement('div');
  tabsEl.className = 'editor-tabs';
  tabsEl.id = 'editor-tabs-' + newGroupId;

  const editorContainerEl = document.createElement('div');
  editorContainerEl.id = 'monaco-editor-container-' + newGroupId;
  editorContainerEl.className = 'editor-container';

  newGroupEl.appendChild(tabsEl);
  newGroupEl.appendChild(editorContainerEl);

  // 插入分拆手柄
  const splitHandle = document.createElement('div');
  splitHandle.className = 'editor-split-handle';
  splitHandle.addEventListener('mousedown', (e) => {
    e.preventDefault();
    splitHandle.classList.add('dragging');
    // 找到分拆手柄前后相邻的group元素
    const leftGroup = splitHandle.previousElementSibling;
    const rightGroup = splitHandle.nextElementSibling;
    if (!leftGroup || !rightGroup) return;
    const startX = e.clientX;
    const startWLeft = leftGroup.offsetWidth;
    const startWRight = rightGroup.offsetWidth;
    let _dragRaf = null;  // 🔧 Task#383：rAF节流，拖拽中实时更新编辑器布局

    const onMouseMove = (e) => {
      const delta = e.clientX - startX;
      const newWLeft = Math.max(80, startWLeft + delta);
      const newWRight = Math.max(80, startWRight - delta);
      leftGroup.style.flex = 'none';
      leftGroup.style.width = newWLeft + 'px';
      rightGroup.style.flex = 'none';
      rightGroup.style.width = newWRight + 'px';
      // 🔧 Task#383：拖拽中实时更新Monaco布局，防止横向滚动条闪现
      if (!_dragRaf) {
        _dragRaf = requestAnimationFrame(() => { layoutAllMonacoEditors(); _dragRaf = null; });
      }
    };

    const onMouseUp = () => {
      splitHandle.classList.remove('dragging');
      if (_dragRaf) { cancelAnimationFrame(_dragRaf); _dragRaf = null; }
      document.removeEventListener('mousemove', onMouseMove);
      document.removeEventListener('mouseup', onMouseUp);
      layoutAllMonacoEditors();
    };

    document.addEventListener('mousemove', onMouseMove);
    document.addEventListener('mouseup', onMouseUp);
  });

  // 🔧 Bug6修复：将新列插入到活跃列右边而非末尾
  const activeGroupEl = groupsContainer.querySelector(`[data-group-id="${activeGroupId}"]`);

  if (activeGroupEl) {
    if (activeGroupEl.nextElementSibling) {
      groupsContainer.insertBefore(newGroupEl, activeGroupEl.nextElementSibling);
      groupsContainer.insertBefore(splitHandle, newGroupEl);
    } else {
      groupsContainer.appendChild(splitHandle);
      groupsContainer.appendChild(newGroupEl);
    }
  } else {
    groupsContainer.appendChild(splitHandle);
    groupsContainer.appendChild(newGroupEl);
  }

  // 复制当前tab到新group
  const copiedTab = {
    ...currentTab,
    id: 'tab-split-' + Date.now(),
    groupId: newGroupId
  };

  // 初始化分组数据
  const newGroup = {
    id: newGroupId,
    tabs: [copiedTab],
    currentTabId: copiedTab.id,
    monacoEditor: null,
    containerId: 'monaco-editor-container-' + newGroupId
  };
  state.editorGroups.push(newGroup);

  // 渲染标签页、创建Monaco编辑器、设置共享model
  renderGroupTabs(newGroupId);
  await createGroupEditor(newGroupId);
  if (newGroup.monacoEditor) {
    if (copiedTab.path && window.monaco) {
      const sharedModel = getOrCreateSharedModel(copiedTab.path, copiedTab.content, copiedTab.language);
      if (sharedModel) {
        newGroup.monacoEditor.setModel(sharedModel);
      }
    } else {
      window._suppressContentChangeTracking = true;
      try {
        newGroup.monacoEditor.setValue(copiedTab.content || '');
        if (copiedTab.language && copiedTab.language !== 'plaintext' && window.monaco) {
          window.monaco.editor.setModelLanguage(newGroup.monacoEditor.getModel(), copiedTab.language);
        }
      } finally { window._suppressContentChangeTracking = false; }
    }
  }

  // 更新路径显示
  updateEditorPathDisplay();

  // 🔧 修复：分拆后自动按平均计算重置所有列宽度
  resetGroupWidths(groupsContainer);

  // 🔧 Task#341修复：为新group容器注册mousedown追踪器
  initGroupMousedownTrackers();

  // 🔧 Task#178 Bug1修复：分拆后焦点切换到新分拆列的编辑器
  // 🔧 Task#331修复：同时更新_lastActiveGroupId，确保刷新后能恢复到正确的活跃group
  state._lastActiveGroupId = newGroupId;
  if (newGroup.monacoEditor) {
    setTimeout(() => {
      if (newGroup.monacoEditor) {
        newGroup.monacoEditor.focus();
      }
    }, 50);
  }

  showSimpleToast('编辑器已分拆', 'success');
  saveEditorTabs();
}

/** 重置所有编辑器分拆列为等宽 */
function resetGroupWidths(groupsContainer) {
  if (!groupsContainer) return;
  const groups = groupsContainer.querySelectorAll('.editor-group');
  if (groups.length === 0) return;
  const equalPercent = (100 / groups.length).toFixed(2) + '%';
  groups.forEach(g => {
    g.style.flex = '1 1 ' + equalPercent;
    g.style.width = '';
  });
}

/** 在指定分组创建新文件（统一命名：新文档N） */
async function createNewFileInGroup(groupId) {
  const group = findGroupById(groupId);
  if (!group) return;

  // 从所有分组和editorTabs中查找最大的"新文档"ID，防止重名
  let maxId = 0;
  const newDocRegex = /^新文档(\d+)$/;
  const allTabs = getAllEditorTabs();
  allTabs.forEach(tab => {
    if (tab.name) {
      const match = tab.name.match(newDocRegex);
      if (match) {
        const id = parseInt(match[1], 10);
        if (id > maxId) maxId = id;
      }
    }
  });
  const newId = maxId + 1;

  // 确定新文档的 nest 和路径：继承当前活动标签页，没有则用全局 nest/工作目录
  const currentTab = findTabById(getActiveEditorTabId());
  let baseDir;
  let nest;
  if (currentTab && currentTab.path) {
    nest = currentTab.nest || getSelectedNestName() || null;
    const parts = currentTab.path.split('/');
    parts.pop(); // 移除文件名，保留目录
    baseDir = parts.join('/') || (state.workdir || '/');
  } else {
    nest = getSelectedNestName() || null;
    baseDir = (state.workdir || '').replace(/\/+$/, '') || '/';
  }

  const newTab = {
    id: 'tab-' + Date.now(),
    name: '新文档' + newId,
    content: '',
    language: 'markdown',  // 无扩展名默认markdown
    path: baseDir + '/新文档' + newId + '.md',
    scrollTop: null,
    isNewFile: true,
    isVirtual: false,
    nest: nest,
    workdir: state.workdir || null,  // 🔧 Task#820：记录打开时的工作目录
    groupId: groupId
  };

  // 添加标签页到group（唯一数据源）
  group.tabs.push(newTab);
  group.currentTabId = newTab.id;
  setActiveEditorTabId(newTab.id);

  renderGroupTabs(groupId);
  await createGroupEditor(groupId);
  await switchTabInGroup(groupId, newTab.id);
  updateEditorPathDisplay(newTab);

  // 延迟聚焦到编辑器
  const groupAfter = findGroupById(groupId);
  if (groupAfter && groupAfter.monacoEditor) {
    setTimeout(() => { if (groupAfter.monacoEditor) groupAfter.monacoEditor.focus(); }, 50);
  }
}

/** 获取group0的tabs数组（唯一的标签页数据源）
 *  🔧 Task#340：替代state.editorTabs，消除双数据源同步问题
 */
function getG0Tabs() {
  if (state.editorGroups && state.editorGroups.length > 0) {
    return state.editorGroups[0].tabs;
  }
  return [];
}

/** 获取所有编辑器标签页（包括所有分拆列，去重）
 *  🔧 Task#340：只从editorGroups获取，不使用state.editorTabs
 */
function getAllEditorTabs() {
  const seen = new Set();
  const allTabs = [];
  if (state.editorGroups) {
    for (const g of state.editorGroups) {
      for (const t of g.tabs) {
        if (!seen.has(t.id)) {
          seen.add(t.id);
          allTabs.push(t);
        }
      }
    }
  }
  return allTabs;
}

/** 渲染指定分组的标签页 */
function renderGroupTabs(groupId) {
  const group = findGroupById(groupId);
  if (!group) return;

  const tabsContainer = document.getElementById('editor-tabs-' + groupId);
  if (!tabsContainer) return;

  // 🔧 修复：克隆容器替换，清除所有旧事件监听器，防止drag/drop事件重复绑定
  const newContainer = tabsContainer.cloneNode(false);
  newContainer.id = tabsContainer.id;
  newContainer.className = tabsContainer.className;
  tabsContainer.parentNode.replaceChild(newContainer, tabsContainer);

  group.tabs.forEach(tab => {
    const tabElement = document.createElement('div');
    // 🔧 Task#327：大文件标签页加粗体样式
    const largeFileClass = (typeof isTabLargeFile === 'function' && isTabLargeFile(tab)) ? ' large-file-tab' : '';
    tabElement.className = `editor-tab ${tab.id === group.currentTabId ? 'active' : ''}${largeFileClass}`;
    tabElement.dataset.tabId = tab.id;
    tabElement.dataset.groupId = groupId;
    tabElement.draggable = true;
    // hover hint 显示完整路径（nest + 绝对路径）
    var tabTitle = tab.path || '';
    if (tab.nest && tab.nest !== 'local') tabTitle = '[' + tab.nest + '] ' + tabTitle;
    if (tab.isNewFile && !tabTitle) tabTitle = tab.name;
    tabElement.title = tabTitle;
    tabElement.innerHTML = `<span class="tab-name">${tab.name}</span><button class="tab-close" title="关闭">&times;</button>`;
    tabElement.addEventListener('click', (e) => {
      if (e.target.classList.contains('tab-close')) {
        e.stopPropagation();
        closeTabInGroup(groupId, tab.id);
      } else {
        switchTabInGroup(groupId, tab.id);
      }
    });

    // 拖拽支持：开始拖拽标签页
    tabElement.addEventListener('dragstart', (e) => {
      e.dataTransfer.setData('text/plain', JSON.stringify({ tabId: tab.id, sourceGroupId: groupId }));
      e.dataTransfer.effectAllowed = 'move';
      tabElement.classList.add('dragging');
      // 高亮所有分拆列的标签容器作为放置目标
      document.querySelectorAll('.editor-tabs').forEach(tc => tc.classList.add('drop-target'));
    });
    tabElement.addEventListener('dragend', () => {
      tabElement.classList.remove('dragging');
      document.querySelectorAll('.editor-tabs').forEach(tc => tc.classList.remove('drop-target'));
    });

    newContainer.appendChild(tabElement);
  });

  // 放置目标：允许从其他分拆列拖入标签页（绑定到editor-group整个区域，不只是标签栏）
  const groupContainer = newContainer.closest('.editor-group');
  const dropTarget = groupContainer || newContainer;
  // 使用捕获阶段确保在Monaco等子元素之前处理
  dropTarget.addEventListener('dragenter', (e) => {
    e.preventDefault();  // 必须在dragenter中preventDefault才能允许drop
  }, true);
  dropTarget.addEventListener('dragover', (e) => {
    e.preventDefault();
    e.stopPropagation();
    e.dataTransfer.dropEffect = 'move';
    newContainer.classList.add('drop-hover');
  }, true);
  dropTarget.addEventListener('dragleave', (e) => {
    if (!dropTarget.contains(e.relatedTarget)) {
      newContainer.classList.remove('drop-hover');
    }
  });
  dropTarget.addEventListener('drop', async (e) => {
    e.preventDefault();
    e.stopPropagation();
    newContainer.classList.remove('drop-hover');
    document.querySelectorAll('.editor-tabs').forEach(tc => tc.classList.remove('drop-target'));
    try {
      const data = JSON.parse(e.dataTransfer.getData('text/plain'));
      const { tabId: draggedTabId, sourceGroupId } = data;
      if (sourceGroupId === groupId) return; // 同一列内不处理

      await moveTabBetweenGroups(sourceGroupId, groupId, draggedTabId);
    } catch (err) {
      // 忽略无效拖拽数据
    }
  });

  // 新建标签页按钮
  const newTabBtn = document.createElement('button');
  newTabBtn.className = 'editor-tab-new';
  newTabBtn.textContent = '+';
  newTabBtn.title = '新建文档';
  newTabBtn.addEventListener('click', () => createNewFileInGroup(groupId));
  newContainer.appendChild(newTabBtn);

  // 确保新创建的标签页跟随字体设置
  if (typeof FONT_SETTINGS !== 'undefined') {
    const fontFamilyWithCJK = typeof buildFontFamilyWithCJK === 'function'
      ? buildFontFamilyWithCJK(FONT_SETTINGS.fontFamily)
      : FONT_SETTINGS.fontFamily;
    newContainer.style.fontFamily = fontFamilyWithCJK;
    newContainer.style.fontSize = `${FONT_SETTINGS.fontSizeUI}px`;
  }

  // 🔧 Task#971：每次渲染任意group后，同步更新group1的"全部关闭"按钮
  updateCloseAllButton();
}

/**
 * 🔧 Task#971：所有分拆列标签总数 >= 5 时，在第一个group标签栏左边显示"全部关闭"按钮
 * 一旦显示不再隐藏（即使标签数减少到 < 5），只有关闭全部文件后才消失
 * 注意：renderGroupTabs 每次会 cloneNode 重建DOM，所以靠 state._closeAllButtonShown 标记持久化
 */
function updateCloseAllButton() {
  const firstGroup = state.editorGroups?.[0];
  if (!firstGroup) return;

  const tabsContainer = document.getElementById('editor-tabs-' + firstGroup.id);
  if (!tabsContainer) return;

  // 标记：总数 >= 5 时置 true，由 closeAllTabsInGroup 重置为 false
  if (getAllEditorTabs().length >= 5) {
    state._closeAllButtonShown = true;
  }

  const existing = tabsContainer.querySelector('.editor-tab-close-all');
  if (existing) return; // 已存在则不处理

  if (state._closeAllButtonShown) {
    const closeAllBtn = document.createElement('button');
    closeAllBtn.className = 'editor-tab-close-all';
    closeAllBtn.textContent = 'c';
    closeAllBtn.title = '全部关闭';
    closeAllBtn.addEventListener('click', () => closeAllTabsInGroup());
    tabsContainer.insertBefore(closeAllBtn, tabsContainer.firstChild);
  }
}

/** 在两个分拆列之间移动标签页 */
async function moveTabBetweenGroups(sourceGroupId, targetGroupId, tabId) {
  const sourceGroup = findGroupById(sourceGroupId);
  const targetGroup = findGroupById(targetGroupId);
  if (!sourceGroup || !targetGroup) return;

  const tabIndex = sourceGroup.tabs.findIndex(t => t.id === tabId);
  if (tabIndex === -1) return;

  // 从源列移除标签
  const [movedTab] = sourceGroup.tabs.splice(tabIndex, 1);

  // 检查目标列是否已有同名文件（同列不允许同名）
  const existingInTarget = targetGroup.tabs.find(t => t.name === movedTab.name && t.path && t.path === movedTab.path);
  if (existingInTarget) {
    // 目标列已有同名文件，放回源列
    sourceGroup.tabs.splice(tabIndex, 0, movedTab);
    showSimpleToast('目标列已打开同名文件', 'warning');
    renderGroupTabs(sourceGroupId);
    return;
  }

  // 添加到目标列
  movedTab.groupId = targetGroupId;
  targetGroup.tabs.push(movedTab);
  targetGroup.currentTabId = movedTab.id;

  // 处理源列变空
  if (sourceGroup.tabs.length === 0) {
    if (state.editorGroups.length <= 1) {
      const autoCreateEmptyDoc = state.webuiConfig?.editor?.auto_create_empty_doc ?? false;
      if (autoCreateEmptyDoc) {
        createNewFileInGroup(sourceGroupId);
      }
    } else {
      removeEditorGroup(sourceGroupId);
    }
  } else {
    // 源列切换到下一个标签
    if (sourceGroup.currentTabId === tabId) {
      const nextTab = sourceGroup.tabs[Math.min(tabIndex, sourceGroup.tabs.length - 1)];
      sourceGroup.currentTabId = nextTab.id;
      switchTabInGroup(sourceGroupId, nextTab.id);
    }
    renderGroupTabs(sourceGroupId);
  }

  // 渲染目标列并切换
  renderGroupTabs(targetGroupId);
  await createGroupEditor(targetGroupId);
  await switchTabInGroup(targetGroupId, movedTab.id);
  updateEditorPathDisplay();
  // 🔧 拖拽后进行一致性检查
  validateEditorGroupConsistency();
  saveEditorTabs();
}

/** 为分组确保Monaco编辑器 */
async function createGroupEditor(groupId) {
  const group = findGroupById(groupId);
  if (!group) return;

  // 检查group.monacoEditor是否有效（可能指向已dispose的旧实例）
  if (group.monacoEditor) {
    try {
      // 如果编辑器已被dispose，getDomNode()会返回null
      if (group.monacoEditor.getDomNode()) return;  // 有效，无需重建
    } catch (e) {
      // 已dispose，清除引用
      group.monacoEditor = null;
    }
  }

  // 全局monacoEditor如果有效且是group0的，直接关联
  if (!group.monacoEditor && monacoEditor && groupId === 0) {
    try {
      if (monacoEditor.getDomNode()) {
        group.monacoEditor = monacoEditor;
        return;
      }
    } catch (e) {
      monacoEditor = null;
    }
  }
  if (group.monacoEditor) return;

  await loadMonacoEditor();
  if (!window.monaco) return;

  const container = document.getElementById(group.containerId);
  if (!container) return;

  applyMonacoTheme(state.theme);
  const customThemeName = `peng-${state.theme}`;

  const editor = window.monaco.editor.create(container, {
    value: '',
    language: 'plaintext',
    theme: customThemeName,
    automaticLayout: false,  // 🔧 Task#383：改用自定义ResizeObserver
    fontSize: FONT_SETTINGS.fontSizeEditor,  // 🔧 Task#1035：编辑器字号独立设置
    fontFamily: FONT_SETTINGS.fontFamily,
    minimap: { enabled: true, maxColumn: 50 },
    lineNumbers: 'on',
    scrollBeyondLastLine: false,
    scrollBeyondLastColumn: 0,  // 🔧 Task#383
    wordWrap: 'on',
    wrappingStrategy: 'advanced',  // 🔧 Task#383：高级换行策略，精确处理CJK字符
    wrappingIndent: 'same',
    formatOnPaste: true,
    formatOnType: true,
    largeFileOptimizations: true,  // 🔧 Task#810：启用大文件优化，配合switchTabInGroup中的动态选项调整
    scrollbar: {
      vertical: 'hidden',       // 🔧 Task#411：隐藏垂直滚动条（minimap提供导航）
      verticalScrollbarSize: 0,
      horizontal: 'hidden',     // 🔧 Task#866：默认隐藏横向滚动条，大文件时动态切换为visible
      horizontalScrollbarSize: 0,
    }
  });

  // 绑定 Ctrl+S
  editor.addCommand(window.monaco.KeyMod.CtrlCmd | window.monaco.KeyCode.KeyS, () => {
    saveEditorContent();
  });

  // 🔧 F1 打开命令面板
  editor.addCommand(window.monaco.KeyCode.F1, function () {
    editor.trigger('keyboard', 'editor.action.quickCommand');
  });
  editor.addCommand(window.monaco.KeyMod.CtrlCmd | window.monaco.KeyMod.Shift | window.monaco.KeyCode.KeyP, function () {
    editor.trigger('keyboard', 'editor.action.quickCommand');
  });

  // 🔧 Task#426：修复LSP右键菜单快捷键绑定
  // F12 转到定义（Monaco内置默认Ctrl+F12，覆盖为F12匹配VS Code标准）
  editor.addCommand(window.monaco.KeyCode.F12, function () {
    editor.trigger('peng', 'editor.action.revealDefinition');
  });
  // F2 转到符号
  editor.addCommand(window.monaco.KeyCode.F2, function () {
    editor.trigger('peng', 'editor.action.quickOutline');
  });

  // 🔧 Task#421：hook find/replace 搜索词变化，自动保存搜索历史
  _hookFindStateChanges(editor);

  // 监听内容变化
  editor.onDidChangeModelContent(() => {
    if (window._suppressContentChangeTracking) return;
    const currentTab = group.tabs.find(t => t.id === group.currentTabId);
    // 🔧 Task#421：如果tracker不存在（replay后清除），自动hook
    // Task#421: auto-hook — undo/redo时不创建tracker
    const cm = editor.getModel();
    if (cm && !window._pengUndoRedoing &&
        currentTab && currentTab.path && (!window._pengEditTrackers || !window._pengEditTrackers[currentTab.path])) {
      hookEditTracker(editor, currentTab);
    }
    if (currentTab) {
      currentTab.content = editor.getValue();
      autoSaveToTemp(currentTab);
      // 🔧 Bug4修复：同步同一文件到其他分拆列（同路径文件实时同步编辑）
      syncContentToOtherGroups(group.id, currentTab);
    }
  });

  // 光标位置变化时更新信息显示
  editor.onDidChangeCursorPosition(() => {
    updateEditorCursorInfo();
  });

  // 🔧 Task#383：自定义ResizeObserver + MutationObserver修正minimap位置
  if (typeof _safeLayoutEditor === 'function') {
    var _splitEditorRO = new ResizeObserver(function () { _safeLayoutEditor(editor); });
    _splitEditorRO.observe(container);
    _safeLayoutEditor(editor);
  } else {
    editor.layout();
  }
  if (typeof _setupMinimapFix === 'function') {
    _setupMinimapFix(editor);
  }

  // 🔧 Task#341修复：mousedown追踪已统一到initGroupMousedownTrackers()中管理
  // onDidFocusEditorWidget保留tab切换和路径显示功能
  // 🔧 Task#416：焦点切换时同步nest下拉框和工作目录
  editor.onDidFocusEditorWidget(() => {
    setActiveEditorTabId(group.currentTabId);  // 🔧 Task#334
    updateEditorPathDisplay();
    // 焦点切换到不同分拆列时，同步nest和工作目录
    var focusTab = group.tabs.find(t => t.id === group.currentTabId);
    // 🔧 Task#820：isNewFile标签页也同步工作目录（使用记录的workdir）
    if (focusTab && !focusTab.isContentEditor && !focusTab.isVirtual && focusTab.path) {
      if (typeof _syncNestAndWorkdirForTab === 'function') {
        _syncNestAndWorkdirForTab(focusTab);
      }
    }
  });

  group.monacoEditor = editor;
  // 🔧 Task#326修复：不再覆盖全局monacoEditor！
  // 之前每个新group创建Monaco后都覆盖全局monacoEditor，导致group0的编辑器引用丢失
  // 这就是分拆后"原来的列只有标签没有输入框"的根因
  // 全局monacoEditor只应在group0创建时设置
  if (groupId === 0 || (state.editorGroups.length > 0 && state.editorGroups[0].id === groupId)) {
    monacoEditor = editor;
  }
}

/** 🔧 Bug8修复：共享Monaco Model注册表
 *  同一文件路径对应同一个monaco.editor.ITextModel实例
 *  不同分拆列的同一文档标签页通过setModel共享同一个model，实现自动实时同步
 */
window._sharedModels = window._sharedModels || {};  // path -> { model, language }

/** 获取或创建共享的Monaco Model */
function getOrCreateSharedModel(filePath, content, language) {
  if (!filePath || !window.monaco) return null;
  const key = filePath;
  if (window._sharedModels[key] && window._sharedModels[key].model) {
    // 已有model，如果内容不同则更新
    const existing = window._sharedModels[key].model;
    if (content !== undefined && existing.getValue() !== content) {
      window._suppressContentChangeTracking = true;
      try { existing.setValue(content); } finally { window._suppressContentChangeTracking = false; }
    }
    if (language && language !== 'plaintext') {
      window.monaco.editor.setModelLanguage(existing, language);
    }
    // 🔧 Task#421：确保已有model也有tracker（replay后旧tracker可能失效）
    _hookModelEditTracker(existing, filePath);
    return existing;
  }
  // 创建新model
  const model = window.monaco.editor.createModel(content || '', language || 'plaintext');
  window._sharedModels[key] = { model, language };

  // 🔧 Task#421：为新创建的共享model hook编辑操作追踪
  _hookModelEditTracker(model, filePath);

  return model;
}

/** 从共享Model注册表中移除（当所有引用都关闭时） */
function releaseSharedModel(filePath) {
  if (!filePath) return;
  if (window._sharedModels[filePath]) {
    // 检查是否还有tab在使用此model
    const allTabs = getAllEditorTabs();
    const stillInUse = allTabs.some(t => t.path === filePath);
    if (!stillInUse) {
      try { window._sharedModels[filePath].model.dispose(); } catch (e) { }
      delete window._sharedModels[filePath];
    }
  }
}

/** 🔧 Bug8修复：同步编辑内容到其他分拆列中同路径的文件
 *  使用共享monaco model，不再手动复制内容。
 *  保留此函数作为fallback，用于非共享model场景（如新建文件无path时）
 */
function syncContentToOtherGroups(sourceGroupId, sourceTab) {
  if (!state.editorGroups || state.editorGroups.length <= 1) return;
  if (!sourceTab.path) {
    // 无path的文件（新建文件等），用传统方式复制内容
    const newContent = sourceTab.content;
    state.editorGroups.forEach(group => {
      if (group.id === sourceGroupId) return;
      group.tabs.forEach(tab => {
        if (tab.name === sourceTab.name && tab.id !== sourceTab.id && !tab.path) {
          tab.content = newContent;
          if (group.currentTabId === tab.id && group.monacoEditor) {
            window._suppressContentChangeTracking = true;
            try {
              const currentEditorContent = group.monacoEditor.getValue();
              if (currentEditorContent !== newContent) {
                const pos = group.monacoEditor.getPosition();
                const scrollTop = group.monacoEditor.getScrollTop();
                group.monacoEditor.setValue(newContent || '');
                if (pos) group.monacoEditor.setPosition(pos);
                if (scrollTop != null) group.monacoEditor.setScrollTop(scrollTop);
              }
            } finally {
              window._suppressContentChangeTracking = false;
            }
          }
        }
      });
    });
    return;
  }
  // 有path的文件通过共享model自动同步，只需更新tab.content引用
  const newContent = sourceTab.content;
  state.editorGroups.forEach(group => {
    if (group.id === sourceGroupId) return;
    group.tabs.forEach(tab => {
      if (tab.path === sourceTab.path && tab.id !== sourceTab.id) {
        tab.content = newContent;
      }
    });
  });
}

/** 切换分组中的标签页 */
async function switchTabInGroup(groupId, tabId) {
  const group = findGroupById(groupId);
  if (!group) return;

  const tab = group.tabs.find(t => t.id === tabId);
  if (!tab) return;

  // 保存当前标签页状态
  // 🔧 Bug2修复：如果currentTabId已被外部设置为目标tab（如openFileInGroup中），
  // 说明prevTab就是目标tab自己，此时不应保存（会用编辑器旧内容覆盖API加载的新内容）
  if (group.currentTabId && group.currentTabId !== tabId && group.monacoEditor) {
    const prevTab = group.tabs.find(t => t.id === group.currentTabId);
    if (prevTab && group.monacoEditor.getModel()) {
      prevTab.content = group.monacoEditor.getValue();
      prevTab.scrollTop = group.monacoEditor.getScrollTop();
      // 🔧 Task#327：缓存viewState（光标、滚动、选中、undo/redo）用于持久化恢复
      try {
        prevTab._savedViewState = group.monacoEditor.saveViewState();
      } catch (e) { /* 忽略 */ }
    }
  }

  group.currentTabId = tabId;
  // 🔧 Task#326修复：切换标签页时更新_lastActiveGroupId
  // 否则在多group场景下，点击标签页后点分拆，getActiveEditorGroupId()
  // 返回的是_lastActiveGroupId（旧值），不是当前标签页所在的group
  state._lastActiveGroupId = groupId;
  setActiveEditorTabId(tabId);
  await createGroupEditor(groupId);

  window._suppressContentChangeTracking = true;
  try {
    if (group.monacoEditor) {
      // 🔧 Task#810：大文件性能优化
      // wrappingStrategy:'advanced' 会对所有行做CJK感知换行计算(O(n))，大文件时导致浏览器卡死
      // 策略：大文件setModel前禁用换行和minimap；小文件setModel后再恢复（小内容换行计算很快）
      const LARGE_CONTENT_THRESHOLD = 500 * 1024; // 500KB字符
      const isLargeContent = tab._isLargeFile || (tab.content && tab.content.length > LARGE_CONTENT_THRESHOLD);
      if (isLargeContent) {
        group.monacoEditor.updateOptions({
          readOnly: true,  // 🔧 Task#866：超大文件只读，禁止修改
          wordWrap: 'off',
          wrappingStrategy: 'simple',
          scrollbar: { horizontal: 'visible', horizontalScrollbarSize: 10 },  // 🔧 Task#866：大文件始终显示横向滚动条
        });
      }

      // 🔧 Bug8修复：使用共享model而非setValue复制内容
      if (tab.path && window.monaco) {
        const sharedModel = getOrCreateSharedModel(tab.path, tab.content, tab.language);
        if (sharedModel) {
          group.monacoEditor.setModel(sharedModel);
        } else {
          group.monacoEditor.setValue(tab.content || '');
        }
      } else {
        group.monacoEditor.setValue(tab.content || '');
        if (tab.language) {
          window.monaco.editor.setModelLanguage(group.monacoEditor.getModel(), tab.language);
        }
      }

      // 🔧 Task#810：小文件恢复完整编辑器特性（在setModel之后，此时内容已是新的小内容，换行计算很快）
      if (!isLargeContent) {
        group.monacoEditor.updateOptions({
          readOnly: false,
          minimap: { enabled: true, maxColumn: 50 },
          wordWrap: 'on',
          wrappingStrategy: 'advanced',
          scrollbar: { horizontal: 'hidden', horizontalScrollbarSize: 0 },  // 🔧 Task#866：小文件隐藏横向滚动条
        });
      }

      if (tab.scrollTop != null && typeof tab.scrollTop === 'number') {
        group.monacoEditor.setScrollTop(tab.scrollTop);
      }
      // 🔧 Task#327：恢复该标签页缓存的viewState（光标、选中、undo/redo）
      if (tab._savedViewState) {
        try {
          group.monacoEditor.restoreViewState(tab._savedViewState);
          tab._savedViewState = null;  // 用完清除
        } catch (e) { /* 忽略不兼容的viewState */ }
      }
    }
  } finally {
    window._suppressContentChangeTracking = false;
  }

  // 更新文件路径显示
  updateEditorPathDisplay(tab);

  // 🔧 Task#416：分拆视图切换标签页时，同步nest下拉框和工作目录
  if (!tab.isContentEditor && !tab.isVirtual && tab.path) {
    if (typeof _syncNestAndWorkdirForTab === 'function') {
      _syncNestAndWorkdirForTab(tab);
    }
  }

  renderGroupTabs(groupId);
  // 🔧 Task#178 Bug1修复：延迟聚焦覆盖initFocusManagement的10ms延迟
  if (group.monacoEditor) {
    setTimeout(() => {
      if (group.monacoEditor) {
        group.monacoEditor.focus();
      }
    }, 50);
  }
}

/** 🔧 Bug修复：恢复所有分拆列的宽度比例
 *  根据保存的宽度按比例分配当前容器的实际宽度
 */
function _restoreSplitWidths(savedGroups) {
  if (!savedGroups || savedGroups.length <= 1) return;

  const groupsContainer = getEditorGroupsContainer();
  if (!groupsContainer) return;

  // 收集保存的宽度，计算总比例
  const totalSavedWidth = savedGroups.reduce((sum, g) => sum + (g.width || 0), 0);
  if (totalSavedWidth <= 0) return;  // 没有有效的宽度数据

  const actualTotalWidth = groupsContainer.offsetWidth;
  const handleCount = savedGroups.length - 1;
  const handleWidth = 3;
  const availableWidth = actualTotalWidth - handleWidth * handleCount;

  if (availableWidth <= 0) return;

  // 按比例分配宽度
  savedGroups.forEach(g => {
    if (!g.width) return;
    const groupEl = groupsContainer.querySelector('[data-group-id="' + g.id + '"]');
    if (!groupEl) return;
    const proportionalWidth = Math.round(availableWidth * g.width / totalSavedWidth);
    if (proportionalWidth > 80) {
      groupEl.style.flex = 'none';
      groupEl.style.width = proportionalWidth + 'px';
    }
  });
}

/** 恢复分拆状态（从localStorage保存的数据重建分组）
 *  @param savedGroup 保存的group数据
 *  @param allRestoredTabs restoreEditorTabs恢复的所有原始tab数据，用于查找split副本
 */
async function splitEditorGroupRestore(savedGroup, allRestoredTabs) {
  const isDefault = state.currentLayout === 'default';
  let groupsContainer;

  if (isDefault) {
    const editorView = document.getElementById('editor-view');
    if (!editorView) return;
    // 🔧 使用ensureGroupStructure统一处理group初始化
    ensureGroupStructure(editorView);
    groupsContainer = editorView.querySelector('.editor-groups-container');
  } else {
    groupsContainer = document.getElementById('editor-groups-container');
    // 🔧 Task#343：非默认布局中也需要确保group数据已初始化（使用ensureGroup0统一处理）
    if (!state.editorGroups || state.editorGroups.length === 0) {
      ensureGroup0();
    }
  }
  if (!groupsContainer) return;

  const existingGroups = groupsContainer.querySelectorAll('.editor-group');
  if (existingGroups.length >= 4) return;

  const newGroupId = savedGroup.id;
  const newGroupEl = document.createElement('div');
  newGroupEl.className = 'editor-group';
  newGroupEl.dataset.groupId = newGroupId;

  const tabsEl = document.createElement('div');
  tabsEl.className = 'editor-tabs';
  tabsEl.id = 'editor-tabs-' + newGroupId;

  const editorContainerEl = document.createElement('div');
  editorContainerEl.id = 'monaco-editor-container-' + newGroupId;
  editorContainerEl.className = 'editor-container';

  newGroupEl.appendChild(tabsEl);
  newGroupEl.appendChild(editorContainerEl);

  // 分拆手柄
  const splitHandle = document.createElement('div');
  splitHandle.className = 'editor-split-handle';
  splitHandle.addEventListener('mousedown', (e) => {
    e.preventDefault();
    splitHandle.classList.add('dragging');
    const leftGroup = splitHandle.previousElementSibling;
    const rightGroup = splitHandle.nextElementSibling;
    if (!leftGroup || !rightGroup) return;
    const startX = e.clientX;
    const startWLeft = leftGroup.offsetWidth;
    const startWRight = rightGroup.offsetWidth;
    let _dragRaf = null;  // 🔧 Task#383：rAF节流
    const onMouseMove = (e) => {
      const delta = e.clientX - startX;
      const newWLeft = Math.max(80, startWLeft + delta);
      const newWRight = Math.max(80, startWRight - delta);
      leftGroup.style.flex = 'none';
      leftGroup.style.width = newWLeft + 'px';
      rightGroup.style.flex = 'none';
      rightGroup.style.width = newWRight + 'px';
      // 🔧 Task#383：拖拽中实时更新Monaco布局
      if (!_dragRaf) {
        _dragRaf = requestAnimationFrame(() => { layoutAllMonacoEditors(); _dragRaf = null; });
      }
    };
    const onMouseUp = () => {
      splitHandle.classList.remove('dragging');
      if (_dragRaf) { cancelAnimationFrame(_dragRaf); _dragRaf = null; }
      document.removeEventListener('mousemove', onMouseMove);
      document.removeEventListener('mouseup', onMouseUp);
      layoutAllMonacoEditors();
    };
    document.addEventListener('mousemove', onMouseMove);
    document.addEventListener('mouseup', onMouseUp);
  });

  groupsContainer.appendChild(splitHandle);
  groupsContainer.appendChild(newGroupEl);

  // 🔧 Task#327/340修复：savedGroup可能是新格式（只有tabIds）或旧格式（完整tabs）
  // 新格式时需要从所有group中查找完整tab对象
  let restoredTabs;
  if (savedGroup.tabs && savedGroup.tabs.length > 0) {
    // 旧格式：直接有完整的tabs数据
    restoredTabs = await Promise.all((savedGroup.tabs || []).map(async (tab) => {
      let content = tab.content;
      // 对非新文件、非虚拟文件，优先从服务器或临时文件重新加载
      if (!tab.isNewFile && !tab.isVirtual && tab.path) {
        const tempContent = await loadFromTemp(tab.id);
        if (tempContent !== undefined && tempContent !== null) {
          content = tempContent;
        } else {
          try {
            const reloadNestParam = tab.useLocal ? '' : (tab.nest ? `&nest=${encodeURIComponent(tab.nest)}` : '');
            const response = await fetch(`/api/files/read?path=${encodeURIComponent(tab.path)}${reloadNestParam}`);
            const result = await response.json();
            // 🔧 Bug6修复：/api/files/read 没有 success 字段，只有 content/language/path
            if (response.ok && result.content !== undefined) {
              content = result.content;
            }
          } catch (e) {
            // 静默处理，使用localStorage中的内容
          }
        }
      } else if (tab.isNewFile) {
        // 新文件尝试从临时文件恢复
        const tempContent = await loadFromTemp(tab.id);
        if (tempContent !== undefined && tempContent !== null) {
          content = tempContent;
        }
      }
      return { ...tab, content: content };
    }));
  } else if (savedGroup.tabIds && savedGroup.tabIds.length > 0) {
    // 🔧 Task#326修复：优先从allRestoredTabs（原始恢复数据）中查找tab对象
    // split副本tab不在group0.tabs中，findTabById会找不到
    restoredTabs = [];
    for (const tabId of savedGroup.tabIds) {
      // 先从原始恢复数据中查找（split副本tab在这里）
      const found = allRestoredTabs
        ? allRestoredTabs.find(t => t.id === tabId)
        : null;
      // 找不到再从groups中查找
      restoredTabs.push(found || findTabById(tabId));
    }
    restoredTabs = restoredTabs.filter(Boolean);
  } else {
    restoredTabs = [];
  }

  const newGroup = {
    id: newGroupId,
    tabs: restoredTabs,
    currentTabId: savedGroup.currentTabId,
    monacoEditor: null,
    containerId: savedGroup.containerId || ('monaco-editor-container-' + newGroupId)
  };
  state.editorGroups.push(newGroup);

  renderGroupTabs(newGroupId);

  // 创建Monaco编辑器
  await createGroupEditor(newGroupId);

  // 恢复内容 - 🔧 Bug8修复：使用共享model
  if (newGroup.currentTabId && newGroup.monacoEditor) {
    const tab = newGroup.tabs.find(t => t.id === newGroup.currentTabId);
    if (tab) {
      if (tab.path && window.monaco) {
        const sharedModel = getOrCreateSharedModel(tab.path, tab.content, tab.language);
        if (sharedModel) {
          newGroup.monacoEditor.setModel(sharedModel);
        }
      } else {
        window._suppressContentChangeTracking = true;
        try {
          newGroup.monacoEditor.setValue(tab.content || '');
          if (tab.language && tab.language !== 'plaintext') {
            window.monaco.editor.setModelLanguage(newGroup.monacoEditor.getModel(), tab.language);
          }
        } finally {
          window._suppressContentChangeTracking = false;
        }
      }
    }
  }

  // 宽度恢复在 restoreEditorTabs 中统一处理（所有group恢复完成后按比例分配）
}
