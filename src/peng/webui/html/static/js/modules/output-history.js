// ==================== outputHistory 管理 ====================

// 🔧 Fix #1751：单事务输出缓存上限 —— 长时间运行时 outputHistory 无限增长
// （活跃事务可达20万+条/几十MB），而 renderFromCache/renderPageRange/
// cacheHasFullRange 每次切换事务都 O(n) 全量遍历，导致切换卡顿与内存膨胀。
// 超限时裁剪最老内容；翻页缺失范围自动走后端 O(1) seek 直读（fetchPage）。
const OUTPUT_HISTORY_MAX = 20000;   // 触发裁剪的条数上限
const OUTPUT_HISTORY_KEEP = 15000;  // 裁剪后保留的最新条数

/**
 * 获取指定 task 的 outputHistory
 * @param {number} taskId - 事务ID
 * @returns {Array} 指定的 outputHistory 数组
 */
function getOutputHistory(taskId) {
  if (!state.outputHistory.has(taskId)) {
    state.outputHistory.set(taskId, []);
  }
  const history = state.outputHistory.get(taskId);
  if (history.length > OUTPUT_HISTORY_MAX) {
    history.splice(0, history.length - OUTPUT_HISTORY_KEEP);
  }
  return history;
}

/**
 * 检查指定 task 是否有输出历史缓存
 * @param {number} taskId - 事务ID
 * @returns {boolean} 是否有缓存
 */
function hasOutputHistoryCache(taskId) {
  const history = state.outputHistory.get(taskId);
  return history && history.length > 0;
}
