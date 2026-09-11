// ==================== 输出来源过滤器 ====================

// 防抖保存过滤器状态到后端
let _saveFiltersTimer = null;
function debouncedSaveFilters() {
  if (_saveFiltersTimer) clearTimeout(_saveFiltersTimer);
  _saveFiltersTimer = setTimeout(() => {
    fetch('/api/output-source-filters', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(state.outputSourceFilters)
    }).catch(err => console.warn('保存过滤器状态失败:', err));
  }, 500);
}

// 🔧 防抖保存 xterm 行号显示开关到后端（/api/config/ui-settings → data/webui.json 覆盖层）
let _saveLineNoTimer = null;
function debouncedSaveLineNumbers() {
  if (_saveLineNoTimer) clearTimeout(_saveLineNoTimer);
  _saveLineNoTimer = setTimeout(() => {
    fetch('/api/config/ui-settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ xterm: { show_line_numbers: !!state.webuiConfig.xterm.show_line_numbers } })
    }).catch(err => console.warn('保存行号显示配置失败:', err));
  }, 500);
}

function initOutputFilters() {
  // 初始化checkbox状态
  const filters = document.querySelectorAll('.filter-checkbox input[type="checkbox"]');
  filters.forEach(checkbox => {
    const source = checkbox.dataset.source;
    if (source && state.outputSourceFilters.hasOwnProperty(source)) {
      checkbox.checked = state.outputSourceFilters[source];

      // 添加change事件监听
      checkbox.addEventListener('change', (e) => {
        state.outputSourceFilters[source] = e.target.checked;
        // 重新过滤并显示输出
        refreshOutputWithFilters();
        // 同步保存过滤器状态到后端配置文件
        debouncedSaveFilters();
      });
    }
  });

  // 🔧 xterm 行号显示开关（默认不勾选，持久化到后端 xterm 配置）
  // 注意：loadWebUIConfig() 已在 initOutputFilters() 之前 await 完成（init.js），此处 state 即为持久化值
  const lineNoToggle = document.getElementById('xterm-line-numbers-toggle');
  if (lineNoToggle) {
    lineNoToggle.checked = state.webuiConfig.xterm.show_line_numbers === true;
    lineNoToggle.addEventListener('change', (e) => {
      state.webuiConfig.xterm.show_line_numbers = e.target.checked;
      // 重绘当前显示内容（静默无toast；底部锚定精确恢复滚动位置，视口不跳变）
      refreshOutputWithFilters(false, true);
      // 防抖保存到后端配置文件
      debouncedSaveLineNumbers();
    });
  }
}


// 根据当前过滤器重新显示输出
// @param {boolean} [showToast=true] - 是否显示"已更新过滤输出"toast（行号切换等场景传 false 静默重绘）
// @param {boolean} [exactScroll=false] - 精确恢复滚动位置（底部锚定）。重绘不改变内容行数的场景（行号开关）
//   必须传 true：默认的 scrollPages 页级恢复在 clear() 后 buffer 变短时会被 clamp 到顶部，视口跳变
function refreshOutputWithFilters(showToast = true, exactScroll = false) {
  if (!state.output) {
    console.warn('state.output为null，无法刷新输出');
    return;
  }

  const buffer = state.output.buffer.active;
  const viewportY = buffer.viewportY;
  const rows = state.output.rows;
  // 🔧 底部锚定精确恢复基准：视口顶部距内容底部的渲染行数（重绘前后保持不变）
  const linesFromBottom = Math.max(0, buffer.length - viewportY);

  // 清空终端
  state.output.clear(); state.currentColor = '';

  // 获取当前应该显示的输出
  const currentHistory = getOutputHistory(state.currentTaskId);
  // 如果还没有设置时间戳（首次加载），显示最新的内容
  if (state.currentOldestTimestamp && state.currentNewestTimestamp) {
    // 基于currentOldestTimestamp和currentNewestTimestamp决定显示内容
    // 🔧 Fix Task#840：翻页浏览时用 _pagingPageNewestTs 代替 currentNewestTimestamp
    const pageNewestTs = state._pagingPageNewestTs || state.currentNewestTimestamp;
    const startIdx = findIndexByTimestamp(currentHistory, state.currentOldestTimestamp);
    const endIdx = findIndexByTimestamp(currentHistory, pageNewestTs);

    if (startIdx !== -1 && endIdx !== -1 && startIdx <= endIdx) {
      const chunks = [];
      for (let i = startIdx; i <= endIdx; i++) {
        const item = currentHistory[i];
        if (passFilters(item.source)) {
          chunks.push(formatLineNoPrefix(item.fileLineNo) + convertFilePathsToLinks(item.content) + '\r\n');
        }
      }
      // 🔧 Fix Task#1111: 使用 writeAndScroll 替代坏掉的 write callback
      const pages = Math.ceil(viewportY / rows);
      const restoreScroll = () => {
        if (!state.output) return;
        if (exactScroll) {
          // 🔧 底部锚定精确恢复：先到底部，再回退到"视口顶部距内容底部 linesFromBottom 行"处。
          // writeAndScroll 重置 isUserScrolling 后写入完成时视口已在底部，此处消除其影响并精确定位，
          // 视口相对内容的位置与重绘前完全一致（滚动条位置不因行号开关切换而改变）。
          state.output.scrollToBottom();
          const nb = state.output.buffer.active;
          state.output.scrollToLine(Math.max(0, nb.length - linesFromBottom));
        } else {
          state.output.scrollPages(pages);
        }
      };
      if (chunks.length > 0) {
        writeAndScroll(chunks.join(''), restoreScroll);
      } else {
        setTimeout(restoreScroll, 0);
      }
      if (showToast !== false) showSimpleToast(`已更新过滤输出。`, 'success');
    }
  }
}

