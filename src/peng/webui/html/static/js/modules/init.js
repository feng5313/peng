// ==================== 初始化 ====================

document.addEventListener('DOMContentLoaded', async () => {
  // 🔧 Task#215: 性能计时 - 测量初始化各阶段耗时
  const _t0 = performance.now();
  const _timing = { start: _t0 };
  const _mark = (name) => { _timing[name] = (performance.now() - _t0).toFixed(0); };
  window.__initTiming = _timing;

  try {
    initTheme();
    _mark('theme');

    // 🔧 性能优化：合并config加载为一次请求（_fetchConfig内部缓存）
    // loadConfig + loadWebUIConfig 并行执行（共享_fetchConfig缓存，只有一次HTTP请求）
    await Promise.all([loadConfig(), loadWebUIConfig()]);
    _mark('config');

    // 初始化xterm终端（在配置加载之后，确保 scrollback 有效）
    initOutput();
    _mark('xterm');

    // 加载字体设置（从localStorage，无网络请求）
    loadFontSettings();

    // 初始化WebSocket（无网络依赖，异步建立连接）
    initWebSocket();
    _mark('ws_init');

    // 🔧 恢复 undo 栈（从 localStorage）
    try {
      const savedUndoStack = localStorage.getItem('peng_undo_stack');
      if (savedUndoStack) {
        state.undoStack = JSON.parse(savedUndoStack);
      }
    } catch (e) {
      console.warn('[undo] 恢复失败:', e);
      state.undoStack = [];
    }

    // 🔧 优化(task#113)：并行化数据加载
    // loadInputHistory、loadProviders、loadProjectStructure、loadMessages 四个请求互不依赖
    // loadTasks 依赖 loadMessages（需要 displayedMessageIds）
    const [, , ,] = await Promise.all([
      loadInputHistory(),
      loadProviders(),
      loadProjectStructure(),  // 加载项目目录结构（用于路径超链接二次过滤）
      loadMessages(false, true),  // 首次加载：获取完整数据（含tree）
    ]);
    _mark('parallel_load');
    // loadTasks 必须在 loadMessages 之后执行（依赖 displayedMessageIds）
    await loadTasks();
    _mark('tasks');

    // 🔧 页面初始化后，从最新活跃事务同步 provider 到大模型按钮图标
    syncCurrentProviderFromTasks();

    // 恢复折叠展开状态（从已加载的消息/事务数据中提取，无网络请求）
    restoreCollapseStates();

    // 渲染消息树
    const messageTreeContainer = document.getElementById('message-tree');
    if (messageTreeContainer) {
      messageTreeContainer.innerHTML = '';
    }
    renderMessageTree();

    // 恢复事务折叠状态（从已加载的事务数据中提取，无网络请求）
    // 注意：事务折叠状态恢复需要在 loadTasks 之后
    restoreTaskCollapseStates();
    const taskTreeContainer = document.getElementById('task-tree');
    if (taskTreeContainer) {
      taskTreeContainer.innerHTML = '';
    }
    renderTaskTree();

    // 🔧 Task#153：从配置恢复选中状态（优先于自动选最新消息）
    // 如果配置中记录了选中的消息ID，优先恢复；否则保持原有的自动选最新逻辑
    if (state.selectedMessageIdFromConfig) {
      const configMsgId = state.selectedMessageIdFromConfig;
      // 验证该消息是否存在于当前消息列表中
      const configMsg = findMessageByIdRecursive(state.messages, configMsgId);
      if (configMsg) {
        // 如果配置中还有事务ID，优先选中事务（事务选中会联动选中关联消息）
        if (state.selectedTaskIdFromConfig) {
          const configTaskId = state.selectedTaskIdFromConfig;
          const allTasks = flattenTasks(state.tasks);
          const configTask = allTasks.find(t => t.id === configTaskId);
          if (configTask) {
            selectTask(configTaskId, configTask.message_id);  // 不 await
            // 选中事务后，如果配置中还有分支消息ID，恢复分支消息选中
            if (state.selectedBranchMessageIdFromConfig) {
              const configBranchMsgId = state.selectedBranchMessageIdFromConfig;
              const configBranchMsg = findMessageByIdRecursive(state.messages, configBranchMsgId);
              if (configBranchMsg) {
                selectMessageBranch(configMsgId, configBranchMsgId);  // 不 await
              }
            }
          } else {
            // 事务不存在，回退到选中消息
            // 如果配置中还有分支消息ID，选中分支消息
            if (state.selectedBranchMessageIdFromConfig) {
              const configBranchMsgId = state.selectedBranchMessageIdFromConfig;
              const configBranchMsg = findMessageByIdRecursive(state.messages, configBranchMsgId);
              if (configBranchMsg) {
                selectMessageBranch(configMsgId, configBranchMsgId);  // 不 await
              } else {
                selectMessage(configMsgId);  // 不 await
              }
            } else {
              selectMessage(configMsgId);  // 不 await
            }
          }
        } else if (state.selectedBranchMessageIdFromConfig) {
          // 没有事务ID但有分支消息ID，选中分支消息
          const configBranchMsgId = state.selectedBranchMessageIdFromConfig;
          const configBranchMsg = findMessageByIdRecursive(state.messages, configBranchMsgId);
          if (configBranchMsg) {
            selectMessageBranch(configMsgId, configBranchMsgId);  // 不 await
          } else {
            selectMessage(configMsgId);  // 不 await
          }
        } else {
          selectMessage(configMsgId);  // 不 await
        }
      } else {
        // 配置中的消息不存在，回退到自动选最新
        if (state.messages.length > 0) {
          const latestMsg = state.messages[0];
          selectMessage(latestMsg.id);  // 不 await
        }
      }
      // 清除配置恢复标记，避免后续干扰
      state.selectedMessageIdFromConfig = null;
      state.selectedTaskIdFromConfig = null;
      state.selectedBranchMessageIdFromConfig = null;
    } else if (state.messages.length > 0) {
      // 原有逻辑：自动选中最近的消息
      const latestMsg = state.messages[0];
      selectMessage(latestMsg.id);  // 不 await
    }

    // 🔧 初始化远程终端（不 await，避免 WebSocket 连接阻塞后续初始化）
    initRemoteTerminal();  // 不 await

    initEventListeners();
    initFileUpload();
    initFocusManagement();
    initOutputFilters();  // 初始化输出来源过滤器
    initOutputActions();  // 初始化输出操作按钮
    initOutputKeywordHighlight();  // 初始化xterm关键词高亮输入框
    if (typeof initOutputSearch === 'function') initOutputSearch();  // 初始化输出搜索
    initViewTabs();  // 初始化视图标签页切换
    initLayoutSelector();  // 初始化布局选择器
    initTextSelectionCopy();  // 初始化文本选择自动复制功能
    initCustomTooltip();  // 初始化自定义tooltip（替代原生title，跟随字体设置）
    initTaskIdHoverTooltip();  // 初始化事务ID hover tooltip（上下文长度和费用）
    initTaskStatusHoverTooltip();  // 初始化运行中状态图标 hover tooltip（provider吞吐率）

    // 🔧 Task#327修复：restoreViewTab内部会调用switchViewTab→restoreEditorTabs
    // 必须await确保restoreEditorTabs完成后再判断是否需要兜底
    // 否则会出现竞态：全局currentTabId指向幽灵tab（restoreTabs还没完成就被其他代码覆盖）
    await restoreViewTab();

    // 🔧 Task#412修复：只在editor视图下才兜底调用restoreEditorTabs
    // 非editor视图（output/terminal）Monaco未加载，restoreEditorTabs会因
    // "monaco is not defined"报错。这些视图下标签页延迟到switchViewTab('editor')时恢复
    if (state.currentViewTab === 'editor' && getG0Tabs().length === 0) {
      await restoreEditorTabs();
    }

    // 🔧 设置拖动勾选框的初始状态
    const enableDragToggle = document.getElementById('enable-drag-toggle');
    const enableTaskDragToggle = document.getElementById('enable-task-drag-toggle');
    if (enableDragToggle) {
      enableDragToggle.checked = state.enableDrag;
    }
    if (enableTaskDragToggle) {
      enableTaskDragToggle.checked = state.enableDrag;
    }

    // 恢复侧边栏宽度和初始化拖拽
    restoreSidebarWidths();
    initResizeHandles();

    // 启动消息和事务列表定时刷新
    startMessagesRefresh();
    startTasksRefresh();

    // 🔧 Task#215: 初始化性能计时（可通过 window.__initTiming 查询）
    _mark('done');
  } catch (error) {
    console.error('❌ 初始化失败:', error);
    showError('初始化失败', error);
  }
});

