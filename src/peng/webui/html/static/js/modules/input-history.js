// ==================== 输入历史记录（消息输入框） ====================

// 消息输入历史下拉组件实例（延迟初始化）
let messageHistoryDropdown = null;

/**
 * 获取消息输入历史下拉组件实例
 */
function getMessageHistoryDropdown() {
  if (messageHistoryDropdown) return messageHistoryDropdown;

  const inputEl = document.getElementById('message-input');
  if (!inputEl) return null;

  messageHistoryDropdown = createHistoryDropdown({
    inputEl,
    listId: 'input-history-list',
    listClassName: 'input-history-list',
    containerSelector: '.input-row',
    position: 'above',
    getHistory: () => state.inputHistory,
    setHistory: (h) => { state.inputHistory = h; },
    saveHistory: async (history) => {
      const response = await fetch('/api/input-history', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ history }),
      });
      if (!response.ok) throw new Error('保存失败');
    },
    getInputText: () => {
      const el = document.getElementById('message-input');
      return el ? el.innerText.trim() : '';
    },
    setInputText: (text) => {
      const el = document.getElementById('message-input');
      if (el) el.innerText = text;
    },
    onHide: () => {
      // 🔧 清除防抖定时器，防止隐藏后又被自动重新弹出（如点击xterm区域后）
      if (state._historyListTimer) {
        clearTimeout(state._historyListTimer);
        state._historyListTimer = null;
      }
    },
  });

  return messageHistoryDropdown;
}

/**
 * 刷新消息历史（在发送消息后调用）
 */
async function refreshMessageHistory() {
  await loadInputHistory();
}

/**
 * 显示输入历史记录列表
 */
function showInputHistoryList() {
  const dd = getMessageHistoryDropdown();
  if (dd) dd.show();
}

/**
 * 从消息数据库加载历史记录（包含新消息和回复消息）
 * 从老到新排序
 */
async function loadInputHistory() {
  try {
    const limit = state.webuiConfig.input_history_limit || 500;
    const response = await fetch(`/api/input-history?limit=${limit}`);
    if (!response.ok) {
      console.error('加载输入历史失败');
      state.inputHistory = [];
      return;
    }

    const history = await response.json();
    // 去重：基于content（不区分大小写，忽略首尾空格）
    const seen = new Set();
    state.inputHistory = (history || []).filter(item => {
      const key = (item.content || '').trim().toLowerCase();
      if (seen.has(key)) {
        return false;
      }
      seen.add(key);
      return true;
    });
    state.historyIndex = -1;
  } catch (e) {
    console.error('加载输入历史失败:', e);
    state.inputHistory = [];
  }
}

/**
 * 保存输入历史记录到服务器
 * @param {string} content - 输入内容
 */
async function saveInputHistory(content) {
  if (!content || !content.trim()) return;

  const trimmedContent = content.trim();
  const timestamp = Date.now();

  // 去重：如果历史中已存在相同内容，移除旧的
  const existingIndex = state.inputHistory.findIndex(item => item.content === trimmedContent);
  if (existingIndex !== -1) {
    state.inputHistory.splice(existingIndex, 1);
  }

  // 添加到历史记录末尾
  state.inputHistory.push({
    content: trimmedContent,
    timestamp: timestamp,
  });

  // 限制历史记录数量
  const maxLimit = state.webuiConfig.input_history_limit || 500;
  if (state.inputHistory.length > maxLimit) {
    state.inputHistory.shift();
  }

  // 保存到服务器
  try {
    const response = await fetch('/api/input-history', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        history: state.inputHistory,
      }),
    });

    if (!response.ok) {
      throw new Error('保存失败');
    }

  } catch (e) {
    console.error('保存输入历史失败:', e);
  }
}

/**
 * 隐藏输入历史记录列表
 */
function hideInputHistoryList() {
  // 🔧 清除防抖定时器，防止隐藏后又被自动重新弹出
  if (state._historyListTimer) {
    clearTimeout(state._historyListTimer);
    state._historyListTimer = null;
  }
  const dd = getMessageHistoryDropdown();
  if (dd) dd.hide();
  else {
    // fallback: 直接操作 DOM
    const historyList = document.getElementById('input-history-list');
    if (historyList) {
      historyList.style.display = 'none';
    }
  }
  state.filteredHistoryIndex = -1;
}

