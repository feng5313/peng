// ==================== xterm关键词高亮 ====================
//
// 在output-filters栏中提供关键词高亮输入框，用户输入关键词后在xterm中
// 高亮显示匹配的文本。支持与或语法：空格=AND（全部高亮），|=OR（全部高亮）。
//
// 实现原理：参考消息栏的 <mark> 标签做法（黄底黑字），在 xterm 中使用
// ANSI 转义码 \x1b[43m（黄底）包裹匹配关键词。通过 monkey-patch
// state.output.write 拦截所有写入，自动注入高亮 ANSI 码。
// 关键词变化时触发全量重渲染。

let _outputHighlightTerms = [];
let _highlightDebounceTimer = null;
let _origWrite = null;
let _inRefresh = false;  // 刷新期间抑制钩子，避免与实时输出冲突

/** 匹配消息栏 --warning-color: dark=#d29922, light=#FFDA33 */
const _HL_BG_DARK = '\x1b[48;2;210;153;34m';
const _HL_BG_LIGHT = '\x1b[48;2;255;218;51m';
/** 黑字 + 恢复默认背景+前景 */
const _HL_FG_BLACK = '\x1b[30m';
const _HL_RESET = '\x1b[49m\x1b[39m';

function _hlBg() {
  return document.body.classList.contains('light-theme') ? _HL_BG_LIGHT : _HL_BG_DARK;
}

function parseOutputHighlightKeywords(raw) {
  if (!raw || !raw.trim()) return [];
  if (typeof parseSearchTerms === 'function') {
    const { andTerms, orTerms } = parseSearchTerms(raw);
    return [...new Set([...andTerms, ...orTerms])].filter(t => t.length > 0);
  }
  return raw.split(/[\s|]+/).filter(t => t.length > 0);
}

/**
 * 给文本中的关键词包裹 ANSI 黄底码。
 * 通过 split ANSI 序列来隔离纯文本段，对纯文本段做关键词替换，
 * ANSI 段原样保留。高亮结束后用 \x1b[49m（仅重置背景）而非
 * \x1b[0m（重置全部），避免破坏原有的文字颜色。
 */
function _applyAnsiHighlight(text, keywords) {
  if (!keywords || keywords.length === 0) return text;
  const escaped = keywords.map(k => k.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).filter(k => k);
  if (escaped.length === 0) return text;

  const kwRe = new RegExp('(' + escaped.join('|') + ')', 'gi');
  const bgCode = _hlBg();
  const hlStart = bgCode + _HL_FG_BLACK;  // 黄底+黑字
  const hlEnd = _HL_RESET;                 // 恢复默认背景+前景

  const parts = text.split(/(\x1b\[[0-9;]*m|\x1b\][^\x07]*\x07)/);
  let result = '';
  for (let i = 0; i < parts.length; i++) {
    if (parts[i] === undefined) continue;
    if (i % 2 === 1) {
      result += parts[i];
    } else {
      result += parts[i].replace(kwRe, hlStart + '$1' + hlEnd);
    }
  }
  return result;
}

/**
 * 安装 write 钩子：拦截 state.output.write 自动注入高亮
 */
function _installWriteHook() {
  if (!state.output || _origWrite) return;
  _origWrite = state.output.write.bind(state.output);
  state.output.write = function(data) {
    if (!_inRefresh && _outputHighlightTerms.length > 0) {
      data = _applyAnsiHighlight(data, _outputHighlightTerms);
    }
    return _origWrite(data);
  };
}

/**
 * 刷新：清除终端并用高亮重写当前可见内容
 */
