/**
 * {{ agent_name }} Web UI JavaScript
 * 
 * 功能：
 * - 消息树管理
 * - 事务树管理
 * - WebSocket 实时输出
 * - 主题切换
 * - 文件上传
 */

// ==================== 终端自动重连配置 ====================

/**
 * 获取当前选中的nest查询参数字符串
 * 用于文件API和终端WebSocket请求中附加nest参数
 * @returns {string} 如 "&nest=xxx" 或空字符串
 */
function getNestParam() {
  const nestSelect = document.getElementById('nest-select');
  const currentNest = nestSelect ? nestSelect.value : '';
  // 🔧 "local" 也发送 nest 参数，走 local nest 通道（支持盘符切换等功能）
  return currentNest ? `&nest=${encodeURIComponent(currentNest)}` : '';
}

/**
 * 获取当前选中的nest名称
 * @returns {string|null} nest名称或null（表示本地）
 */
function getSelectedNestName() {
  const nestSelect = document.getElementById('nest-select');
  const currentNest = nestSelect ? nestSelect.value : '';
  // 🔧 "local" 也返回，让后端通过 local nest 通道处理
  return currentNest || null;
}

/**
 * 检查文件是否存在，支持从远程nest回退到本地
 * 如果当前选中了远程nest但文件在远端不存在，会自动检查本地
 * @param {string} filePath - 文件路径
 * @param {boolean} fallbackToLocal - 是否回退到本地文件系统检查（图片文件=true，非图片文件=false）
 * @returns {Promise<{exists: boolean, useLocal: boolean, nest?: string, is_dir?: boolean}>} 文件是否存在以及是否使用本地
 */
async function checkFileExistsWithLocalFallback(filePath, fallbackToLocal = true) {
  const encodedPath = encodeURIComponent(filePath);
  const nestParam = getNestParam();
  const selectedNest = getSelectedNestName();

  /** 统一解析 /api/files/exists 的响应，区分文件和目录 */
  const parseResult = (result, useLocal, nestName) => {
    if (!result.success) return null;
    if (result.exists) return { exists: true, useLocal, nest: nestName, is_dir: false };
    if (result.is_dir) return { exists: false, useLocal, nest: nestName, is_dir: true };
    return null;
  };

  // 本地模式直接检查
  if (!selectedNest) {
    try {
      const response = await fetch(`/api/files/exists?path=${encodedPath}`);
      const result = await response.json();
      const parsed = parseResult(result, true, undefined);
      if (parsed) return parsed;
    } catch (error) {
      console.warn('[FileExists] 检查本地文件存在性失败:', error);
    }
  }

  // 远程模式：先检查当前选中的远端
  if (selectedNest) {
    try {
      const response = await fetch(`/api/files/exists?path=${encodedPath}${nestParam}`);
      const result = await response.json();
      const parsed = parseResult(result, false, selectedNest);
      if (parsed) return parsed;
    } catch (error) {
      console.warn('[FileExists] 检查远程文件存在性失败:', error);
    }
  }

  // 🔧 当前nest找不到文件，遍历所有其他可用nest查找
  const allNests = window._latestNests || {};
  for (const [nestName, nestInfo] of Object.entries(allNests)) {
    // 跳过当前已检查的nest和未连接的nest
    if (nestName === selectedNest || !nestInfo.connected) continue;
    try {
      const response = await fetch(`/api/files/exists?path=${encodedPath}&nest=${encodeURIComponent(nestName)}`);
      const result = await response.json();
      const parsed = parseResult(result, false, nestName);
      if (parsed) return parsed;
    } catch (error) {
      // 忽略单个nest的查询错误
    }
  }

  // 🔧 仅图片文件回退到本地文件系统检查（非图片文件只检查nest路径）
  if (selectedNest && fallbackToLocal) {
    try {
      const response = await fetch(`/api/files/exists?path=${encodedPath}`);
      const result = await response.json();
      const parsed = parseResult(result, true, undefined);
      if (parsed) return parsed;
    } catch (error) {
      console.warn('[FileExists] 回退检查本地文件存在性失败:', error);
    }
  }

  return { exists: false, useLocal: false, is_dir: false };
}

const TERMINAL_RECONNECT_CONFIG = {
  maxRetries: 10,  // 最大重连次数
  retryDelay: 1000,  // 重连间隔（毫秒）
  retryDelayMultiplier: 1.5,  // 重连间隔倍数（指数退避）
  maxRetryDelay: 30000,  // 最大重连间隔（毫秒）
};

/**
 * 创建带自动重连的WebSocket连接
 * @param {string} wsUrl - WebSocket URL
 * @param {Terminal} xterm - xterm实例
 * @param {string} tabId - 标签页ID
 * @param {Function} onOpen - 连接成功回调
 * @param {Function} onClose - 连接关闭回调
 * @param {Function} onReconnect - 重连成功回调
 * @returns {WebSocket} WebSocket实例
 */
