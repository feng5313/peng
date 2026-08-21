// ==================== Task#185：后端驱动的终端事件处理 ====================

/**
 * 请求后端创建新终端（+按钮调用）
 * 后端创建终端后广播terminal_created事件，所有webui实例（包括本实例）收到后自动创建标签页
 */
async function requestCreateTerminal() {
  try {
    const nest = getSelectedNestName() || 'local';

    // 🔧 Task#185：计算终端容器的实际尺寸，以发起者为准
    let cols = 80, rows = 24;
    const terminalContainer = document.getElementById('terminal-container');
    const terminalView = document.getElementById('terminal-view');
    if (terminalContainer && typeof Terminal !== 'undefined') {
      // 临时显示终端容器以计算尺寸
      const viewWasVisible = terminalView && terminalView.style.display !== 'none';
      if (terminalView && !viewWasVisible) {
        terminalView.style.display = 'flex';
      }
      // 创建临时xterm测量尺寸
      const tempDiv = document.createElement('div');
      tempDiv.style.width = '100%';
      tempDiv.style.height = '100%';
      tempDiv.style.position = 'absolute';
      tempDiv.style.visibility = 'hidden';
      terminalContainer.appendChild(tempDiv);
      const tempTerm = new Terminal({
        theme: XTERM_THEMES[state.theme] || XTERM_THEMES.dark,
        fontFamily: FONT_SETTINGS.fontFamily,
        fontSize: FONT_SETTINGS.fontSizeXterm,
        lineHeight: 1.0,
        letterSpacing: 0,
        scrollback: 100,
      });
      tempTerm.open(tempDiv);
      const FitAddonClass = window.FitAddon ? window.FitAddon.FitAddon : window.FitAddon;
      if (FitAddonClass) {
        const tempFit = new FitAddonClass();
        tempTerm.loadAddon(tempFit);
        tempFit.fit();
        cols = tempTerm.cols;
        rows = tempTerm.rows;
      }
      tempTerm.dispose();
      tempDiv.remove();
      if (terminalView && !viewWasVisible) {
        terminalView.style.display = 'none';
      }
    }

    // 🔧 Task#185：获取当前工作目录，传给后端设置终端初始目录
    const workdirInput = document.getElementById('workdir-input');
    const cwd = workdirInput ? workdirInput.value : (state.workdir || '');

    const response = await fetch('/api/terminal/create', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        nest: nest !== 'local' ? nest : null,
        cols: cols,
        rows: rows,
        cwd: cwd || undefined,
      }),
    });

    if (!response.ok) {
      const errorData = await response.json().catch(() => ({}));
      const errorMsg = errorData.detail || '创建终端失败';
      showToast('error', '创建终端失败', errorMsg, 3000);
      return;
    }

    const result = await response.json();
    console.log(`[requestCreateTerminal] 终端创建请求已发送: session_id=${result.session_id}`);
    // 后端会广播terminal_created事件 → handleTerminalEvent → createTerminalUI
  } catch (error) {
    console.error('[requestCreateTerminal] 创建终端请求失败:', error);
    showToast('error', '创建终端失败', error.message, 3000);
  }
}

/**
 * 处理后端广播的终端事件（terminal_created / terminal_closed）
 * 所有webui实例通过 /ws/output 收到相同的事件，保持终端标签页一致
 */
function handleTerminalEvent(data) {
  const eventName = data.event;
  const sessionId = data.session_id;

  if (eventName === 'terminal_created') {
    // 后端创建了新终端 → 所有webui实例自动创建终端标签页
    const title = data.title || (data.nest ? data.nest + '-?' : 'terminal');
    const nest = data.nest || 'local';

    // 检查是否已经有此session_id的标签页（避免重复创建）
    const existing = state.terminalTabs.find(t => t.sessionId === sessionId);
    if (existing) {
      console.log(`[handleTerminalEvent] 终端标签页已存在: sessionId=${sessionId}`);
      return;
    }

    // 创建终端UI（不创建新会话，连接已有session）
    createTerminalUI(title, sessionId, nest);
    console.log(`[handleTerminalEvent] 终端已创建: sessionId=${sessionId}, title=${title}, nest=${nest}`);

  } else if (eventName === 'terminal_closed') {
    // 后端关闭了终端 → 所有webui实例自动关闭终端标签页
    const tab = state.terminalTabs.find(t => t.sessionId === sessionId);
    if (tab) {
      // 直接移除UI，不发送close消息（后端已经关闭了）
      removeTerminalTabUI(tab.id);
      console.log(`[handleTerminalEvent] 终端已关闭: sessionId=${sessionId}`);
    } else {
      console.log(`[handleTerminalEvent] 未找到终端标签页: sessionId=${sessionId}`);
    }
  }
}

/**
 * 创建终端UI并连接到已有的后端会话
 * 与 createNewTerminal 不同，此函数不创建新会话，只创建UI并连接
 *
 * @param {string} title - 标签页标题
 * @param {string} sessionId - 后端会话ID
 * @param {string} nest - nest名称
 */
