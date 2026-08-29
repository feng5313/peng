// ==================== 终端输出 ====================

/**
 * 🔧 Fix Task#840：格式化行号前缀（使用主题配置的灰色）
 * 从 XTERM_THEMES 当前主题的 brightBlack 取灰色
 * @param {number} lineNo - 文件行号
 * @returns {string} ANSI 灰色行号前缀，或空字符串（如果配置关闭）
 */
function formatLineNoPrefix(lineNo) {
  if (!state.webuiConfig.xterm.show_line_numbers) return '';
  if (!lineNo) return '';
  // 从主题取 grey 颜色
  var theme = (typeof XTERM_THEMES !== 'undefined') ? (XTERM_THEMES[state.theme] || XTERM_THEMES.dark) : null;
  var gray = theme ? (theme.darkGrey || theme.grey || theme.brightBlack || '#484f58') : '#484f58';
  // 转换 hex 为 RGB
  var r = parseInt(gray.slice(1, 3), 16);
  var g = parseInt(gray.slice(3, 5), 16);
  var b = parseInt(gray.slice(5, 7), 16);
  // 真彩色 ANSI: \x1b[38;2;R;G;Bm + 行号右对齐7位 + reset + 空格
  return '\x1b[38;2;' + r + ';' + g + ';' + b + 'm' + String(lineNo).padEnd(6) + '\x1b[0m';
}

/**
 * 输出消息到 xterm 终端（带来源标记，与大模型输出区分）
 * @param {string} message - 消息内容
 * @param {string} type - 消息类型：'info' | 'success' | 'warning' | 'error'
 */
function showInOutput(message, type = 'info') {
  if (!state.output) {
    return;
  }

  // 颜色映射
  const colors = {
    'info': '\x1b[36m',      // 青色
    'success': '\x1b[32m',   // 绿色
    'warning': '\x1b[33m',   // 黄色
    'error': '\x1b[31m',     // 红色
  };
  const color = colors[type] || colors['info'];
  const reset = '\x1b[0m';

  // 来源标记，与大模型输出区分，转换文件路径为OSC8超链接格式
  state.output.write(`${color}${convertFilePathsToLinks(message)}${reset}\r\n`);
  // 🔧 修复 Task #87：showInOutput 在链接转换后额外追加了 reset，需要同步更新颜色记忆
  state.currentColor = reset;
}

/**
 * 根据时间戳查找历史记录中的索引位置
 * @param {Array} history - 历史记录数组
 * @param {number} timestamp - 要查找的时间戳
 * @returns {number} 索引位置，如果找不到则返回 -1
 */
function findIndexByTimestamp(history, timestamp) {
  for (let i = 0; i < history.length; i++) {
    if (history[i].timestamp === timestamp) {
      return i;
    }
  }
  return -1;
}

function getNewestTimestamp() {
  const currentHistory = getOutputHistory(state.currentTaskId);
  if (currentHistory.length === 0) return null;
  return currentHistory[currentHistory.length - 1].timestamp;
}

function hasNewerOutput() {
  const limit = state.webuiConfig.xterm.scrollback || 500;
  if (state._pagingStartLine < 0) return false;  // 实时模式已是最新
  // 🔧 Fix #1064 缓存兜底：_totalLines 可能尚未同步，用缓存最大行号兜底
  const currentHistory = getOutputHistory(state.currentTaskId);
  const maxLineNo = currentHistory.length > 0
    ? (currentHistory[currentHistory.length - 1].fileLineNo || 0) : 0;
  const total = Math.max(state._totalLines, maxLineNo);
  return state._pagingEndLine < total;
}

function hasOlderOutput() {
  const limit = state.webuiConfig.xterm.scrollback || 500;
  if (state._pagingStartLine < 0) {
    // 实时模式：总行数超过一页 或 服务端还有更早历史
    return state._totalLines > limit
      || state.hasMoreHistory.get(state.currentTaskId) === true;
  }
  return state._pagingStartLine > 1;
}

// ==================== 🔧 Task#1725：O(1) 行号翻页核心 ====================

/**
 * 缓存是否完整覆盖行号范围 [startLine, endLine]（含两端）
 */