function createWebSocketWithReconnect(wsUrl, xterm, tabId, onOpen, onClose, onReconnect) {
  let retryCount = 0;
  let retryDelay = TERMINAL_RECONNECT_CONFIG.retryDelay;
  let reconnectTimeout = null;
  let isManualClose = false;
  let isFirstConnection = true;  // 🔧 修复：标记是否是首次连接

  const connect = () => {
    const ws = new WebSocket(wsUrl);

    ws.onopen = () => {
      retryCount = 0;
      retryDelay = TERMINAL_RECONNECT_CONFIG.retryDelay;
      if (isFirstConnection) {
        // 首次连接，显示连接消息并调用onOpen
        xterm.write('\x1b[32m✓ 远程终端已连接\x1b[0m\r\n');
        if (onOpen) onOpen(ws);
        isFirstConnection = false;
      } else {
        // 重连成功，显示重连消息并调用onReconnect
        xterm.write('\x1b[32m✓ 远程终端已重连\x1b[0m\r\n');
        if (onReconnect) onReconnect(ws);  // 🔧 修复：调用onReconnect回调
      }
    };

    ws.onmessage = (event) => {
      try {
        // 先尝试解析为JSON消息
        const msg = JSON.parse(event.data);
        if (msg.type === 'session_created' && msg.session_id) {
          // 🔧 修复：后端创建新会话后返回session_id，保存到tab对象
          const tab = state.terminalTabs.find(t => t.id === tabId);
          if (tab) {
            tab.sessionId = msg.session_id;
          }
          return; // 不写入xterm
        } else if (msg.type === 'error' && msg.message === '会话不存在') {
          // 🔧 修复：需求#412 - 后端返回会话不存在错误，关闭对应的终端标签页
          console.warn(`[WebSocketReconnect] 收到会话不存在错误: session_id=${msg.session_id}, tabId: ${tabId}`);
          const tab = state.terminalTabs.find(t => t.id === tabId);
          if (tab) {
            closeTerminalTab(tabId);
          }
          return; // 不写入xterm
        } else if (msg.type === 'screen_restore') {
          // 🔧 修复：需求#7 - 收到序列化屏幕恢复消息，使用xterm.write恢复终端
          // xterm-addon-serialize 的 serialize() 输出是ANSI转义序列，可以直接write
          const tab = state.terminalTabs.find(t => t.id === tabId);
          if (tab && msg.screen) {
            try {
              // 先清空终端并重置状态
              xterm.clear();
              xterm.reset();
              // 使用write写入序列化内容恢复屏幕状态（包括光标位置、颜色、内容等）
              xterm.write(msg.screen);
            } catch (e) {
              console.error('[WebSocketReconnect] 屏幕恢复失败:', e);
              // 恢复失败时，清空终端显示连接消息
              xterm.write('\x1b[33m⚠ 终端屏幕恢复失败，显示为空\x1b[0m\r\n');
            }
          }
          return; // 不写入xterm
        }
        // 其他JSON消息，写入xterm
        xterm.write(event.data);
      } catch (e) {
        // 不是JSON，直接写入xterm（pty输出）
        xterm.write(event.data);
      }
    };

    ws.onclose = (event) => {

      // 🔧 修复：需求#7 - 断开前尝试最后同步一次屏幕（页面刷新等场景）
      // 使用 sendBeacon 或同步发送（WebSocket可能已经关闭，所以用try-catch）
      try {
        const tab = state.terminalTabs.find(t => t.id === tabId);
        if (tab && tab.serializeAddon && ws.readyState === WebSocket.OPEN) {
          const serialized = tab.serializeAddon.serialize();
          if (serialized && serialized.length > 0) {
            ws.send(JSON.stringify({ type: 'screen_sync', screen: serialized }));
          }
        }
      } catch (e) {
        // WebSocket可能已关闭，忽略
      }

      // 手动关闭不重连
      if (isManualClose) {
        if (onClose) onClose();
        return;
      }

      // 正常关闭（code=1000）不重连
      if (event.code === 1000) {
        xterm.write('\x1b[31m✗ 远程终端已断开\x1b[0m\r\n');
        if (onClose) onClose();
        return;
      }

      // 异常关闭，尝试重连
      xterm.write('\x1b[33m⚠ 远程终端连接异常，尝试重连...\x1b[0m\r\n');

      if (retryCount >= TERMINAL_RECONNECT_CONFIG.maxRetries) {
        xterm.write('\x1b[31m✗ 重连失败，已达到最大重连次数\x1b[0m\r\n');
        console.error(`[WebSocketReconnect] 达到最大重连次数 (${TERMINAL_RECONNECT_CONFIG.maxRetries}), tabId: ${tabId}`);
        if (onClose) onClose();
        return;
      }

      retryCount++;

      reconnectTimeout = setTimeout(() => {
        const newWs = connect();
        // 返回新的WebSocket实例
        return newWs;
      }, retryDelay);

      // 指数退避：增加重连间隔
      retryDelay = Math.min(
        retryDelay * TERMINAL_RECONNECT_CONFIG.retryDelayMultiplier,
        TERMINAL_RECONNECT_CONFIG.maxRetryDelay
      );
    };

    ws.onerror = (error) => {
      console.error(`[WebSocketReconnect] 连接错误, tabId: ${tabId}`, error);
      // onclose会处理重连逻辑
    };

    // 标记为手动关闭的函数
    ws.manualClose = () => {
      isManualClose = true;
      if (reconnectTimeout) {
        clearTimeout(reconnectTimeout);
        reconnectTimeout = null;
      }
    };

    return ws;
  };

  return connect();
}
