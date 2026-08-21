// ==================== 事务管理 ====================

// 事务刷新定时器
let tasksRefreshTimer = null;
let messagesRefreshTimer = null;
let lastTasksData = null;

// 请求节流标记：WebSocket 触发加载后，跳过下一次轮询（500ms 窗口）
let skipNextMessagesPoll = false;
let skipNextTasksPoll = false;

// 加载 WebUI 配置（复用配置缓存，避免重复请求 /api/config）
async function loadWebUIConfig() {
  const config = await _fetchConfig();
  state.webuiConfig.messages_refresh_interval = config.messages_refresh_interval;
  state.webuiConfig.tasks_refresh_interval = config.tasks_refresh_interval;

  // 加载 xterm 配置
  if (config.xterm) {
    state.webuiConfig.xterm.scrollback = config.xterm.scrollback;
    state.webuiConfig.xterm.show_line_numbers = config.xterm.show_line_numbers !== false;
  }

  // 加载内容截断配置
  if (config.content_trunc_lines !== undefined) {
    state.webuiConfig.content_trunc_lines = config.content_trunc_lines;
  }
  if (config.content_trunc_chars !== undefined) {
    state.webuiConfig.content_trunc_chars = config.content_trunc_chars;
  }
  // 加载字体配置
  if (config.font) {
    if (config.font.family) state.webuiConfig.font.family = config.font.family;
    if (config.font.size !== undefined) state.webuiConfig.font.size = config.font.size;
    if (config.font.size_xterm !== undefined) state.webuiConfig.font.size_xterm = config.font.size_xterm;
    if (config.font.size_editor !== undefined) state.webuiConfig.font.size_editor = config.font.size_editor;
  }
  // 加载侧边栏宽度配置
  if (config.sidebar_left_width !== undefined) {
    state.webuiConfig.sidebar_left_width = config.sidebar_left_width;
  }
  if (config.sidebar_right_width !== undefined) {
    state.webuiConfig.sidebar_right_width = config.sidebar_right_width;
  }

  // 🔧 Task#262：加载大文件编辑器加载配置
  if (config.large_file_threshold !== undefined) {
    state.webuiConfig.large_file_threshold = config.large_file_threshold;
  }
  if (config.large_file_parallel_threshold !== undefined) {
    state.webuiConfig.large_file_parallel_threshold = config.large_file_parallel_threshold;
  }
  if (config.large_file_parallel_concurrency !== undefined) {
    state.webuiConfig.large_file_parallel_concurrency = config.large_file_parallel_concurrency;
  }
  if (config.large_file_chunk_size !== undefined) {
    state.webuiConfig.large_file_chunk_size = config.large_file_chunk_size;
  }

  // 🔧 Task#418：加载编辑器配置
  if (config.editor) {
    if (!state.webuiConfig.editor) state.webuiConfig.editor = {};
    if (config.editor.emoji_text_mode !== undefined) {
      state.webuiConfig.editor.emoji_text_mode = config.editor.emoji_text_mode;
    }
  }

  // 加载工作目录配置
  if (config.workdir) {
    state.workdir = config.workdir;
  }

  // 🔧 加载项目根目录（用于选中消息/事务时 workdir 回退）
  if (config.project_root) {
    state.projectRoot = config.project_root;
  }

  // 🔧 Task#153：加载选中的消息/事务ID（刷新页面后恢复选中状态）
  if (config.selected_message_id !== undefined) {
    state.selectedMessageIdFromConfig = config.selected_message_id;
  }
  if (config.selected_task_id !== undefined) {
    state.selectedTaskIdFromConfig = config.selected_task_id;
  }
  if (config.selected_branch_message_id !== undefined) {
    state.selectedBranchMessageIdFromConfig = config.selected_branch_message_id;
  }

  // 🔧 Task#228：加载顶置列表
  state.toppedMessageIds = new Set(config.topped_message_ids || []);
  state.toppedTaskIds = new Set(config.topped_task_ids || []);

  // 加载输出来源过滤配置
  if (config.output_source_filters) {
    for (const [source, filterConfig] of Object.entries(config.output_source_filters)) {
      if (state.outputSourceFilters.hasOwnProperty(source)) {
        state.outputSourceFilters[source] = filterConfig.enabled !== false;
      }
    }
    // 同步更新checkbox视觉状态（如果DOM已存在）
    const checkboxes = document.querySelectorAll('.filter-checkbox input[type="checkbox"]');
    checkboxes.forEach(checkbox => {
      const source = checkbox.dataset.source;
      if (source && state.outputSourceFilters.hasOwnProperty(source)) {
        checkbox.checked = state.outputSourceFilters[source];
      }
    });
  }

  // 初始化工作目录输入框
  const workdirInput = document.getElementById('workdir-input');
  if (workdirInput && state.workdir) {
    workdirInput.value = toNativeDisplayPath(state.workdir);
  }

  // 加载主题配置
  if (config.themes) {
    // 保存主题配置到 state
    state.themeConfig = config.themes;

    // 应用 dark 主题
    if (config.themes.dark) {
      // 应用 xterm 配置
      if (config.themes.dark.xterm) {
        XTERM_THEMES.dark = { ...XTERM_THEMES.dark, ...config.themes.dark.xterm };
      }
    }

    // 应用 light 主题
    if (config.themes.light) {
      // 应用 xterm 配置
      if (config.themes.light.xterm) {
        XTERM_THEMES.light = { ...XTERM_THEMES.light, ...config.themes.light.xterm };
      }
    }

    // 应用当前主题的所有配置
    applyThemeConfig(state.theme);
  }
}

// 应用主题配置（CSS 变量 + xterm + 图标）
function applyThemeConfig(theme) {
  const themeConfig = state.themeConfig?.[theme];
  if (!themeConfig) return;

  // 应用 UI 颜色到 CSS 变量
  if (themeConfig.ui) {
    const ui = themeConfig.ui;
    const root = document.documentElement;

    // 映射配置键到 CSS 变量名
    const cssVarMap = {
      'bg_primary': '--bg-primary',
      'bg_secondary': '--bg-secondary',
      'bg_tertiary': '--bg-tertiary',
      'bg_hover': '--bg-hover',
      'text_primary': '--text-primary',
      'text_secondary': '--text-secondary',
      'text_muted': '--text-muted',
      'border_color': '--border-color',
      'accent_color': '--accent-color',
      'accent_hover': '--accent-hover',
      'success_color': '--success-color',
      'warning_color': '--warning-color',
      'error_color': '--error-color',
      'link_color': '--link-color',
      'task_selection_border_color': '--task-selection-border-color',
      'task_selection_border_rgba': '--task-selection-border-rgba',
      'message_selection_border_color': '--message-selection-border-color',
      'message_selection_border_rgba': '--message-selection-border-rgba'
    };

    // 应用所有 CSS 变量（设置到 body 上，优先级高于 CSS 类）
    const targetEl = document.body;
    for (const [key, varName] of Object.entries(cssVarMap)) {
      if (ui[key]) {
        targetEl.style.setProperty(varName, ui[key]);
      }
    }
  }

  // 应用 xterm 主题
  if (state.output && XTERM_THEMES[theme]) {
    state.output.options.theme = { ...XTERM_THEMES[theme] };
  }

  // 应用 output 背景色
  const outputBg = themeConfig.xterm?.background;
  if (outputBg) {
    let styleEl = document.getElementById('webui-theme-override');
    if (!styleEl) {
      styleEl = document.createElement('style');
      styleEl.id = 'webui-theme-override';
      document.head.appendChild(styleEl);
    }
    styleEl.textContent = `.${theme}-theme .output { background-color: ${outputBg}; }`;
  }

  // 更新静态图标
  updateStaticIcons(theme);
}

// 应用 Monaco Editor 自定义主题（使用 webui.json 中的 bg_secondary）
function applyMonacoTheme(theme) {
  if (!window.monaco) return;

  const themeConfig = state.themeConfig?.[theme];
  if (!themeConfig || !themeConfig.ui) return;

  const bgColor = themeConfig.ui.bg_primary;
  const textColor = themeConfig.ui.text_primary;
  const lineHighlightBg = themeConfig.ui.bg_tertiary || themeConfig.ui.bg_secondary;
  const selectionBg = themeConfig.ui.accent_color || '#3886af';

  // 定义自定义主题
  const customThemeName = `peng-${theme}`;
  monaco.editor.defineTheme(customThemeName, {
    base: theme === 'dark' ? 'vs-dark' : 'vs',
    inherit: true,
    rules: [
      { token: '', foreground: textColor, background: bgColor }
    ],
    colors: {
      'editor.background': bgColor,
      'editor.foreground': textColor,
      'editor.lineHighlightBackground': lineHighlightBg,
      'editor.selectionBackground': `${selectionBg}80`,
      'editorCursor.foreground': textColor,
      'editorLineNumber.foreground': themeConfig.ui.text_muted || '#6e7681',
      // 滚动条颜色（必须定义，否则verticalScrollbarSize配置不会生效）
      'editor.scrollbarSlider.background': themeConfig.ui.border_color || '#586069',
      'editor.scrollbarSlider.hoverBackground': themeConfig.ui.text_secondary || '#4a525a',
      'editor.scrollbarSlider.activeBackground': themeConfig.ui.text_primary || '#3c434a'
    }
  });

  // 应用自定义主题
  monaco.editor.setTheme(customThemeName);
}

// 获取图标（从当前主题配置中获取）
function getIcon(name, defaultIcon = '') {
  const themeConfig = state.themeConfig?.[state.theme];
  if (!themeConfig) return defaultIcon;
  const key = `icon_${name}`;
  return themeConfig[key] || defaultIcon;
}

// 从icon字符串中提取emoji图标（去除文字部分）
function extractIcon(iconString, defaultIcon = '') {
  if (!iconString) return defaultIcon;
  // 提取第一个unicode字符（emoji）
  // 正则匹配单个emoji（包括复合emoji）
  const emojiMatch = iconString.match(/^[\p{Emoji}\p{Emoji_Component}\p{Emoji_Presentation}]+/u);
  return emojiMatch ? emojiMatch[0] : defaultIcon;
}

// 更新 HTML 中的静态图标
function updateStaticIcons(theme) {
  const themeConfig = state.themeConfig?.[theme];
  if (!themeConfig) return;

  // 帮助按钮使用 favicon 图标，无需主题切换

  // 更新附件按钮
  const attachBtn = document.getElementById('attach-btn');
  if (attachBtn) {
    attachBtn.textContent = themeConfig.icon_attach_file || '📂';
  }

  // 更新新消息按钮
  const newMsgBtn = document.getElementById('new-msg-btn');
  if (newMsgBtn) {
    newMsgBtn.textContent = themeConfig.icon_new_message || '✨';
  }

  // 更新主题切换按钮
  const themeToggle = document.getElementById('theme-toggle');
  if (themeToggle) {
    themeToggle.textContent = theme === 'dark'
      ? (themeConfig.icon_theme_dark || '🌙')
      : (themeConfig.icon_theme_light || '☀️');
  }

  // 更新消息标题
  const messagesTitle = document.querySelector('.sidebar-left .sidebar-header h2');
  if (messagesTitle) {
    messagesTitle.textContent = themeConfig.icon_messages_title || '📜 消息';
  }

  // 更新事务标题
  const tasksTitle = document.querySelector('.sidebar-right .sidebar-header h2');
  if (tasksTitle) {
    tasksTitle.textContent = themeConfig.icon_tasks_title || '🎋 事务';
  }
}