function cacheHasFullRange(startLine, endLine) {
  const history = getOutputHistory(state.currentTaskId);
  const need = endLine - startLine + 1;
  let found = 0;
  for (const item of history) {
    const ln = item.fileLineNo;
    if (ln >= startLine && ln <= endLine) {
      found++;
      if (found >= need) return true;
    }
  }
  return false;
}

/**
 * 原子渲染行号范围 [startLine, endLine]（唯一写屏入口）
 *
 * - clear+write 一体完成，消除"清屏后等待"黑屏窗口
 * - 行数据从缓存按 fileLineNo 精确匹配；passFilters 渲染期过滤
 * - 更新 _pagingStartLine/_pagingEndLine/_xtermNewestLineNo
 */
function renderPageRange(startLine, endLine) {
  if (!state.output) return;
  const history = getOutputHistory(state.currentTaskId);
  const byLine = new Map();
  for (const item of history) {
    const ln = item.fileLineNo;
    if (ln >= startLine && ln <= endLine) byLine.set(ln, item);
  }

  // 抑制渲染期间的 onScroll 误判（原子窗口）
  state._pagingRecoveryUntil = Date.now() + 800;
  state.output.clear();
  state.currentColor = '';

  const chunks = [];
  for (let ln = startLine; ln <= endLine; ln++) {
    const item = byLine.get(ln);
    if (item && passFilters(item.source)) {
      chunks.push(formatLineNoPrefix(item.fileLineNo) + convertFilePathsToLinks(item.content) + '\r\n');
    }
  }
  if (chunks.length > 0) {
    writeAndScroll(chunks.join(''), () => {
      if (state.output) state.output.scrollToBottom();
    });
  } else {
    setTimeout(() => { if (state.output) state.output.scrollToBottom(); }, 0);
  }

  // 状态推进
  state._pagingStartLine = startLine;
  state._pagingEndLine = endLine;
  state._xtermNewestLineNo = Math.max(state._xtermNewestLineNo || 0, endLine);
  // 时间戳指针同步到页面最新行（保证 appendOutput 的去重判断正确）
  let newestItem = null;
  for (let ln = endLine; ln >= startLine; ln--) {
    const it = byLine.get(ln);
    if (it) { newestItem = it; break; }
  }
  if (newestItem && newestItem.timestamp) {
    state.currentNewestTimestamp = newestItem.timestamp;
  }
  let oldestItem = null;
  for (let ln = startLine; ln <= endLine; ln++) {
    const it = byLine.get(ln);
    if (it) { oldestItem = it; break; }
  }
  if (oldestItem && oldestItem.timestamp) {
    state.currentOldestTimestamp = oldestItem.timestamp;
  }
  updatePaginationButtons();
}

/**
 * 向后端请求行号范围页（O(1) seek直读）—— 非破坏性：不清屏，仅 toast 提示
 * 响应由 websocket.js 的 handlePageRangeBatch 缓存并触发 renderPageRange
 */
function fetchPage(startLine, endLine) {
  if (state._loadingPage) return;  // 已有翻页请求进行中
  state._loadingPage = { start: startLine, end: endLine };
  // 🔧 Task#1725：按风主要求去除"加载第x-y行"toast（渲染本身就是反馈，toast纯属打扰）
  subscribeOutput(0, false, null, null, startLine, endLine);
  // 🔧 超时兜底：后端无响应时解锁（屏幕保持当前页，绝不黑屏）
  const token = state._loadingPage;
  setTimeout(() => {
    if (state._loadingPage === token) {
      state._loadingPage = null;
      showSimpleToast('❌ 加载超时，请重试', 'error');
    }
  }, 5000);
}

// ==================== 输出头部功能链接 ====================
/**
 * 更新分页按钮状态
 */