async function createTerminalUI(title, sessionId, nest) {
  if (typeof Terminal === 'undefined') {
    console.warn('[createTerminalUI] xterm.js 未加载，无法创建终端');
    return;
  }

  try {
    // 生成标签页ID
    const tabId = 'terminal-' + Date.now() + '-' + Math.random().toString(36).substring(2, 6);

    // 创建容器div
    const container = document.createElement('div');
    container.className = 'terminal-instance';
    container.id = tabId + '-container';

    const terminalContainer = document.getElementById('terminal-container');
    if (!terminalContainer) {
      console.error('[createTerminalUI] terminal-container 元素未找到');
      return;
    }
    terminalContainer.appendChild(container);

    // 创建xterm实例
    const xterm = new Terminal({
      theme: XTERM_THEMES[state.theme] || XTERM_THEMES.dark,
      fontFamily: FONT_SETTINGS.fontFamily,
      fontSize: FONT_SETTINGS.fontSizeXterm,
      lineHeight: 1.0,
      letterSpacing: 0,
      scrollback: state.webuiConfig.xterm.scrollback,
      allowTransparency: false,
      allowProposedApi: true,
      cursorBlink: true,
      cursorStyle: 'block',
      convertEol: true,
    });

    // 临时显示容器，计算准确的窗口大小
    const terminalView = document.getElementById('terminal-view');
    const terminalViewWasVisible = terminalView && terminalView.style.display !== 'none';
    if (terminalView && !terminalViewWasVisible) {
      terminalView.style.display = 'flex';
    }
    container.classList.add('active');

    xterm.open(container);

    // 🔧 Task#1092：设置 helper textarea tabindex=-1 防止抢焦点
    const helperTa = container.querySelector('.xterm-helper-textarea');
    if (helperTa) helperTa.setAttribute('tabindex', '-1');

    // 绑定fit插件
    let fit = null;
    const FitAddonClass = window.FitAddon ? window.FitAddon.FitAddon : window.FitAddon;
    if (FitAddonClass) {
      fit = new FitAddonClass();
      xterm.loadAddon(fit);
      patchFitAddonScrollbar(fit);  // 🔧 Task#1028: 确保scrollbar宽度正确
    }

    // 绑定unicode11插件
    const unicode11Addon = new Unicode11Addon.Unicode11Addon();
    xterm.loadAddon(unicode11Addon);
    xterm.unicode.activeVersion = '11';

    // 🔧 Task#1028: 使用统一helper注册扩展Unicode11 provider
    registerExtendedUnicodeV11(xterm);

    fit.fit();
    const { rows, cols } = xterm;
    container.classList.remove('active');
    if (terminalView && !terminalViewWasVisible) {
      terminalView.style.display = 'none';
    }

    // 连接WebSocket到已有会话
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    let wsUrl = `${protocol}//${window.location.host}/ws/terminal`;
    wsUrl += `?session_id=${sessionId}`;
    wsUrl += `&cols=${cols}&rows=${rows}`;
    if (nest && nest !== 'local') {
      wsUrl += `&nest=${encodeURIComponent(nest)}`;
    }

    // 加载SerializeAddon
    let serializeAddon = null;
    const SerializeAddonClass = window.SerializeAddon ? window.SerializeAddon.SerializeAddon : null;
    if (SerializeAddonClass) {
      try {
        serializeAddon = new SerializeAddonClass();
        xterm.loadAddon(serializeAddon);
      } catch (e) {
        console.warn('[createTerminalUI] SerializeAddon加载失败:', e.message);
      }
    }

    // 屏幕同步
    let screenSyncInterval = null;
    const SCREEN_SYNC_INTERVAL = 2000;

    function syncScreenToBackend() {
      const tab = state.terminalTabs.find(t => t.id === tabId);
      if (!tab || !tab.ws || tab.ws.readyState !== WebSocket.OPEN) return;
      if (!serializeAddon) return;
      try {
        const serialized = serializeAddon.serialize();
        if (serialized && serialized.length > 0) {
          tab.ws.send(JSON.stringify({ type: 'screen_sync', screen: serialized }));
        }
      } catch (e) {
        console.warn('[screenSync] 序列化失败:', e.message);
      }
    }

    function startScreenSync() {
      if (screenSyncInterval) clearInterval(screenSyncInterval);
      screenSyncInterval = setInterval(syncScreenToBackend, SCREEN_SYNC_INTERVAL);
    }

    function stopScreenSync() {
      if (screenSyncInterval) {
        clearInterval(screenSyncInterval);
        screenSyncInterval = null;
      }
      syncScreenToBackend();
    }

    // 创建WebSocket连接（带重连）
    const ws = createWebSocketWithReconnect(
      wsUrl,
      xterm,
      tabId,
      (wsInstance) => {
        const tab = state.terminalTabs.find(t => t.id === tabId);
        if (tab) {
          tab.ws = wsInstance;
          tab.sessionId = sessionId;
        }
        startScreenSync();
      },
      () => {
        stopScreenSync();
      },
      (wsInstance) => {
        const tab = state.terminalTabs.find(t => t.id === tabId);
        if (tab) {
          tab.ws = wsInstance;
        }
        startScreenSync();
      }
    );

    // xterm输入事件
    xterm.onData((data) => {
      const tab = state.terminalTabs.find(t => t.id === tabId);
      if (tab && tab.ws && tab.ws.readyState === WebSocket.OPEN) {
        tab.ws.send(data);
      }
    });

    // xterm大小变化事件
    xterm.onResize(({ cols, rows }) => {
      const tab = state.terminalTabs.find(t => t.id === tabId);
      if (tab && tab.ws && tab.ws.readyState === WebSocket.OPEN) {
        tab.ws.send(JSON.stringify({ type: 'resize', cols, rows }));
      }
    });

    // resizeObserver
    // 🔧 修复：使用**双 requestAnimationFrame** 延迟 fit 调用
    // 嵌套 flex 布局可能需要多帧才能完成尺寸计算
    let resizeTimeout = null;
    const resizeObserver = new ResizeObserver((entries) => {
      if (state.currentTerminalTabId !== tabId) return;
      if (resizeTimeout) clearTimeout(resizeTimeout);
      resizeTimeout = setTimeout(() => {
        if (fit && entries.length > 0) {
          requestAnimationFrame(() => {
            requestAnimationFrame(() => {
              try {
                if (fit) {
                  fit.fit();
                }
              } catch (error) {
                console.warn('[createTerminalUI] fit.fit() error:', error);
              }
            });
          });
        }
      }, 50);
    });
    resizeObserver.observe(container);

    // 创建标签页对象
    const newTab = {
      id: tabId,
      title: title,
      xterm: xterm,
      fit: fit,
      ws: ws,
      sessionId: sessionId,
      resizeObserver: resizeObserver,
      resizeTimeout: resizeTimeout,
      createdAt: new Date().toISOString(),
      serializeAddon: serializeAddon,
      stopScreenSync: stopScreenSync,
      nest: nest || 'local',
    };

    state.terminalTabs.push(newTab);
    state.currentTerminalTabId = tabId;

    await switchTerminalTab(tabId);
    renderTerminalTabs();

    // 🔧 修复：使用**双 requestAnimationFrame** 延迟fit，确保嵌套flex布局完成
    // 然后发送resize给后端PTY，以发起者/当前实例的尺寸为准
    setTimeout(() => {
      if (fit) {
        requestAnimationFrame(() => {
          requestAnimationFrame(() => {
            try {
              if (fit) {
                fit.fit();
                const { cols: newCols, rows: newRows } = xterm;
                if (ws.readyState === WebSocket.OPEN) {
                  ws.send(JSON.stringify({ type: 'resize', cols: newCols, rows: newRows }));
                }
              }
            } catch (e) {
              console.warn('[createTerminalUI] delayed fit.fit() error:', e);
            }
          });
        });
      }
    }, 200);
  } catch (error) {
    console.error('[createTerminalUI] 创建终端失败:', error);
  }
}

