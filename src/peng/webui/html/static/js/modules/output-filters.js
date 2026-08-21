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
}


// 根据当前过滤器重新显示输出
function refreshOutputWithFilters() {
  if (!state.output) {
    console.warn('state.output为null，无法刷新输出');
    return;
  }

  const buffer = state.output.buffer.active;
  const viewportY = buffer.viewportY;
  const rows = state.output.rows;

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
        if (state.output) state.output.scrollPages(pages);
      };
      if (chunks.length > 0) {
        writeAndScroll(chunks.join(''), restoreScroll);
      } else {
        setTimeout(restoreScroll, 0);
      }
      showSimpleToast(`已更新过滤输出。`, 'success');
    }
  }
}