function updatePaginationButtons() {
  const prevBtn = document.getElementById('btn-prev-page');
  const nextBtn = document.getElementById('btn-next-page');

  const hasOlder = hasOlderOutput();
  const hasNewer = hasNewerOutput();

  if (prevBtn) {
    prevBtn.disabled = !hasOlder;
    prevBtn.style.opacity = prevBtn.disabled ? '0.5' : '1';
    prevBtn.title = '上一页';
  }

  if (nextBtn) {
    nextBtn.disabled = !hasNewer;
    nextBtn.style.opacity = nextBtn.disabled ? '0.5' : '1';
    nextBtn.title = '下一页';
  }

  // 同步更新非默认布局的翻页按钮状态
  ['2', '3'].forEach(suffix => {
    const p = document.getElementById('btn-prev-page-' + suffix);
    const n = document.getElementById('btn-next-page-' + suffix);
    if (p) { p.disabled = !hasOlder; p.style.opacity = p.disabled ? '0.5' : '1'; }
    if (n) { n.disabled = !hasNewer; n.style.opacity = n.disabled ? '0.5' : '1'; }
  });
}

function isAtBottom() {
  if (!state.output) return true;
  const buffer = state.output.buffer.active;
  const viewportBottom = buffer.viewportY + state.output.rows;
  const bufferLength = buffer.length;
  // 🔧 Fix Task#840：收紧阈值从-5到-2，避免提前触发翻页/跳底
  // 如果距离底部小于2行，认为已到底部
  return viewportBottom >= bufferLength - 2;
}

/**
 * 🔧 Fix Task#1096：精确底部检测（阈值0），用于 onScroll 中判断用户是否向上滚动
 * isAtBottom() 的 -2 阈值在 buffer 滚动区很小时（1-2行超出viewport），
 * 即使用户已滚动到顶部仍返回 true，导致 userScrolledUp 永远不会被设为 true，
 * appendOutput 的 scrollToBottom() 不断将用户拉回底部。
 * 此函数使用精确匹配（阈值0），确保任何偏离底部的滚动都能被正确检测。
 */
function isExactlyAtBottom() {
  if (!state.output) return true;
  const buffer = state.output.buffer.active;
  const viewportBottom = buffer.viewportY + state.output.rows;
  return viewportBottom >= buffer.length;
}

function isAtTop() {
  if (!state.output) return true;
  return state.output.buffer.active.viewportY === 0;
}

/**
 * 🔧 Fix Task#1111：可靠的"写入后滚动"辅助函数
 *
 * 根因：xterm.js v6 的 write(data, callback) callback 从不触发（内部出队列但不调用），
 * 导致 commit 9de75a77 的所有 write+callback 模式失效，scrollToBottom/scrollToTop 永不执行。
 *
 * 方案：写入数据后，轮询 xterm 内部 _writeBuffer 直到写入完成（通常<15ms），
 * 然后执行回调。比 setTimeout(50) 更可靠（大数据也能正确等待），比 callback 更可用。
 *
 * 🔧 Fix Task#1404：写入前重置 isUserScrolling，防止 clear() 后 viewport 弹到顶部。
 * xterm.js 的 clear() 不重置 BufferService.isUserScrolling，导致 write() 过程中
 * ydisp 不跟随 ybase 增长（_fillFgRows 中 isUserScrolling 为 true 时跳过 ydisp 更新），
 * viewport 闪到顶部。重置后 write() 的自动滚动正常工作。
 *
 * @param {string} data - 要写入的数据
 * @param {function} afterWrite - 写入完成后的回调（如 scrollToBottom）
 */
function writeAndScroll(data, afterWrite) {
  if (!state.output) return;
  // 🔧 Fix Task#1404：写入前清除 xterm 内部的 isUserScrolling 标志
  // clear() 不重置此标志，残留的 true 会导致 write() 过程中 viewport 卡在顶部
  var core = state.output._core;
  if (core && core._bufferService) {
    core._bufferService.isUserScrolling = false;
  }
  state.output.write(data);
  // 轮询内部 writeBuffer 检测写入完成
  var wb = core && core._writeBuffer;
  if (!wb) { setTimeout(afterWrite, 50); return; }
  var start = Date.now();
  function poll() {
    if (!state.output) return;
    if (wb._writeBuffer.length === 0 && wb._pendingData === 0) {
      afterWrite();
    } else if (Date.now() - start < 2000) {  // 最多等2秒
      setTimeout(poll, 5);
    } else {
      afterWrite();  // 超时也执行回调
    }
  }
  setTimeout(poll, 5);
}

