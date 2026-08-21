// ==================== 通用历史列表下拉组件 ====================

// 🔧 事务#1545优化：历史项小写内容缓存（WeakMap，键为 item 对象引用）
// 1. 避免每次按键对全部 ≤500 条历史全文重复 toLowerCase
// 2. 用 WeakMap 而非直接挂在 item 上（item._lc）：后者会被 JSON.stringify
//    序列化进 POST /api/input-history 与 localStorage（payload 翻倍、污染持久化数据）
// 3. item 对象生命周期与缓存生命周期天然绑定：历史重新加载（新对象）自动失效重算
const _historyLcCache = new WeakMap();
function getHistoryItemLc(item) {
  let lc = _historyLcCache.get(item);
  if (lc === undefined) {
    lc = (item.content || '').toLowerCase();
    _historyLcCache.set(item, lc);
  }
  return lc;
}

/**
 * 创建一个通用的历史列表下拉组件（工厂函数）
 * 可复用于消息输入历史、文件浏览器路径历史等场景
 *
 * @param {Object} config - 配置项
 * @param {HTMLElement} config.inputEl - 关联的输入框元素
 * @param {string} config.listId - 历史列表容器的 DOM id
 * @param {string} config.listClassName - 历史列表容器的 CSS 类名
 * @param {string} config.containerSelector - 列表要插入到的父容器选择器（如 '.input-row'）或直接传入 element
 * @param {string} config.position - 列表位置: 'above'(默认, 在输入框上方) 或 'below'(在输入框下方)
 * @param {Function} config.getHistory - 返回当前历史数组 [{content, timestamp}, ...]
 * @param {Function} config.setHistory - 设置历史数组（删除时调用）(historyArray) => void
 * @param {Function} config.saveHistory - 保存历史到持久化存储 (historyArray) => Promise<void>
 * @param {Function} config.getInputText - 获取输入框当前文本 () => string
 * @param {Function} config.setInputText - 设置输入框文本 (text) => void
 * @param {Function} [config.applyCallback] - 选中历史项后的额外回调 (text) => void
 * @param {number} [config.maxItems=1000] - 最大历史条数
 * @returns {Object} - 历史列表控制器 { show, hide, render, navigate, applySelected, deleteSelected, getFilteredIndex, setFilteredIndex, getFilteredHistory, getListEl }
 */
