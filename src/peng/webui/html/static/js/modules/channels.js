// ===== Channel 配置模态框 =====

/**
 * 初始化 Channel 配置模态框事件
 */
function initChannelModals() {
  // 微信配置按钮
  const wechatBtn = document.getElementById('wechat-config-btn');
  if (wechatBtn) {
    wechatBtn.addEventListener('click', showWechatConfigModal);
  }

  // 飞书配置按钮
  const feishuBtn = document.getElementById('feishu-config-btn');
  if (feishuBtn) {
    feishuBtn.addEventListener('click', showFeishuConfigModal);
  }

  // 微信关闭按钮
  const wcClose = document.getElementById('wechat-config-close');
  const wcCloseBtn = document.getElementById('wechat-config-close-btn');
  if (wcClose) wcClose.addEventListener('click', () => hideModal('wechat-config-modal'));
  if (wcCloseBtn) wcCloseBtn.addEventListener('click', () => hideModal('wechat-config-modal'));

  // 飞书模态框关闭
  const fsClose = document.getElementById('feishu-config-close');
  const fsCloseBtn = document.getElementById('feishu-config-close-btn');
  if (fsClose) fsClose.addEventListener('click', () => hideModal('feishu-config-modal'));
  if (fsCloseBtn) fsCloseBtn.addEventListener('click', () => hideModal('feishu-config-modal'));

  // 模态框背景点击关闭
  ['wechat-config-modal', 'feishu-config-modal'].forEach(id => {
    const m = document.getElementById(id);
    if (m) m.addEventListener('click', (e) => { if (e.target === m) hideModal(id); });
  });

  // ClawBot 扫码登录按钮
  const clawbotLogin = document.getElementById('wc-clawbot-login-btn');
  if (clawbotLogin) clawbotLogin.addEventListener('click', clawbotLoginStart);

  // 飞书连接按钮
  const fsConnect = document.getElementById('fs-connect-btn');
  if (fsConnect) fsConnect.addEventListener('click', connectFeishu);

  // 飞书断开按钮
  const fsDisconnect = document.getElementById('fs-disconnect-btn');
  if (fsDisconnect) fsDisconnect.addEventListener('click', disconnectChannel('feishu'));

  // 加载初始状态
  loadChannelStatus();
  // 定期刷新状态
  setInterval(loadChannelStatus, 10000);
}

/**
 * 显示模态框
 */
function showModal(id) {
  const m = document.getElementById(id);
  if (m) m.style.display = 'flex';
}

/**
 * 隐藏模态框
 */
function hideModal(id) {
  const m = document.getElementById(id);
  if (m) m.style.display = 'none';
}

/**
 * 显示微信配置模态框
 */
async function showWechatConfigModal() {
  showModal('wechat-config-modal');
  try {
    const resp = await fetch('/api/channels/config');
    const config = await resp.json();
    const wc = config.wechat || {};
    if (wc.clawbot && wc.clawbot.has_bot_token) {
      document.getElementById('wechat-status-text').textContent = 'ClawBot 已连接';
      document.getElementById('wechat-status-dot').className = 'status-dot connected';
    }
  } catch (e) {
    console.warn('加载微信配置失败:', e);
  }
}

/**
 * 显示飞书配置模态框
 */
async function showFeishuConfigModal() {
  showModal('feishu-config-modal');
  // 加载当前配置
  try {
    const resp = await fetch('/api/channels/config');
    const config = await resp.json();
    const fs = config.feishu || {};
    const appId = document.getElementById('fs-app-id');
    const secret = document.getElementById('fs-app-secret');
    const encryptKey = document.getElementById('fs-encrypt-key');
    if (appId) appId.value = fs.app_id || '';
    if (secret) secret.value = fs.app_secret || '';
    if (encryptKey) encryptKey.value = fs.encrypt_key || '';
  } catch (e) {
    console.warn('加载飞书配置失败:', e);
  }
}

/**
 * ClawBot 扫码登录
 */