/**
 * 🔧 Fix Task#840：计算页码信息
 * 页码从末尾倒算：最后一页 = totalPages，每往前 limit 条减1页
 * @param {number} startLine - 当前页起始行号
 * @returns {object} {page, totalPages} 或 null
 */
function getPageInfo(startLine) {
  const limit = state.webuiConfig.xterm.scrollback || 500;
  // 🔧 Fix Task#840：总行数 = 后端 totalLines + appendOutput 新增行数
  const total = state._totalLines || 0;
  if (total <= 0 || limit <= 0 || startLine <= 0) return null;
  const totalPages = Math.ceil(total / limit);
  const lastPageStart = (totalPages - 1) * limit + 1;
  const pagesFromEnd = Math.floor((lastPageStart - startLine) / limit);
  const page = totalPages - pagesFromEnd;
  return { page: Math.max(1, page), totalPages };
}
/**
 * 🔧 Task#1725：加载上一页（更旧的输出）
 *
 * 行号语义：当前页 [s,e] → 上一页 = [max(1, s-limit), s-1]；实时模式先取窗口 [total-limit+1, total]。
 * 缓存完整命中 → renderPageRange 直接渲染（0ms）；否则 fetchPage 走后端 O(1) seek。
 * 全程不清屏（消灭黑屏），加载中为非破坏性 toast。
 */
async function loadPrevPage() {
  if (!state.output || state._loadingPage) return;
  const currentHistory = getOutputHistory(state.currentTaskId);
  if (currentHistory.length === 0 && state._totalLines <= 0) return;

  const limit = state.webuiConfig.xterm.scrollback || 500;
  let startLine, endLine;

  if (state._pagingStartLine < 0) {
    // 实时模式 → 首次翻页：上一页是 [total-2*limit+1, total-limit]
    const maxLineNo = currentHistory.length > 0
      ? (currentHistory[currentHistory.length - 1].fileLineNo || 0) : 0;
    const total = Math.max(state._totalLines, maxLineNo);
    if (total <= limit) {
      showSimpleToast('⏹️ 没有更旧的输出了', 'info');
      return;
    }
    endLine = total - limit;
    startLine = Math.max(1, endLine - limit + 1);
  } else {
    if (state._pagingStartLine <= 1) {
      showSimpleToast('⏹️ 没有更旧的输出了', 'info');
      return;
    }
    endLine = state._pagingStartLine - 1;
    startLine = Math.max(1, endLine - limit + 1);
  }

  state._browsingHistory = true;
  state.userScrolledUp = true;

  if (cacheHasFullRange(startLine, endLine)) {
    // 缓存完整命中 → 直接渲染（无网络请求，无闪烁）
    renderPageRange(startLine, endLine);
    var pi = getPageInfo(startLine);
    showSimpleToast(pi ? ('↑️ 第' + pi.page + '/' + pi.totalPages + '页') : '↑️ 上一页', 'info');
  } else {
    // 缓存不足 → 后端 O(1) 行号翻页（响应后自动渲染）
    fetchPage(startLine, endLine);
  }
}


/**
 * 🔧 Task#1725：加载下一页（更新的输出）
 *
 * 行号语义：当前页 [s,e] → 下一页 = [e+1, min(total, e+limit)]。
 * 已在最后一页 → 回到实时模式。缓存命中直渲，否则后端 O(1)。
 */
async function loadNextPage() {
  if (!state.output || state._loadingPage) return;
  const currentHistory = getOutputHistory(state.currentTaskId);

  // 未翻页模式 → 直接回实时
  if (state._pagingStartLine < 0) {
    state._browsingHistory = false;
    state.userScrolledUp = false;
    scrollToNewest();
    return;
  }

  const limit = state.webuiConfig.xterm.scrollback || 500;
  const maxLineNo = currentHistory.length > 0
    ? (currentHistory[currentHistory.length - 1].fileLineNo || 0) : 0;
  const total = Math.max(state._totalLines, maxLineNo);
  const startLine = state._pagingEndLine + 1;

  if (startLine > total) {
    // 已是最后一页 → 回实时模式
    scrollToNewest();
    return;
  }

  const endLine = Math.min(total, startLine + limit - 1);
  state._browsingHistory = true;
  state.userScrolledUp = true;

  if (cacheHasFullRange(startLine, endLine)) {
    renderPageRange(startLine, endLine);
    var pi = getPageInfo(startLine);
    showSimpleToast(pi ? ('↓️ 第' + pi.page + '/' + pi.totalPages + '页') : '↓️ 下一页', 'info');
  } else {
    fetchPage(startLine, endLine);
  }
}