function _refreshWithHighlight() {
  if (!state.output || !_origWrite) return;

  const term = state.output;
  const currentHistory = typeof getOutputHistory === 'function' ? getOutputHistory(state.currentTaskId) : null;
  if (!currentHistory || currentHistory.length === 0) return;

  // 找到当前显示的范围
  const startTs = state.currentOldestTimestamp;
  // 🔧 Fix Task#840：翻页浏览时用 _pagingPageNewestTs 代替 currentNewestTimestamp
  const endTs = state._pagingPageNewestTs || state.currentNewestTimestamp || currentHistory[currentHistory.length - 1].timestamp;
  if (!startTs) return;

  const startIdx = typeof findIndexByTimestamp === 'function' ?
    findIndexByTimestamp(currentHistory, startTs) : 0;
  const endIdx = typeof findIndexByTimestamp === 'function' ?
    findIndexByTimestamp(currentHistory, endTs) : currentHistory.length - 1;

  if (startIdx < 0 || endIdx < 0) return;

  _inRefresh = true;
  term.clear();
  if (typeof state !== 'undefined') state.currentColor = '';

  // 收集带高亮的行
  const terms = _outputHighlightTerms;
  const chunks = [];
  for (let i = startIdx; i <= endIdx; i++) {
    const item = currentHistory[i];
    if (!item || !item.content) continue;
    if (typeof passFilters === 'function' && !passFilters(item.source)) continue;
    const text = typeof convertFilePathsToLinks === 'function' ?
      convertFilePathsToLinks(item.content) : item.content;
    const prefix = typeof formatLineNoPrefix === 'function' ? formatLineNoPrefix(item.fileLineNo) : '';
    chunks.push(prefix + _applyAnsiHighlight(text, terms) + '\r\n');
  }

  if (chunks.length > 0) {
    _origWrite(chunks.join(''));
  }

  // 🔧 Task#1119: 搜索导航期间不强制 scrollToBottom，避免覆盖搜索定位
  const hasSearch = typeof _searchMatches !== 'undefined' && _searchMatches.length > 0;
  setTimeout(() => {
    if (!hasSearch) {
      term.scrollToBottom();
    }
    _inRefresh = false;
  }, 50);
}

function applyOutputHighlight() {
  _refreshWithHighlight();
}

function clearOutputHighlight() {
  // ANSI方式无需手动清理，重渲染即可
}

function scheduleOutputHighlight() {
  if (_highlightDebounceTimer) clearTimeout(_highlightDebounceTimer);
  _highlightDebounceTimer = setTimeout(_refreshWithHighlight, 150);
}

function initOutputKeywordHighlight() {
  const input = document.getElementById('output-keyword-highlight');
  if (!input) return;

  _installWriteHook();

  // input事件：只更新关键词和UI样式，不刷新终端（避免输入时闪屏）
  // 刷新时机：按Enter或失焦时才触发一次全量重渲染
  input.addEventListener('input', (e) => {
    const value = e.target.value;
    const hasKeyword = value.trim().length > 0;
    input.classList.toggle('has-content', hasKeyword);
    // 立即更新关键词，这样后续新写入的内容会自动带高亮
    _outputHighlightTerms = parseOutputHighlightKeywords(value);
    // 清除过期的搜索结果
    if (typeof _clearSearch === 'function') _clearSearch();
  });

  // Enter键：搜索输出（Shift+Enter = 上一个匹配）
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      const keyword = input.value.trim();
      if (!keyword) return;
      if (e.shiftKey) {
        // Shift+Enter: 上一个匹配
        if (typeof searchPrevMatch === 'function') searchPrevMatch();
      } else {
        // Enter: 如果已有匹配则下一个，否则执行搜索
        if (typeof _searchMatches !== 'undefined' && _searchMatches.length > 0
          && typeof searchNextMatch === 'function') {
          searchNextMatch();
        } else if (typeof performOutputSearch === 'function') {
          performOutputSearch(keyword);
        } else {
          _refreshWithHighlight();
        }
      }
    } else if (e.key === 'Escape') {
      e.preventDefault();
      e.stopPropagation();
      input.value = '';
      input.classList.remove('has-content');
      _outputHighlightTerms = [];
      // 清除搜索并回到底部
      if (typeof clearOutputSearch === 'function') {
        clearOutputSearch();
      } else {
        _refreshWithHighlight();
      }
      input.focus();
    }
  });

  // 失焦时也刷新一次（如果有关键词变化但未按Enter）
  input.addEventListener('change', _refreshWithHighlight);

  // 清除按钮点击：清除高亮+搜索，恢复底部
  const clearBtn = input.parentElement.querySelector('.output-highlight-clear-btn');
  if (clearBtn) {
    clearBtn.addEventListener('click', () => {
      input.value = '';
      input.classList.remove('has-content');
      _outputHighlightTerms = [];
      if (typeof clearOutputSearch === 'function') {
        clearOutputSearch();
      } else {
        _refreshWithHighlight();
      }
      input.focus();
    });
  }
}
