/**
 * {{ agent_name }} Web UI - 主入口文件
 * 
 * 本文件负责按依赖顺序加载所有模块脚本。
 * 原始业务代码已拆分到 modules/ 目录下，第三方库移至 vendor/ 目录。
 * 
 * 使用 document.write 同步加载，确保：
 * 1. 加载顺序与原始单文件一致
 * 2. const/let/var 声明在全局作用域（与原<script>标签行为一致）
 */

// 模块加载列表（按依赖顺序，路径相对于 /static/js/）
var __peng_modules = [
  'modules/state.js',
  'modules/output-history.js',
  'modules/config.js',
  'modules/utils.js',
  'modules/xterm-themes.js',
  'modules/terminal-reconnect.js',
  'modules/workdir.js',
  'modules/sidebar-resize.js',
  'modules/output-filters.js',
  'modules/status.js',
  'modules/theme-ui.js',
  'modules/font-settings.js',
  'modules/view-tabs.js',
  'modules/focus.js',
  'modules/upload.js',
  'modules/terminal-core.js',
  'modules/terminal-remote.js',
  'modules/terminal-events.js',
  'modules/websocket.js',
  'modules/lang-icons.js',
  'modules/lsp-provider.js',
  'modules/editor-core.js',
  'modules/editor-storage.js',
  'modules/editor-layout.js',
  'modules/editor-tab-history.js',
  'modules/editor-autosave.js',
  'modules/file-changes.js',
  'modules/file-operations.js',
  'modules/output-display.js',
  'modules/output-highlight.js',
  'modules/output-search.js',
  'modules/messages.js',
  'modules/message-interaction.js',
  'modules/message-actions.js',
  'modules/drag-drop.js',
  'modules/tasks.js',
  'modules/history-dropdown.js',
  'modules/input-history.js',
  'modules/send-message.js',
  'modules/providers.js',
  'modules/coding-plan.js',
  'modules/deepseek-usage.js',
  'modules/snapshots.js',
  'modules/nest.js',
  'modules/channels.js',
  'modules/debug.js',
  'modules/events.js',
  'modules/init.js',
];

// 使用 document.write 在解析阶段同步插入 <script> 标签
// 路径基于 app.js 的位置（/static/js/），使用绝对路径确保正确解析
// 🔧 Task#327：使用随机时间戳破坏浏览器缓存
// 每次页面加载（index.html的cache_v服务端时间戳已经让app.js重新加载了）
// 模块文件也需要每次重新加载，确保开发期代码更新立即生效
var __peng_v = window.__peng_cache_v || Date.now().toString();
for (var i = 0; i < __peng_modules.length; i++) {
  document.write('<script src="/static/js/' + __peng_modules[i] + '?v=' + __peng_v + '"><\/script>');
}
