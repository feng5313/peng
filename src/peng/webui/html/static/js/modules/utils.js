// ==================== 路径格式工具 ====================

/**
 * 解析指定 nest 的平台标识（统一小写）
 * @param {string} nestName - nest名称（'local' 或远程nest键名）
 * @returns {string|null} 'windows'/'linux'/null（无法确定时）
 */
function _resolveNestPlatform(nestName) {
  // 本地 nest：使用后端平台信息（/api/config 已返回，可靠）
  if (!nestName || nestName === 'local') {
    return state.platform || null;
  }
  // 远程 nest：从缓存的 nest 连接信息获取
  if (window._latestNests) {
    const info = window._latestNests[nestName];
    if (info && info.platform) {
      const p = String(info.platform).toLowerCase();
      if (p === 'windows' || p === 'linux' || p === 'darwin') return p === 'windows' ? 'windows' : 'linux';
    }
  }
  return null;
}

/**
 * 判断当前选中的 nest 是否为 Windows 平台
 * @returns {boolean}
 */
function isCurrentNestWindows() {
  // 🔧 Task#1641：优先使用确定性状态 state.nestPlatform（由 _prepareNestState /
  // _updateWorkdirForNest 在nest切换时同步维护），避免 DOM 异步更新时序导致误判
  if (state.nestPlatform) {
    return state.nestPlatform === 'windows';
  }
  // 回退：动态解析（远程nest信息或后端平台）
  const nestName = state.remoteNest || (() => {
    const nestSelect = document.getElementById('nest-select');
    return nestSelect ? nestSelect.value : '';
  })();
  const platform = _resolveNestPlatform(nestName);
  if (platform) return platform === 'windows';
  // 最后回退：后端平台信息
  return state.platform === 'windows';
}

/**
 * 将路径转换为当前平台的本机显示格式
 * Windows 平台将正斜杠 (/) 转为反斜杠 (\)，其他平台保持不变
 * 仅用于UI显示，内部存储和API通信始终使用正斜杠
 * @param {string} path - 内部路径（正斜杠格式）
 * @returns {string} 本机显示格式路径
 */