// 启动消息列表定时刷新
function startMessagesRefresh() {
  if (messagesRefreshTimer) return;  // 避免重复启动

  const interval = state.webuiConfig.messages_refresh_interval;
  messagesRefreshTimer = setInterval(() => {
    // 🔧 修复性能：页面不可见时跳过轮询
    if (document.hidden) return;
    if (skipNextMessagesPoll) {
      // WebSocket 已触发加载，跳过本次轮询
      skipNextMessagesPoll = false;
      return;
    }
    // 🔧 修复：WebSocket断开时跳过轮询，避免浏览器console疯狂报错
    if (!state.ws || state.ws.readyState !== WebSocket.OPEN) {
      return;
    }
    loadMessages();
  }, interval);
}

// 停止消息刷新
function stopMessagesRefresh() {
  if (messagesRefreshTimer) {
    clearInterval(messagesRefreshTimer);
    messagesRefreshTimer = null;
  }
}

// 🔧 Task #182：启动事务列表定时刷新
// WebSocket连接时使用慢速兜底轮询（30秒），WebSocket断开时使用原始间隔
const TASKS_SLOW_INTERVAL = 30000;  // WebSocket连接时的兜底轮询间隔（30秒）
function startTasksRefresh(forceInterval) {
  // 先停止已有定时器（切换轮询间隔时需要重建）
  if (tasksRefreshTimer) {
    clearInterval(tasksRefreshTimer);
    tasksRefreshTimer = null;
  }

  // WebSocket连接时使用慢速兜底，断开时使用配置间隔
  const wsConnected = state.ws && state.ws.readyState === WebSocket.OPEN;
  const interval = forceInterval || (wsConnected ? TASKS_SLOW_INTERVAL : state.webuiConfig.tasks_refresh_interval);
  tasksRefreshTimer = setInterval(() => {
    // 🔧 修复性能：页面不可见时跳过轮询
    if (document.hidden) return;
    if (skipNextTasksPoll) {
      // WebSocket 已触发加载，跳过本次轮询
      skipNextTasksPoll = false;
      return;
    }
    loadTasks();
  }, interval);
}

// 停止事务刷新
function stopTasksRefresh() {
  if (tasksRefreshTimer) {
    clearInterval(tasksRefreshTimer);
    tasksRefreshTimer = null;
  }
}

/**
 * 🔧 Task #112：从后端事务数据恢复折叠状态
 * 
 * 只对尚未在前端手动操作过（不在 expandedTasks 也不在 collapsedTasks）的事务，
 * 根据后端数据的 collapsed 字段设置展开/折叠状态。
 * 这样既能确保首次加载时使用正确的折叠状态，又不会覆盖用户的手动操作。
 */
function _restoreTaskCollapseStatesFromData(tasks) {
  try {
    if (!tasks || tasks.length === 0) return;
    const allTasks = flattenTasks(tasks);
    for (const task of allTasks) {
      // 只处理尚未在前端操作过的事务（不在任何状态 Map 中）
      if (!state.expandedTasks.has(task.id) && !state.collapsedTasks.has(task.id)) {
        if (task.collapsed) {
          state.collapsedTasks.set(task.id, Date.now());
        } else {
          state.expandedTasks.set(task.id, Date.now());
        }
      }
    }
  } catch (e) {
    console.warn('从数据恢复事务折叠状态失败:', e);
  }
}

async function loadTasks() {
  try {
    // 🔧 设计原则：右边事务栏列表的唯一来源是左边消息栏显示的消息列表
    // 任何时候左边显示的消息列表有变更，右边事务列表就从消息显示列表获取所有显示的根消息id
    // 将前端当前显示的根消息ID列表传给后端，让后端只返回这些消息关联的事务
    const displayedIds = state.displayedMessageIds;
    // include_completed=true：显示已完成事务（message_ids 已限制范围，数据量可控）
    // 搜索模式下（有关键词）强制包含已归档事务，否则根据 showArchived 状态决定
    const taskIncludeArchived = state._taskSearchIncludedArchived || state.showArchived;

    let url = '/api/tasks?for_displayed_messages=true&include_completed=true';
    // 将当前显示的根消息ID列表传给后端（搜索过滤后的事务来源）
    // 只有当 displayedMessageIds 被 renderMessageTree 更新过（非初始空Set）时才传
    // 初始空Set表示尚未渲染过消息树，此时让后端用默认逻辑
    const _renderUpdated = displayedIds && (displayedIds.size > 0 || state._messageTreeRendered);
    if (_renderUpdated) {
      url += '&message_ids=' + Array.from(displayedIds).join(',');
    }
    if (taskIncludeArchived) {
      url += '&include_archived=true';
    }
    const response = await fetch(url);
    if (!response.ok) throw new Error('获取事务失败');

    const tasks = await response.json();

    // 保存旧的事务ID集合（用于检测新事务）
    const oldTaskIds = new Set(flattenTasks(state.tasks || []).map(t => t.id));

    // 后端已根据链首消息过滤，直接使用
    let filteredTasks = tasks;

    // 🔧 Task#338：客户端侧归档过滤兜底
    // loadTasks() 可能在 loadMessages() 完成 displayMessageIds 更新之前被调用（竞态），
    // 导致后端仍返回已归档消息关联的事务。前端侧二次过滤确保已归档事务不显示。
    if (!state.showArchived) {
      filteredTasks = filteredTasks.filter(t => !t.is_archived);
    }

    // 清理过期的事务展开记录
    cleanupExpandedTasks(tasks);

    // 🔧 修复 Task #112：每次加载事务数据时，从后端数据恢复折叠状态
    // 确保即使存在竞态条件（如 WebSocket onopen 触发的 loadTasks 在 restoreTaskCollapseStates 之前），
    // 每次渲染都使用正确的折叠状态，避免旧事务（如事务#1）因时间规则被错误折叠
    _restoreTaskCollapseStatesFromData(tasks);

    // 只对顶层事务排序：新事务在上（降序），不破坏事务树结构，但事务#1始终在最上面
    const task1 = filteredTasks.find(t => t.id === 1);
    const otherTasks = filteredTasks.filter(t => t.id !== 1);
    otherTasks.sort((a, b) => {
      const timeA = new Date(a.created_at || a.timestamp || 0).getTime();
      const timeB = new Date(b.created_at || b.timestamp || 0).getTime();
      return timeB - timeA;  // 降序：新事务在上
    });
    if (task1) {
      filteredTasks = [task1, ...otherTasks];
    } else {
      filteredTasks = otherTasks;
    }

    // 比较数据，只有变化时才更新DOM（避免闪烁）
    // 使用 stripVolatileFieldsForCompare 剔除 heartbeat_at 等易变字段，
    // 避免心跳更新导致每秒触发无意义的DOM重渲染
    const tasksJson = stripVolatileFieldsForCompare(filteredTasks);
    if (tasksJson !== lastTasksData) {
      state.tasks = filteredTasks;
      lastTasksData = tasksJson;

      // 渲染事务树
      renderTaskTree();

      // 恢复选中状态（因为重新渲染会清除选中状态）
      if (state.currentTaskId) {
        document.querySelectorAll('.task-chain').forEach(el => {
          const head = el.querySelector('.task-chain-head');
          if (head) {
            el.classList.toggle('active', head.dataset.id == state.currentTaskId);
          }
        });
      }

      // 🔧 修复(Task#88)：检测新事务：如果属于当前选中的消息，且当前没有选中具体事务，则自动订阅
      // 根因：sendMessage → selectMessage 时事务尚未创建，state.currentTaskId=null
      // 之后事务创建 → loadTasks 检测到新事务但未设置 currentTaskId
      // 导致 appendOutput 中 shouldAppend=false（null !== 实际taskId），xterm输出停滞
      if (state.currentMessageId !== null && state.currentTaskId === null) {
        const newTasks = flattenTasks(filteredTasks)
          .filter(t => t.message_id === state.currentMessageId && !oldTaskIds.has(t.id));

        if (newTasks.length > 0) {
          // 自动选中第一个新事务并订阅输出
          const newTask = newTasks[0];
          state.currentTaskId = newTask.id;

          // 更新事务列表的选中状态
          document.querySelectorAll('.task-chain').forEach(el => {
            const head = el.querySelector('.task-chain-head');
            if (head) {
              el.classList.toggle('active', head.dataset.id == newTask.id);
            }
          });

          // 订阅输出（不 await，避免阻塞 loadTasks）
          subscribeOutput(0, hasOutputHistoryCache(newTask.id));

          // 更新输出标题
          updateOutputTitle(newTask.id, state.currentMessageId);

          console.log(`[Task#88] 自动订阅新事务: task=${newTask.id}, message=${state.currentMessageId}`);
        }
      }
    } else {
      // 🔧 Task #182：数据未变化时，仅在搜索词确实变化时才重新渲染
      // 原逻辑每次轮询都调用renderTaskTree()，导致无意义的DOM操作
      const container = document.getElementById('task-tree');
      const currentSearchTerm = container?.dataset?.taskSearchTerm || '';
      const newSearchTerm = (state._taskSearchTerm || state.taskSearchTerm || '').trim();
      if (currentSearchTerm !== newSearchTerm) {
        renderTaskTree();
      }
    }
  } catch (error) {
    console.error('加载事务失败:', error);
    // 不显示错误toast，避免频繁打扰
  }
}

// 清理过期的事务展开/折叠记录
function cleanupExpandedTasks(tasks) {
  const allTasks = flattenTasks(tasks);
  const taskIds = new Set(allTasks.map(t => t.id));

  // 只移除不存在的事务记录，保留用户的主动操作（展开或折叠）
  for (const taskId of state.expandedTasks.keys()) {
    if (!taskIds.has(taskId)) {
      state.expandedTasks.delete(taskId);
    }
  }
  for (const taskId of state.collapsedTasks.keys()) {
    if (!taskIds.has(taskId)) {
      state.collapsedTasks.delete(taskId);
    }
  }
}