/**
 * 判断光标是否在输入框末尾
 * @param {Range} selectionRange - 当前的选区范围
 * @param {HTMLElement} inputElement - 输入框元素
 * @returns {boolean} - 光标是否在末尾
 */
function isCursorAtInputEnd(selectionRange, inputElement) {
  if (!selectionRange || !inputElement) {
    return false;
  }

  // 检查选区是否在输入框内
  if (!inputElement.contains(selectionRange.commonAncestorContainer)) {
    return false;
  }

  // 获取输入框的文本内容
  const textContent = inputElement.innerText || '';
  const textLength = textContent.length;

  // 判断光标是否在末尾：选区为折叠状态且在输入框内
  if (!selectionRange.collapsed) {
    return false; // 有选中文本，不是在末尾
  }

  // 检查光标是否在文本内容的最后一个位置
  const startOffset = selectionRange.startOffset;
  const endOffset = selectionRange.endOffset;

  // 如果光标在文本节点中，检查是否在末尾
  if (selectionRange.startContainer.nodeType === Node.TEXT_NODE) {
    const textNode = selectionRange.startContainer;
    // 检查是否是输入框的最后一个文本节点
    if (inputElement.contains(textNode) && isLastTextNode(inputElement, textNode)) {
      // 光标在最后一个文本节点，且偏移量等于文本长度
      return startOffset === endOffset && endOffset === textNode.length;
    }
  }

  // 其他情况（如光标在元素节点中），检查是否在末尾
  const rangeAtEnd = inputElement.compareDocumentPosition(selectionRange.commonAncestorContainer);
  if (rangeAtEnd & Node.DOCUMENT_POSITION_FOLLOWING) {
    return false;
  }
  return true;
}

/**
 * 判断一个文本节点是否是容器内的最后一个文本节点
 * @param {HTMLElement} container - 容器元素
 * @param {Text} textNode - 文本节点
 * @returns {boolean} - 是否是最后一个文本节点
 */
function isLastTextNode(container, textNode) {
  const walker = document.createTreeWalker(
    container,
    NodeFilter.SHOW_TEXT,
    null,
    false
  );

  let lastTextNode = null;
  let node;
  while (node = walker.nextNode()) {
    if (node.textContent.trim() !== '') {  // 忽略空文本节点
      lastTextNode = node;
    }
  }

  return lastTextNode === textNode;
}

/**
 * 在过滤后的历史列表中导航（上下键）
 * 只改变高亮选中项，不改变输入框内容
 * @param {number} direction - -1: 上键（往上），1: 下键（往下）
 */
function navigateFilteredHistory(direction) {
  const dd = getMessageHistoryDropdown();
  if (dd) dd.navigate(direction);
}

/**
 * 应用选中的历史项（Tab键或点击）
 * 把选中项的内容填入输入框
 */
function applySelectedHistoryItem() {
  const dd = getMessageHistoryDropdown();
  if (dd) dd.applySelected();
}

/**
 * 删除选中的历史项（Del键）
 * 当光标在输入框末尾时，按Del键删除选中的历史项并保存
 */
async function deleteSelectedHistoryItem() {
  const dd = getMessageHistoryDropdown();
  if (dd) dd.deleteSelected();
}

/**
 * 格式化历史记录时间戳
 * @param {string|number} timestamp - 时间戳
 */
function formatHistoryTime(timestamp) {
  if (!timestamp) return '未知时间';

  const date = new Date(timestamp);
  if (isNaN(date.getTime())) return '未知时间';

  const now = new Date();
  const diff = now - date;

  if (diff < 60000) {
    return '刚刚';
  } else if (diff < 3600000) {
    return `${Math.floor(diff / 60000)} 分钟前`;
  } else if (diff < 86400000) {
    return `${Math.floor(diff / 3600000)} 小时前`;
  } else {
    return date.toLocaleDateString('zh-CN') + ' ' + date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
  }
}

/**
 * HTML 转义
 * @param {string} text - 要转义的文本
 */