/**
 * 🔧 Fix Task#1433：从缓存全量渲染到 xterm（切换事务时立即调用）
 * 🔧 Task#1725：内部统一走 renderPageRange（渲染最后一页）
 */
function renderFromCache(taskId) {
  if (!state.output) return;
  const currentHistory = getOutputHistory(taskId);
  if (currentHistory.length === 0) return;

  const limit = state.webuiConfig.xterm.scrollback || 500;

  // 取最新的 limit 条的行号范围
  const maxLineNo = currentHistory.reduce(function(m, i) { return Math.max(m, i.fileLineNo || 0); }, 0);
  const endLine = Math.max(maxLineNo, currentHistory.length);
  const startLine = Math.max(1, endLine - limit + 1);

  renderPageRange(startLine, endLine);

  // 🔧 首屏渲染后恢复实时模式（renderPageRange 会设置页范围，实时语义必须为 -1）
  state._pagingStartLine = -1;
  state._pagingEndLine = -1;
  state._browsingHistory = false;
  state.userScrolledUp = false;

  // 从缓存恢复 _totalLines
  if (maxLineNo > state._totalLines) {
    state._totalLines = maxLineNo;
  }
}

/**
 * 🔧 Task#1725：回到底部 —— 恢复实时模式
 *
 * 快速路径：未翻页且 xterm 已含最新行 → 仅 scrollToBottom（无闪屏，高频实时输出安全）
 * 否则：全量渲染最后一页（缓存命中直渲 / 后端 O(1) 请求），彻底消灭"时间戳指针脱节导致的内容缺失"
 */
function scrollToNewest() {
  // 清除翻页加载占位与浏览状态
  state._loadingPage = null;
  state._browsingHistory = false;
  state.userScrolledUp = false;

  // 清除输出搜索状态（回到底部时搜索不再有效）
  if (typeof _clearSearch === 'function') _clearSearch();

  if (!state.output) return;
  const currentHistory = getOutputHistory(state.currentTaskId);
  const limit = state.webuiConfig.xterm.scrollback || 500;
  const maxLineNo = currentHistory.length > 0
    ? (currentHistory[currentHistory.length - 1].fileLineNo || 0) : 0;
  const total = Math.max(state._totalLines, maxLineNo);

  // 快速路径：实时模式且 xterm 已含全部最新行 → 只滚动到底部
  if (state._pagingStartLine < 0 && (state._xtermNewestLineNo || 0) >= maxLineNo) {
    setTimeout(() => { if (state.output) state.output.scrollToBottom(); }, 0);
    state._pagingStartLine = -1;
    state._pagingEndLine = -1;
    updatePaginationButtons();
    return;
  }

  // 全量渲染最后一页
  if (total <= 0) {
    state._pagingStartLine = -1;
    state._pagingEndLine = -1;
    return;
  }
  const endLine = total;
  const startLine = Math.max(1, endLine - limit + 1);
  if (cacheHasFullRange(startLine, endLine)) {
    renderPageRange(startLine, endLine);
    // 渲染后恢复实时模式
    state._pagingStartLine = -1;
    state._pagingEndLine = -1;
    state._browsingHistory = false;
    state.userScrolledUp = false;
    updatePaginationButtons();
  } else {
    // 后端请求（响应后 handlePageRangeBatch 渲染），渲染完成后恢复实时
    state._browsingHistory = true;  // 请求期间保持浏览态，防止渲染前被实时输出干扰
    fetchPage(startLine, endLine);
    // 渲染后恢复实时：由 renderPageRange 完成后的统一出口处理
    const watch = setInterval(() => {
      if (!state._loadingPage && state._pagingEndLine >= endLine) {
        clearInterval(watch);
        state._pagingStartLine = -1;
        state._pagingEndLine = -1;
        state._browsingHistory = false;
        state.userScrolledUp = false;
        updatePaginationButtons();
      } else if (!state._loadingPage) {
        clearInterval(watch);  // 超时/空响应
      }
    }, 100);
    setTimeout(() => clearInterval(watch), 8000);
  }
}