/**
 * 直接移除终端标签页UI（不通知后端）
 * 用于后端广播terminal_closed事件时，前端只做UI清理
 *
 * @param {string} tabId - 标签页ID
 */
function removeTerminalTabUI(tabId) {
  const tabIndex = state.terminalTabs.findIndex(tab => tab.id === tabId);
  if (tabIndex === -1) return;

  const tab = state.terminalTabs[tabIndex];

  // 清理WebSocket
  if (tab.ws) {
    try {
      if (typeof tab.stopScreenSync === 'function') tab.stopScreenSync();
      if (typeof tab.ws.manualClose === 'function') tab.ws.manualClose();
      tab.ws.close();
    } catch (error) {
      console.warn('[removeTerminalTabUI] 关闭WebSocket失败:', error);
    }
  }

  // 清理resizeObserver
  if (tab.resizeObserver) tab.resizeObserver.disconnect();
  if (tab.resizeTimeout) clearTimeout(tab.resizeTimeout);

  // 清理xterm
  if (tab.xterm) {
    try { tab.xterm.dispose(); } catch (error) {
      console.warn('[removeTerminalTabUI] 销毁xterm失败:', error);
    }
  }

  // 移除容器
  const container = document.getElementById(tabId + '-container');
  if (container) container.remove();

  // 从列表中移除
  state.terminalTabs.splice(tabIndex, 1);

  if (state.currentTerminalTabId === tabId) {
    if (state.terminalTabs.length > 0) {
      const nextTab = state.terminalTabs[tabIndex] || state.terminalTabs[tabIndex - 1];
      if (nextTab) {
        state.currentTerminalTabId = nextTab.id;
        const nextContainer = document.getElementById(nextTab.id + '-container');
        if (nextContainer) nextContainer.style.display = 'block';
        if (nextTab.fit) nextTab.fit.fit();
      }
    } else {
      state.currentTerminalTabId = null;
    }
  }

  renderTerminalTabs();
}