function createHistoryDropdown(config) {
  const {
    inputEl,
    listId,
    listClassName,
    containerSelector,
    position = 'above',
    getHistory,
    setHistory,
    saveHistory,
    getInputText,
    setInputText,
    applyCallback,
    onHide,
    maxItems = 1000,
    // 🔧 事务#1545优化：单次渲染的最大条目数（截断渲染）
    // 此前每次按键都全量渲染全部匹配项（≤500项DOM重建+高亮计算），是历史列表卡顿元凶；
    // 现在只渲染前 N 条（最新的在前查询场景足够），footer 显示总数并提示精确输入
    maxRenderItems = 50,
    // 🔧 事务#1545优化：历史项内容预览截断长度（字符）。
    // CSS 已用 max-height:80px+overflow 裁剪视觉，但 DOM/正则仍需处理全文，故截断后再转义/高亮
    previewTruncChars = 300,
  } = config;

  let filteredHistoryIndex = -1;
  let filteredHistory = [];
  let listEl = null;
  let _hintTimer = null;       // 选中项停留提示计时器
  let _hintEl = null;          // 提示气泡 DOM 元素
  let _outsideClickHandler = null;  // 外部点击关闭处理器

  /**
   * 绑定外部点击关闭事件（点击列表和输入框以外的区域时自动隐藏）
   * 使用捕获阶段，确保在 xterm 等组件内部捕获事件之前就能收到
   */
  function bindOutsideClick() {
    if (_outsideClickHandler) return;  // 已绑定，不重复
    _outsideClickHandler = (e) => {
      const el = getListEl();
      if (!el || el.style.display === 'none') return;
      // 点击在列表内不关闭；点击其他任何区域（包括输入框、xterm终端等）都关闭
      if (el.contains(e.target)) return;
      hide();
    };
    // 同时监听 mousedown 和 pointerdown（捕获阶段），确保在各种组件（xterm等）中点击都能关闭
    document.addEventListener('mousedown', _outsideClickHandler, true);
    document.addEventListener('pointerdown', _outsideClickHandler, true);
  }

  /**
   * 解绑外部点击关闭事件
   */
  function unbindOutsideClick() {
    if (_outsideClickHandler) {
      document.removeEventListener('mousedown', _outsideClickHandler, true);
      document.removeEventListener('pointerdown', _outsideClickHandler, true);
      _outsideClickHandler = null;
    }
  }

  /**
   * 获取或创建历史列表 DOM 容器
   */
  function getListEl() {
    if (listEl && listEl.parentNode) return listEl;

    listEl = document.createElement('div');
    listEl.id = listId;
    listEl.className = listClassName;

    // 🔧 阻止 mousedown 默认行为，防止点击列表项时输入框失焦
    // 否则 focus.js 的 blur 兜底机制（setTimeout 0 中 safeFocusMainInput）
    // 会在 click 事件之前隐藏列表，导致点击无法选中历史项
    listEl.addEventListener('mousedown', (e) => {
      e.preventDefault();
    });

    // 🔧 事务#1545优化：click 事件委托（绑定一次，替代此前每次 render 后对全部条目逐项 addEventListener）
    // 此前每次按键重渲染都对 ≤500 项逐项绑定监听器，是历史列表卡顿的元凶之一
    listEl.addEventListener('click', (e) => {
      const item = e.target.closest('.history-item');
      if (!item) return;
      const idx = parseInt(item.dataset.filteredIndex, 10);
      if (isNaN(idx)) return;
      filteredHistoryIndex = idx;
      applySelected();
    });

    // 找到容器
    let container;
    if (typeof containerSelector === 'string') {
      container = inputEl.closest(containerSelector);
    } else if (containerSelector instanceof HTMLElement) {
      container = containerSelector;
    }

    if (container) {
      container.appendChild(listEl);
    }

    return listEl;
  }

  /**
   * 显示历史列表
   */
  function show() {
    const el = getListEl();
    render(el);
    bindOutsideClick();  // 🔧 显示时绑定外部点击关闭
  }

  /**
   * 隐藏选中项停留提示气泡
   */
  function hideHintBubble() {
    if (_hintTimer) {
      clearTimeout(_hintTimer);
      _hintTimer = null;
    }
    if (_hintEl && _hintEl.parentNode) {
      _hintEl.parentNode.removeChild(_hintEl);
    }
    _hintEl = null;
  }

  /**
   * 显示选中项停留提示气泡（在选中项左侧、历史列表外面弹出）
   */
  function showHintBubble(targetItem) {
    hideHintBubble();
    if (!targetItem) return;

    _hintTimer = setTimeout(() => {
      // 确认该项仍然选中
      const currentList = getListEl();
      if (!currentList) return;
      const currentSelected = currentList.querySelector('.history-item.selected');
      if (currentSelected !== targetItem) return;

      // 创建提示气泡，插入到 body 以避免被 overflow 裁剪
      const hint = document.createElement('div');
      hint.className = 'history-hint-bubble';
      hint.textContent = '按 Tab 键选用此项';
      hint.style.visibility = 'hidden';  // 先隐藏以测量尺寸
      document.body.appendChild(hint);

      // 根据选中项的位置计算气泡位置（在选中项左侧外面）
      const itemRect = targetItem.getBoundingClientRect();
      const listRect = currentList.getBoundingClientRect();
      hint.style.position = 'fixed';
      hint.style.top = (itemRect.top + itemRect.height / 2 - hint.offsetHeight / 2) + 'px';
      // 放在列表左侧外面（留4px间距）
      hint.style.left = (listRect.left - hint.offsetWidth - 4) + 'px';
      hint.style.visibility = '';  // 显示

      _hintEl = hint;
    }, 1000);
  }

  /**
   * 隐藏历史列表
   */
  function hide() {
    hideHintBubble();
    unbindOutsideClick();  // 🔧 隐藏时解绑外部点击关闭
    const el = getListEl();
    if (el) {
      el.style.display = 'none';
    }
    filteredHistoryIndex = -1;
    // 🔧 通知外部（清除防抖定时器等）
    if (onHide) onHide();
  }

  /**
   * 渲染历史列表
   * @param {HTMLElement} historyList - 列表容器
   */
  function render(historyList) {
    const history = getHistory();
    if (history.length === 0) {
      historyList.innerHTML = '<div class="history-empty">暂无历史记录</div>';
      historyList.style.display = 'block';
      return;
    }

    // 获取输入框内容用于过滤
    const filterText = getInputText().trim();

    // 优化：如果没有过滤文本，直接使用原始历史
    let displayHistory = history;
    let highlightTerms = [];
    if (filterText) {
      const { andTerms, orTerms } = parseSearchTerms(filterText);
      // 🔧 事务#1545优化：关键词小写化只做一次（此前 filter 回调内每项每词重复 toLowerCase）
      const andTermsLc = andTerms.map(t => t.toLowerCase());
      const orTermsLc = orTerms.map(t => t.toLowerCase());
      displayHistory = history.filter(item => {
        // 🔧 事务#1545优化：小写内容 WeakMap 缓存，历史内容不变时跨按键复用
        const content = getHistoryItemLc(item);
        if (andTermsLc.length > 0) {
          for (const term of andTermsLc) {
            if (!content.includes(term)) return false;
          }
        }
        if (orTermsLc.length > 0) {
          let anyMatch = false;
          for (const term of orTermsLc) {
            if (content.includes(term)) { anyMatch = true; break; }
          }
          if (!anyMatch) return false;
        }
        return true;
      });
      highlightTerms = [...andTerms, ...orTerms];
    }

    // 去重（🔧 事务#1545优化：用缓存的小写内容做 key，避免重复 toLowerCase/trim 生成新字符串）
    const seenContents = new Set();
    displayHistory = displayHistory.filter(item => {
      const key = getHistoryItemLc(item);
      if (seenContents.has(key)) {
        return false;
      }
      seenContents.add(key);
      return true;
    });

    filteredHistory = displayHistory;

    if (displayHistory.length === 0) {
      if (filterText) {
        hide();
        return;
      }
      historyList.innerHTML = '<div class="history-empty">暂无历史记录</div>';
      historyList.style.display = 'block';
      return;
    }

    // 🔧 事务#1545优化：滑动窗口渲染 —— filteredHistory 仍保留全部匹配（navigate/applySelected
    // 基于完整数组），但 DOM 只渲染窗口内的 maxRenderItems 条：
    //   - 默认（未选中，filteredHistoryIndex === -1）：窗口取最后 N 条（最新，ArrowUp 首选）
    //   - 已有选中项：窗口平移以始终包含选中索引（类似 IDE 补全列表的虚拟滚动）
    const total = displayHistory.length;
    let renderStart = 0;
    if (total > maxRenderItems) {
      renderStart = total - maxRenderItems;
      if (filteredHistoryIndex >= 0) {
        if (filteredHistoryIndex < renderStart) {
          renderStart = filteredHistoryIndex;
        } else if (filteredHistoryIndex >= renderStart + maxRenderItems) {
          renderStart = filteredHistoryIndex - maxRenderItems + 1;
        }
      }
    }
    const renderEnd = Math.min(total, renderStart + maxRenderItems);
    const renderedItems = displayHistory.slice(renderStart, renderEnd);

    // 使用 DocumentFragment 批量插入
    const fragment = document.createDocumentFragment();

    renderedItems.forEach((item, idx) => {
      const realIdx = renderStart + idx;  // 🔧 映射回完整 filteredHistory 的真实索引
      const isSelected = realIdx === filteredHistoryIndex;
      // 🔧 事务#1545优化：内容预览截断（CSS max-height:80px 本就裁剪视觉，
      // 截断后转义/高亮的正则开销从"全文"降为"≤300字符"）；防御 content 为 null/undefined
      const rawContent = item.content || '';
      const contentPreview = rawContent.length > previewTruncChars
        ? rawContent.slice(0, previewTruncChars) + '…'
        : rawContent;
      const highlightedContent = highlightTerms.length > 0
        ? highlightSearch(escapeHtml(contentPreview), highlightTerms.join('|'))
        : escapeHtml(contentPreview);

      const div = document.createElement('div');
      div.className = `history-item ${isSelected ? 'selected' : ''}`;
      div.dataset.filteredIndex = realIdx;

      const contentDiv = document.createElement('div');
      contentDiv.className = 'history-content';
      contentDiv.innerHTML = highlightedContent;

      div.appendChild(contentDiv);
      fragment.appendChild(div);
    });

    // 🔧 事务#1545优化：匹配总数超过渲染上限时追加 footer 提示（不可点击，不计入导航）
    if (total > maxRenderItems) {
      const more = document.createElement('div');
      more.className = 'history-more-footer';
      const hiddenBefore = renderStart;
      const hiddenAfter = total - renderEnd;
      const parts = [];
      if (hiddenBefore > 0) parts.push(`上方还有 ${hiddenBefore} 条`);
      if (hiddenAfter > 0) parts.push(`下方还有 ${hiddenAfter} 条`);
      more.textContent = `共匹配 ${total} 条，${parts.join('，')}（↑↓ 导航查看，输入更多字符精确过滤）`;
      fragment.appendChild(more);
    }

    historyList.innerHTML = '';
    historyList.appendChild(fragment);
    historyList.style.display = 'block';

    // 🔧 事务#1545优化：click 已在 getListEl() 创建时事件委托绑定一次，无需逐项绑定

    const selectedItem = historyList.querySelector('.history-item.selected');
    if (selectedItem) {
      selectedItem.scrollIntoView({ block: 'nearest' });
      // 🔧 选中项停留超过1秒时弹出提示
      showHintBubble(selectedItem);
    }
  }

  /**
   * 在过滤后的历史列表中导航（上下键）
   * 🔧 修复：先 render 确保 filteredHistory 与当前输入框内容同步，再计算导航目标
   */
  function navigate(direction) {
    const el = getListEl();

    // 先 render 一次，确保 filteredHistory 与当前输入框内容一致
    // （用户可能刚输入了新字符但防抖定时器还未触发）
    render(el);

    if (!filteredHistory || filteredHistory.length === 0) {
      return;
    }

    const maxIndex = filteredHistory.length - 1;
    let newIndex;

    if (filteredHistoryIndex === -1 || filteredHistoryIndex > maxIndex) {
      newIndex = direction === -1 ? maxIndex : 0;
    } else {
      newIndex = filteredHistoryIndex + direction;
      if (newIndex < 0) {
        newIndex = maxIndex;
      } else if (newIndex > maxIndex) {
        newIndex = 0;
      }
    }
    filteredHistoryIndex = newIndex;

    // 再次 render 显示新的选中项
    render(el);
  }

  /**
   * 应用选中的历史项（Tab键或点击）
   */
  function applySelected() {
    if (!filteredHistory || filteredHistoryIndex < 0 || filteredHistoryIndex >= filteredHistory.length) {
      return;
    }

    hideHintBubble();

    const selectedItem = filteredHistory[filteredHistoryIndex];
    setInputText(selectedItem.content);

    hide();

    inputEl.focus();

    if (applyCallback) {
      applyCallback(selectedItem.content);
    }
  }

  /**
   * 删除选中的历史项（Del键）
   */
  async function deleteSelected() {
    if (!filteredHistory || filteredHistoryIndex < 0 || filteredHistoryIndex >= filteredHistory.length) {
      return;
    }

    const selectedItem = filteredHistory[filteredHistoryIndex];
    const deletedContent = selectedItem.content;

    // 从原始历史中删除该项
    const history = getHistory();
    const originalIndex = history.findIndex(item => item.content === deletedContent);
    if (originalIndex !== -1) {
      history.splice(originalIndex, 1);
    }
    setHistory(history);

    // 持久化保存
    await saveHistory(history);

    // 🔧 删除后：如果还有剩余项则重新渲染保持打开，否则关闭列表
    if (getHistory().length === 0) {
      hide();
    } else {
      const el = getListEl();
      if (el) {
        if (filteredHistoryIndex >= filteredHistory.length - 1) {
          filteredHistoryIndex = Math.max(0, filteredHistory.length - 2);
        }
        render(el);
      }
    }
  }

  return {
    show,
    hide,
    render,
    navigate,
    applySelected,
    deleteSelected,
    getFilteredIndex: () => filteredHistoryIndex,
    setFilteredIndex: (idx) => { filteredHistoryIndex = idx; },
    getFilteredHistory: () => filteredHistory,
    getListEl,
  };
}