function toNativeDisplayPath(path) {
  if (!path) return path;
  if (isCurrentNestWindows()) {
    return path.replace(/\//g, '\\');
  }
  return path;
}

// ==================== 加载状态 ====================

function setLoading(loading) {
  state.isLoading = loading;

  const sendBtn = document.getElementById('send-btn');
  if (sendBtn) {
    sendBtn.disabled = loading;
    sendBtn.innerHTML = loading
      ? '<span class="spinner"></span>'
      : 'Enter';
  }
}

// ==================== 工具函数 ====================

function getStatusClass(status) {
  const statusMap = {
    'pending': 'status-pending',
    'running': 'status-running',
    'processing': 'status-processing',
    'replied': 'status-replied',
    'done': 'status-done',
    'paused': 'status-paused',
    'interrupted': 'status-interrupted',
    'failed': 'status-failed',
    'completed': 'status-done',
    'cancelled': 'status-cancelled',
    'unread': 'status-unread',
    'read': 'status-read',
    'accepted': 'status-done',
    'accepting': 'status-processing',
  };
  return statusMap[status] || '';
}

function getStatusIcon(status, providerName = null) {
  // running状态时，使用provider的图标
  if (status === 'running' && providerName) {
    const providerIcon = getProviderIcon(providerName);
    if (providerIcon) return providerIcon;
  }

  const icon = getIcon(`status_${status}`, null);
  if (icon) return icon;

  // 默认图标（配置未定义时使用）
  const iconMap = {
    'pending': '⏳',
    'running': '🥎',
    'accepting': '🔍',
    'accepted': '✅',
    'processing': '⏳',
    'replied': '✔️',
    'done': '✅',
    'paused': '⏸️',
    'interrupted': '⚠️',
    'failed': '🔴',
    'completed': '✅',
    'cancelled': '🚫',
    'unread': '📩',
    'read': '☑️',
  };
  return iconMap[status] || '';
}

// 获取provider的图标
function getProviderIcon(providerName) {
  if (!providerName || !state.providers) return null;
  const provider = state.providers.find(p => p.name === providerName);
  return provider?.icon || null;
}

function getStatusTitle(status) {
  const titleMap = {
    'pending': '待定',
    'running': '执行中',
    'accepting': '验收中',
    'accepted': '已验收',
    'processing': '处理中',
    'replied': '已回复',
    'done': '已完成',
    'paused': '已暂停',
    'interrupted': '已中断',
    'failed': '失败',
    'completed': '已完成',
    'cancelled': '已取消',
    'unread': '未读',
    'read': '已读',
  };
  return titleMap[status] || status || '';
}

function showError(message, error = null, context = {}) {
  // 获取当前调用栈（即使在有 error 的情况下也获取，可能包含更多信息）
  let currentStack = '';
  try {
    currentStack = new Error().stack || '';
  } catch (e) { }

  // 格式化时间戳
  const timestamp = new Date().toISOString().replace('T', ' ').substring(0, 19);

  // 收集当前状态上下文
  const stateContext = {
    currentMessageId: state.currentMessageId,
    currentTaskId: state.currentTaskId,
    isCreatingNewMessage: state.isCreatingNewMessage,
    workdir: state.workdir,
    attachedFilesCount: state.attachedFiles?.length || 0,
  };

  if (!state.output) {
    console.error('[WebUI 错误]', message, error);
    console.error('[上下文]', { timestamp, stateContext, extraContext: context, currentStack });
    return;
  }

  // 在终端显示醒目的错误信息
  let outputMessage = '\r\n\x1b[41m\x1b[37m ═══════════════════════════════════════════════════════════════════ \x1b[0m\r\n';
  outputMessage += '\x1b[41m\x1b[37m                    [WebUI] JavaScript 错误                          \x1b[0m\r\n';


  // 时间戳
  outputMessage += `\r\n\x1b[90m时间: ${timestamp}\x1b[0m\r\n`;

  // 错误摘要
  outputMessage += `\x1b[31m${message}\x1b[0m\r\n`;

  if (error) {
    // 错误类型和信息
    outputMessage += `\r\n\x1b[33m┌─ 错误详情 ─────────────────────────────────────────────────────\x1b[0m\r\n`;
    if (error.name) {
      outputMessage += `\x1b[33m│ 错误类型: ${error.name}\x1b[0m\r\n`;
    }
    if (error.message) {
      outputMessage += `\x1b[33m│ 错误信息: ${error.message}\x1b[0m\r\n`;
    }
    outputMessage += `\x1b[33m└─────────────────────────────────────────────────────────────────\x1b[0m\r\n`;

    // 完整调用栈（优先使用 error.stack，但也显示当前调用栈）
    if (error.stack) {
      outputMessage += `\r\n\x1b[36m┌─ 错误调用栈 (error.stack) ───────────────────────────────────────\x1b[0m\r\n`;
      outputMessage += `\x1b[90m${error.stack}\x1b[0m\r\n`;
      outputMessage += `\x1b[36m└───────────────────────────────────────────────────────────────────\x1b[0m\r\n`;
    }

    // 当前调用栈（showError 被调用时的栈，可能包含更多信息）
    if (currentStack) {
      outputMessage += `\r\n\x1b[35m┌─ 当前调用栈 (new Error().stack) ─────────────────────────────────\x1b[0m\r\n`;
      outputMessage += `\x1b[90m${currentStack}\x1b[0m\r\n`;
      outputMessage += `\x1b[35m└───────────────────────────────────────────────────────────────────\x1b[0m\r\n`;
    }

    // HTTP 响应信息
    if (error.response) {
      outputMessage += `\r\n\x1b[34m┌─ HTTP 响应 ──────────────────────────────────────────────────────\x1b[0m\r\n`;
      try {
        outputMessage += `\x1b[90m${JSON.stringify(error.response, null, 2)}\x1b[0m\r\n`;
      } catch (e) {
        outputMessage += `\x1b[90m${String(error.response)}\x1b[0m\r\n`;
      }
      outputMessage += `\x1b[34m└───────────────────────────────────────────────────────────────────\x1b[0m\r\n`;
    }
  }

  // 当前状态上下文
  outputMessage += `\r\n\x1b[32m┌─ 当前状态上下文 ──────────────────────────────────────────────────\x1b[0m\r\n`;
  try {
    outputMessage += `\x1b[90m${JSON.stringify(stateContext, null, 2)}\x1b[0m\r\n`;
  } catch (e) {
    outputMessage += `\x1b[90m${String(stateContext)}\x1b[0m\r\n`;
  }
  outputMessage += `\x1b[32m└───────────────────────────────────────────────────────────────────\x1b[0m\r\n`;

  // 额外上下文（调用者传入的）
  if (Object.keys(context).length > 0) {
    outputMessage += `\r\n\x1b[33m┌─ 额外上下文 ──────────────────────────────────────────────────────\x1b[0m\r\n`;
    try {
      outputMessage += `\x1b[90m${JSON.stringify(context, null, 2)}\x1b[0m\r\n`;
    } catch (e) {
      outputMessage += `\x1b[90m${String(context)}\x1b[0m\r\n`;
    }
    outputMessage += `\x1b[33m└───────────────────────────────────────────────────────────────────\x1b[0m\r\n`;
  }

  outputMessage += '\r\n\x1b[41m\x1b[37m ═══════════════════════════════════════════════════════════════════ \x1b[0m\r\n';
  state.output.write(outputMessage);

  // 同时打印到控制台（更详细）
  console.error('[WebUI 错误]', message);
  console.error('[错误对象]', error);
  console.error('[当前状态]', stateContext);
  console.error('[额外上下文]', context);
  console.error('[当前调用栈]', currentStack);
}

// 定期刷新状态
// 🔧 修复性能：添加 Page Visibility 检测，页面不可见时暂停轮询，减少资源浪费
let statusPollingTimer = setInterval(async () => {
  // 页面不可见时跳过轮询
  if (document.hidden) return;
  try {
    const response = await fetch('/api/status');
    if (response.ok) {
      const status = await response.json();
      handleStatusUpdate(status);
    }
  } catch (error) {
    console.error('获取状态失败:', error);
  }
}, 5000);

// ==================== 编辑器保存按钮图标辅助 ====================

const SAVE_BTN_ICONS = {
  save: '💾',
  saving: '💾',
  readonly: '🔒'
};

function setSaveBtnContent(btn, mode) {
  if (!btn) return;
  btn.textContent = SAVE_BTN_ICONS[mode] || SAVE_BTN_ICONS.save;
  if (mode === 'saving') {
    btn.style.opacity = '0.6';
    btn.style.cursor = 'wait';
  } else if (mode === 'readonly') {
    btn.style.opacity = '0.5';
    btn.style.cursor = '';
  } else {
    btn.style.opacity = '';
    btn.style.cursor = '';
  }
}