function renderTaskTree() {
  const container = document.getElementById('task-tree');
  if (!container) return;

  const rawSearchTerm = (state._taskSearchTerm || state.taskSearchTerm || '').trim();

  // 过滤事务：有关键词时搜索所有事务，无关键词时显示所有事务
  let filteredTasks;
  let highlightTerms = []; // 用于高亮的关键词数组
  if (rawSearchTerm) {
    // 解析搜索语法：空格=AND，|=OR
    const { andTerms, orTerms } = parseSearchTerms(rawSearchTerm);
    // 合并所有关键词用于高亮
    highlightTerms = [...andTerms, ...orTerms];

    // 递归过滤事务树
    const filterTaskTree = (tasks) => {
      return tasks.filter(task => {
        // 获取搜索文本
        const texts = [
          task.content || '',
          task.id?.toString() || '',
          ...(task.children || []).map(child => child.content || '')
        ].join(' ').toLowerCase();

        // OR 条件：满足任一 OR 关键词
        let orMatch = true;
        if (orTerms.length > 0) {
          orMatch = orTerms.some(term => texts.includes(term.toLowerCase()));
        }

        // AND 条件：满足所有 AND 关键词
        let andMatch = true;
        if (andTerms.length > 0) {
          andMatch = andTerms.every(term => texts.includes(term.toLowerCase()));
        }

        const taskMatch = andMatch && orMatch;

        // 如果事务匹配，保留其所有子事务
        if (taskMatch) {
          return true;
        }

        // 如果事务不匹配，但子事务匹配，保留该事务并递归过滤子事务
        if (task.children && task.children.length > 0) {
          const filteredChildren = filterTaskTree(task.children);
          if (filteredChildren.length > 0) {
            return true;
          }
        }

        return false;
      }).map(task => {
        // 递归过滤子事务
        if (task.children && task.children.length > 0) {
          return {
            ...task,
            children: filterTaskTree(task.children)
          };
        }
        return task;
      });
    };

    filteredTasks = filterTaskTree(state.tasks);
  } else {
    filteredTasks = state.tasks;
  }

  if (filteredTasks.length === 0) {
    container.innerHTML = rawSearchTerm
      ? `<div class="loading">未找到匹配 "${escapeHtml(state.taskSearchTerm)}" 的事务</div>`
      : '';
    return;
  }

  // 🔧 Task#228：事务#1永远排第一，顶置事务排在#1之后，其余按时间排序
  if (!rawSearchTerm) {
    filteredTasks = [...filteredTasks].sort((a, b) => {
      // 事务#1永远排第一
      if (a.id === 1) return -1;
      if (b.id === 1) return 1;
      const aTopped = state.toppedTaskIds.has(a.id) ? 0 : 1;
      const bTopped = state.toppedTaskIds.has(b.id) ? 0 : 1;
      if (aTopped !== bTopped) return aTopped - bTopped;
      // 保持原有时间排序
      return new Date(b.created_at || b.timestamp) - new Date(a.created_at || a.timestamp);
    });
  }

  // 检查是否需要全量渲染（首次加载或容器为空）
  const existingTasks = container.querySelectorAll('.task-chain-head');
  if (existingTasks.length === 0) {
    // 首次加载时保存hover状态
    const hoveredHead = container.querySelector('.task-chain-head:hover');
    const hoveredId = hoveredHead?.dataset?.id;

    // 首次加载，全量渲染
    // 🔧 Task#272：保存当前顶置状态到容器dataset
    container.dataset.toppedTaskIds = Array.from(state.toppedTaskIds).sort().join(',');
    container.dataset.toppedMessageIds = Array.from(state.toppedMessageIds).sort().join(',');
    container.innerHTML = filteredTasks.map(task => renderTaskItem(task, 0, hoveredId, highlightTerms)).join('');
    // 自动选中关联事务
    autoSelectRelatedTask();

    // 用户移动鼠标时移除临时类（让真正的:hover接管）
    if (hoveredId) {
      const removePreserve = () => {
        const el = container.querySelector(`.task-chain-head[data-id="${hoveredId}"].preserve-hover`);
        if (el) el.classList.remove('preserve-hover');
        container.removeEventListener('mousemove', removePreserve);
      };
      container.addEventListener('mousemove', removePreserve, { once: true });
    }
    return;
  }

  // 签名比较：检查搜索词和事务数据是否变化
  const lastSearchTerm = container.dataset.taskSearchTerm || '';
  const searchChanged = lastSearchTerm !== rawSearchTerm;

  // 🔧 Task#272：检查顶置状态是否变化（顶置状态不在签名中，需额外检测）
  let toppedChanged = false;
  const lastToppedTaskIds = container.dataset.toppedTaskIds || '';
  const currentToppedTaskIds = Array.from(state.toppedTaskIds).sort().join(',');
  if (lastToppedTaskIds !== currentToppedTaskIds) {
    toppedChanged = true;
  }

  // 为每个根事务计算签名，检查是否有变化
  const existingChains = container.querySelectorAll(':scope > .task-chain');
  let allSignaturesMatch = !searchChanged && !toppedChanged && existingChains.length === filteredTasks.length;

  if (allSignaturesMatch) {
    // 检查每个根事务的签名是否匹配
    for (let i = 0; i < filteredTasks.length; i++) {
      const task = filteredTasks[i];
      const newSignature = computeTaskSignature(task);
      const oldSignature = existingChains[i]?.dataset?.signature || '';
      if (newSignature !== oldSignature) {
        allSignaturesMatch = false;
        break;
      }
    }
  }

  if (allSignaturesMatch) {
    // 所有签名都未变，跳过全量innerHTML，只轻量更新（active状态、时间等）
    // 构建taskMap避免每次forEach都线性查找
    const allTasks = flattenTasks(state.tasks);
    const taskMap = new Map(allTasks.map(t => [t.id, t]));
    const allTaskHeads = container.querySelectorAll('.task-chain-head');
    allTaskHeads.forEach(headEl => {
      const taskId = parseInt(headEl.dataset.id);
      const task = taskMap.get(taskId);
      if (task) {
        updateTaskElement(headEl, task);
      }
    });

    // 🔧 修复 Task #112：检查折叠状态是否需要更新
    // 如果有任何事务的折叠 CSS 状态与 state 不一致，需要全量重新渲染
    // （因为折叠/展开影响子事务的渲染，轻量更新无法处理）
    let needFullRender = false;
    container.querySelectorAll('.task-chain').forEach(chainEl => {
      const taskId = parseInt(chainEl.dataset.taskId);
      if (!taskId || isNaN(taskId)) return;
      const shouldCollapse = _computeShouldCollapse(taskId, state.currentTaskId);
      const isCurrentlyCollapsed = chainEl.classList.contains('collapsed');
      if (shouldCollapse !== isCurrentlyCollapsed) {
        needFullRender = true;
      }
    });

    if (needFullRender) {
      // 折叠状态有变化，需要全量重新渲染
      const hoveredHead = container.querySelector('.task-chain-head:hover');
      const hoveredId = hoveredHead?.dataset?.id;
      const currentActiveId = state.currentTaskId;

      container.dataset.taskSearchTerm = rawSearchTerm;
      // 🔧 Task#272：保存当前顶置状态到容器dataset
      container.dataset.toppedTaskIds = Array.from(state.toppedTaskIds).sort().join(',');
      container.innerHTML = filteredTasks.map(task => renderTaskItem(task, 0, hoveredId, highlightTerms)).join('');

      // 恢复选中状态
      if (currentActiveId) {
        const activeHead = container.querySelector(`.task-chain-head[data-id="${currentActiveId}"]`);
        if (activeHead) {
          activeHead.classList.add('active');
        }
      }

      if (hoveredId) {
        const removePreserve = () => {
          const el = container.querySelector(`.task-chain-head[data-id="${hoveredId}"].preserve-hover`);
          if (el) el.classList.remove('preserve-hover');
          container.removeEventListener('mousemove', removePreserve, { once: true });
        };
        container.addEventListener('mousemove', removePreserve, { once: true });
      }

      autoSelectRelatedTask();
      return;
    }

    // 🔧 修复：更新搜索词状态，确保后续渲染能正确识别搜索词变化
    container.dataset.taskSearchTerm = rawSearchTerm;
    // 🔧 Task#272：同步更新顶置状态到容器dataset
    container.dataset.toppedTaskIds = Array.from(state.toppedTaskIds).sort().join(',');
    return;
  }

  // 签名有变化或搜索词变化，执行全量渲染
  // 保存当前hover和选中状态
  const hoveredHead = container.querySelector('.task-chain-head:hover');
  const hoveredId = hoveredHead?.dataset?.id;
  const currentActiveId = state.currentTaskId;

  container.dataset.taskSearchTerm = rawSearchTerm;
  // 🔧 Task#272：保存当前顶置状态到容器dataset，用于后续增量更新时检测变化
  container.dataset.toppedTaskIds = Array.from(state.toppedTaskIds).sort().join(',');
  container.innerHTML = filteredTasks.map(task => renderTaskItem(task, 0, hoveredId, highlightTerms)).join('');

  // 恢复选中状态
  if (currentActiveId) {
    const activeHead = container.querySelector(`.task-chain-head[data-id="${currentActiveId}"]`);
    if (activeHead) {
      activeHead.classList.add('active');
    }
  }

  // 用户移动鼠标时移除临时类（让真正的:hover接管）
  if (hoveredId) {
    const removePreserve = () => {
      const el = container.querySelector(`.task-chain-head[data-id="${hoveredId}"].preserve-hover`);
      if (el) el.classList.remove('preserve-hover');
      container.removeEventListener('mousemove', removePreserve);
    };
    container.addEventListener('mousemove', removePreserve, { once: true });
  }

  // 自动选中关联事务
  autoSelectRelatedTask();
}

// 自动选中与当前消息关联的事务
function autoSelectRelatedTask() {
  // 如果已有选中的事务，不自动切换
  if (state.currentTaskId !== null) return;

  // 如果没有选中的消息，不自动选中事务
  if (state.currentMessageId === null) return;

  // 查找关联事务
  const relatedTask = findTaskByMessageId(state.currentMessageId);
  if (relatedTask) {
    state.currentTaskId = relatedTask.id;
    document.querySelectorAll('.task-chain').forEach(el => {
      const head = el.querySelector('.task-chain-head');
      if (head) {
        el.classList.toggle('active', head.dataset.id == relatedTask.id);
      }
    });
    // 更新输出过滤状态
    updateOutputTitle(relatedTask.id, state.currentMessageId);
  }
  // 🔧 修复：初次自动选中事务时，清空 xterm 并重置时间戳
  if (state.output) {
    state.output.clear(); state.currentColor = '';
  }
  state.currentOldestTimestamp = null;
  state.currentNewestTimestamp = null;
  // 🔧 修复#307：只在无记录时初始化为true，避免覆盖后端已确认的false值
  if (!state.hasMoreHistory.has(state.currentTaskId)) {
    state.hasMoreHistory.set(state.currentTaskId, true);
  }
  subscribeOutput(0, hasOutputHistoryCache(state.currentTaskId));
  // 🔧 Task#219：记录当前 taskId
  state._prevTaskIdForSwitch = state.currentTaskId;
}

// 扁平化事务树，保留层级信息
function flattenTasks(tasks, level = 0) {
  const result = [];
  for (const task of tasks) {
    task._level = level;
    result.push(task);
    if (task.children && task.children.length > 0) {
      result.push(...flattenTasks(task.children, level + 1));
    }
  }
  return result;
}

// 找到事务的根事务（递归向上查找，直到找到没有 parent_task_id 的事务）
function findRootTask(taskId) {
  const allTasks = flattenTasks(state.tasks);
  const taskMap = new Map();
  allTasks.forEach(t => taskMap.set(t.id, t));

  let task = taskMap.get(taskId);
  if (!task) return null;

  // 递归向上查找根事务
  while (task.parent_task_id) {
    task = taskMap.get(task.parent_task_id);
    if (!task) break;
  }

  return task;
}

// 获取事务树的所有事务ID（包括子事务）
function getAllTaskIds(taskId) {
  const allTasks = flattenTasks(state.tasks);
  const taskIds = [taskId];

  // 递归查找所有子事务
  function findChildren(id) {
    allTasks.forEach(t => {
      if (t.parent_id === id) {
        taskIds.push(t.id);
        findChildren(t.id);
      }
    });
  }

  findChildren(taskId);
  return taskIds;
}

// 订阅消息/事务的输出（发送给后端）
// 订阅重试计数器（用于新消息发送时事务尚未创建的情况）
let subscribeRetryCount = 0;
const MAX_SUBSCRIBE_RETRY = 5;
const SUBSCRIBE_RETRY_DELAY = 500;  // 500ms

// 🔧 优化：增加WebSocket连接等待重试机制
// beforeTimestamp: 用于"上一页"加载更多老历史，指定获取此时间戳之前的数据
async function subscribeOutput(retryCount = 0, skipHistory = false, beforeTimestamp = null, sinceTime = null) {
  const maxRetries = 200;  // 200次 * 100ms = 20秒
  const retryDelay = 100;
  const messageId = state.currentMessageId;
  const taskId = state.currentTaskId;

  if (messageId === null || taskId === null) {
    return;
  }

  // 等待WebSocket连接
  if (!state.ws || state.ws.readyState !== WebSocket.OPEN) {
    if (retryCount >= maxRetries) {
      console.error('❌ WebSocket 连接超时，订阅失败，0.5秒后自动重试');
      // 🔥 启动后台重试事务
      setTimeout(() => subscribeOutput(0, skipHistory, beforeTimestamp, sinceTime), 500);
      return;
    }
    await new Promise(resolve => setTimeout(resolve, retryDelay));
    return subscribeOutput(retryCount + 1, skipHistory, beforeTimestamp, sinceTime);
  }
  // 发送订阅消息
  try {
    const msg = {
      type: 'subscribe',
      messageId: messageId,
      taskIds: [taskId],
      skipHistory: skipHistory
    };
    if (beforeTimestamp) {
      msg.beforeTimestamp = beforeTimestamp;
    }
    // 🔧 修复Task#219：传递 sinceTime 用于增量获取切换期间的新输出
    if (sinceTime) {
      msg.sinceTime = sinceTime;
    }
    state.ws.send(JSON.stringify(msg));
  } catch (error) {
    console.error('订阅失败，0.5秒后重试:', error);
    setTimeout(() => subscribeOutput(0, skipHistory, beforeTimestamp, sinceTime), 500);
  }
}

