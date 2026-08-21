// ==================== outputHistory 管理 ====================

/**
 * 获取指定 task 的 outputHistory
 * @param {number} taskId - 事务ID
 * @returns {Array} 指定的 outputHistory 数组
 */
function getOutputHistory(taskId) {
  if (!state.outputHistory.has(taskId)) {
    state.outputHistory.set(taskId, []);
  }
  return state.outputHistory.get(taskId);
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