async function clawbotLoginStart() {
  const btn = document.getElementById('wc-clawbot-login-btn');
  if (btn) { btn.disabled = true; btn.textContent = '获取二维码中...'; }
  try {
    // 获取二维码（不需要先保存配置，扫码成功后在 poll-login 中保存）
    const resp = await fetch('/api/channels/clawbot/qrcode');
    const result = await resp.json();
    if (result.success && result.qr_url) {
      const qrContainer = document.getElementById('wechat-qr-container');
      const qrSection = document.getElementById('wechat-qr-section');
      // 🔧 性能优化：QRCode.js 按需懒加载
      if (qrContainer) {
        qrContainer.innerHTML = '';
        try {
          await ensureQRCode();
          new QRCode(qrContainer, { text: result.qr_url, width: 200, height: 200 });
        } catch (e) {
          console.warn('QRCode加载失败，显示文本链接:', e);
          qrContainer.innerHTML = '<div style="padding:10px;text-align:center;word-break:break-all;">' + escapeHtml(result.qr_url) + '</div>';
        }
      }
      if (qrSection) qrSection.style.display = 'block';
      // 开始轮询扫码状态
      clawbotPollLogin(result.uuid);
    } else {
      showToast('获取二维码失败: ' + (result.error || '未知错误'));
    }
  } catch (e) {
    console.error('ClawBot登录失败:', e);
    showToast('ClawBot登录失败');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '扫码登录'; }
  }
}

/**
 * 轮询 ClawBot 扫码状态
 * @param {string} uuid - 二维码唯一标识
 * @param {string} redirectHost - IDC 重定向后的新 host（由 poll-login 返回的 redirect_host 传递）
 */
function clawbotPollLogin(uuid, redirectHost) {
  const statusEl = document.getElementById('wechat-qr-status');
  const maxPoll = 60; // 最多轮询60次
  let pollCount = 0;
  // 保存当前的 redirect_host，IDC 重定向时会更新
  let currentRedirectHost = redirectHost || '';

  async function doPoll() {
    pollCount++;
    if (pollCount > maxPoll) {
      if (statusEl) statusEl.textContent = '扫码超时，请重试';
      return;
    }
    try {
      // iLink get_qrcode_status 是长轮询（约35秒），等待返回后再发下一次
      let pollUrl = '/api/channels/clawbot/poll-login?uuid=' + encodeURIComponent(uuid);
      // 如果有 IDC 重定向 host，传递给后端（参考 OpenClaw login-qr.ts currentApiBaseUrl）
      if (currentRedirectHost) {
        pollUrl += '&redirect_host=' + encodeURIComponent(currentRedirectHost);
      }
      const resp = await fetch(pollUrl);
      const result = await resp.json();
      if (result.status === 'confirmed') {
        if (statusEl) statusEl.textContent = '✅ 登录成功！';
        showToast('微信 ClawBot 登录成功！');
        const statusText = document.getElementById('wechat-status-text');
        const statusDot = document.getElementById('wechat-status-dot');
        if (statusText) statusText.textContent = 'ClawBot 已连接';
        if (statusDot) statusDot.className = 'status-dot connected';
        setTimeout(() => hideModal('wechat-config-modal'), 1500);
      } else if (result.status === 'scanned') {
        if (statusEl) statusEl.textContent = '已扫码，请在手机上确认...';
        // IDC 重定向：更新后续轮询的 host（参考 OpenClaw login-qr.ts 第391-401行）
        if (result.redirect_host) {
          currentRedirectHost = result.redirect_host;
          console.log('ClawBot IDC 重定向，切换轮询 host:', currentRedirectHost);
        }
        setTimeout(doPoll, 500); // 快速继续轮询
      } else if (result.status === 'timeout') {
        if (statusEl) statusEl.textContent = '二维码已过期，请重新获取';
      } else {
        if (statusEl) statusEl.textContent = '等待扫码... (' + pollCount + ')';
        setTimeout(doPoll, 1000); // 1秒后继续轮询
      }
    } catch (e) {
      console.warn('轮询扫码状态失败:', e);
      // 网络错误后延迟重试
      setTimeout(doPoll, 2000);
    }
  }

  if (statusEl) statusEl.textContent = '等待扫码...';
  doPoll();
}

/**
 * 显示微信二维码
 */
