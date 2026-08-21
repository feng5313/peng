// ==================== xterm输出搜索 ====================
//
// 搜索当前事务的 outputHistory 纯文本缓存（已接收的全部输出）。
// 自行搜索 OutputHistory → 找到匹配项 → 计算对应页面 → 渲染到 xterm
// → 滚动定位到匹配行 → 高亮控件(write hook)自动高亮已显示的关键词。
//
// 上下箭头按钮 / Enter / Shift+Enter 跳转匹配位置，自动翻页+自动定位。

// 搜索状态
let _searchMatches = [];   // Array<{historyIdx, fileLineNo}>
let _searchCurrentIdx = -1; // 当前显示的匹配在 _searchMatches 中的索引
let _searchKeyword = '';    // 当前搜索关键词（用于同步高亮）
let _searchLastStartIdx = -1; // 上次渲染的页面起始索引（检测翻页）

/**
 * 去除所有 ANSI 转义序列，返回纯文本
 * 处理 SGR(\x1b[...m)、OSC(\x1b]...\x07/\x1b\\)、
 * CSI 光标移动/擦除(\x1b[...H/J/K 等)、其他单字符转义
 */
function _stripAnsi(text) {
  if (!text) return '';
  return text
    // CSI 序列: \x1b[ ... (终止于 0x40-0x7E)
    .replace(/\x1b\[[0-9;?]*[ -/]*[@-~]/g, '')
    // OSC 序列: \x1b] ... \x07 或 \x1b] ... \x1b\\
    .replace(/\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)/g, '')
    // 其他转义: \x1b 后跟单字符 (如 \x1b=, \x1b>, \x1bM)
    .replace(/\x1b[=>MX^_]/g, '')
    // 残留的控制字符
    .replace(/[\x00-\x08\x0e-\x1f]/g, '');
}

/**
 * 执行搜索：在整个 outputHistory 缓存中查找匹配行
 * @param {string} keyword - 搜索关键词
 */
function performOutputSearch(keyword) {
  if (!keyword || !keyword.trim()) {
    _clearSearch();
    return;
  }

  const trimmed = keyword.trim();
  _searchKeyword = trimmed;

  // 同步高亮关键词：确保搜索跳转后 write hook 自动高亮匹配文本
  if (typeof _outputHighlightTerms !== 'undefined') {
    _outputHighlightTerms = typeof parseOutputHighlightKeywords === 'function'
      ? parseOutputHighlightKeywords(trimmed)
      : [trimmed];
  }

  const currentHistory = getOutputHistory(state.currentTaskId);
  if (!currentHistory || currentHistory.length === 0) {
    showSimpleToast('🔍 没有可搜索的输出', 'info');
    _updateSearchNavUI();
    return;
  }

  // 构建正则（不区分大小写）
  const escaped = trimmed.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const re = new RegExp(escaped, 'i');

  // 遍历所有 history 项查找匹配（完整搜索整个缓存）
  _searchMatches = [];
  for (let i = 0; i < currentHistory.length; i++) {
    const item = currentHistory[i];
    if (!item || !item.content) continue;
    // 只搜索未被过滤器隐藏的来源
    if (typeof passFilters === 'function' && !passFilters(item.source)) continue;
    const text = _stripAnsi(item.content);
    if (re.test(text)) {
      _searchMatches.push({ historyIdx: i, fileLineNo: item.fileLineNo || (i + 1) });
    }
  }

  if (_searchMatches.length === 0) {
    showSimpleToast('🔍 未找到匹配 "' + trimmed + '"', 'info');
    _searchCurrentIdx = -1;
    _updateSearchNavUI();
    return;
  }

  // 从最后一个匹配开始（最接近底部的匹配，用户通常在底部）
  _searchCurrentIdx = _searchMatches.length - 1;
  _navigateToMatch(_searchCurrentIdx);
}

/**
 * 导航到指定匹配项：
 * 1. 计算匹配项所在页面范围
 * 2. 将该页面内容渲染到 xterm（clear + write）
 * 3. 滚动终端使匹配行可见（居中偏上，上方留2行上下文）
 *
 * @param {number} matchIdx - 在 _searchMatches 中的索引
 */