/**
 * 渲染"上一页"加载的更老历史（旧 beforeTimestamp 协议兼容入口）
 * 🔧 Task#1725：内部统一走 renderPageRange（按行号渲染，不再手工拼 chunks）
 */
function renderOlderHistory(taskId, newOutputs) {
  if (!state.output) return;
  if (!newOutputs || newOutputs.length === 0) {
    restoreCurrentPage();
    return;
  }
  const currentHistory = getOutputHistory(taskId);
  if (currentHistory.length === 0) return;

  const limit = state.webuiConfig.xterm.scrollback || 500;

  // 取 newOutputs 的行号范围
  const lineNos = newOutputs.map(o => o.fileLineNo || 0).filter(ln => ln > 0);
  if (lineNos.length === 0) return;
  const newStartLine = Math.min(...lineNos);
  const newEndLine = Math.max(...lineNos);

  renderPageRange(newStartLine, newEndLine);

  var pi = getPageInfo(newStartLine);
  showSimpleToast(pi ? ('↑️ 第' + pi.page + '/' + pi.totalPages + '页') : '↑️ 上一页', 'info');
  updatePaginationButtons();
}

/**
 * 🔧 Task#1725：恢复当前页（后端返回空数据/失败时调用）
 */
function restoreCurrentPage() {
  if (!state.output) return;
  if (state._pagingStartLine > 0 && state._pagingEndLine > 0) {
    renderPageRange(state._pagingStartLine, state._pagingEndLine);
  } else {
    scrollToNewest();
  }
}

/**
 * 显示历史输出头部提示信息
 * @param {number} messageId - 消息ID
 * @param {Array} taskIds - 事务ID列表
 * @param {number} scrollback - 缓冲区行数
 */
function showOutputHeaderLinks(messageId, taskIds, scrollback) {

  // 更新分页按钮状态
  updatePaginationButtons();
}
// 收集事务及其所有子孙事务的ID列表
function collectTaskIds(taskId) {
  const allTasks = flattenTasks(state.tasks);
  const taskMap = new Map();
  allTasks.forEach(t => taskMap.set(t.id, t));

  const ids = [taskId];
  const collectChildren = (parentId) => {
    for (const t of allTasks) {
      if (t.parent_task_id === parentId) {
        ids.push(t.id);
        collectChildren(t.id);
      }
    }
  };
  collectChildren(taskId);
  return ids;
}

async function clearOutput(task_id = 0, deleteFile = false, extraTaskIds = null) {
  if (!state.output) return;

  try {
    const taskId = task_id ? task_id : state.currentTaskId;

    if (deleteFile) {
      // 调用API清空输出文件
      const url = `/api/output-history?task_id=${taskId}`;

      const response = await fetch(url, {
        method: 'DELETE'
      });

      if (!response.ok) {
        showSimpleToast('❌ 清空输出失败', 'error');
        return;
      }
    }

    // 清空主事务的缓存
    state.outputHistory.delete(taskId);
    state.hasMoreHistory.delete(taskId);
    state.currentOldestTimestamp = null;
    state.currentNewestTimestamp = null;
    state._pagingStartLine = -1;
    state._pagingEndLine = -1;
    state._totalLines = 0;
    state._loadingPage = null;  // 🔧 Task#1725：取消进行中的行号翻页请求
    state._xtermNewestLineNo = 0;  // 🔧 Task#1725：重置 xterm 内容行号

    // 清空额外事务的缓存（子事务等）
    if (extraTaskIds && Array.isArray(extraTaskIds)) {
      for (const id of extraTaskIds) {
        state.outputHistory.delete(id);
        state.hasMoreHistory.delete(id);
      }
    }

    // 清空终端
    state.output.clear(); state.currentColor = '';
    const clearedCount = extraTaskIds ? extraTaskIds.length + 1 : 1;
    showSimpleToast(`✅ 已清空 ${clearedCount} 个事务的输出`, 'success');
    await subscribeOutput(0, true); // 重新订阅输出
    setTimeout(() => {
      scrollToNewest();
    }, 50);
  } catch (error) {
    console.error('清空输出失败:', error);
    showSimpleToast(`❌ 清空输出失败${error.message}`, 'error');
  }
}

