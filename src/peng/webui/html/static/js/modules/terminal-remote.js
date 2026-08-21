// ==================== 远程终端 ====================

async function initRemoteTerminal() {
  // 检查xterm是否加载
  if (typeof Terminal === 'undefined') {
    console.warn('[initRemoteTerminal] xterm.js 未加载，跳过远程终端初始化');
    return;
  }

  try {
    // 🔧 修复：页面刷新时，请求后端获取终端会话列表（不带任何参数）
    const response = await fetch('/api/terminal/sessions');
    if (!response.ok) {
      throw new Error('获取终端会话列表失败');
    }

    const data = await response.json();
    const sessions = data.sessions || [];

    // 🔧 修复：需求#412 - 先清空前端终端标签页列表
    state.terminalTabs = [];
    state.currentTerminalTabId = null;

    if (sessions.length === 0) {
      // 🔧 Task#84/#469：后端没有活跃会话，不自动创建终端
      // 用户可通过"+"按钮手动创建
      console.log('[initRemoteTerminal] 无活跃终端会话，等待用户手动创建');
      // 🔧 Task#115：渲染空的标签栏（含"+"按钮），否则用户无法新建终端
      renderTerminalTabs();
    } else {
      // 后端有活跃会话，恢复标签页
      // 🔧 Task#84：根据后端返回的nest信息生成正确的标题
      // 先按nest分组计数
      const nestCountMap = {};
      for (let i = 0; i < sessions.length; i++) {
        const session = sessions[i];
        const nestName = session.nest || 'local';
        if (!nestCountMap[nestName]) {
          nestCountMap[nestName] = 0;
        }
        nestCountMap[nestName]++;
        const title = nestName + '-' + nestCountMap[nestName];
        // 🔧 Task#185：使用createTerminalUI（后端驱动模式），不再用createNewTerminal
        await createTerminalUI(title, session.session_id, nestName);
      }
    }
  } catch (error) {
    console.error('[initRemoteTerminal] 初始化失败:', error);
    // 🔧 Task#84/#469：发生错误时，不自动创建终端
    state.terminalTabs = [];
    state.currentTerminalTabId = null;
    // 🔧 Task#115：渲染空的标签栏（含"+"按钮），否则用户无法新建终端
    renderTerminalTabs();
  }
}