function _navigateToMatch(matchIdx) {
  if (matchIdx < 0 || matchIdx >= _searchMatches.length) return;
  if (!state.output) return;

  _searchCurrentIdx = matchIdx;
  const match = _searchMatches[matchIdx];
  const currentHistory = getOutputHistory(state.currentTaskId);
  if (!currentHistory || currentHistory.length === 0) return;

  const limit = state.webuiConfig.xterm.scrollback || 500;
  const matchHistIdx = match.historyIdx;

  // 计算页面范围：以匹配行为中心，取 limit 条 history
  const halfPage = Math.floor(limit / 2);
  let startIdx = Math.max(0, matchHistIdx - halfPage);
  let endIdx = Math.min(currentHistory.length - 1, startIdx + limit - 1);
  // 如果末尾不够，向前扩展
  if (endIdx - startIdx + 1 < limit) {
    startIdx = Math.max(0, endIdx - limit + 1);
  }

  const outputsToShow = currentHistory.slice(startIdx, endIdx + 1);
  if (outputsToShow.length === 0) return;

  // 渲染页面：clear + write（与 loadPrevPage / scrollToNewest 相同的渲染逻辑）
  state._pagingRecoveryUntil = Date.now() + 800;
  state._browsingHistory = true;
  state.userScrolledUp = false;  // 🔧 重置：搜索导航显式控制滚动，清除可能的残留状态
  state.output.clear();
  state.currentColor = '';

  // 构建写入数据，同时计算匹配行在 xterm buffer 中的行偏移
  // 高亮由 output-highlight.js 的 write hook 自动注入（_inRefresh=false 时生效）
  const chunks = [];
  let visibleLineOffset = 0; // 匹配行之前在 xterm 中实际占用的行数

  for (let i = startIdx; i <= endIdx; i++) {
    const item = outputsToShow[i - startIdx];
    if (!item) continue;
    if (typeof passFilters === 'function' && !passFilters(item.source)) continue;

    const text = typeof convertFilePathsToLinks === 'function'
      ? convertFilePathsToLinks(item.content) : item.content;
    const prefix = typeof formatLineNoPrefix === 'function' ? formatLineNoPrefix(item.fileLineNo) : '';
    chunks.push(prefix + text + '\r\n');

    // 🔧 多行内容处理：计算该 item 在 xterm 中实际占用的行数
    // （content 可能包含 \n，每个 \n 会额外产生一行）
    if (i < matchHistIdx) {
      const lineCount = Math.max(1, (text.match(/\n/g) || []).length + 1);
      visibleLineOffset += lineCount;
    }
  }

  // 🔧 Fix Task#1111: 使用 writeAndScroll 替代坏掉的 write callback
  if (chunks.length > 0) {
    writeAndScroll(chunks.join(''), _scrollToMatchLine);
  } else {
    setTimeout(_scrollToMatchLine, 0);
  }

  // 更新分页状态
  const firstItem = outputsToShow[0];
  const lastItem = outputsToShow[outputsToShow.length - 1];
  state._pagingStartLine = firstItem.fileLineNo || -1;
  state._pagingEndLine = lastItem.fileLineNo || -1;
  state.currentOldestTimestamp = firstItem.timestamp;
  state._pagingPageNewestTs = lastItem.timestamp;

  if (typeof updatePaginationButtons === 'function') updatePaginationButtons();

  // 🔧 Task#1119: 页面跳转时弹toast提示页码
  if (_searchLastStartIdx >= 0 && startIdx !== _searchLastStartIdx) {
    if (typeof getPageInfo === 'function') {
      var pi = getPageInfo(state._pagingStartLine);
      showSimpleToast(pi ? ('📄 第' + pi.page + '/' + pi.totalPages + '页') : '📄 已翻页', 'info');
    } else {
      showSimpleToast('📄 已翻页', 'info');
    }
  }
  _searchLastStartIdx = startIdx;

  _updateSearchNavUI();

  // 滚动终端使匹配行可见
  // writeAndScroll 回调执行时，数据已全部写入 buffer，viewportY 在底部(ybase)
  // 需要向上滚动到匹配行位置
  function _scrollToMatchLine() {
    if (!state.output) return;
    const buffer = state.output.buffer.active;
    const rows = state.output.rows;
    // 目标视口位置：匹配行上方留2行上下文
    const targetLine = Math.max(0, Math.min(
      Math.max(0, buffer.length - rows),
      visibleLineOffset - 2
    ));
    const delta = targetLine - buffer.viewportY;
    if (delta !== 0) {
      state.output.scrollLines(delta);
    }
    _updateSearchNavUI();
  }
}