/**
 * 在Monaco编辑器中查看完整输出（从缓存中获取纯文本内容，去除ANSI转义序列）
 */
async function viewOutputInEditor() {
  if (!state.output) return;

  try {
    // 从outputHistory获取纯文本内容
    const currentHistory = getOutputHistory(state.currentTaskId);
    const text = currentHistory
      .map(item => {
        // 移除ANSI转义序列
        return (item?.content || '').replace(/\x1b\[[0-9;]*m/g, '').replace(/\x1b\][^\x07]+\x07/g, '');
      })
      .join('\n');

    // 生成标签ID
    const tabId = 'tab-output-full-text';

    // 切换到编辑器视图（这会恢复保存的标签页）
    await switchViewTab('editor', { skipAutoCreate: true });

    // 等待Monaco Editor完全加载
    await loadMonacoEditor();

    // 检查是否已存在该标签
    const existingTab = findTabById(tabId);

    if (existingTab) {
      // 更新内容
      existingTab.content = text;
      setActiveEditorTabId(tabId);  // 🔧 Task#334
      updateEditorContent(existingTab);
    } else {
      // 保存当前标签的状态（如果存在）
      if (getActiveEditorTabId()) {  // 🔧 Task#334
        saveCurrentTabState();
      }

      // 创建新标签（虚拟文件，不可保存）
      const newTab = {
        id: tabId,
        path: '输出全文.md',
        name: '输出全文.md',
        language: 'markdown',
        content: text,
        cursorPosition: null,
        scrollTop: null,
        isVirtual: true  // 标记为虚拟文件
      };

      // 🔧 Task#340：添加到group0（唯一数据源）
      ensureGroup0();
      state.editorGroups[0].tabs.push(newTab);
      setActiveEditorTabId(tabId);  // 🔧 Task#334

      // 更新编辑器内容
      updateEditorContent(newTab);
    }

    // 更新标题
    document.getElementById('editor-file-path').textContent = '输出全文（只读）';
    if (typeof setLanguageBadge === 'function') { setLanguageBadge('markdown'); } else { document.getElementById('editor-language').textContent = 'markdown'; }

    // 获取编辑器视图容器
    const editorView = document.getElementById('editor-view');
    if (editorView) {
      editorView.dataset.currentFilePath = '输出全文.md';
      editorView.dataset.isVirtual = 'true';  // 标记为虚拟文件
    }

    // 更新标签页 UI
    renderTabs();

    // 禁用保存按钮（虚拟文件不可保存）
    const saveBtn = document.getElementById('editor-save');
    if (saveBtn) {
      saveBtn.disabled = true;
      setSaveBtnContent(saveBtn, 'readonly');
    }


    showSimpleToast('📄 已在编辑器中打开全文（' + text.split('\n').length + ' 行）', 'success');
  } catch (error) {
    console.error('打开编辑器失败:', error);
    showSimpleToast('❌ 打开编辑器失败: ' + error, 'error');
  }
}

/**
 * 初始化输出操作按钮
 */
function initOutputActions() {
  const clearBtn = document.getElementById('btn-clear-output');
  const copyBtn = document.getElementById('btn-copy-output');
  const bottomBtn = document.getElementById('btn-scroll-bottom');
  const prevBtn = document.getElementById('btn-prev-page');
  const nextBtn = document.getElementById('btn-next-page');

  if (clearBtn) {
    clearBtn.addEventListener('click', () => clearOutput(0, true));
  }

  if (copyBtn) {
    copyBtn.addEventListener('click', viewOutputInEditor);
  }

  if (bottomBtn) {
    bottomBtn.addEventListener('click', () => {
      state._browsingHistory = false;
      scrollToNewest();
    });
  }

  if (prevBtn) prevBtn.addEventListener('click', loadPrevPage);
  if (nextBtn) nextBtn.addEventListener('click', loadNextPage);
}