function showWechatQRCode(qrUrl) {
  const section = document.getElementById('wechat-qr-section');
  if (section) section.style.display = 'block';

  // 使用QRCode库或简单显示链接
  const canvas = document.getElementById('wechat-qr-canvas');
  if (canvas) {
    const ctx = canvas.getContext('2d');
    ctx.clearRect(0, 0, 200, 200);
    ctx.fillStyle = '#ffffff';
    ctx.fillRect(0, 0, 200, 200);
    // 简单绘制二维码URL文本（实际应使用qrcode库）
    ctx.fillStyle = '#333333';
    ctx.font = '11px monospace';
    ctx.textAlign = 'center';
    // 显示提示
    ctx.fillText('微信扫码登录', 100, 90);
    ctx.font = '10px monospace';
    // 截断URL显示
    const displayUrl = qrUrl.length > 30 ? qrUrl.substring(0, 30) + '...' : qrUrl;
    ctx.fillText(displayUrl, 100, 110);

    // 🔧 性能优化：QRCode.js 按需懒加载
    ensureQRCode().then(() => {
      new QRCode(canvas, { text: qrUrl, width: 200, height: 200 });
    }).catch(e => {
      console.warn('QRCode加载失败:', e);
    });
  }
}

/**
 * 连接飞书
 */
async function connectFeishu() {
  const appId = document.getElementById('fs-app-id')?.value || '';
  const appSecret = document.getElementById('fs-app-secret')?.value || '';
  const encryptKey = document.getElementById('fs-encrypt-key')?.value || '';

  if (!appId || !appSecret) {
    showToast('请填写 App ID 和 App Secret');
    return;
  }

  // app_secret 显示的是脱敏值 "***"，如果用户没有修改，原样传回（后端会识别并保留原值）
  updateChannelStatus('feishu', 'connecting');

  try {
    const resp = await fetch('/api/channels/config/feishu', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        enabled: true,
        app_id: appId,
        app_secret: appSecret,
        encrypt_key: encryptKey,
      }),
    });
    const result = await resp.json();
    if (result.success) {
      showToast('飞书配置已保存，正在连接...');
      setTimeout(loadChannelStatus, 2000);
    } else {
      showToast('飞书配置失败: ' + (result.error || '未知错误'));
      updateChannelStatus('feishu', 'error');
    }
  } catch (e) {
    console.error('连接飞书失败:', e);
    showToast('连接飞书失败');
    updateChannelStatus('feishu', 'error');
  }
}

/**
 * 断开通道
 */
function disconnectChannel(channelType) {
  return async function () {
    try {
      const resp = await fetch(`/api/channels/config/${channelType}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled: false }),
      });
      const result = await resp.json();
      if (result.success) {
        showToast(`${channelType === 'wechat' ? '微信' : '飞书'}已断开`);
        loadChannelStatus();
      }
    } catch (e) {
      console.error('断开通道失败:', e);
    }
  };
}

/**
 * 加载通道状态
 */
async function loadChannelStatus() {
  try {
    const resp = await fetch('/api/channels/status');
    const status = await resp.json();
    const channels = status.channels || {};

    // 更新微信状态
    const wechat = channels.wechat || {};
    updateChannelStatus('wechat', wechat.status || 'disconnected');
    updateChannelIcon('wechat-config-btn', wechat.status === 'connected');

    // 更新飞书状态
    const feishu = channels.feishu || {};
    updateChannelStatus('feishu', feishu.status || 'disconnected');
    updateChannelIcon('feishu-config-btn', feishu.status === 'connected');
  } catch (e) {
    // 静默失败
  }
}

/**
 * 更新通道状态显示
 */
function updateChannelStatus(channelType, status) {
  const dot = document.getElementById(`${channelType}-status-dot`);
  const text = document.getElementById(`${channelType}-status-text`);
  const connectBtn = document.getElementById(`${channelType === 'wechat' ? 'wc' : 'fs'}-connect-btn`);
  const disconnectBtn = document.getElementById(`${channelType === 'wechat' ? 'wc' : 'fs'}-disconnect-btn`);

  if (dot) {
    dot.className = 'status-dot ' + status;
  }

  const statusTexts = {
    connected: '已连接',
    connecting: '连接中...',
    disconnected: '未连接',
    error: '连接失败',
  };
  if (text) text.textContent = statusTexts[status] || status;

  if (connectBtn) connectBtn.style.display = status === 'connected' ? 'none' : 'block';
  if (disconnectBtn) disconnectBtn.style.display = status === 'connected' ? 'block' : 'none';
}

/**
 * 更新通道图标连接状态
 */
function updateChannelIcon(btnId, connected) {
  const btn = document.getElementById(btnId);
  if (btn) {
    if (connected) {
      btn.classList.add('connected');
    } else {
      btn.classList.remove('connected');
    }
    // 飞书图标用img切换彩色/灰度
    const img = btn.querySelector('.feishu-icon');
    if (img) {
      img.src = connected ? img.dataset.colorSrc : img.dataset.graySrc;
    }
  }
}