/**
 * 更新搜索导航 UI（匹配计数、按钮状态）
 */
function _updateSearchNavUI() {
  const navEl = document.getElementById('output-search-nav');
  const counterEl = document.getElementById('output-search-counter');
  const prevBtn = document.getElementById('output-search-prev');
  const nextBtn = document.getElementById('output-search-next');

  if (!navEl) return;

  if (_searchMatches.length === 0) {
    navEl.style.display = 'none';
    return;
  }

  navEl.style.display = 'inline-flex';
  if (counterEl) {
    counterEl.textContent = (_searchCurrentIdx + 1) + '/' + _searchMatches.length;
  }
  // 🔧 Task#1119: 循环跳转，按钮永不disabled
  if (prevBtn) prevBtn.disabled = false;
  if (nextBtn) nextBtn.disabled = false;
}

/**
 * 清除搜索状态（不恢复 xterm 显示）
 */
function _clearSearch() {
  _searchMatches = [];
  _searchCurrentIdx = -1;
  _searchKeyword = '';
  _searchLastStartIdx = -1;
  _updateSearchNavUI();
}

/**
 * 上一个匹配（↑ 更老 = 索引更小，循环到末尾）
 */
function searchPrevMatch() {
  if (_searchMatches.length === 0) return;
  var newIdx;
  if (_searchCurrentIdx <= 0) {
    newIdx = _searchMatches.length - 1;  // 循环到最后一个
    _navigateToMatch(newIdx);
    showSimpleToast('🔍 循环到最后一个 (第 ' + (newIdx + 1) + '/' + _searchMatches.length + ')', 'info');
  } else {
    newIdx = _searchCurrentIdx - 1;
    _navigateToMatch(newIdx);
    showSimpleToast('🔍 第 ' + (newIdx + 1) + '/' + _searchMatches.length + ' 个匹配', 'info');
  }
}

/**
 * 下一个匹配（↓ 更新 = 索引更大，循环到开头）
 */
function searchNextMatch() {
  if (_searchMatches.length === 0) return;
  var newIdx;
  if (_searchCurrentIdx >= _searchMatches.length - 1) {
    newIdx = 0;  // 循环到第一个
    _navigateToMatch(newIdx);
    showSimpleToast('🔍 循环到第一个 (第 ' + 1 + '/' + _searchMatches.length + ')', 'info');
  } else {
    newIdx = _searchCurrentIdx + 1;
    _navigateToMatch(newIdx);
    showSimpleToast('🔍 第 ' + (newIdx + 1) + '/' + _searchMatches.length + ' 个匹配', 'info');
  }
}

/**
 * 清除搜索并恢复到底部（实时模式）
 */
function clearOutputSearch() {
  _clearSearch();
  if (typeof scrollToNewest === 'function') {
    scrollToNewest();
  }
}

/**
 * 初始化搜索按钮事件
 */
function initOutputSearch() {
  const searchBtn = document.getElementById('output-search-btn');
  const prevBtn = document.getElementById('output-search-prev');
  const nextBtn = document.getElementById('output-search-next');
  const input = document.getElementById('output-keyword-highlight');

  if (searchBtn) {
    searchBtn.addEventListener('click', () => {
      if (input) {
        const keyword = input.value.trim();
        if (keyword) {
          performOutputSearch(keyword);
        } else {
          input.focus();
        }
      }
    });
  }

  if (prevBtn) {
    prevBtn.addEventListener('click', searchPrevMatch);
  }

  if (nextBtn) {
    nextBtn.addEventListener('click', searchNextMatch);
  }
}