/**
 * 文本截断函数（同时限制行数和字符数）
 * @param {string} text - 原始文本（纯文本，不含HTML）
 * @param {number} maxLines - 最大显示行数（null则使用配置默认值）
 * @param {number|string} msgId - 消息/事务 ID（用于生成展开链接）
 * @param {string} type - 类型：'chain' | 'node' | 'task'
 * @param {boolean} addExpandLink - 是否添加展开链接（默认 true）
 * @returns {string} 截断后的文本（包含展开链接，纯文本，不转义HTML）
 */
function truncateText(text, maxLines = null, msgId = null, type = 'chain', addExpandLink = true) {
  if (!text) return '';
  const configMaxLines = maxLines !== null ? maxLines : state.webuiConfig.content_trunc_lines;
  const configMaxChars = state.webuiConfig.content_trunc_chars;

  const totalChars = text.length;
  const totalLines = text.split('\n').length;

  // 判断是否需要截断（行数或字符数任一超标）
  const exceedsLines = totalLines > configMaxLines;
  const exceedsChars = totalChars > configMaxChars;

  if (!exceedsLines && !exceedsChars) {
    return text;
  }

  // 按行数截断（在换行处截断）
  const byLines = text.split('\n').slice(0, configMaxLines).join('\n');

  // 按字符数截断：找到不超过 configMaxChars 的最后一行（在换行处截断）
  let byChars = text;
  const lines = text.split('\n');
  let charCount = 0;
  let keepLines = 0;
  for (let i = 0; i < lines.length; i++) {
    // 加上当前行长度和换行符（除了最后一行）
    const lineLen = lines[i].length + (i < lines.length - 1 ? 1 : 0);
    if (charCount + lineLen > configMaxChars && i > 0) {
      break;
    }
    charCount += lineLen;
    keepLines = i + 1;
  }
  if (keepLines < lines.length) {
    byChars = lines.slice(0, keepLines).join('\n');
  }

  // 取截断后更短的方案
  const truncated = byLines.length <= byChars.length ? byLines : byChars;

  // 放弃截断条件：截断后行数只比原行数少4行以内，或者截断后长度只比原长度少60字符以内
  const truncLines = truncated.split('\n').length;
  const truncChars = truncated.length;
  if ((totalLines - truncLines < 4) || (totalChars - truncChars < 60)) {
    return text;
  }

  if (addExpandLink && msgId !== null) {
    const expandLink = `<span class="expand-link" data-msg-id="${msgId}" data-type="${type}" onclick="event.stopPropagation(); toggleMessageExpand(event, ${msgId}, '${type}')" title="点击展开完整内容">...</span>`;
    return truncated + expandLink;
  }
  return truncated;
}

function escapeHtml(text) {
  // 🔧 事务#1545优化：改纯字符串替换实现
  // 此前用 createElement('div') + textContent + innerHTML 转义，每条历史记录都要
  // 创建一次 DOM 元素；历史列表（≤500项）每键重渲染时该开销被放大 500 倍。
  // 纯字符串 replace 无 DOM 分配，且语义与浏览器序列化规则等价（覆盖5个关键字符）
  if (!text) return text;
  return text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

/**
 * 删半句话（Shift+Backspace）
 * 往前删直到碰到标点符号
 * @param {HTMLElement} input - 输入框元素
 */
function deleteHalfSentence(input) {
  const value = input.innerText;
  // contenteditable不支持selectionStart，暂时使用innerText长度
  const cursorPos = value.length;

  if (cursorPos === 0) return;  // 已经在开头

  // 标点符号集合（中英文）
  const punctuations = '.,;:!?，。；：！？、」「』『】【））（(）"<>"\'\"\'\n\r';

  // 从光标位置往前找，直到碰到标点符号
  let newPos = cursorPos - 1;
  while (newPos > 0 && !punctuations.includes(value[newPos - 1])) {
    newPos--;
  }

  // 删除从 newPos 到 cursorPos 的内容
  const newValue = value.substring(0, newPos) + value.substring(cursorPos);
  input.innerText = newValue;
  // contenteditable不支持selectionStart，暂时不设置光标位置

  // 触发 input 事件以更新任何监听器
  input.dispatchEvent(new Event('input', { bubbles: true }));
}

/**
 * 转义正则表达式特殊字符
 * @param {string} string - 要转义的字符串
 */