// 计算事务签名（包含content、status、children摘要等稳定字段）
// 参考消息树的 extractTreeSignatureData + updateMessageElement 签名模式
function computeTaskSignature(task) {
  if (!task) return '';
  // 提取子事务摘要（每个子事务的 id + content + status，按 id 排序保证稳定性）
  const childrenSummary = task.children
    ? task.children.map(c => ({
      id: c.id,
      content: c.content || '',
      status: c.status || ''
    })).sort((a, b) => a.id - b.id)
    : [];
  const signatureData = JSON.stringify({
    content: task.content || '',
    status: task.status || '',
    provider: task.provider || '',
    parent_task_id: task.parent_task_id || null,
    message_id: task.message_id || null,
    workdir: task.workdir || '',
    children: childrenSummary
  });
  return btoa(encodeURIComponent(signatureData));
}

// 更新单个事务元素（只更新变化的属性）
function updateTaskElement(headEl, task) {
  const isActive = task.id === state.currentTaskId;
  const statusClass = getStatusClass(task.status);
  const statusIcon = getStatusIcon(task.status, task.provider);
  const timestamp = task.created_at || task.timestamp || '';
  const timeStr = timestamp ? timestamp.substring(0, 19).replace('T', ' ') : '';
  const caller = task.parent_task_id ? `↑#${task.parent_task_id}` : (task.message_id ? `#${task.message_id}` : '');

  // 更新 active 状态
  headEl.classList.toggle('active', isActive);

  // 更新 data-provider 属性（供动画速度等逻辑读取）
  const newProvider = task.provider || '';
  if (headEl.dataset.provider !== newProvider) {
    headEl.dataset.provider = newProvider;
  }

  // 更新 meta 信息
  const meta = headEl.querySelector('.task-chain-meta');
  if (meta) {
    const timeEl = meta.querySelector('.task-chain-time');
    if (timeEl && timeEl.textContent !== timeStr) {
      timeEl.textContent = timeStr;
    }

    const statusEl = meta.querySelector('.task-chain-status');
    if (statusEl) {
      if (statusEl.textContent !== statusIcon) {
        statusEl.textContent = statusIcon;
      }
      statusEl.className = `task-chain-status ${statusClass}`;
      statusEl.title = getStatusTitle(task.status);
      // 为running状态添加随机动画延迟（如果元素没有动画延迟的话）
      if (task.status === 'running' && !statusEl.style.animationDelay) {
        statusEl.style.animationDelay = `-${Math.random() * 3}s`;
      }
      // 动态调整running状态的动画速度（基于provider吞吐率）
      if (task.status === 'running' && task.provider && state.providerFrameRates[task.provider] !== undefined) {
        statusEl.style.animationDuration = `${state.providerFrameRates[task.provider]}s`;
      } else if (task.status !== 'running') {
        statusEl.style.animationDuration = '';
      }
    }

    // 更新操作按钮
    const actionsEl = meta.querySelector('.task-chain-actions');
    if (actionsEl) {
      const newActionsHtml = renderTaskActions(task);
      if (actionsEl.innerHTML !== newActionsHtml) {
        actionsEl.innerHTML = newActionsHtml;
      }
    }

    // 🔧 Task#272：更新顶置按钮状态（增量更新路径也需要更新图标和样式）
    const topBtn = meta.querySelector('.top-btn');
    if (topBtn && task.id !== 1) {
      const isTopped = state.toppedTaskIds.has(task.id);
      topBtn.classList.toggle('topped', isTopped);
      topBtn.textContent = isTopped ? '★' : '☆';
      topBtn.title = isTopped ? '取消顶置' : '顶置';
    }
  }

  // 更新标题内容和编辑图标
  const contentEl = headEl.querySelector('.task-chain-content');
  const newTitle = task.content || '';
  if (contentEl) {
    // 检查 content 是否变化（对比 data-raw-text 属性）
    const oldRawText = contentEl.getAttribute('data-raw-text') || '';
    const newRawText = escapeHtml(newTitle).replace(/"/g, '&quot;');
    if (oldRawText !== newRawText) {
      // content 变化，重新渲染内容区域
      const truncatedTitle = truncateText(newTitle, null, task.id, 'task', true);
      const displayTitle = convertFilePathsToHtmlLinks(truncatedTitle);
      const editIconHtml = isActive
        ? `<span class="edit-text-icon" title="编辑" data-edit-type="task" data-edit-id="${task.id}" data-raw-text="${newRawText}">📝</span>`
        : '';
      contentEl.setAttribute('data-raw-text', newRawText);
      contentEl.innerHTML = displayTitle + editIconHtml;

      // 同步更新摘要
      const summaryEl = headEl.querySelector('.task-chain-summary');
      if (summaryEl) {
        const truncatedSummary = truncateText(newTitle, null, null, 'task', false);
        summaryEl.innerHTML = convertFilePathsToHtmlLinks(truncatedSummary);
      }
    } else {
      // content 未变，只更新编辑图标的显示状态
      const existingEditIcon = contentEl.querySelector('.edit-text-icon');
      if (isActive && !existingEditIcon) {
        const editIcon = document.createElement('span');
        editIcon.className = 'edit-text-icon';
        editIcon.title = '编辑';
        editIcon.setAttribute('data-edit-type', 'task');
        editIcon.setAttribute('data-edit-id', task.id);
        editIcon.setAttribute('data-raw-text', newRawText);
        editIcon.textContent = '📝';
        contentEl.appendChild(editIcon);
      } else if (!isActive && existingEditIcon) {
        existingEditIcon.remove();
      } else if (isActive && existingEditIcon) {
        existingEditIcon.setAttribute('data-edit-type', 'task');
        existingEditIcon.setAttribute('data-raw-text', newRawText);
        existingEditIcon.setAttribute('data-edit-id', task.id);
      }
    }
  }

  // 更新折叠状态
  const taskChain = headEl.closest('.task-chain');
  if (taskChain) {
    // 判断是否需要折叠（和 renderTaskItem 一样的逻辑）
    const taskTime = new Date(timestamp);
    const oneHourAgo = Date.now() - 60 * 60 * 1000;
    const isManuallyCollapsed = state.collapsedTasks.has(task.id);
    const isManuallyExpanded = state.expandedTasks.has(task.id);
    let shouldCollapse = false;

    if (isManuallyCollapsed) {
      shouldCollapse = true;
    } else if (isManuallyExpanded) {
      shouldCollapse = false;
    } else if (isActive) {
      shouldCollapse = false;
    } else {
      shouldCollapse = taskTime < oneHourAgo;
    }

    // 更新折叠状态
    taskChain.classList.toggle('collapsed', shouldCollapse);
  }
}

function renderTaskItem(task, level = 0, hoveredId = null, highlightTerms = []) {
  const isActive = task.id === state.currentTaskId;
  const isHovered = String(task.id) === String(hoveredId);
  const statusClass = getStatusClass(task.status);
  const statusIcon = getStatusIcon(task.status, task.provider)
  const timestamp = task.created_at || task.timestamp || '';
  const timeStr = timestamp ? timestamp.substring(0, 19).replace('T', ' ') : '';
  const indent = level * 24;

  // 为running状态添加随机动画延迟，避免所有图标动画完全同步
  // 同时根据 provider 吞吐率动态调整动画速度
  let taskAnimStyle = '';
  if (task.status === 'running') {
    const provider = task.provider;
    if (provider && state.providerFrameRates[provider] !== undefined) {
      const duration = state.providerFrameRates[provider];
      taskAnimStyle = `animation-delay: -${Math.random() * 3}s; animation-duration: ${duration}s;`;
    } else {
      taskAnimStyle = `animation-delay: -${Math.random() * 3}s;`;
    }
  }
  const randomAnimationDelay = taskAnimStyle;

  // 从配置中提取事务图标（去除文字）
  const themeConfig = state.themeConfig?.[state.theme];
  const taskIcon = extractIcon(themeConfig?.icon_tasks_title, '🚩');
  const messageIcon = extractIcon(themeConfig?.icon_messages_title, '📜');

  // 调用者显示：子事务显示父事务ID，根事务显示消息ID
  const caller = task.parent_task_id ? `${task.parent_task_id}` : (task.message_id ? `${task.message_id}` : '');
  // 判断是否需要折叠：
  // 优先级：主动折叠 > 主动展开 > isActive > 时间规则
  // 时间计算：优先使用心跳更新时间 heartbeat_at，回退到创建时间 created_at
  const taskTime = new Date(task.heartbeat_at || timestamp)
  const oneHourAgo = Date.now() - 60 * 60 * 1000;
  const isManuallyCollapsed = state.collapsedTasks.has(task.id);
  const isManuallyExpanded = state.expandedTasks.has(task.id);
  let shouldCollapse = false;

  if (isManuallyCollapsed) {
    // 用户主动折叠的事务要折叠（优先级最高）
    shouldCollapse = true;
  } else if (isManuallyExpanded) {
    // 用户主动展开的事务不折叠
    shouldCollapse = false;
  } else if (isActive) {
    // 当前选中的事务不折叠
    shouldCollapse = false;
  } else {
    // 默认规则：1小时前的事务折叠
    shouldCollapse = taskTime < oneHourAgo;
  }

  // 移除JS中的缩进计算，只使用CSS的.task-children缩进
  // 通过data-level属性标记层级，用于CSS样式
  // 动态计算层级底色：
  // - level 0（根事务）：保持原样
  // - 奇数层（1, 3, 5...）：绿色分量固定加35%
  // - 偶数层（2, 4, 6...）：红色和蓝色分量固定加35%，绿色分量减30%
  const baseR = 48, baseG = 128, baseB = 192;
  const r = (level > 0 && level % 2 === 0) ? Math.min(255, Math.round(baseR * 1.35)) : baseR; // 偶数层红色+35%
  const g = level % 2 === 1 ? Math.min(255, Math.round(baseG * 1.35)) : Math.max(0, Math.round(baseG * 0.7)); // 奇数层绿色+35%，偶数层绿色-30%
  const b = (level > 0 && level % 2 === 0) ? Math.min(255, Math.round(baseB * 1.35)) : baseB; // 偶数层蓝色+35%
  const opacity = 0.08 + level * 0.02; // 透明度也随层级递增
  // 根节点底色从主题配置读取，子节点使用动态计算
  const rootTaskBgColor = themeConfig?.ui?.root_task_bg_color;
  const isRootLevel = level === 0;

  // 判断是否有子事务
  const hasChildren = task.children && task.children.length > 0;

  // 生成摘要（折叠时显示）- 取前10行
  const title = task.content || '';
  // 🔧 先截断纯文本，再转换文件路径为HTML超链接
  const truncatedTitle = truncateText(title, null, task.id, 'task', true);
  const displayTitle = convertFilePathsToHtmlLinks(truncatedTitle); // 添加展开链接
  const truncatedSummary = truncateText(title, null, null, 'task', false);
  const summary = convertFilePathsToHtmlLinks(truncatedSummary); // 摘要不需要展开链接

  // 判断是否可以折叠（有子事务 或 超过3行或标题太长）
  // 注意：标题太长也会导致CSS自动换行成多行，需要估算实际显示的行数
  const titleLength = title.length;
  // 估算实际显示的行数：按每行80字符估算（考虑CSS自动换行）
  const estimatedLines = Math.ceil(titleLength / 80);
  const titleLines = title.split('\n');
  const actualLineCount = Math.max(titleLines.length, estimatedLines);
  const canCollapse = hasChildren || actualLineCount > 3;

  // 计算事务签名（用于增量更新比较，签名未变时跳过全量innerHTML渲染）
  const taskSignature = computeTaskSignature(task);

  // 计算折叠指示器位置
  // level 0（根事务）：-19px
  // level 1+（子事务）：每层递减12px，再左移5px
  const foldIconLeft = level === 0 ? -19 : -17 - (level - 1) * 12;

  let html = `
         <div class="task-chain ${isActive ? 'active' : ''} ${shouldCollapse ? 'collapsed' : ''} ${canCollapse ? 'has-children' : 'no-children'} ${state.enableDrag ? 'drag-enabled' : ''}"
           data-level="${level}"
           data-can-collapse="${canCollapse}"
           data-task-id="${task.id}"
           data-signature="${taskSignature}"
             style="--level-color: ${isRootLevel && rootTaskBgColor ? rootTaskBgColor : 'rgba(' + r + ', ' + g + ', ' + b + ', ' + opacity + ')'}; --level-color-dark: ${isRootLevel && rootTaskBgColor ? rootTaskBgColor : 'rgba(' + r + ', ' + g + ', ' + b + ', ' + (opacity + 0.02) + ')'}; --level-color-light: ${isRootLevel && rootTaskBgColor ? rootTaskBgColor : 'rgba(' + r + ', ' + g + ', ' + b + ', ' + (opacity - 0.02) + ')'}; --fold-icon-left: ${foldIconLeft}px;">
         ${canCollapse && shouldCollapse ? `<span class="task-fold-icon" onclick="event.stopPropagation(); expandTaskFully(${task.id})">▶</span>` : ''}
              <div class="task-chain-head ${isHovered ? 'preserve-hover' : ''}"
                   data-id="${task.id}" data-provider="${task.provider || ''}">
                    <div class="task-chain-main" onclick="selectTaskHead(${task.id}, ${task.message_id || 'null'}, event)">
                        <div class="task-chain-meta">
                            <span class="task-chain-time">${escapeHtml(timeStr)}</span>
                             <span class="task-chain-id" data-task-id="${task.id}" onclick="event.stopPropagation(); showTaskDetails(${task.id})">${taskIcon}${task.id}</span>
                             ${task.parent_task_id ? `<span class="task-chain-sender"><span class="caller-id parent-task-id" data-task-id="${task.parent_task_id}" onclick="event.stopPropagation(); showTaskDetails(${task.parent_task_id})">${taskIcon}${task.parent_task_id}</span></span>` : ''}
                             ${!task.parent_task_id && task.message_id ? `<span class="task-chain-sender"><span class="caller-id" onclick="event.stopPropagation(); showMessageDetails(${task.message_id})">${messageIcon}${task.message_id}</span></span>` : ''}
                             ${statusIcon ? `<span class="task-chain-status ${statusClass}" title="${getStatusTitle(task.status)}"${randomAnimationDelay ? ` style="${randomAnimationDelay}"` : ''}>${statusIcon}</span>` : ''}
                             ${level === 0 ? (task.id === 1 ? '<span class="action-btn top-btn topped" title="系统事务，永远顶置">★</span>' : `<span class="action-btn top-btn${state.toppedTaskIds.has(task.id) ? ' topped' : ''}" onclick="event.stopPropagation(); toggleTaskTop(${task.id})" title="${state.toppedTaskIds.has(task.id) ? '已顶置，点击取消' : '顶置'}">${state.toppedTaskIds.has(task.id) ? '★' : '☆'}</span>`) : ''}
                             <span class="task-chain-actions">
                                 ${renderTaskActions(task)}
                             </span>
                        </div>
                          <div class="task-chain-content" data-raw-text="${escapeHtml(title).replace(/"/g, '&quot;')}">${highlightTerms.length > 0 ? highlightSearch(displayTitle, highlightTerms.join('|')) : displayTitle}${isActive ? '<span class="edit-text-icon" title="编辑" data-edit-type="task" data-edit-id="' + task.id + '" data-raw-text="' + escapeHtml(title).replace(/"/g, '&quot;') + '">📝</span>' : ''}</div>
                        <div class="task-chain-summary">${highlightTerms.length > 0 ? highlightSearch(summary, highlightTerms.join('|')) : summary}</div>
                    </div>
             </div>
             <div class="task-children">
     `;

  // 折叠时不渲染子事务，展开时才渲染
  if (!shouldCollapse && task.children && task.children.length > 0) {
    for (const child of task.children) {
      html += renderTaskItem(child, level + 1, hoveredId, highlightTerms);
    }
  }

  html += '</div></div>';
  return html;
}

/**
 * 🔧 Task #112：计算事务是否应该折叠（与 renderTaskItem 中的逻辑一致）
 * 用于轻量更新路径中同步 CSS collapsed 状态。
 * 
 * 优先级：主动折叠 > 主动展开 > isActive > 时间规则
 */
function _computeShouldCollapse(taskId, currentTaskId) {
  const allTasks = flattenTasks(state.tasks);
  const task = allTasks.find(t => t.id === taskId);
  if (!task) return false;

  // 时间计算：优先使用心跳更新时间 heartbeat_at，回退到创建时间 created_at
  const timestamp = task.created_at || task.timestamp || '';
  const taskTime = new Date(task.heartbeat_at || timestamp);
  const oneHourAgo = Date.now() - 60 * 60 * 1000;
  const isManuallyCollapsed = state.collapsedTasks.has(taskId);
  const isManuallyExpanded = state.expandedTasks.has(taskId);
  const isActive = task.id === currentTaskId;

  if (isManuallyCollapsed) {
    return true;
  } else if (isManuallyExpanded) {
    return false;
  } else if (isActive) {
    return false;
  } else {
    return taskTime < oneHourAgo;
  }
}

// 点击事务链首（可切换折叠）
async function selectTaskHead(taskId, messageId = null, event = null) {
  // 如果有文本被选中（用户正在划选），不触发任何操作
  if (hasSelectedText()) {
    return;
  }

  // 如果点击的是编辑图标或超链接（文件路径/图片链接），不触发折叠
  if (event && (event.target.classList.contains('edit-text-icon') || event.target.closest('a'))) {
    return;
  }

  // 如果点击的是已选中的事务，切换折叠状态
  if (state.currentTaskId === taskId) {
    // 确保切换到任务提示模式（selectMessage 会设置 currentTaskId，导致再次点击事务走此分支）
    switchInputMode('task');
    // 如果超过3秒才点击已选中的事务，不触发折叠状态改变（但仍更新时间戳，恢复快速点击能力）
    const taskLastTime = state.lastSelectedTimestamps['task_' + taskId];
    if (taskLastTime && (Date.now() - taskLastTime > state.collapseToggleTimeout)) {
      state.lastSelectedTimestamps['task_' + taskId] = Date.now();
      return;
    }
    // 更新时间戳，确保连续快速折叠/展开不会因超过3秒阈值而被阻止
    state.lastSelectedTimestamps['task_' + taskId] = Date.now();
    toggleTaskCollapse(taskId);
    return;
  }

  // 否则选中该事务
  await selectTask(taskId, messageId);
}

async function selectTask(taskId, messageId = null) {
  // 清除回复标志（用户主动切换事务）
  state.isReplyingMessage = false;
  // 清除分支消息ID（选中事务时选中根消息，不再回复分支消息）
  state.currentBranchMessageId = null;

  state.currentTaskId = taskId;
  state.currentMessageId = messageId;
  state.isCreatingNewMessage = false;
  state.lastSelectedTimestamps['task_' + taskId] = Date.now();

  // 🔧 获取 workdir 和 nest（优先级：子事务 > 父事务 > 分支消息 > 父消息）
  const allTasks = flattenTasks(state.tasks);
  const taskMap = new Map();
  allTasks.forEach(t => taskMap.set(t.id, t));

  let workdir = null;
  let nestName = null;
  let task = taskMap.get(taskId);

  // 🔧 优先解析 nest（必须在 validateAndSetWorkdir 之前，确保远程 nest 路径不走本地验证）
  if (task && task.nest) {
    nestName = task.nest;
  } else if (task && task.parent_task_id) {
    let parentTask = taskMap.get(task.parent_task_id);
    while (parentTask) {
      if (parentTask.nest) {
        nestName = parentTask.nest;
        break;
      }
      parentTask = parentTask.parent_task_id ? taskMap.get(parentTask.parent_task_id) : null;
    }
  }
  if (!nestName && messageId) {
    if (state.currentBranchMessageId) {
      const branchMsg = findMessageByIdRecursive(state.messages, state.currentBranchMessageId);
      if (branchMsg && branchMsg.nest) {
        nestName = branchMsg.nest;
      }
    }
    if (!nestName) {
      const msg = findMessageByIdRecursive(state.messages, messageId);
      if (msg && msg.nest) {
        nestName = msg.nest;
      }
    }
  }
  // 🔧 关键修复：先同步 nest 状态（设置 state.remoteNest），再设置 workdir
  _prepareNestState(nestName);

  // 优先级1：子事务 > 父事务
  // 先检查事务本身是否有 workdir
  if (task && task.workdir) {
    workdir = task.workdir;
  } else if (task && task.parent_task_id) {
    // 如果事务本身没有 workdir，递归查找父事务的 workdir
    let parentTask = taskMap.get(task.parent_task_id);
    while (parentTask) {
      if (parentTask.workdir) {
        workdir = parentTask.workdir;
        break;
      }
      parentTask = parentTask.parent_task_id ? taskMap.get(parentTask.parent_task_id) : null;
    }
  }

  // 如果事务层级没有 workdir，尝试从消息层级获取（优先级：分支消息 > 父消息）
  if (!workdir && messageId) {
    // 优先级2：分支消息 > 父消息
    // 检查是否有选中的分支消息
    if (state.currentBranchMessageId) {
      const branchMsg = findMessageByIdRecursive(state.messages, state.currentBranchMessageId);
      if (branchMsg && branchMsg.workdir) {
        workdir = branchMsg.workdir;
      }
    }
    // 如果分支消息没有 workdir，检查父消息（根消息）
    if (!workdir) {
      const msg = findMessageByIdRecursive(state.messages, messageId);
      if (msg && msg.workdir) {
        workdir = msg.workdir;
      }
    }
  }

  // 如果找到了 workdir，更新输入框（异步执行，不阻塞UI响应）
  if (workdir) {
    const workdirInput = document.getElementById('workdir-input');
    if (workdirInput) {
      workdirInput.value = toNativeDisplayPath(workdir);
      validateAndSetWorkdir(workdir);  // 不 await，避免阻塞事务切换
    }
  } else if (state.projectRoot) {
    // workdir 不存在，回退到项目目录
    const workdirInput = document.getElementById('workdir-input');
    if (workdirInput) {
      workdirInput.value = toNativeDisplayPath(state.projectRoot);
      validateAndSetWorkdir(state.projectRoot);
    }
  }

  // 🔧 同步 nest 下拉框 UI
  _syncNestDropdown(nestName);

  // 找到该事务关联的消息ID
  const rootTask = findRootTask(taskId);
  const rootMessageId = rootTask ? rootTask.message_id : messageId;

  // 🔧 修复：当 selectTaskHead 传入 messageId=null 时，通过 rootTask 补全 currentMessageId。
  // 否则 subscribeOutput() 因 messageId===null 直接 return，WebSocket 订阅不发送，
  // 导致该事务的输出（含回滚超链接）无法加载到 xterm。
  if (!messageId && rootMessageId) {
    state.currentMessageId = rootMessageId;
  }

  // 记录选中的事务和时间戳，但不覆盖从数据库恢复的折叠状态
  // 如果事务之前被手动折叠过，保持折叠状态；否则标记为展开
  if (!state.collapsedTasks.has(taskId)) {
    state.expandedTasks.set(taskId, Date.now());
  }
  // 同步更新消息的展开状态（同样不覆盖从数据库恢复的折叠状态）
  if (messageId && !state.collapsedMessages.has(messageId)) {
    state.expandedMessages.set(messageId, Date.now());
  }
  // 如果事务链是折叠状态且没有被手动折叠过，需要重新渲染以展开
  const taskChain = document.querySelector(`.task-chain-head[data-id="${taskId}"]`)?.closest('.task-chain');
  if (taskChain?.classList.contains('collapsed') && !state.collapsedTasks.has(taskId)) {
    renderTaskTree();
  }
  // 更新事务 UI
  document.querySelectorAll('.task-chain').forEach(el => {
    const head = el.querySelector('.task-chain-head');
    if (head) {
      const isActive = head.dataset.id == taskId;
      el.classList.toggle('active', isActive);

      // 即时添加/移除编辑图标
      const contentEl = head.querySelector('.task-chain-content');
      if (contentEl) {
        const existingEditIcon = contentEl.querySelector('.edit-text-icon');
        if (isActive && !existingEditIcon) {
          // 选中时添加编辑图标
          const rawText = contentEl.getAttribute('data-raw-text') || '';
          const currentTaskId = head.dataset.id;  // 获取事务ID
          const editIcon = document.createElement('span');
          editIcon.className = 'edit-text-icon';
          editIcon.title = '编辑';
          editIcon.setAttribute('data-edit-type', 'task');
          editIcon.setAttribute('data-edit-id', currentTaskId);
          editIcon.setAttribute('data-raw-text', rawText);
          editIcon.textContent = '📝';
          contentEl.appendChild(editIcon);
        } else if (!isActive && existingEditIcon) {
          // 未选中时移除编辑图标
          existingEditIcon.remove();
        }
      }
    }
  });
  // 更新消息 UI - 选中根事务对应的消息树根节点（使用前面已定义的rootMessageId）
  document.querySelectorAll('.message-chain-head').forEach(el => {
    const isActive = el.dataset.id == rootMessageId;
    el.classList.toggle('active', isActive);

    // 即时添加/移除编辑图标
    const contentEl = el.querySelector('.message-chain-content');
    if (contentEl) {
      const existingEditIcon = contentEl.querySelector('.edit-text-icon');
      if (isActive && !existingEditIcon) {
        // 选中时添加编辑图标
        const rawText = contentEl.getAttribute('data-raw-text') || '';
        const messageId = el.dataset.id;  // 获取消息ID
        const editIcon = document.createElement('span');
        editIcon.className = 'edit-text-icon';
        editIcon.title = '编辑';
        editIcon.setAttribute('data-edit-type', 'message');
        editIcon.setAttribute('data-edit-id', messageId);
        editIcon.setAttribute('data-raw-text', rawText);
        editIcon.textContent = '📝';
        contentEl.appendChild(editIcon);
      } else if (!isActive && existingEditIcon) {
        // 未选中时移除编辑图标
        existingEditIcon.remove();
      }
    }
  });
  // 清除消息树节点的选中状态
  document.querySelectorAll('.message-tree-node').forEach(el => {
    el.classList.remove('active');

    // 移除复制图标
    const contentEl = el.querySelector('.tree-node-content');
    if (contentEl) {
      const existingCopyIcon = contentEl.querySelector('.edit-text-icon');
      if (existingCopyIcon) {
        existingCopyIcon.remove();
      }
    }
  });
  // 滚动消息列表使根事务对应的消息可见
  if (rootMessageId) {
    const msgHead = document.querySelector(`.message-chain-head[data-id="${rootMessageId}"]`);
    if (msgHead) {
      msgHead.scrollIntoView({ behavior: 'instant', block: 'nearest' });
    }
  }
  // 更新输出过滤状态
  updateOutputTitle(taskId, messageId);
  // 更新输入框提示
  updateInputPlaceholder(messageId);

  // 🔧 修复Task#219：保存旧事务的最新输出时间戳
  const prevTaskId2 = state._prevTaskIdForSwitch;
  const prevNewestTs2 = state.currentNewestTimestamp;
  if (prevTaskId2 != null && prevNewestTs2 != null) {
    state.lastViewedTimestamps.set(prevTaskId2, prevNewestTs2);
  }

  // 🔧 修复：切换时立即清空 xterm 并重置时间戳指针
  if (state.output) {
    state.output.clear(); state.currentColor = '';
  }
  state.userScrolledUp = false;  // 🔧 Task #92：重置滚动状态
  state._browsingHistory = false;  // 🔧 修复#305：切换事务时重置翻页状态，确保scrollToNewest生效
  state._userScrollIntentUntil = 0;  // 🔧 Fix #1428：清除用户滚动意图标记
  state._pagingStartLine = -1;  // 🔧 Fix Task#840：清除翻页行号
  state._pagingEndLine = -1;
  state._totalLines = 0;  // 🔧 Fix Task#840：切换事务重置总行数
  state._pagingAnchorNewestTs = null;
  state._pagingPageNewestTs = null;
  state.currentOldestTimestamp = null;
  state.currentNewestTimestamp = null;
  // 🔧 Task#1119: 清除输出搜索状态（搜索结果针对旧事务，新事务无效）
  if (typeof _clearSearch === 'function') _clearSearch();
  // 🔧 修复#307：只在无记录时初始化为true，避免覆盖后端已确认的false值
  if (!state.hasMoreHistory.has(state.currentTaskId)) {
    state.hasMoreHistory.set(state.currentTaskId, true);
  }

  // 🔧 修复Task#219：切回时增量获取
  const newTaskId2 = state.currentTaskId;
  const hasCache2 = hasOutputHistoryCache(newTaskId2);
  const lastViewedTs2 = newTaskId2 != null ? state.lastViewedTimestamps.get(newTaskId2) : null;
  state._prevTaskIdForSwitch = newTaskId2;

  closeEditor();
  // 🔧 Fix Task#1433：有缓存时立即从缓存全量渲染到 xterm（不延迟！）
  // 根因：之前用 setTimeout(scrollToNewest, 200) 延迟渲染，但在这200ms窗口内
  // _watch_loop 的实时输出通过 appendOutput 写入了几行并设置了 currentNewestTimestamp，
  // 导致 scrollToNewest 误判"已在最新"跳过缓存渲染，只显示几行实时输出。
  // 现在改为立即同步渲染缓存，彻底消除竞态窗口。
  // scrollToBottom 的 fit() 时序问题由 writeAndScroll 内部的轮询机制处理。
  if (hasCache2) {
    renderFromCache(state.currentTaskId);
    // 延迟 scrollToBottom 以处理 fit() 异步链（不涉及内容渲染）
    setTimeout(() => { if (state.output) state.output.scrollToBottom(); }, 200);
  }
  // 🔧 异步订阅输出（不阻塞UI响应）
  if (hasCache2 && lastViewedTs2) {
    subscribeOutput(0, true, null, lastViewedTs2);
  } else {
    subscribeOutput(0, hasCache2);
  }
  switchInputMode('task')

  // 🔧 Task#153：保存选中状态到后端配置（刷新页面后恢复）
  _saveSelectionToBackend();
}

// 切换事务折叠状态
async function toggleTaskCollapse(taskId) {
  // 查找事务元素，检查是否可以折叠
  const taskHead = document.querySelector(`.task-chain-head[data-id="${taskId}"]`);
  const taskChain = taskHead?.closest('.task-chain');

  // 检查是否可以折叠（有子事务或内容超过3行）
  const canCollapse = taskChain?.getAttribute('data-can-collapse') === 'true';

  // 如果不可以折叠，不切换折叠状态
  if (!canCollapse) {
    return;
  }

  const now = Date.now();

  // 🔧 修复 Task #44：从 state 读取折叠状态，而非从 DOM 读取
  // 原因：selectTask() 可能在 DOM 和 state 之间产生不同步（DOM 未重渲染而 state 已更新）
  // 使用 state 可以避免"快速点击无法展开"的问题
  const isCollapsed = state.collapsedTasks.has(taskId);

  if (isCollapsed) {
    // 当前是折叠状态，切换为展开：记录展开状态，清除折叠状态
    state.expandedTasks.set(taskId, now);
    state.collapsedTasks.delete(taskId);
    // 调用API保存到数据库
    try {
      await fetch(`/api/tasks/${taskId}/collapsed?collapsed=false`, {
        method: 'POST'
      });
    } catch (e) {
      console.warn(`保存事务 ${taskId} 折叠状态失败:`, e);
    }
  } else {
    // 当前是展开状态，切换为折叠：记录折叠状态，清除展开状态
    state.collapsedTasks.set(taskId, now);
    state.expandedTasks.delete(taskId);
    // 调用API保存到数据库
    try {
      await fetch(`/api/tasks/${taskId}/collapsed?collapsed=true`, {
        method: 'POST'
      });
    } catch (e) {
      console.warn(`保存事务 ${taskId} 折叠状态失败:`, e);
    }
  }

  // 重新渲染事务树
  renderTaskTree();

  // 保存折叠展开状态到 localStorage
  saveCollapseStates();
}

// showTaskInOutput 已废弃
// 信息显示框只显示进程输出，不显示事务详情
// 事务详情在右边栏事务列表中显示

/**
 * 处理吞吐率更新消息
 * 
 * 接收后端定期广播的 Provider 吞吐率数据，
 * 更新 state 中的吞吐率和帧率数据，
 * 并动态调整 running 状态图标的动画速度。
 */
function handleThroughputUpdate(data) {
  if (!data) return;

  // 更新 state 中的吞吐率和帧率数据
  state.providerThroughputs = data.throughputs || {};
  state.providerFrameRates = data.frame_rates || {};

  // 🔧 更新 nest 流量数据
  state.nestTraffic = data.nest_traffic || [];

  // 动态更新所有 running 状态图标的动画速度
  updateRunningAnimationSpeed();

  // 更新底部状态栏的吞吐率显示
  renderThroughputStatusBar();
}

/**
 * 渲染底部状态栏的 Nest 流量和 Provider 吞吐率
 * 
 * Nest流量显示在"服务运行中"后面，Provider吞吐率显示在右侧
 * 流量格式：'🪹name 1.2KB/s'（上下行合并计算，远程nest显示平台图标）
 */

/**
 * 根据平台名称获取图标（HTML字符串，支持内联SVG）
 * Windows使用Microsoft标准四色方块图标，其他平台使用emoji
 */
function getPlatformIconHTML(platform, linuxId) {
  if (!platform) return '';
  const p = platform.toLowerCase();
  if (p.includes('win')) {
    // Windows 标准四色方块图标（Microsoft官方配色）
    return '<svg viewBox="0 0 16 16" width="12" height="12" style="vertical-align:middle;margin-right:2px;">' +
      '<rect x="0.5" y="0.5" width="6.5" height="6.5" fill="#F25022"/>' +
      '<rect x="8" y="0.5" width="6.5" height="6.5" fill="#7FBA00"/>' +
      '<rect x="0.5" y="8" width="6.5" height="6.5" fill="#00A4EF"/>' +
      '<rect x="8" y="8" width="6.5" height="6.5" fill="#FFB900"/>' +
      '</svg>';
  }
  if (p.includes('linux')) {
    // 根据linux发行版显示不同图标（使用发行版官方logo SVG）
    const distro = (linuxId || '').toLowerCase();
    const basePath = '/static/img/distro/';
    const imgStyle = 'vertical-align:middle;margin-right:2px;';
    if (distro.includes('ubuntu')) return '<img src="' + basePath + 'ubuntu.png" width="14" height="14" style="' + imgStyle + '" alt="Ubuntu">';
    if (distro.includes('debian')) return '<img src="' + basePath + 'debian.png" width="14" height="14" style="' + imgStyle + '" alt="Debian">';
    if (distro.includes('fedora')) return '<img src="' + basePath + 'fedora.png" width="14" height="14" style="' + imgStyle + '" alt="Fedora">';
    if (distro.includes('centos')) return '<img src="' + basePath + 'centos.png" width="14" height="14" style="' + imgStyle + '" alt="CentOS">';
    if (distro.includes('rhel')) return '<img src="' + basePath + 'rhel.png" width="14" height="14" style="' + imgStyle + '" alt="RHEL">';
    if (distro.includes('rocky')) return '<img src="' + basePath + 'rocky.png" width="14" height="14" style="' + imgStyle + '" alt="Rocky">';
    if (distro.includes('alma')) return '<img src="' + basePath + 'alma.svg" width="14" height="14" style="' + imgStyle + '" alt="Alma">';
    if (distro.includes('arch')) return '<img src="' + basePath + 'arch.png" width="14" height="14" style="' + imgStyle + '" alt="Arch">';
    if (distro.includes('manjaro')) return '<img src="' + basePath + 'manjaro.png" width="14" height="14" style="' + imgStyle + '" alt="Manjaro">';
    if (distro.includes('suse') || distro.includes('opensuse')) return '<img src="' + basePath + 'opensuse.png" width="14" height="14" style="' + imgStyle + '" alt="openSUSE">';
    if (distro.includes('alpine')) return '<img src="' + basePath + 'alpine.png" width="14" height="14" style="' + imgStyle + '" alt="Alpine">';
    return '<img src="' + basePath + 'linux.png" width="14" height="14" style="' + imgStyle + '" alt="Linux">';
  }
  if (p.includes('darwin') || p.includes('mac') || p.includes('osx')) return '🍎';
  return '💻';
}

function renderThroughputStatusBar() {
  const nestTrafficEl = document.getElementById('nest-traffic');
  const throughputEl = document.getElementById('status-throughputs');

  // 🔧 渲染 Nest 流量到 status-left 中"服务运行中"后面
  if (nestTrafficEl) {
    let nestTraffic = state.nestTraffic || [];
    if (nestTraffic.length === 0) {
      nestTrafficEl.innerHTML = '';
    } else {
      // 🔧 排序：本地nest排首位
      nestTraffic = [...nestTraffic].sort((a, b) => {
        const aLocal = a.display_key === 'local';
        const bLocal = b.display_key === 'local';
        if (aLocal && !bLocal) return -1;
        if (!aLocal && bLocal) return 1;
        return 0;
      });
      let parts = [];
      for (const nest of nestTraffic) {
        const name = nest.display_key || nest.display_name || '?';
        const rateStr = formatTrafficRate(nest.total_rate || 0);
        // 流量为0时不显示数字，但图标和名字照常显示
        const rateDisplay = rateStr ? ` ${rateStr}` : '';
        // 使用与 nest 下拉框相同的图标逻辑（_getNestIconHTML），确保图标一致
        const iconHTML = _getNestIconHTML(nest.display_key, nest);
        parts.push(`<span style="margin-right:10px">${iconHTML}${name}${rateDisplay}</span>`);
      }
      nestTrafficEl.innerHTML = '  ' + parts.join('');
    }
  }

  // 🔧 渲染 Provider 吞吐率到 status-throughputs 区域
  if (throughputEl) {
    const throughputs = state.providerThroughputs;
    if (!throughputs || Object.keys(throughputs).length === 0) {
      throughputEl.textContent = '';
      return;
    }

    let parts = [];
    for (const [provider, tps] of Object.entries(throughputs)) {
      const icon = getProviderIcon(provider) || '🤖';
      let tpsStr;
      if (tps < 10) {
        tpsStr = tps.toFixed(1);
      } else if (tps < 1000) {
        tpsStr = Math.round(tps).toString();
      } else {
        tpsStr = (tps / 1000).toFixed(1) + 'k';
      }
      parts.push(icon + " " + tpsStr + ' tok/s');
    }
    throughputEl.textContent = parts.join('  ');
  }
}

/**
 * 格式化网络流量速率
 * @param {number} bytesPerSec - 字节/秒
 * @returns {string} 格式化后的速率字符串，如 "1.2KB/s" 或 "3.5MB/s"
 */
function formatTrafficRate(bytesPerSec) {
  if (bytesPerSec < 1) return '';
  if (bytesPerSec < 1024) return Math.round(bytesPerSec) + 'B/s';
  if (bytesPerSec < 1024 * 1024) return (bytesPerSec / 1024).toFixed(1) + 'KB/s';
  return (bytesPerSec / (1024 * 1024)).toFixed(1) + 'MB/s';
}

/**
 * 更新所有 running 状态图标的动画速度
 * 
 * 根据当前 running 事务的 provider 吞吐率，动态调整
 * running 状态图标的 CSS animation-duration。
 * 吞吐率越高，动画越快（duration 越短）。
 */
function updateRunningAnimationSpeed() {
  // 找到所有 running 状态的图标元素
  const runningIcons = document.querySelectorAll('.status-running, .task-chain-status.status-running');

  runningIcons.forEach(iconEl => {
    // 查找最近的任务元素，获取 provider 信息
    const taskEl = iconEl.closest('[data-task-id]') || iconEl.closest('.task-chain-head');
    let providerName = null;

    if (taskEl) {
      // 从 task-chain-head 元素中获取 provider
      providerName = taskEl.dataset.provider || null;
    }

    if (providerName && state.providerFrameRates[providerName] !== undefined) {
      iconEl.style.animationDuration = state.providerFrameRates[providerName] + 's';
    }
  });
}

function handleTaskUpdate(data) {
  // 记录事务来源
  const source = data.source || 'unknown';
  const details = data.details || {};
  const task_id = data.task_id;

  // 🔧 性能优化：对只有 provider+icon 变化的更新做去重检测
  // 避免同一事务频繁的 provider 心跳更新触发不必要的 DOM 操作
  const detailKeys = Object.keys(details);
  const isOnlyProviderUpdate = detailKeys.length <= 2 &&
    detailKeys.every(k => k === 'provider' || k === 'icon');
  if (isOnlyProviderUpdate && task_id) {
    const lastProviderUpdate = state._lastProviderUpdates?.[task_id];
    if (lastProviderUpdate &&
        lastProviderUpdate.provider === details.provider &&
        lastProviderUpdate.icon === details.icon) {
      // provider 和 icon 都没变，跳过本次更新
      return;
    }
    // 记录本次更新
    if (!state._lastProviderUpdates) state._lastProviderUpdates = {};
    state._lastProviderUpdates[task_id] = {
      provider: details.provider,
      icon: details.icon,
    };
  }

  // 🔧 provider 更新处理（必须在 updateTaskElement 之前，否则图标查找不到 provider icon）
  if (details.provider) {
    const newProviderName = details.provider;
    const providerIndex = state.providers.findIndex(p => p.name === newProviderName);

    if (providerIndex >= 0) {
      const provider = state.providers[providerIndex];
      provider.icon = details.icon || provider.icon;
    } else {
      state.providers.push({
        name: newProviderName,
        icon: details.icon || '🤖',
        is_current: true,
      });
    }

    state.currentProvider = newProviderName;
    state.providers.forEach(p => {
      p.is_current = (p.name === newProviderName);
    });

    updateApiProviderButtonIcon();

    const modal = document.getElementById('api-provider-modal');
    if (modal && modal.style.display === 'flex') {
      renderProvidersList();
    }
  }

  // 🔧 增量更新模式：用 details 中的字段直接更新 state.tasks 和 DOM
  if (data.action === 'updated' && details && task_id) {
    const allTasks = flattenTasks(state.tasks || []);
    const task = allTasks.find(t => t.id === task_id);
    if (task) {
      // 更新 state 中的字段
      let changed = false;
      for (const [key, value] of Object.entries(details)) {
        if (task[key] !== value) {
          task[key] = value;
          changed = true;
        }
      }
      // 有变化时增量更新 DOM 元素
      if (changed) {
        // 结构性变更字段：需要全量重渲染（改变事务在树中的位置/归属）
        const structuralFields = ['parent_task_id', 'message_id', 'is_archived', 'collapsed', 'nest', 'workdir'];
        const hasStructuralChange = structuralFields.some(f => f in details);

        if (hasStructuralChange) {
          // 🔧 Task#338：is_archived 从 1→0（取消归档）时，task_update 广播比
          // message_update 先到达，loadTasks 需要延迟等待 loadMessages 刷新
          // displayedMessageIds 后再执行，否则后端不会返回刚取消归档的事务
          if ('is_archived' in details && details.is_archived === 0 && task.is_archived === 0) {
            setTimeout(() => {
              lastTasksData = '';
              loadTasks();
            }, 600);
          } else {
            loadTasks();
          }
          skipNextTasksPoll = true;
        } else {
          // 非结构性变更：增量更新 DOM（status、content、provider 等）
          const headEl = document.querySelector(`.task-chain-head[data-id="${task_id}"]`);
          if (headEl) {
            updateTaskElement(headEl, task);
          }
        }
      }
    } else {
      // state 中找不到该事务（可能还没加载或被归档过滤隐藏）
      // 🔧 Task#338：如果是取消归档（is_archived: 0），延迟刷新等待 loadMessages 更新 displayedMessageIds
      if (details && details.is_archived === 0) {
        setTimeout(() => {
          lastTasksData = '';
          loadTasks();
        }, 600);
      } else {
        loadTasks();
      }
      skipNextTasksPoll = true;
    }
  } else if (data.action === 'created' && task_id) {
    // 🔧 新事务创建：全量刷新事务列表（因为需要插入新 DOM 元素）
    loadTasks();
    skipNextTasksPoll = true;

    // 🔧 Task#281修复：发送新消息后事务异步创建的竞态补偿
    // sendMessage中selectMessage时如果事务未创建（currentTaskId===null），
    // 设置了_pendingMessageForTask标记，此处检测匹配后补偿调用selectMessage
    if (state._pendingMessageForTask != null) {
      setTimeout(() => {
        const task = findTaskById(state.tasks, task_id);
        if (!task) return;
        if (task.message_id === state._pendingMessageForTask) {
          const pendingMsgId = state._pendingMessageForTask;
          state._pendingMessageForTask = null;
          console.log(`[Task#281] 竞态补偿：task=${task_id}, msg=${pendingMsgId}`);
          selectMessage(pendingMsgId);
        }
      }, 300);
    }
  } else if (data.action === 'restarted' && task_id) {
    // 🔧 Task#1326：事务重启：output 文件保留不删除，新进程继续追加输出
    // 前端清除 xterm 缓存并重新订阅，从文件当前末尾继续读取新输出
    // 清除该事务及其所有子事务的输出历史缓存
    const allTaskIds = collectTaskIds(task_id);
    for (const tid of allTaskIds) {
      state.outputHistory.delete(tid);
      state.hasMoreHistory.delete(tid);
      _taskContextInfoCache.delete(tid);  // 清除上下文摘要缓存（重启后重新计算）
    }
    // 如果重启的是当前选中事务，清空 xterm 并重置状态
    if (state.currentTaskId === task_id) {
      if (state.output) {
        state.output.clear(); state.currentColor = '';
      }
      state.currentOldestTimestamp = null;
      state.currentNewestTimestamp = null;
      state._totalLines = 0;
      state._pagingStartLine = -1;
      state._pagingEndLine = -1;
      state._pagingAnchorNewestTs = null;
      state._pagingPageNewestTs = null;
      state._browsingHistory = false;
      state.userScrolledUp = false;
      updatePaginationButtons();
    }
    // 刷新事务列表（更新状态显示）
    loadTasks();
    skipNextTasksPoll = true;
  } else if (data.action === 'deleted' && task_id) {
    // 🔧 Task#867：事务删除 — 全量刷新事务列表
    // 消息列表的刷新由 message_update deleted 广播触发（后端已调整为先删消息再删事务）
    // 此处不再调用 loadMessages，避免在消息删除前加载到旧数据
    loadTasks();
    skipNextTasksPoll = true;
  } else if (data.action === 'archived') {
    // 🔧 Task#1518：归档事务 — 清除缓存并延迟刷新，等待 loadMessages 先更新 displayedMessageIds
    // 后端 broadcast_task_update(archived) 比 archiveMessage 的 loadMessages 先到达，
    // 如果立即 loadTasks()，displayedMessageIds 还包含已归档消息，事务可能不会消失
    setTimeout(() => {
      lastTasksData = '';  // 清除缓存，确保本次 loadTasks 不被去重跳过
      loadTasks();
    }, 300);
    skipNextTasksPoll = true;
  } else if (data.action === 'unarchived') {
    // 🔧 Task#338：取消归档延迟刷新，等待 loadMessages 先更新 displayedMessageIds
    // 后端 broadcast_task_update(unarchived) 比 broadcast_message_update(unarchived) 先到达，
    // 如果立即 loadTasks()，displayedMessageIds 还没更新，会导致事务不出现
    setTimeout(() => {
      lastTasksData = '';  // 清除缓存，确保本次 loadTasks 不被去重跳过
      loadTasks();
    }, 500);
    skipNextTasksPoll = true;
  } else {
    loadTasks();
    skipNextTasksPoll = true;
  }

  // 非 updated 场景下刷新 providers
  if (!details.provider && data.action !== 'updated') {
    loadProviders();
  }
}


// ==================== 事务ID hover tooltip：上下文长度和费用 ====================

// context-info 缓存：taskId → { data, fetchedAt }
const _taskContextInfoCache = new Map();
const _TASK_CTX_CACHE_TTL = 15_000; // 缓存有效期 15 秒

// hover tooltip 元素和定时器
let _taskHoverTooltipEl = null;
let _taskHoverTimer = null;
let _taskHoverFetchId = 0; // 防止并发 fetch 竞态

/**
 * 格式化 token 用量为 M 单位字符串
 */
function _fmtTokensM(n) {
  const s = (n / 1_000_000).toFixed(3).replace(/0+$/, '').replace(/\.$/, '');
  return s + 'M';
}

/**
 * 根据费用返回 HTML 颜色（绿→橙红线性插值，与事务详情一致）
 */
function _costHtmlColor(cost) {
  if (cost <= 0) return '#3fb950';
  const lo = 0.1, hi = 10.0;
  const ratio = cost > lo ? Math.min(Math.max((cost - lo) / (hi - lo), 0), 1) : 0;
  const r = Math.round(63 + (255 - 63) * ratio);
  const g = Math.round(185 + (100 - 185) * ratio);
  const b = Math.round(80 + (60 - 80) * ratio);
  return `rgb(${r},${g},${b})`;
}

/**
 * 根据 context_color 名称返回 HTML 颜色（与事务详情一致）
 */
function _contextHtmlColor(colorName) {
  const colors = { "context_red": "#FF6464", "yellow": "#d29922", "green": "#3fb950" };
  return colors[colorName] || "#3fb950";
}

/**
 * 构建事务上下文/费用 tooltip HTML
 */
function _buildTaskCtxTooltipHtml(info) {
  const parts = [];
  const tokens = info.tokens || 0;
  const cost = info.cost || 0;

  // [上下文XXK]（仅 >2000 时显示）
  if (tokens > 2000) {
    const ctxColor = _contextHtmlColor(info.context_color);
    parts.push(`<span style="color:${ctxColor};">[上下文${Math.floor(tokens / 1000)}K]</span>`);
  }

  // [费用X.XX元]
  if (cost > 0) {
    const costColor = _costHtmlColor(cost);
    parts.push(`<span style="color:${costColor};font-weight:bold;">[费用${cost.toFixed(2)}元]</span>`);
    // 分项明细
    const usageParts = [];
    if (info.input_hit > 0) {
      usageParts.push(`输入命中缓存${_fmtTokensM(info.input_hit)} (${info.price_input_hit}元/M)`);
    }
    if (info.input_miss > 0) {
      usageParts.push(`未命中${_fmtTokensM(info.input_miss)} (${info.price_input_miss}元/M)`);
    }
    if (info.output > 0) {
      usageParts.push(`输出${_fmtTokensM(info.output)} (${info.price_output}元/M)`);
    }
    if (usageParts.length > 0) {
      parts.push(`<span style="font-size:0.85em;color:#8b949e;">[${usageParts.join(' ')}]</span>`);
    }
  }

  if (parts.length === 0) {
    return '<span style="color:#8b949e;">无上下文/费用数据</span>';
  }
  return parts.join(' ');
}

/**
 * 获取事务 context-info（带缓存）
 * @param taskId  事务 ID
 * @param forceRefresh  为 true 时绕过缓存直接请求 API（用于 hover 时获取最新数据）
 */
async function _fetchTaskContextInfo(taskId, forceRefresh = false) {
  // 检查缓存（forceRefresh 时跳过）
  if (!forceRefresh) {
    const cached = _taskContextInfoCache.get(taskId);
    if (cached && Date.now() - cached.fetchedAt < _TASK_CTX_CACHE_TTL) {
      return cached.data;
    }
  }

  const response = await fetch(`/api/tasks/${taskId}/context-info`);
  if (!response.ok) return null;
  const data = await response.json();
  _taskContextInfoCache.set(taskId, { data, fetchedAt: Date.now() });
  return data;
}

/**
 * 初始化事务ID hover tooltip 事件委托
 * 在 document 上监听 mouseenter/mouseleave，匹配 .task-chain-id 和 .caller-id
 */
function initTaskIdHoverTooltip() {
  const HOVER_DELAY = 300;

  document.addEventListener('mouseenter', (e) => {
    const target = e.target.closest('.task-chain-id[data-task-id], .caller-id[data-task-id]');
    if (!target) return;

    const taskId = parseInt(target.dataset.taskId);
    if (!taskId || isNaN(taskId)) return;

    clearTimeout(_taskHoverTimer);
    _taskHoverTimer = setTimeout(async () => {
      const fetchSeq = ++_taskHoverFetchId;
      // 先用缓存秒显
      const cached = _taskContextInfoCache.get(taskId);
      if (cached && Date.now() - cached.fetchedAt < _TASK_CTX_CACHE_TTL) {
        _showTaskHoverTooltip(_buildTaskCtxTooltipHtml(cached.data), target);
      } else {
        // 无缓存时先显示 loading
        _showTaskHoverTooltip('<span style="color:#8b949e;">加载中...</span>', target);
      }

      // 异步获取最新数据（绕过缓存，确保显示实时数据）
      try {
        const info = await _fetchTaskContextInfo(taskId, true);
        // 检查是否是最新的 hover 请求
        if (fetchSeq !== _taskHoverFetchId) return;
        // 检查目标元素是否还在 DOM 中
        if (!document.body.contains(target)) return;
        if (info) {
          _showTaskHoverTooltip(_buildTaskCtxTooltipHtml(info), target);
        }
      } catch (err) {
        // 静默失败
      }
    }, HOVER_DELAY);
  }, true);

  document.addEventListener('mouseleave', (e) => {
    const target = e.target.closest('.task-chain-id[data-task-id], .caller-id[data-task-id]');
    if (!target) return;
    clearTimeout(_taskHoverTimer);
    _taskHoverFetchId++; // 取消挂起的 fetch 回调
    _hideTaskHoverTooltip();
  }, true);

  // 点击时隐藏
  document.addEventListener('mousedown', () => {
    _hideTaskHoverTooltip();
    clearTimeout(_taskHoverTimer);
  }, true);
}

function _showTaskHoverTooltip(html, targetEl) {
  _hideTaskHoverTooltip();
  const el = document.createElement('div');
  el.className = 'peng-tooltip peng-tooltip-html';
  el.innerHTML = html;
  el.style.visibility = 'hidden';
  document.body.appendChild(el);
  _positionTooltip(el, targetEl);
  el.style.visibility = '';
  _taskHoverTooltipEl = el;
}

function _hideTaskHoverTooltip() {
  if (_taskHoverTooltipEl) {
    _taskHoverTooltipEl.remove();
    _taskHoverTooltipEl = null;
  }
}

// ==================== 运行中状态图标 hover tooltip：provider吞吐率 ====================

let _taskStatusTooltipEl = null;
let _taskStatusTooltipTimer = null;

/**
 * 初始化运行中状态图标 hover tooltip
 * 鼠标悬停在 .task-chain-status.status-running 上时，显示该事务 provider 的吞吐率
 */
function initTaskStatusHoverTooltip() {
  const HOVER_DELAY = 200;

  document.addEventListener('mouseenter', (e) => {
    const target = e.target.closest('.task-chain-status.status-running');
    if (!target) return;

    // 从最近的 task-chain-head 获取 provider
    const headEl = target.closest('.task-chain-head');
    if (!headEl) return;
    const provider = headEl.dataset.provider;
    if (!provider) return;

    // 🔧 抢占旧tooltip系统：清除旧定时器、隐藏旧tooltip、移除 data-peng-title
    // 否则旧系统会在400ms后显示"执行中"，覆盖吞吐率tooltip
    clearTimeout(_pengTooltipTimer);
    _hidePengTooltip();
    target.removeAttribute('data-peng-title');

    _taskStatusTooltipTimer = setTimeout(() => {
      const tps = state.providerThroughputs?.[provider];
      const icon = getProviderIcon(provider) || '🤖';
      let html;
      if (tps !== undefined && tps > 0) {
        let tpsStr;
        if (tps < 10) tpsStr = tps.toFixed(1);
        else if (tps < 1000) tpsStr = Math.round(tps).toString();
        else tpsStr = (tps / 1000).toFixed(1) + 'k';
        html = `<span style="color:#58a6ff;">${icon} ${escapeHtml(provider)}</span> <b>${tpsStr}</b> tok/s`;
      } else {
        html = `<span style="color:#8b949e;">${icon} ${escapeHtml(provider)}</span> <span style="color:#8b949e;">暂无吞吐数据</span>`;
      }
      _showTaskStatusTooltip(html, target);
    }, HOVER_DELAY);
  }, true);

  document.addEventListener('mouseleave', (e) => {
    const target = e.target.closest('.task-chain-status.status-running');
    if (!target) return;
    clearTimeout(_taskStatusTooltipTimer);
    _hideTaskStatusTooltip();
  }, true);
}

function _showTaskStatusTooltip(html, targetEl) {
  _hideTaskStatusTooltip();
  const el = document.createElement('div');
  el.className = 'peng-tooltip peng-tooltip-html';
  el.innerHTML = html;
  el.style.visibility = 'hidden';
  document.body.appendChild(el);
  _positionTooltip(el, targetEl);
  el.style.visibility = '';
  _taskStatusTooltipEl = el;
}

function _hideTaskStatusTooltip() {
  if (_taskStatusTooltipEl) {
    _taskStatusTooltipEl.remove();
    _taskStatusTooltipEl = null;
  }
}
