// ==================== 全局状态 ====================

const state = {
  messages: [],
  tasks: [],
  currentMessageId: null,
  currentBranchMessageId: null,  // 当前选中的分支消息ID
  currentTaskId: null,
  selectedMessageIdFromConfig: null,  // 🔧 Task#153：从配置恢复的选中消息ID
  selectedTaskIdFromConfig: null,     // 🔧 Task#153：从配置恢复的选中事务ID
  selectedBranchMessageIdFromConfig: null,  // 🔧 Task#153：从配置恢复的选中分支消息ID
  currentEditType: null,  // 当前编辑的类型：'message', 'branch-message', 'task'
  searchTerm: '',
  theme: 'dark',
  inputHistory: [],
  historyIndex: -1,
  historyDraft: '',
  // 🔧 无限 undo/redo 栈（字符级）
  undoStack: [],  // 所有历史状态栈
  redoStack: [],  // redo 栈（新输入时清空）
  isUndoing: false,  // 防止递归记录
  lastInputValue: '',  // 上一次输入框的值（用于检测变化）
  lastInputCursor: 0,  // 上一次光标位置
  expandedMessages: new Map(),  // Map<msgId, expandedTimestamp>
  collapsedMessages: new Map(),  // Map<msgId, collapsedTimestamp>
  expandedTasks: new Map(),  // Map<taskId, expandedTimestamp>
  collapsedTasks: new Map(),  // Map<taskId, collapsedTimestamp>
  fullyExpandedTasks: new Set(),  // Set<taskId> 记录已完全展开（展开所有子事务）的事务树
  expandedMessageNodes: new Map(),  // Map<msgId, expandedTimestamp> 消息树节点的展开状态
  collapsedMessageNodes: new Map(),  // Map<msgId, collapsedTimestamp> 消息树节点的折叠状态
  showArchived: false,  // 是否显示已归档消息
  showDeleteButtons: false,  // 是否显示删除按钮（消息栏）
  showTaskDeleteButtons: false,  // 是否显示删除按钮（事务栏）
  enableDrag: false,  // 是否启用拖动功能（消息/事务拖拽）
  attachedFiles: [],  // 附件文件列表
  inputHistory: [],  // 输入历史记录（服务器存储）
  outputHistory: new Map(),  // 输出历史（缓存：Map<taskId|messageId, outputHistory[]>，每个事务/消息独立缓存）
  hasMoreHistory: new Map(),  // 每个 taskId 是否还有更多老历史在服务器上（Map<taskId, boolean>）
  currentOldestTimestamp: null,  // 当前显示的最旧时间戳
  currentNewestTimestamp: null,  // 当前显示的最新时间戳
  output: null,  // xterm 终端实例
  outputFit: null,  // xterm fit addon
  ws: null,  // WebSocket 实例
  isLoading: false,  // 加载状态
  isCreatingNewMessage: false,  // 是否正在创建新消息
  isReplyingMessage: false,  // 是否正在回复消息（回复消息不自动切换选中状态，保持链首）
  _isLocalReply: false,  // 🔧 Task#295：本地回复标志，供handleMessageUpdate跳过自动选择
  _localCreatedMsgId: null,  // 🔧 Task#265：本实例创建的消息ID，用于区分本地操作和远程广播，避免其他webui实例被干扰
  scrollToMessageId: null,  // 需要滚动到的消息ID（新消息创建后）
  displayedMessageIds: new Set(),  // 当前显示的消息ID集合
  inputMode: 'message',  // 输入模式: 'message'(消息模式) 或 'task'(事务提示模式)
  filteredHistoryIndex: -1,  // 过滤后历史列表的选中索引（-1表示未选中）
  filteredHistory: [],  // 过滤后的历史列表
  // 🔧 自动保存相关标志
  isSaving: false,  // 正在保存到临时文件
  // 输出来源过滤
  outputSourceFilters: {
    thinking: true,
    bash: true,
    tool_call: true,
    tool_result: true,
    system: true,
    task: true
  },
  // WebUI 配置（从 config/webui.json 加载，初始值与后端默认值一致）
  // 🔧 注意：initOutput() 在 loadWebUIConfig() 之前调用，必须有初始值
  webuiConfig: {
    messages_refresh_interval: null,  // 从配置加载
    tasks_refresh_interval: null,     // 从配置加载
    xterm: {
      scrollback: 500,
      show_line_numbers: false
    },
    // 内容截断配置（从 config/webui.json 加载）
    content_trunc_lines: 10,            // 内容最大显示行数
    content_trunc_chars: 300,           // 内容最大显示字符数（不是字节）
    // 字体配置（从 config/webui.json 加载）
    font: {
      family: 'JetBrains Mono, Fira Code, Consolas, Monaco, monospace',  // CSS font-family 候选字符串，逗号分隔
      size: 14,
      size_xterm: 14,
      size_editor: 14
    },
    // 侧边栏宽度配置（从 config/webui.json 加载，单位 px）
    sidebar_left_width: 320,
    sidebar_right_width: 220,
    // 🔧 Task#262：大文件编辑器加载配置（从 config/webui.json 加载）
    large_file_threshold: 3 * 1024 * 1024,            // 大文件分块加载阈值（默认3MB）
    large_file_parallel_threshold: 6 * 1024 * 1024,   // 并发分块加载阈值（默认6MB）
    large_file_parallel_concurrency: 4,               // 并发加载并发数
    large_file_chunk_size: 1024 * 1024,               // 每个分块大小（默认1MB）
    input_history_limit: 500,                          // 输入历史记录最大条数（前后端统一配置）
    // 🔧 Task#418：编辑器配置（从 config/webui.json 加载）
    editor: {
      emoji_text_mode: false,  // emoji文字模式，默认关闭(彩色emoji)
      auto_create_empty_doc: false,  // 切换到编辑器视图时是否自动创建空文档，默认否
    },
  },
  // 添加鹏和master配置
  agentName: '鹏🐣',
  masterName: 'master',
  // 工作目录
  workdir: '~/',
  // 🔧 远程nest相关状态
  remoteNest: null,        // 当前选中的远程nest名称（null表示本地）
  remoteNestRoot: null,    // 远程nest的根路径（如 '/' 或 'C:/'）
  // 🔧 Task#1641：当前选中nest的确定性平台标识（'windows'/'linux'/null未知）
  // 由 _prepareNestState/_updateWorkdirForNest 等nest状态切换点同步维护，
  // isCurrentNestWindows() 优先使用，避免依赖 DOM 异步更新时序导致误判
  nestPlatform: null,
  // 文件浏览器是否从编辑器打开
  isFileBrowserFromEditor: false,
  // 文件浏览器是否处于"另存为"模式
  isSaveAsMode: false,
  // 🔧 Task#326：已删除 editorTabs 全局数组，标签页数据统一从 state.editorGroups 获取
  // 使用 getG0Tabs() 获取group0的tabs，getAllEditorTabs() 获取所有group的tabs
  // 编辑器分组（唯一数据源）
  editorGroups: [],
  // 视图标签页（终端/编辑器）
  currentViewTab: 'output',
  // 远程终端标签页
  terminalTabs: [],  // 终端标签页列表
  currentTerminalTabId: null,  // 当前活动的终端标签页ID
  // API providers
  providers: [],
  currentProvider: null,
  // 拖拽状态
  dragState: {
    isDragging: false,
    type: null,  // 'message' 或 'task'
    id: null,    // 拖拽的节点ID
    sourceRootId: null  // 消息树：根消息ID
  },
  // 每个消息/分支消息/事务/分支事务的上次选中时间戳（用于折叠超时判断）
  lastSelectedTimestamps: {},
  // 折叠超时阈值（毫秒）：超过此时间后点击已选中项不触发折叠
  collapseToggleTimeout: 3000,
  // 项目目录结构（用于路径超链接二次过滤）
  projectDirs: new Set(),   // 项目目录相对路径集合（如 "src", "src/peng", "config"）
  projectFiles: new Set(),  // 项目根目录文件名集合（如 "README.md", "pyproject.toml"）
  // 后端运行平台（"linux" 或 "windows"，用于路径超链接匹配策略）
  platform: 'linux',
  // 🔧 Task #87：跟踪终端当前活动的 ANSI SGR 颜色状态（跨行记忆）
  // 当超链接转换需要恢复颜色时，使用此值作为"前一个颜色"
  // 每次处理完一行后更新为该行最后一个 SGR 序列
  currentColor: '',
  // 项目根目录（从后端 /api/config 加载，用于 workdir 回退）
  projectRoot: null,
  // Provider 吞吐率数据（从 WebSocket throughput_update 消息更新）
  providerThroughputs: {},  // {provider: throughput (tokens/sec)}
  providerFrameRates: {},   // {provider: animation_duration (seconds)}
  // 🔧 Nest 网络流量数据（从 WebSocket throughput_update 消息更新）
  nestTraffic: [],  // [{display_key, display_name, platform, linux_id, total_rate, ...}]
  // 🔧 Task #92：用户是否在xterm中向上滚动查看历史（非底部）
  // 当为true时，appendOutput 不直接写入xterm，保持显示内容稳定
  userScrolledUp: false,
  // 🔧 Task#219：记录每个事务上次查看时的最新输出时间戳
  // 切换到其他事务时保存，切回时用于 sinceTime 增量获取期间的新输出
  lastViewedTimestamps: new Map(),  // Map<taskId, timestamp>
  // 🔧 Task#219：上次切换前的事务ID，用于在切换时保存其时间戳
  _prevTaskIdForSwitch: null,
  // 🔧 Task#228：顶置列表（从 config/webui.json 加载）
  toppedMessageIds: new Set(),
  toppedTaskIds: new Set(),
  // 🔧 Fix Task#840：翻页改用全局行号定位（后端提供确定性行号）
  _pagingStartLine: -1,  // 当前显示页的起始行号（-1=未在翻页模式）
  _pagingEndLine: -1,    // 当前显示页的结束行号
  _totalLines: 0,        // 后端报告的文件总行数
  _pagingRecoveryUntil: null,
  // 🔧 Fix #1064: buffer长度追踪，用于区分程序写入引起的onScroll和用户滚动引起的onScroll
  _lastBufferLength: 0,
  // 🔧 Task#1102：上一页正在从后端加载中（true=xterm显示"加载中"占位）
  _loadingPrevPage: false,
  // 兼容旧代码引用
  _pagingAnchorNewestTs: null,
  _pagingPageNewestTs: null,
  _pagingStartIdx: -1,
  _pagingEndIdx: -1,
};