async function createNewTerminal(title = null, existingSessionId = null, forceNest = null) {
  /**
    * 创建新终端或连接现有终端
    *
    * Args:
    *   title: 可选的标签页标题，如果不提供则自动生成（nest名-序号格式）
    *   existingSessionId: 可选的现有会话ID，如果提供则连接现有会话，否则创建新会话
    *   forceNest: 可选的强制指定nest名称（用于恢复会话时覆盖当前下拉框选择）
    *
    * 🔧 修复：需求#408 - 前端不传任何id给后端（创建新会话时）
     *   后端创建新的pty终端实例，通过session_created消息把session_id传给前端
    *   前端收到session_id后，仅用于标识和显示不同的终端标签页
    *
    *   如果提供了 existingSessionId，则通过WebSocket查询参数连接现有会话
    */
  if (typeof Terminal === 'undefined') {
    console.warn('[createNewTerminal] xterm.js 未加载，无法创建终端');
    return;
  }

  try {
    // 生成标签页ID
    const timestamp = Date.now();
    const tabId = 'terminal-' + timestamp;

    // 🔧 修复：如果没有提供title，则自动生成
    // 🔧 Task#84：使用nest名+序号格式（如"win11-1"、"local-1"）
    // 确定nest名称：优先使用forceNest，否则使用下拉框选择
    const effectiveNest = forceNest || getSelectedNestName() || 'local';
    const nestNameForTitle = effectiveNest === 'local' ? 'local' : effectiveNest;
    if (title === null) {
      // 计算同一nest下的序号
      let maxId = 0;
      const nestRegex = new RegExp('^' + nestNameForTitle.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '-(\\d+)$');
      state.terminalTabs.forEach(tab => {
        const match = tab.title.match(nestRegex);
        if (match) {
          const id = parseInt(match[1], 10);
          if (id > maxId) {
            maxId = id;
          }
        }
      });
      title = nestNameForTitle + '-' + (maxId + 1);
    }

    // 创建容器div
    const container = document.createElement('div');
    container.className = 'terminal-instance';
    container.id = tabId + '-container';

    // 添加到终端容器
    const terminalContainer = document.getElementById('terminal-container');
    if (!terminalContainer) {
      console.error('[createNewTerminal] terminal-container 元素未找到');
      return;
    }
    terminalContainer.appendChild(container);

    // 🔧 修复：需求#418 - 创建xterm实例
    const xterm = new Terminal({
      theme: XTERM_THEMES[state.theme] || XTERM_THEMES.dark,
      fontFamily: FONT_SETTINGS.fontFamily,
      fontSize: FONT_SETTINGS.fontSizeXterm,
      lineHeight: 1.0,
      letterSpacing: 0,
      scrollback: state.webuiConfig.xterm.scrollback,  // 从配置读取缓冲区行数
      allowTransparency: false,  // 禁用透明，让主题背景色生效
      allowProposedApi: true,  // 启用实验性 API，支持更准确的 unicode/emoji 宽度计算
      cursorBlink: true,
      cursorStyle: 'block',
      allowProposedApi: true,  // 允许使用proposed API（Unicode11Addon需要）
      convertEol: true,  // 将 LF 转换为 CR+LF，确保换行正确
    });

    // 🔧 修复：需求#418/#8 - 临时显示容器，计算准确的窗口大小
    // terminal-view 默认 display:none，需要临时可见才能让 fit.fit() 获取正确尺寸
    const terminalView = document.getElementById('terminal-view');
    const terminalViewWasVisible = terminalView && terminalView.style.display !== 'none';
    if (terminalView && !terminalViewWasVisible) {
      terminalView.style.display = 'flex';
    }
    container.classList.add('active');

    // 打开终端
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

    // 🔧 修复：需求#418 - 计算准确的窗口大小
    fit.fit();
    const { rows, cols } = xterm;
    // 🔧 修复：需求#418/#8 - 恢复容器和terminal-view的原始状态
    container.classList.remove('active');
    if (terminalView && !terminalViewWasVisible) {
      terminalView.style.display = 'none';
    }

    // 连接WebSocket（根据existingSessionId决定是否连接现有会话）
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    let wsUrl = `${protocol}//${window.location.host}/ws/terminal`;
    // 🔧 获取当前选中的nest，用于远程终端
    // 🔧 Task#84：使用effectiveNest确定nest参数
    if (existingSessionId) {
      wsUrl += `?session_id=${existingSessionId}`;
      // 🔧 修复：需求#415 - 连接现有会话时也发送窗口大小
      wsUrl += `&cols=${cols}&rows=${rows}`;
    } else {
      // 🔧 修复：需求#414 - 创建新会话时也发送窗口大小
      wsUrl += `?cols=${cols}&rows=${rows}`;
    }
    // 🔧 添加nest参数（远程终端）
    // 🔧 Task#84：使用effectiveNest而非selectedNest，确保恢复会话时正确
    const nestForWs = effectiveNest !== 'local' ? effectiveNest : null;
    if (nestForWs) {
      wsUrl += `&nest=${encodeURIComponent(nestForWs)}`;
    }

    // 🔧 修复：需求#7 - 加载SerializeAddon，用于终端屏幕序列化和反序列化
    let serializeAddon = null;
    const SerializeAddonClass = window.SerializeAddon ? window.SerializeAddon.SerializeAddon : null;
    if (SerializeAddonClass) {
      try {
        serializeAddon = new SerializeAddonClass();
        xterm.loadAddon(serializeAddon);
      } catch (e) {
        console.warn('[createNewTerminal] SerializeAddon加载失败:', e.message);
        serializeAddon = null;
      }
    } else {
      console.warn('[createNewTerminal] SerializeAddon未找到，终端恢复功能将不可用');
    }

    // 🔧 修复：需求#7 - 定期序列化终端屏幕并同步给后端
    let screenSyncInterval = null;
    const SCREEN_SYNC_INTERVAL = 2000;  // 每2秒同步一次

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
      // 最后同步一次
      syncScreenToBackend();
    }

    // 🔧 使用带自动重连功能的WebSocket
    const ws = createWebSocketWithReconnect(
      wsUrl,
      xterm,
      tabId,
      (wsInstance) => {
        // 连接成功回调（首次连接）：更新tab对象中的ws引用
        const tab = state.terminalTabs.find(t => t.id === tabId);
        if (tab) {
          tab.ws = wsInstance;
          // 🔧 修复：需求#408 - 如果是连接现有会话，立即设置session_id
          // 如果是创建新会话，等待后端通过session_created消息发送
          if (existingSessionId) {
            tab.sessionId = existingSessionId;
          }
        }
        // 🔧 修复：需求#7 - 首次连接成功后启动屏幕同步
        startScreenSync();
      },
      () => {
        // onClose回调
        // 🔧 修复：需求#7 - 停止屏幕同步并最后同步一次
        stopScreenSync();
      },
      (wsInstance) => {
        // 🔧 修复：onReconnect回调（重连成功）：更新tab对象中的ws引用
        // 重连后屏幕恢复由后端发送screen_restore消息处理，不需要前端特殊处理
        const tab = state.terminalTabs.find(t => t.id === tabId);
        if (tab) {
          tab.ws = wsInstance;
        }
        // 🔧 修复：需求#7 - 重连后重新启动屏幕同步
        startScreenSync();
      }
    );

    // xterm输入事件 - 发送到WebSocket
    xterm.onData((data) => {
      // 🔧 修复：使用 tab.ws 而不是闭包中的 ws 变量
      // 这样可以确保总是使用最新的WebSocket实例（包括重连后的）
      const tab = state.terminalTabs.find(t => t.id === tabId);
      if (tab && tab.ws && tab.ws.readyState === WebSocket.OPEN) {
        tab.ws.send(data);
      }
    });

    // xterm大小变化事件 - 发送到后端调整pty大小
    xterm.onResize(({ cols, rows }) => {
      // 🔧 修复：使用 tab.ws 而不是闭包中的 ws 变量
      const tab = state.terminalTabs.find(t => t.id === tabId);
      if (tab && tab.ws && tab.ws.readyState === WebSocket.OPEN) {
        tab.ws.send(JSON.stringify({ type: 'resize', cols, rows }));
      }
    });

    // 🔧 修复：使用**双 requestAnimationFrame** 延迟 fit 调用
    // 嵌套 flex 布局可能需要多帧才能完成尺寸计算
    // 🔧 修复：只对激活的终端调用 fit.fit()，避免对隐藏的终端调用导致尺寸计算问题
    let resizeTimeout = null;
    const resizeObserver = new ResizeObserver((entries) => {
      // 检查 state.currentTerminalTabId 而不是依赖 active 类
      // 因为 ResizeObserver 可能在 active 类设置之前就触发
      if (state.currentTerminalTabId !== tabId) {
        return;
      }

      if (resizeTimeout) {
        clearTimeout(resizeTimeout);
      }
      // 防抖：延迟50ms再执行，避免频繁调用
      resizeTimeout = setTimeout(() => {
        if (fit && entries.length > 0) {
          requestAnimationFrame(() => {
            requestAnimationFrame(() => {
              try {
                if (fit) {
                  fit.fit();
                }
              } catch (error) {
                console.warn('[createNewTerminal] fit.fit() error:', error);
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
      sessionId: null, // 🔧 修复：需求#406 - 初始化为null，等待后端通过session_created消息发送
      resizeObserver: resizeObserver,
      resizeTimeout: resizeTimeout, // 保存timeout以便清理
      createdAt: new Date().toISOString(),
      // 🔧 修复：需求#7 - 保存serialize相关引用以便清理
      serializeAddon: serializeAddon,
      stopScreenSync: stopScreenSync,
      nest: effectiveNest || 'local',  // 🔧 Task#72/#84：记住创建时的nest来源
    };

    state.terminalTabs.push(newTab);
    state.currentTerminalTabId = tabId;

    // 切换到新标签页
    await switchTerminalTab(tabId);

    // 渲染标签页
    renderTerminalTabs();
  } catch (error) {
    console.error('[createNewTerminal] 创建终端失败:', error);
  }
}

async function switchTerminalTab(tabId) {
  const targetTab = state.terminalTabs.find(tab => tab.id === tabId);
  if (!targetTab) {
    console.error('[switchTerminalTab] 标签页不存在, tabId:', tabId);
    return;
  }

  // 隐藏所有终端容器
  state.terminalTabs.forEach(tab => {
    const container = document.getElementById(tab.id + '-container');
    if (container) {
      container.classList.remove('active');
    }
  });

  // 显示目标终端容器
  const targetContainer = document.getElementById(tabId + '-container');
  if (targetContainer) {
    targetContainer.classList.add('active');
  }

  // 更新当前标签页ID
  state.currentTerminalTabId = tabId;

  // 🔧 Task#84：切换终端标签页时，同步nest下拉框和工作目录
  if (targetTab.nest) {
    const nestName = targetTab.nest;
    _prepareNestState(nestName);
    _syncNestDropdown(nestName);
    // 同步工作目录
    const nests = window._latestNests || {};
    if (nestName === 'local') {
      // 本地模式：恢复默认工作目录
      const workdirInput = document.getElementById('workdir-input');
      if (workdirInput) {
        workdirInput.value = toNativeDisplayPath(state.projectRoot || '');
      }
      state.remoteNest = null;
      state.remoteNestRoot = null;
    } else if (nests[nestName]) {
      _updateWorkdirForNest(nestName, nests);
    }
  }

  // 🔧 修复：先清除所有终端的 resizeTimeout，避免防抖延迟干扰
  state.terminalTabs.forEach(tab => {
    if (tab.resizeTimeout) {
      clearTimeout(tab.resizeTimeout);
      tab.resizeTimeout = null;
    }
  });

  // 🔧 新功能：点击终端标签时，自动将输入焦点切换到该终端
  // 使用两次聚焦：第一次立即聚焦，第二次延迟20ms覆盖 initFocusManagement 的10ms延迟
  if (targetTab.xterm && typeof targetTab.xterm.focus === 'function') {
    try {
      // 立即聚焦
      targetTab.xterm.focus();
      // 延迟20ms再次聚焦，覆盖 initFocusManagement 的自动聚焦机制（10ms）
      setTimeout(() => {
        if (targetTab.xterm && typeof targetTab.xterm.focus === 'function') {
          targetTab.xterm.focus();
        }
      }, 20);
    } catch (error) {
      console.warn('[switchTerminalTab] 聚焦终端失败:', error);
    }
  }

  // 渲染标签页
  renderTerminalTabs();
}

async function closeTerminalTab(tabId) {
  const tabIndex = state.terminalTabs.findIndex(tab => tab.id === tabId);
  if (tabIndex === -1) {
    console.error('[closeTerminalTab] 标签页不存在, tabId:', tabId);
    return;
  }

  const tab = state.terminalTabs[tabIndex];

  // 🔧 Task#185：发送 close 消息给后端，后端关闭PTY后广播terminal_closed事件
  // 所有webui实例（包括本实例）通过handleTerminalEvent收到terminal_closed后执行UI清理
  if (tab.ws && tab.ws.readyState === WebSocket.OPEN) {
    try {
      tab.ws.send(JSON.stringify({ type: 'close' }));
    } catch (error) {
      console.warn('[closeTerminalTab] 发送close消息失败:', error);
      // WebSocket发送失败，直接本地移除（后端可能已不可达）
      removeTerminalTabUI(tabId);
    }
  } else {
    // WebSocket未连接，直接本地移除
    removeTerminalTabUI(tabId);
  }
}

function renderTerminalTabs() {
  const tabsContainer = document.getElementById('terminal-tabs');
  if (!tabsContainer) {
    console.warn('[renderTerminalTabs] terminal-tabs 元素未找到');
    return;
  }

  // 清空现有标签页
  tabsContainer.innerHTML = '';

  // 渲染每个标签页
  state.terminalTabs.forEach(tab => {
    const tabEl = document.createElement('div');
    tabEl.className = 'terminal-tab' + (tab.id === state.currentTerminalTabId ? ' active' : '');
    tabEl.dataset.tabId = tab.id;

    // 标签页标题
    const titleEl = document.createElement('span');
    titleEl.className = 'terminal-tab-title';
    titleEl.textContent = tab.title;
    tabEl.appendChild(titleEl);

    // 关闭按钮
    const closeBtn = document.createElement('span');
    closeBtn.className = 'terminal-tab-close';
    closeBtn.textContent = '×';
    closeBtn.title = '关闭终端';
    closeBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      closeTerminalTab(tab.id);
    });
    tabEl.appendChild(closeBtn);

    // 点击标签页切换
    tabEl.addEventListener('click', () => {
      switchTerminalTab(tab.id);
    });

    tabsContainer.appendChild(tabEl);
  });

  // 新建终端按钮
  const newTerminalBtn = document.createElement('button');
  newTerminalBtn.className = 'terminal-tab-new';
  newTerminalBtn.textContent = '+';
  newTerminalBtn.title = '新建终端';
  newTerminalBtn.addEventListener('click', () => {
    requestCreateTerminal();
  });
  tabsContainer.appendChild(newTerminalBtn);
}

