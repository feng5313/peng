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
  if (state._pagingEndLine < 0) return false;
  if (state._pagingEndLine < state._totalLines) return true;
  // 🔧 Fix #1064: 缓存兜底检查 —— _totalLines 可能尚未同步，检查缓存中是否有更新的行
  const currentHistory = getOutputHistory(state.currentTaskId);
  if (currentHistory.length > 0) {
    const maxLineNo = currentHistory[currentHistory.length - 1].fileLineNo || 0;
    return maxLineNo > state._pagingEndLine;
  }
  return false;
}

function hasOlderOutput() {
  const limit = state.webuiConfig.xterm.scrollback || 500;
  if (state._pagingStartLine < 0) {
    // 实时模式：缓存数据量 > 一页行数，或服务端还有历史
    const currentHistory = getOutputHistory(state.currentTaskId);
    const cacheLen = currentHistory ? currentHistory.length : 0;
    return cacheLen > limit
      || state._totalLines > limit
      || state.hasMoreHistory.get(state.currentTaskId) === true;
  }
  return state._pagingStartLine > 1;
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
 * 🔧 Task#1102：在 xterm 中显示"加载中"占位
 * 清空当前内容，在终端居中位置显示加载提示
 */
function showLoadingInXterm() {
  if (!state.output) return;
  state._pagingRecoveryUntil = Date.now() + 10000;  // 抑制 onScroll，防止误触发
  state.output.clear();
  state.currentColor = '';
  const rows = state.output.rows || 24;
  const padTop = Math.max(0, Math.floor((rows - 1) / 2));
  state.output.write('\r\n'.repeat(padTop) + '\x1b[33m  ⏳ 加载中...\x1b[0m\r\n');
}

/**
 * 🔧 Task#1102：恢复之前显示的页面内容（后端返回空数据时调用）
 * 根据当前 paging 状态从缓存重新渲染
 */
function restoreCurrentPage() {
  if (!state.output) return;
  const currentHistory = getOutputHistory(state.currentTaskId);
  if (currentHistory.length === 0) return;

  if (state._pagingStartLine > 0 && state._pagingEndLine > 0) {
    // 翻页模式：重新渲染当前页
    const outputsToShow = currentHistory.filter(item =>
      item.fileLineNo >= state._pagingStartLine && item.fileLineNo <= state._pagingEndLine
    );
    if (outputsToShow.length === 0) {
      // 缓存无数据，回到底部
      scrollToNewest();
      return;
    }
    state._pagingRecoveryUntil = Date.now() + 500;
    state.output.clear();
    state.currentColor = '';
    const chunks = [];
    for (const item of outputsToShow) {
      if (passFilters(item.source)) {
        chunks.push(formatLineNoPrefix(item.fileLineNo) + convertFilePathsToLinks(item.content) + '\r\n');
      }
    }
    // 🔧 Fix Task#1111: 使用 writeAndScroll 替代坏掉的 write callback
    if (chunks.length > 0) {
      writeAndScroll(chunks.join(''), () => {
        if (state.output) state.output.scrollToBottom();
      });
    } else {
      setTimeout(() => { if (state.output) state.output.scrollToBottom(); }, 0);
    }
  } else {
    // 实时模式：回到底部
    scrollToNewest();
  }
}

/**
 * 加载上一页（更旧的输出）- 先操作缓存，缓存耗尽时向后端请求
 */
async function loadPrevPage() {
  const currentHistory = getOutputHistory(state.currentTaskId);
  if (!state.output || currentHistory.length === 0) return;

  state._browsingHistory = true;
  const limit = state.webuiConfig.xterm.scrollback || 500;

  // 🔧 Fix Task#840：基于全局行号定位
  let curStartLine = state._pagingStartLine;
  if (curStartLine < 0) {
    // 首次翻页：用 _totalLines，fallback 到缓存最大 fileLineNo
    const total = state._totalLines || 0;
    if (total > 0) {
      curStartLine = Math.max(1, total - limit + 1);
    } else {
      // _totalLines 未设置（增量切换），用缓存实际数据
      const maxLn = currentHistory.reduce(function(m, i){ return Math.max(m, i.fileLineNo || 0); }, 0);
      curStartLine = Math.max(1, maxLn - limit + 1);
    }
  }

  if (curStartLine <= 1) {
    showSimpleToast('⏹️ 没有更旧的输出了', 'info');
    state._browsingHistory = false;
    return;
  }

  // 在缓存中查找行号 < curStartLine 的项
  const olderItems = currentHistory.filter(item => item.fileLineNo && item.fileLineNo < curStartLine);

  if (olderItems.length > 0) {
    // 取最近的 limit 条
    const startIdx = Math.max(0, olderItems.length - limit);
    const outputsToShow = olderItems.slice(startIdx);
    const newStartLine = outputsToShow[0].fileLineNo;
    const newEndLine = outputsToShow[outputsToShow.length - 1].fileLineNo;

    // 🔧 Fix Task#840：检查行号连续性
    // 期望行数 = newEndLine - newStartLine + 1，如果实际行数远小于期望值说明有空洞
    const expectedCount = newEndLine - newStartLine + 1;
    if (outputsToShow.length < expectedCount * 0.5) {
      // 行号严重不连续，从后端请求补全
      const beforeTimestamp = outputsToShow[0].timestamp;
      if (beforeTimestamp) {
        // 🔧 Task#1102：显示加载中占位，收到后端数据后由 renderOlderHistory 替换
        showLoadingInXterm();
        state._loadingPrevPage = true;
        await subscribeOutput(0, false, beforeTimestamp);
        return;
      }
    }

    // 🔧 Fix #1064: 在 clear+write 前设置 _pagingRecoveryUntil，防止 onScroll 误设 userScrolledUp
    state._pagingRecoveryUntil = Date.now() + 500;
    state.output.clear(); state.currentColor = '';

    // 只有到达最早输出（newStartLine===1）时才填充空行
    const padCount = (newStartLine === 1) ? Math.max(0, limit - outputsToShow.length) : 0;
    const chunks = [];
    if (padCount > 0) chunks.push('\r\n'.repeat(padCount));
    for (const item of outputsToShow) {
      if (passFilters(item.source)) {
        chunks.push(formatLineNoPrefix(item.fileLineNo) + convertFilePathsToLinks(item.content) + '\r\n');
      }
    }
    // 🔧 Fix Task#1111: 使用 writeAndScroll 替代坏掉的 write callback
    if (chunks.length > 0) {
      writeAndScroll(chunks.join(''), () => {
        if (state.output) state.output.scrollToBottom();
      });
    } else {
      // 无内容可写时直接滚动
      setTimeout(() => { if (state.output) state.output.scrollToBottom(); }, 0);
    }

    state._pagingStartLine = newStartLine;
    state._pagingEndLine = newEndLine;
    state.currentOldestTimestamp = outputsToShow[0].timestamp;
    state._pagingPageNewestTs = outputsToShow[outputsToShow.length - 1].timestamp;

    var pi = getPageInfo(newStartLine);
    showSimpleToast(pi ? ('↑️ 第' + pi.page + '/' + pi.totalPages + '页') : '↑️ 上一页', 'info');

    updatePaginationButtons();
    return;
  }

  // 缓存中没有更老的行，向后端请求
  if (curStartLine <= 1) {
    showSimpleToast('⏹️ 没有更旧的输出了', 'info');
    state._browsingHistory = false;
    return;
  }

  const beforeTimestamp = currentHistory.length > 0
    ? currentHistory[0].timestamp
    : state.currentOldestTimestamp;

  if (!beforeTimestamp) {
    showSimpleToast('⏹️ 没有更旧的输出了', 'info');
    state._browsingHistory = false;
    return;
  }

  // 🔧 Task#1102：缓存无更老数据，显示加载中占位，收到后端数据后由 renderOlderHistory 替换
  showLoadingInXterm();
  state._loadingPrevPage = true;
  await subscribeOutput(0, false, beforeTimestamp);
}


/**
 * 加载下一页（更新的输出）
 */
async function loadNextPage() {
  const currentHistory = getOutputHistory(state.currentTaskId);
  if (!state.output || currentHistory.length === 0) {
    state._browsingHistory = false;
    state.userScrolledUp = false;
    return;
  }

  // 🔧 Fix Task#840：未翻页模式直接回到实时（不翻页）
  if (state._pagingEndLine < 0) {
    state._browsingHistory = false;
    state.userScrolledUp = false;
    state._pagingRecoveryUntil = Date.now() + 500;
    scrollToNewest();
    return;
  }

  // 如果不在底部，先滚到底部
  if (!isAtBottom()) {
    setTimeout(() => {
      if (state.output) state.output.scrollToBottom();
    }, 50);
    return;
  }

  state._browsingHistory = true;
  const limit = state.webuiConfig.xterm.scrollback || 500;

  const newItems = currentHistory.filter(item => item.fileLineNo > state._pagingEndLine);

  if (newItems.length === 0) {
    // 缓存中没有更新的，回到实时模式
    state._browsingHistory = false;
    state.userScrolledUp = false;
    state._pagingRecoveryUntil = Date.now() + 500;
    scrollToNewest();
    return;
  }

  const outputsToShow = newItems.slice(0, limit);
  const newStartLine = outputsToShow[0].fileLineNo;
  const newEndLine = outputsToShow[outputsToShow.length - 1].fileLineNo;
  // 🔧 Fix #1064: 用 _totalLines 判断最后一页，并用缓存兜底
  // _totalLines 可能因 appendOutput 未及时同步而偏小，需检查缓存中是否还有更新的行
  const total = state._totalLines || 0;
  const hasEvenNewer = newItems.length > limit;  // newItems 中还有未展示的
  const isLastPage = newEndLine >= total && !hasEvenNewer;

  // 被全部过滤，跳过
  const hasVisible = outputsToShow.some(item => passFilters(item.source));
  if (!hasVisible) {
    state._pagingStartLine = newStartLine;
    state._pagingEndLine = newEndLine;
    state.currentOldestTimestamp = outputsToShow[0].timestamp;
    state._pagingPageNewestTs = outputsToShow[outputsToShow.length - 1].timestamp;
    if (isLastPage) {
      state._browsingHistory = false;
      state.userScrolledUp = false;
      state._pagingRecoveryUntil = Date.now() + 500;
      scrollToNewest();
    }
    updatePaginationButtons();
    return;
  }

  // 🔧 Fix #1064: 在 clear+write 前设置 _pagingRecoveryUntil，防止 onScroll 误设 userScrolledUp
  state._pagingRecoveryUntil = Date.now() + 500;
  state.output.clear(); state.currentColor = '';

  const chunks = [];
  for (const item of outputsToShow) {
    if (passFilters(item.source)) {
      chunks.push(formatLineNoPrefix(item.fileLineNo) + convertFilePathsToLinks(item.content) + '\r\n');
    }
  }
  // 🔧 Fix Task#1111: 使用 writeAndScroll 替代坏掉的 write callback
  if (chunks.length > 0) {
    writeAndScroll(chunks.join(''), () => {
      if (state.output) state.output.scrollToTop();
    });
  } else {
    setTimeout(() => { if (state.output) state.output.scrollToTop(); }, 0);
  }

  state._pagingStartLine = newStartLine;
  state._pagingEndLine = newEndLine;
  state.currentOldestTimestamp = outputsToShow[0].timestamp;
  state._pagingPageNewestTs = outputsToShow[outputsToShow.length - 1].timestamp;

  var pi = getPageInfo(newStartLine);
  if (isLastPage) {
    // 到最后一页 → 回到实时模式，确保 append_output 正常显示
    // 🔧 Fix #1064: 必须重置 _browsingHistory，否则 appendOutput 的 shouldSkipWrite
    // 永远为 true（_browsingHistory || userScrolledUp），导致翻到最后一页后
    // 不再显示任何新输出，用户被困在浏览模式中无法回到实时模式
    state._browsingHistory = false;
    state.userScrolledUp = false;
    showSimpleToast('⏹️ 已回到底部', 'info');
    state._pagingRecoveryUntil = Date.now() + 500;
    scrollToNewest();
    return;
  }
  showSimpleToast(pi ? ('↓️ 第' + pi.page + '/' + pi.totalPages + '页') : '↓️ 下一页', 'info');

  state._pagingRecoveryUntil = Date.now() + 500;
  updatePaginationButtons();
}

/**
 * 🔧 Fix Task#1433：从缓存全量渲染到 xterm（切换事务时立即调用）
 *
 * 根因修复：selectTask 清空 xterm 后延迟调用 scrollToNewest()，但在延迟窗口内
 * _watch_loop 的实时输出通过 appendOutput 写入了几行并设置了 currentNewestTimestamp，
 * 导致 scrollToNewest 误认为"已在最新位置"跳过缓存渲染，只显示那几行实时输出。
 *
 * 本函数在清空 xterm 后立即同步执行缓存渲染，不依赖延迟回调，彻底消除竞态窗口。
 *
 * @param {number} taskId - 要渲染的事务ID
 */
function renderFromCache(taskId) {
  if (!state.output) return;
  const currentHistory = getOutputHistory(taskId);
  if (currentHistory.length === 0) return;

  const limit = state.webuiConfig.xterm.scrollback || 500;

  // 取最新的 limit 条
  const startIdx = Math.max(0, currentHistory.length - limit);
  const outputsToShow = currentHistory.slice(startIdx);

  // 抑制 onScroll，防止 clear+write 过程中 userScrolledUp 被误设为 true
  state._pagingRecoveryUntil = Date.now() + 500;
  state.output.clear(); state.currentColor = '';

  // 批量合并为单个字符串再 write，减少渲染帧数
  const chunks = [];
  for (const item of outputsToShow) {
    if (passFilters(item.source)) {
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

  // 设置时间戳指针（关键：防止后续 scrollToNewest 误判"已在最新位置"）
  state.currentOldestTimestamp = outputsToShow[0].timestamp;
  state.currentNewestTimestamp = outputsToShow[outputsToShow.length - 1].timestamp;

  // 🔧 Fix Task#1433：从缓存恢复 _totalLines（取缓存中最大的 fileLineNo）
  // 否则 _totalLines 为 0 导致翻页功能异常（getPageInfo 计算 0 页，hasOlderOutput/hasNewerOutput 判断错误）
  const maxLineNo = currentHistory.reduce(function(m, i) { return Math.max(m, i.fileLineNo || 0); }, 0);
  if (maxLineNo > state._totalLines) {
    state._totalLines = maxLineNo;
  }

  // 重置翻页和滚动状态
  state._pagingStartLine = -1;
  state._pagingEndLine = -1;
  state._pagingAnchorNewestTs = null;
  state._pagingPageNewestTs = null;
  state.userScrolledUp = false;
  state._browsingHistory = false;
  state._userScrollIntentUntil = 0;
}

/**
 * 回到底部 - 加载缓存中最新的内容
 */
function scrollToNewest() {
  // 🔧 Task#1102：清除加载标志（回到实时模式无需加载占位）
  state._loadingPrevPage = false;
  const currentHistory = getOutputHistory(state.currentTaskId);
  if (!state.output || currentHistory.length === 0) return;

  // 🔧 清除输出搜索状态（回到底部时搜索不再有效）
  if (typeof _clearSearch === 'function') _clearSearch();

  // 🔧 Task#816修复：scrollToNewest 是用户/系统主动"回到底部"的操作，
  // 不应被 _browsingHistory 阻止。_browsingHistory 只用于阻止 appendOutput 的自动滚动。
  // 如果需要保护，由调用方在调用前重置 _browsingHistory。
  // 🔧 Fix #1064: 安全退出浏览模式，防止遗漏导致 appendOutput 永久跳过写入
  state._browsingHistory = false;

  // 🔧 检查是否已经在显示最新内容
  const latestTimestamp = currentHistory[currentHistory.length - 1].timestamp;
  if (state.currentNewestTimestamp !== latestTimestamp) {
    // 🔧 Fix Task#1404：实时模式下增量追加缺失输出，避免 clear()+write() 导致 viewport 弹到顶部
    // 原方案：clear() + write(全部历史) → clear() 将 ydisp 重置为 0（顶部），用户看到弹到顶部
    // 新方案：找到 currentNewestTimestamp 之后的增量输出，直接追加到 xterm buffer 末尾，
    //         viewport 保持稳定，scrollToBottom() 后直接显示最新内容
    const isRealtimeMode = state._pagingStartLine < 0;

    // 找到 currentNewestTimestamp 在缓存中的位置
    let newestIdx = -1;
    if (state.currentNewestTimestamp !== null) {
      for (let i = currentHistory.length - 1; i >= 0; i--) {
        if (currentHistory[i].timestamp === state.currentNewestTimestamp) {
          newestIdx = i;
          break;
        }
        // timestamp 单调递增，找到第一个 <= currentNewestTimestamp 的即可
        if (currentHistory[i].timestamp < state.currentNewestTimestamp) {
          newestIdx = i;  // 取前一条（可能 timestamp 不完全匹配但内容已在 xterm 中）
          break;
        }
      }
    }

    if (isRealtimeMode && newestIdx >= 0) {
      // 实时模式 + 已有显示基准：增量追加缺失输出
      const newOutputs = currentHistory.slice(newestIdx + 1);
      if (newOutputs.length > 0) {
        const chunks = [];
        for (const item of newOutputs) {
          if (passFilters(item.source)) {
            chunks.push(formatLineNoPrefix(item.fileLineNo) + convertFilePathsToLinks(item.content) + '\r\n');
          }
        }
        if (chunks.length > 0 && state.output) {
          writeAndScroll(chunks.join(''), () => {
            if (state.output) state.output.scrollToBottom();
          });
        } else if (state.output) {
          setTimeout(() => { state.output.scrollToBottom(); }, 0);
        }
      } else if (state.output) {
        // 没有增量输出，只需滚动到底部
        setTimeout(() => { state.output.scrollToBottom(); }, 0);
      }
      // 更新时间戳指针
      state.currentNewestTimestamp = latestTimestamp;
      if (state.currentOldestTimestamp === null) {
        state.currentOldestTimestamp = currentHistory[0].timestamp;
      }
    } else {
      // 翻页模式或无基准：全量 clear+write（原有逻辑）
      const limit = state.webuiConfig.xterm.scrollback || 500;

      // 取最新的 limit 条
      const startIdx = Math.max(0, currentHistory.length - limit);
      const outputsToShow = currentHistory.slice(startIdx);

      // 🔧 Fix #1064: 抑制 onScroll，防止 clear+write 过程中 userScrolledUp 被误设为 true
      state._pagingRecoveryUntil = Date.now() + 500;
      state.output.clear(); state.currentColor = '';

      // 🔧 优化(task#113)：批量合并为单个字符串再 write，减少渲染帧数
      const chunks = [];
      for (const item of outputsToShow) {
        if (passFilters(item.source)) {
          chunks.push(formatLineNoPrefix(item.fileLineNo) + convertFilePathsToLinks(item.content) + '\r\n');
        }
      }
      // 🔧 Fix Task#1111: 使用 writeAndScroll 替代坏掉的 write callback
      if (chunks.length > 0) {
        writeAndScroll(chunks.join(''), () => {
          if (state.output) state.output.scrollToBottom();
        });
      } else {
        setTimeout(() => { if (state.output) state.output.scrollToBottom(); }, 0);
      }

      // 记录时间戳指针
      state.currentOldestTimestamp = outputsToShow[0].timestamp;
      state.currentNewestTimestamp = outputsToShow[outputsToShow.length - 1].timestamp;
    }
  } else {
    // 已在最新位置，只需滚动到底部
    setTimeout(() => { if (state.output) state.output.scrollToBottom(); }, 0);
  }
  // 🔧 Task #92：重置用户滚动状态（已显示最新内容）
  state.userScrolledUp = false;
  // 🔧 Fix Task#840：清除翻页状态，恢复正常实时模式
  state._pagingStartLine = -1;
  state._pagingEndLine = -1;
  state._pagingAnchorNewestTs = null;
  state._pagingPageNewestTs = null;
  updatePaginationButtons();

}

/**
 * 渲染"上一页"加载的更老历史
 * 当后端返回老历史数据后，重新渲染 xterm 显示完整页面
 */
function renderOlderHistory(taskId, newOutputs) {
  // 🔧 Task#1102：清除加载标志（后端数据已到达）
  state._loadingPrevPage = false;
  const currentHistory = getOutputHistory(taskId);
  if (!state.output || currentHistory.length === 0) return;

  const limit = state.webuiConfig.xterm.scrollback || 500;

  // 🔧 Fix Task#840：老历史已带 fileLineNo，直接定位显示范围
  // 取 newOutputs 的第一个 fileLineNo 作为显示页的起始
  const newStartLine = newOutputs[0]?.fileLineNo;
  if (!newStartLine) return;

  // 从缓存中取行号 >= newStartLine 且 < newStartLine + limit 的项
  const outputsToShow = currentHistory.filter(item =>
    item.fileLineNo >= newStartLine && item.fileLineNo < newStartLine + limit
  );

  if (outputsToShow.length === 0) return;

  // 🔧 Fix #1064: 在 clear+write 前设置 _pagingRecoveryUntil，防止 onScroll 误设 userScrolledUp
  state._pagingRecoveryUntil = Date.now() + 500;
  state.output.clear(); state.currentColor = '';

  // 到达最早输出时填充空行
  const padCount = (newStartLine === 1) ? Math.max(0, limit - outputsToShow.length) : 0;
  const chunks = [];
  if (padCount > 0) chunks.push('\r\n'.repeat(padCount));
  for (const item of outputsToShow) {
    if (passFilters(item.source)) {
      chunks.push(formatLineNoPrefix(item.fileLineNo) + convertFilePathsToLinks(item.content) + '\r\n');
    }
  }
  // 🔧 Fix Task#1111: 使用 writeAndScroll 替代坏掉的 write callback
  if (chunks.length > 0) {
    writeAndScroll(chunks.join(''), () => {
      if (state.output) state.output.scrollToBottom();
    });
  } else {
    setTimeout(() => { if (state.output) state.output.scrollToBottom(); }, 0);
  }

  state._pagingStartLine = outputsToShow[0].fileLineNo;
  state._pagingEndLine = outputsToShow[outputsToShow.length - 1].fileLineNo;
  state.currentOldestTimestamp = outputsToShow[0].timestamp;
  state._pagingPageNewestTs = outputsToShow[outputsToShow.length - 1].timestamp;

  var pi = getPageInfo(state._pagingStartLine);
  showSimpleToast(pi ? ('↑️ 第' + pi.page + '/' + pi.totalPages + '页') : '↑️ 上一页', 'info');

  updatePaginationButtons();
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

