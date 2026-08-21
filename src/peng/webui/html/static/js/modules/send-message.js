// ==================== 发送消息 ====================

// 内置命令处理
function processBuiltinCommand(content) {
  const trimmed = content.trim();

  // /new 或 /clear - 开始新消息链
  if (trimmed === '/new') {
    state.currentMessageId = null;
    state.currentTaskId = null;
    state.currentBranchMessageId = null;  // 清除分支消息选中状态
    state.isCreatingNewMessage = true;  // 标记为新建消息状态
    state.isReplyingMessage = false;  // 清除回复标志
    document.querySelectorAll('.message-chain-head').forEach(el => {
      el.classList.remove('active');
    });
    document.querySelectorAll('.message-tree-node').forEach(el => {
      el.classList.remove('active');  // 清除分支消息的视觉选中状态
    });
    document.querySelectorAll('.task-chain').forEach(el => {
      el.classList.remove('active');
    });
    updateInputPlaceholder(null);  // 更新输入框提示
    // 清空终端
    if (state.output) {
      state.output.clear(); state.currentColor = '';
    }
    showSimpleToast('📜  开始新话题', 'info');
    return { handled: true, clear: true };
  }

  // /reply id 消息 - 回复指定消息链
  const replyMatch = trimmed.match(/^\/reply\s+(\d+)\s*(.*)$/);
  if (replyMatch) {
    const msgId = parseInt(replyMatch[1]);
    const msgContent = replyMatch[2];
    state.currentMessageId = msgId;
    // 更新选中状态
    document.querySelectorAll('.message-chain-head').forEach(el => {
      el.classList.toggle('active', el.dataset.id == msgId);
    });
    updateOutputTitle(null, msgId);
    updateInputPlaceholder(msgId);  // 更新输入框提示
    showSimpleToast(`📜  回复话题 #${msgId}`, 'info');
    if (msgContent) {
      return { handled: false, content: msgContent, replyTo: msgId };
    }
    return { handled: true, clear: true };
  }

  // /msg ... - 调用消息管理
  if (trimmed.startsWith('/msg ') || trimmed === '/msg') {
    const args = trimmed.substring(5).trim();
    return { handled: true, command: 'msg', args };
  }

  // /task ... - 调用事务管理
  if (trimmed.startsWith('/task ') || trimmed === '/task') {
    const args = trimmed.substring(6).trim();
    return { handled: true, command: 'task', args };
  }

  // /clear - 清空output
  if (trimmed === '/clear') {
    // 清空终端
    if (state.output) {
      state.output.clear(); state.currentColor = '';
    }
    showSimpleToast('📜  事务输出已清空', 'info');
    const args = `output ${state.currentTaskId} --delete`;
    return { handled: true, command: 'task', args };
  }

  // /reset-webui - 全面清理浏览器数据并刷新页面（替代/clear-cookies）
  if (trimmed === '/reset-webui') {
    return { handled: true, command: 'reset-webui' };
  }

  if (trimmed.startsWith('/go ')) {
    const args = `info ${state.currentTaskId} ${trimmed.substring(4).trim()}`;
    return { handled: true, command: 'task', args };
  }

  if (trimmed === '/go') {
    const args = `resume ${state.currentTaskId}`;
    return { handled: true, command: 'task', args };
  }

  if (trimmed === '/restart') {
    const args = `restart ${state.currentTaskId}`;
    return { handled: true, command: 'task', args };
  }

  // /continue - 继续最近的消息
  if (trimmed === '/continue') {
    return { handled: true, command: 'continue' };
  }

  // /help - 显示帮助
  if (trimmed === '/help') {
    return { handled: true, command: 'help' };
  }

  // /model - 显示/切换模型
  if (trimmed === '/model' || trimmed.startsWith('/model ')) {
    return { handled: true, command: 'model', args: trimmed };
  }

  // /cd <dir> - 切换工作目录
  if (trimmed.startsWith('/cd ') || trimmed === '/cd') {
    const args = trimmed.substring(4).trim();
    return { handled: true, command: 'cd', args };
  }

  // /cmd <command> [args...] - 调用任意命令行命令
  if (trimmed.startsWith('/c ')) {
    const args = trimmed.substring(3).trim();
    return { handled: true, command: 'cmd', args };
  }

  // /cmd <command> [args...] - 调用任意命令行命令
  if (trimmed.startsWith('/cmd ')) {
    const args = trimmed.substring(5).trim();
    return { handled: true, command: 'cmd', args };
  }

  // /clear-all-data [--force] - 清空所有数据
  if (trimmed.startsWith('/clear-all-data')) {
    const args = trimmed.substring(15).trim();
    return { handled: true, command: 'clear-all-data', args };
  }

  // /clear-cookies 已被 /reset-webui 替代（兼容旧命令）
  if (trimmed === '/clear-cookies' || trimmed === '/reset-webui') {
    return { handled: true, command: 'reset-webui' };
  }

  return { handled: false };
}

// 清除所有peng相关的localStorage数据并刷新页面
async function clearLocalStorageData() {
  const keysToRemove = [];
  for (let i = 0; i < localStorage.length; i++) {
    const key = localStorage.key(i);
    if (key && (key.startsWith('peng-') || key.startsWith('peng_'))) {
      keysToRemove.push(key);
    }
  }
  const count = keysToRemove.length;
  for (const key of keysToRemove) {
    localStorage.removeItem(key);
  }
  showSimpleToast(`✅ 已清除 ${count} 项localStorage数据，即将刷新页面...`, 'success');
  // 延迟刷新，让用户看到提示
  setTimeout(() => {
    window.location.reload();
  }, 1000);
}

// 全面重置WebUI：清理所有浏览器端数据和服务端编辑器临时数据，然后强制刷新
async function resetWebUI() {
  let clearedCount = 0;

  // 0. 先清空内存中的编辑器状态，防止后续saveEditorTabs被触发重新写入localStorage
  if (typeof state !== 'undefined') {
    // 🔧 Task#340：清空group tabs（唯一数据源）
    if (state.editorGroups && state.editorGroups.length > 0) {
      state.editorGroups[0].tabs = [];
    }
    // 🔧 Task#334：state.currentTabId 已删除，不再需要清空
    state.editorGroups = null;
    state._lastActiveGroupId = null;
    state.currentViewTab = null;
  }

  // 1. 清除服务端编辑器自动保存的临时文件
  try {
    const resp = await fetch('/api/editor/clear-all-temp', { method: 'POST' });
    const data = await resp.json();
    if (data.success) {
      clearedCount += data.deleted_count || 0;
    }
  } catch (e) { console.warn('服务端编辑器临时文件清理失败:', e); }

  // 2. 清理 localStorage（所有key，不区分前缀）
  try {
    const lsKeys = [];
    for (let i = 0; i < localStorage.length; i++) {
      const key = localStorage.key(i);
      if (key) lsKeys.push(key);
    }
    localStorage.clear();
    clearedCount += lsKeys.length;
  } catch (e) { console.warn('localStorage清理失败:', e); }

  // 3. 清理 sessionStorage
  try {
    sessionStorage.clear();
    clearedCount += 1;
  } catch (e) { console.warn('sessionStorage清理失败:', e); }

  // 4. 清理 Cookies
  try {
    document.cookie.split(';').forEach(c => {
      const name = c.split('=')[0].trim();
      if (name) {
        document.cookie = `${name}=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/`;
      }
    });
    clearedCount += 1;
  } catch (e) { console.warn('Cookies清理失败:', e); }

  // 5. 清理 IndexedDB
  try {
    const dbs = await indexedDB.databases();
    for (const db of dbs) {
      if (db.name) {
        indexedDB.deleteDatabase(db.name);
        clearedCount += 1;
      }
    }
  } catch (e) { console.warn('IndexedDB清理失败:', e); }

  // 6. 清理 Cache Storage
  try {
    const cacheNames = await caches.keys();
    for (const name of cacheNames) {
      await caches.delete(name);
      clearedCount += 1;
    }
  } catch (e) { console.warn('Cache Storage清理失败:', e); }

  // 7. 注销所有 Service Workers
  try {
    if (navigator.serviceWorker) {
      const regs = await navigator.serviceWorker.getRegistrations();
      for (const reg of regs) {
        await reg.unregister();
        clearedCount += 1;
      }
    }
  } catch (e) { console.warn('ServiceWorker清理失败:', e); }

  showSimpleToast(`🔄 WebUI已重置（清理${clearedCount}项），即将刷新页面...`, 'success');
  // 🔧 Task#327修复：强制刷新所有JS模块缓存
  setTimeout(async () => {
    try { sessionStorage.setItem('__peng_force_reload', Date.now().toString()); } catch(e) {}
    // 清除所有HTTP缓存（Cache API）
    if ('caches' in window) {
      try {
        const names = await caches.keys();
        for (const name of names) await caches.delete(name);
      } catch(e) {}
    }
    window.location.replace(window.location.pathname + '?_t=' + Date.now());
  }, 800);
}

// 执行内置命令
async function executeBuiltinCommand(cmd) {
  switch (cmd.command) {
    case 'msg':
      // 调用后端 API 执行 msg 命令
      await executeCommandInOutput('/msg ' + (cmd.args || ''));
      break;
    case 'task':
      // 调用后端 API 执行 task 命令
      await executeCommandInOutput('/task ' + (cmd.args || ''));
      break;
    case 'continue':
      // 查找最近的未完成消息
      if (state.messages.length > 0) {
        const recent = state.messages[0];  // 最新的消息
        if (recent.status === 'processing' || recent.status === 'interrupted') {
          state.currentMessageId = recent.id;
          showSimpleToast(`📜  继续话题 #${recent.id}`, 'info');
        } else {
          showSimpleToast('📜  没有需要继续的话题', 'warning');
        }
      }
      break;
    case 'help':
      showHelp();
      break;
    case 'model':
      await handleModelCommand(cmd.args);
      break;
    case 'cd': {
      // 调用后端 API 执行 cd 命令，成功后同步前端工作目录输入框
      try {
        const response = await fetch('/api/command', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ command: '/cd ' + (cmd.args || '') })
        });
        const data = await response.json();
        if (state.output && data.output) {
          state.output.write('\r\n');
          state.output.write(convertFilePathsToLinks(data.output.replace(/\\n/g, '\r\n')));
          state.output.write('\r\n');
        }
        // cd 成功时，用后端返回的绝对路径同步前端工作目录输入框和状态
        if (data.success && data.path) {
          const workdirInput = document.getElementById('workdir-input');
          state.workdir = data.path;
          if (workdirInput) {
            workdirInput.value = toNativeDisplayPath(data.path);
            workdirInput.classList.remove('invalid');
          }
          // 同时调用 set-workdir 持久化到后端配置
          try {
            await fetch(`/api/set-workdir?path=${encodeURIComponent(data.path)}`, { method: 'POST' });
          } catch (e) {
            console.warn('持久化工作目录失败:', e);
          }
        }
      } catch (error) {
        showError('执行命令失败', error);
      }
      break;
    }
    case 'cmd':
      await executeCommandInOutput('/cmd ' + (cmd.args || ''));
      break;
    case 'clear-all-data':
      clearOutput();
      state.outputHistory = new Map();
      await executeCommandInOutput('/clear-all-data ' + (cmd.args || ''));
      break;
    case 'reset-webui':
      await resetWebUI();
      break;
  }
}

// 处理 /model 命令（调用后端 API)
async function handleModelCommand(args) {
  try {
    const response = await fetch('/api/command', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ command: args })
    });

    const data = await response.json();

    if (state.output && data.output) {
      state.output.write('\r\n');
      // 处理 ANSI 颜色代码，并转换文件路径为OSC8超链接格式
      state.output.write(convertFilePathsToLinks(data.output.replace(/\\n/g, '\r\n')));
      state.output.write('\r\n');
    }
  } catch (error) {
    showError('执行命令失败', error);
  }
}

// 在终端中执行命令并显示输出
async function executeCommandInOutput(command) {
  try {
    const response = await fetch('/api/command', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ command: command })
    });

    const data = await response.json();

    if (state.output && data.output) {
      state.output.write('\r\n');
      // 处理 ANSI 颜色代码，并转换文件路径为OSC8超链接格式
      state.output.write(convertFilePathsToLinks(data.output.replace(/\\n/g, '\r\n')));
      state.output.write('\r\n');
    }
  } catch (error) {
    showError('执行命令失败', error);
  }
}

// 显示消息详情（弹出窗口，复用 /msg show 的 rich 格式输出）
// 🔧 Task#1011：消息详情也先弹窗，异步加载完整内容
async function showMessageDetails(msgId) {
  // 从 state.messages 快速获取基本信息，立即弹窗
  const msg = findMessageByIdRecursive(state.messages, msgId);
  let placeholderText = '';
  if (msg) {
    const sender = msg.sender || '';
    const timeStr = (msg.created_at || msg.timestamp || '').substring(0, 19).replace('T', ' ');
    const content = msg.content || '';
    placeholderText = `${sender} ${timeStr} #${msgId}\n\n${content}\n\n\x1b[90m正在加载完整详情...\x1b[0m`;
  } else {
    placeholderText = `\x1b[90m正在加载消息 #${msgId} 详情...\x1b[0m`;
  }

  // 立即弹出窗口
  showDetailModal('📜 消息详情', placeholderText);

  // 标记当前弹窗对应的消息ID
  const modal0 = document.getElementById('detail-modal');
  if (modal0) modal0.dataset.detailMsgId = msgId;

  // 异步加载完整详情
  try {
    const response = await fetch('/api/command', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ command: `/msg show ${msgId}` })
    });
    const data = await response.json();

    // 检查弹窗是否仍然存在且仍是当前消息
    const modal = document.getElementById('detail-modal');
    if (!modal || modal.dataset.detailMsgId != msgId) return;

    if (data.output) {
      const preEl = modal.querySelector('.detail-content');
      if (preEl) {
        preEl.innerHTML = ansiToHtml(data.output);
        applyInheritFontSize(modal);
      }
    }
  } catch (error) {
    console.error('获取消息详情失败:', error);
    showSimpleToast('获取消息详情失败', 'error');
  }
}

// 显示事务详情（弹出窗口，使用 HTML 格式输出，支持表格等复杂信息）
// 🔧 Task#1011：先弹窗口（含基本信息），异步加载完整详情
async function showTaskDetails(taskId) {
  // 从 state.tasks 快速获取基本信息，立即弹窗
  const allTasks = flattenTasks(state.tasks || []);
  const task = allTasks.find(t => t.id === taskId);

  // 构建基本信息占位 HTML（秒级弹出）
  let placeholderHtml = '';
  if (task) {
    const statusNames = {"pending": "待执行", "running": "执行中", "completed": "已完成",
      "failed": "已失败", "cancelled": "已取消", "paused": "已暂停", "interrupted": "已中断"};
    const statusName = statusNames[task.status] || task.status || '';
    const content = escapeHtml(task.content || '');
    const timeStr = (task.created_at || task.timestamp || '').substring(0, 19).replace('T', ' ');
    const modelStr = task.small_model ? '小模型' : '大模型';
    const nestStr = task.nest ? escapeHtml(task.nest) : '';
    const workdirStr = task.workdir ? escapeHtml(task.workdir) : '';
    placeholderHtml = `<div class="task-detail-html">
      <div class="task-title"><span style="font-size:1.5em;color:#f0883e;font-weight:bold;">🚩${taskId}</span></div>
      ${content ? `<div class="task-content">${convertFilePathsToHtmlLinks(content)}</div>` : ''}
      <div class="task-section">
        <h3>📋 基本信息</h3>
        <table class="kv-table"><tbody>
          <tr><th>状态</th><td><span class="task-status status-${task.status}">${statusName}</span></td></tr>
          <tr><th>创建时间</th><td>${timeStr}</td></tr>
          <tr><th>模型</th><td>${modelStr}</td></tr>
          ${nestStr ? `<tr><th>鹏🐣窝</th><td>${nestStr}</td></tr>` : ''}
          ${workdirStr ? `<tr><th>工作目录</th><td>${workdirStr}</td></tr>` : ''}
        </tbody></table>
      </div>
      <div class="task-section" style="text-align:center;padding:24px;color:#8b949e;">
        <span class="detail-loading-spinner" style="display:inline-block;width:24px;height:24px;border:3px solid #30363d;border-top-color:#58a6ff;border-radius:50%;animation:detail-spin 0.8s linear infinite;vertical-align:middle;margin-right:8px;"></span>
        正在加载完整详情...
      </div>
    </div>`;
  } else {
    placeholderHtml = `<div class="task-detail-html">
      <div style="text-align:center;padding:48px;color:#8b949e;">
        <span class="detail-loading-spinner" style="display:inline-block;width:24px;height:24px;border:3px solid #30363d;border-top-color:#58a6ff;border-radius:50%;animation:detail-spin 0.8s linear infinite;vertical-align:middle;margin-right:8px;"></span>
        正在加载事务 #${taskId} 详情...
      </div>
    </div>`;
  }

  // 立即弹出窗口（含 loading 占位内容）
  showDetailModalHtml('🚩 事务详情', placeholderHtml, taskId);

  // 标记当前弹窗对应的 taskId（用于异步结果竞态保护）
  const modal0 = document.getElementById('detail-modal');
  if (modal0) modal0.dataset.detailTaskId = taskId;

  // 异步加载完整详情
  try {
    const response = await fetch('/api/command', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ command: `/task show ${taskId} --format html` })
    });
    const data = await response.json();

    // 检查弹窗是否仍然存在且仍是当前事务（用户可能已关闭或切换到其他事务）
    const modal = document.getElementById('detail-modal');
    if (!modal || modal.dataset.detailTaskId != taskId) return;  // 弹窗已关闭或已切换，丢弃结果

    if (data.output) {
      // 替换内容并重新绑定文件链接事件
      const contentEl = modal.querySelector('.detail-content-html');
      if (contentEl) {
        contentEl.innerHTML = data.output;
        _bindFileLinksInModal(contentEl, taskId);
        // 🔧 Task#1011：异步替换内容后必须重新应用字体配置
        applyInheritFontSize(modal);
      }
    }
  } catch (error) {
    console.error('获取事务详情失败:', error);
    // 在弹窗中显示错误信息
    const modal = document.getElementById('detail-modal');
    if (modal && modal.dataset.detailTaskId == taskId) {
      const contentEl = modal.querySelector('.detail-content-html');
      if (contentEl) {
        contentEl.innerHTML = `<div class="task-detail-html"><div style="color:#f85149;padding:24px;text-align:center;">⚠️ 获取事务详情失败: ${escapeHtml(error.message)}</div></div>`;
      }
    } else {
      showSimpleToast('获取事务详情失败', 'error');
    }
  }
}

/**
 * 🔧 Task#1011：为模态框内的文件链接绑定点击事件（提取为独立函数，支持动态更新内容后重新绑定）
 * @param {HTMLElement} contentEl - 内容容器元素
 * @param {number} taskId - 事务ID，用于获取 workdir 和 nest 信息
 */
function _bindFileLinksInModal(contentEl, taskId) {
  const fileLinks = contentEl.querySelectorAll('.file-link');
  fileLinks.forEach(link => {
    link.addEventListener('click', (e) => {
      e.preventDefault();
      const href = link.getAttribute('href');
      if (href && href.startsWith('editor://')) {
        // 解析 editor://path?nest=xxx 格式
        const fullUri = decodeURIComponent(href.replace('editor://', ''));
        let filePath = fullUri;
        let nestFromHref = null;

        // 提取 ?nest= 参数
        const nestMatch = fullUri.match(/^(.+?)\?nest=(.+)$/);
        if (nestMatch) {
          filePath = nestMatch[1];
          nestFromHref = nestMatch[2];
        }

        // 🔧 Task#872：结合事务的 workdir 组合相对路径为绝对路径
        if (taskId && state.tasks) {
          const allTasks = flattenTasks(state.tasks || []);
          const task = allTasks.find(t => t.id === taskId);
          if (task && task.workdir && filePath.indexOf('/') !== 0 && !filePath.match(/^[a-zA-Z]:/)) {
            filePath = task.workdir.replace(/\/+$/, '') + '/' + filePath;
          }
        }

        // nest 优先级：href 中的 > state.tasks 中的
        let foundNest = nestFromHref;
        if (!foundNest && taskId && state.tasks) {
          const allTasks = flattenTasks(state.tasks || []);
          const task = allTasks.find(t => t.id === taskId);
          if (task && task.nest) {
            foundNest = task.nest;
          }
        }

        // 🔧 使用 foundNest 直接检查文件并打开（不再调 checkFileExistsWithLocalFallback，因为它用全局选中nest）
        const isImage = isImageFile(filePath);
        const nestParam = foundNest ? `&nest=${encodeURIComponent(foundNest)}` : getNestParam();
        fetch(`/api/files/exists?path=${encodeURIComponent(filePath)}${nestParam}`).then(r => r.json()).then(result => {
          if (result.success && result.is_dir) {
            // 📁 目录：用文件浏览器打开
            closeDetailModal();
            openPathInFileBrowser(filePath, foundNest);
          } else if (result.success && result.exists) {
            if (isImage) {
              openImageViewer(filePath, false, foundNest);
            } else {
              openEditorForFile(filePath, false, foundNest);
            }
            showSimpleToast('已在编辑器中打开 ' + filePath.split('/').pop().split('\\').pop(), 'success');
          } else {
            showSimpleToast('⚠️ 文件不存在: ' + filePath, 'warning');
          }
        }).catch(error => {
          console.warn('[showDetailModalHtml] 检查文件存在性失败:', error);
          openEditorForFile(filePath, false, foundNest);
        });

      } else if (href && (href.startsWith('http://') || href.startsWith('https://'))) {
        window.open(href, '_blank');
      }
    });
  });
}

/**
 * 显示详情弹出窗口（HTML 内容模式，支持表格自适应）
 * @param {string} title - 窗口标题
 * @param {string} htmlContent - 语义化 HTML 内容
 * @param {number} [taskId] - 可选的事务ID，用于获取 workdir 和 nest 信息
 */
function showDetailModalHtml(title, htmlContent, taskId, autoFit) {
  // 移除已存在的模态框（含清理键盘 handler）
  closeDetailModal();

  // 创建模态框
  const modal = document.createElement('div');
  modal.id = 'detail-modal';
  modal.className = 'detail-modal';
  modal.innerHTML = `
    <div class="detail-modal-content${autoFit ? ' detail-modal-autofit' : ''}">
      <div class="detail-modal-header">
        <h2>${title}</h2>
        <button class="detail-modal-close" onclick="closeDetailModal()">✕</button>
      </div>
      <div class="detail-modal-body detail-modal-body-html">
        <div class="detail-content-html"></div>
      </div>
    </div>
  `;
  document.body.appendChild(modal);

  // 直接注入 HTML 内容
  const contentEl = modal.querySelector('.detail-content-html');
  contentEl.innerHTML = htmlContent;

  // 🔧 Task#1011：文件链接绑定提取为独立函数
  _bindFileLinksInModal(contentEl, taskId);

  // 字体大小遵循配置：遍历弹窗内所有元素，覆盖硬编码的 font-size
  applyInheritFontSize(modal);

  // 点击背景关闭
  modal.addEventListener('click', (e) => {
    if (e.target === modal) closeDetailModal();
  });

  // 🔧 Task#1145：键盘导航 + Esc 关闭
  const bodyEl = modal.querySelector('.detail-modal-body');
  const keyHandler = (e) => {
    if (e.key === 'Escape') {
      closeDetailModal();
      return;
    }
    // 键盘滚动导航（PageUp/PageDown/Home/End）
    if (!bodyEl || !document.getElementById('detail-modal')) return;
    const scrollable = bodyEl.scrollHeight > bodyEl.clientHeight;
    if (!scrollable) return;
    switch (e.key) {
      case 'PageDown':
        e.preventDefault();
        bodyEl.scrollBy({ top: bodyEl.clientHeight * 0.9, behavior: 'smooth' });
        break;
      case 'PageUp':
        e.preventDefault();
        bodyEl.scrollBy({ top: -bodyEl.clientHeight * 0.9, behavior: 'smooth' });
        break;
      case 'Home':
        e.preventDefault();
        bodyEl.scrollTo({ top: 0, behavior: 'smooth' });
        break;
      case 'End':
        e.preventDefault();
        bodyEl.scrollTo({ top: bodyEl.scrollHeight, behavior: 'smooth' });
        break;
    }
  };
  document.addEventListener('keydown', keyHandler);
  // 存储到 modal 上，closeDetailModal 时统一清理
  modal._detailKeyHandler = keyHandler;
}

/**
 * 显示详情弹出窗口（带 ANSI 颜色高亮）
 * @param {string} title - 窗口标题
 * @param {string} ansiText - 包含 ANSI 转义序列的文本（来自 CLI rich 输出）
 */
function showDetailModal(title, ansiText) {
  // 移除已存在的模态框（含清理键盘 handler）
  closeDetailModal();

  // 创建模态框
  const modal = document.createElement('div');
  modal.id = 'detail-modal';
  modal.className = 'detail-modal';
  modal.innerHTML = `
    <div class="detail-modal-content">
      <div class="detail-modal-header">
        <h2>${title}</h2>
        <button class="detail-modal-close" onclick="closeDetailModal()">✕</button>
      </div>
      <div class="detail-modal-body">
        <pre class="detail-content"></pre>
      </div>
    </div>
  `;
  document.body.appendChild(modal);

  // 将 ANSI 文本渲染为带颜色的 HTML
  const preEl = modal.querySelector('.detail-content');
  preEl.innerHTML = ansiToHtml(ansiText);

  // 字体大小遵循配置：遍历弹窗内所有元素，覆盖硬编码的 font-size
  applyInheritFontSize(modal);

  // 点击背景关闭
  modal.addEventListener('click', (e) => {
    if (e.target === modal) closeDetailModal();
  });

  // 🔧 Task#1145：键盘导航 + Esc 关闭
  const bodyEl2 = modal.querySelector('.detail-modal-body');
  const keyHandler2 = (e) => {
    if (e.key === 'Escape') {
      closeDetailModal();
      return;
    }
    if (!bodyEl2 || !document.getElementById('detail-modal')) return;
    const scrollable = bodyEl2.scrollHeight > bodyEl2.clientHeight;
    if (!scrollable) return;
    switch (e.key) {
      case 'PageDown':
        e.preventDefault();
        bodyEl2.scrollBy({ top: bodyEl2.clientHeight * 0.9, behavior: 'smooth' });
        break;
      case 'PageUp':
        e.preventDefault();
        bodyEl2.scrollBy({ top: -bodyEl2.clientHeight * 0.9, behavior: 'smooth' });
        break;
      case 'Home':
        e.preventDefault();
        bodyEl2.scrollTo({ top: 0, behavior: 'smooth' });
        break;
      case 'End':
        e.preventDefault();
        bodyEl2.scrollTo({ top: bodyEl2.scrollHeight, behavior: 'smooth' });
        break;
    }
  };
  document.addEventListener('keydown', keyHandler2);
  modal._detailKeyHandler = keyHandler2;
}

function closeDetailModal() {
  const modal = document.getElementById('detail-modal');
  if (modal) {
    // 🔧 Task#1145：清理键盘导航 handler
    if (modal._detailKeyHandler) {
      document.removeEventListener('keydown', modal._detailKeyHandler);
      modal._detailKeyHandler = null;
    }
    modal.remove();
  }
}

/**
 * 清除事务所有注入信息
 * @param {number} taskId - 事务 ID
 */
async function clearTaskInfo(taskId) {
  try {
    const response = await fetch(`/api/cli/task/${taskId}/info`, {
      method: 'DELETE',
    });
    const data = await response.json();
    if (data.success) {
      showSimpleToast('✅ 注入信息已清除', 'success');
      // 刷新事务详情弹窗
      await showTaskDetails(taskId);
    } else {
      showSimpleToast('❌ 清除失败', 'error');
    }
  } catch (error) {
    console.error('清除注入信息失败:', error);
    showSimpleToast('❌ 清除注入信息失败', 'error');
  }
}
/**
 * 清除事务事项清单（checklist）
 * @param {number} taskId - 事务 ID
 */
async function clearTaskChecklist(taskId) {
  try {
    const response = await fetch(`/api/cli/task/${taskId}/field/checklist`, {
      method: 'DELETE',
    });
    const data = await response.json();
    if (data.success) {
      showSimpleToast('✅ 事项清单已清除', 'success');
      // 刷新事务详情弹窗
      await showTaskDetails(taskId);
    } else {
      showSimpleToast('❌ 清除失败', 'error');
    }
  } catch (error) {
    console.error('清除事项清单失败:', error);
    showSimpleToast('❌ 清除事项清单失败', 'error');
  }
}


/**
 * 删除事务的单条注入信息
 * @param {number} taskId - 事务 ID
 * @param {number} index - info 项索引（0-based）
 */
async function deleteTaskInfoItem(taskId, index) {
  try {
    const response = await fetch(`/api/cli/task/${taskId}/info/item`, {
      method: 'DELETE',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ index }),
    });
    const data = await response.json();
    if (data.success) {
      // 在DOM上标记删除效果（删除线+暗灰色），不重开窗口
      const section = document.getElementById(`info-section-${taskId}`);
      if (section) {
        const item = section.querySelector(`.task-info-item[data-info-index="${index}"]`);
        if (item) {
          item.style.opacity = '0.35';
          item.style.textDecoration = 'line-through';
          // 移除操作链接防止重复操作
          const links = item.querySelectorAll('a[onclick*="deleteTaskInfoItem"], a[onclick*="markTaskInfoItemUnread"]');
          links.forEach(l => l.remove());
        }
      }
      showSimpleToast(`✅ 已删除`, 'success');
    } else {
      showSimpleToast('❌ 删除失败', 'error');
    }
  } catch (error) {
    console.error('删除注入信息失败:', error);
    showSimpleToast('❌ 删除注入信息失败', 'error');
  }
}

/**
 * 切换事务单条注入信息的已读/未读状态
 * @param {number} taskId - 事务 ID
 * @param {number} index - info 项索引（0-based）
 * @param {boolean} currentlyRead - 当前是否已读
 */
async function markTaskInfoItemUnread(taskId, index, currentlyRead) {
  const newRead = !currentlyRead;
  try {
    const response = await fetch(`/api/cli/task/${taskId}/info/item/read`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ index, read: newRead }),
    });
    const data = await response.json();
    if (data.success) {
      // 在DOM上切换颜色，不重开窗口
      const section = document.getElementById(`info-section-${taskId}`);
      if (section) {
        const item = section.querySelector(`.task-info-item[data-info-index="${index}"]`);
        if (item) {
          // 切换颜色：未读=绿色，已读=灰色
          item.style.color = newRead ? '#8b949e' : '#3fb950';
          // 更新"改未读/改已读"链接文字和onclick参数
          const unreadLink = item.querySelector('a[onclick*="markTaskInfoItemUnread"]');
          if (unreadLink) {
            unreadLink.textContent = newRead ? '改未读' : '改已读';
            unreadLink.setAttribute('onclick', `markTaskInfoItemUnread(${taskId}, ${index}, ${newRead})`);
          }
        }
      }
      showSimpleToast(`✅ 已标记为${newRead ? '已读' : '未读'}`, 'success');
    } else {
      showSimpleToast('❌ 操作失败', 'error');
    }
  } catch (error) {
    console.error('修改info已读状态失败:', error);
    showSimpleToast('❌ 修改已读状态失败', 'error');
  }
}

/**
 * 将事务所有未读注入信息标记为已读
 * @param {number} taskId - 事务 ID
 */
async function markTaskInfoRead(taskId) {
  try {
    const response = await fetch(`/api/cli/task/${taskId}/info`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ read: true }),
    });
    const data = await response.json();
    if (data.success) {
      showSimpleToast('✅ 已标记为已读', 'success');
      // 刷新事务详情弹窗
      await showTaskDetails(taskId);
    } else {
      showSimpleToast('❌ 标记失败', 'error');
    }
  } catch (error) {
    console.error('标记注入信息已读失败:', error);
    showSimpleToast('❌ 标记已读失败', 'error');
  }
}

/**
 * 清除事务定时器
 * @param {number} taskId - 事务 ID
 */
async function clearTaskTimer(taskId) {
  try {
    const response = await fetch(`/api/cli/task/${taskId}/timer`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ delete: true }),
    });
    const data = await response.json();
    if (data.success) {
      showSimpleToast('✅ 定时器已清除', 'success');
      await showTaskDetails(taskId);
    } else {
      showSimpleToast('❌ 清除失败', 'error');
    }
  } catch (error) {
    console.error('清除定时器失败:', error);
    showSimpleToast('❌ 清除定时器失败', 'error');
  }
}

/**
 * 编辑事务定时器（内联编辑）
 * @param {number} taskId - 事务 ID
 * @param {number} currentValue - 当前定时器值（秒）
 */
function editTaskTimer(taskId, currentValue) {
  const cell = event.target.closest('td');
  if (!cell) return;
  const oldHtml = cell.innerHTML;
  cell.innerHTML = `<span style="display:inline-flex;align-items:center;gap:4px;"><input type="number" min="0" value="${currentValue || 0}" class="provider-priority-input" style="width:60px;"><span style="font-size:0.8em;color:var(--text-secondary);">秒</span><button onclick="this.parentElement._save()" class="provider-priority-input" style="cursor:pointer;">✅</button><button onclick="this.parentElement._cancel()" class="provider-priority-input" style="cursor:pointer;">✖</button></span>`;
  const wrap = cell.firstElementChild;
  const input = wrap.querySelector('input');
  input.focus();
  input.select();

  wrap._cancel = () => { cell.innerHTML = oldHtml; };
  wrap._save = async () => {
    const seconds = parseInt(input.value, 10);
    if (isNaN(seconds) || seconds < 0) {
      showSimpleToast('❌ 请输入有效的非负整数', 'error');
      return;
    }
    try {
      const response = await fetch(`/api/cli/task/${taskId}/timer`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ seconds }),
      });
      const data = await response.json();
      if (data.success) {
        showSimpleToast(`✅ 定时器已设置为 ${seconds} 秒`, 'success');
        await showTaskDetails(taskId);
      } else {
        showSimpleToast('❌ 设置失败', 'error');
        cell.innerHTML = oldHtml;
      }
    } catch (error) {
      console.error('设置定时器失败:', error);
      showSimpleToast('❌ 设置定时器失败', 'error');
      cell.innerHTML = oldHtml;
    }
  };

  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); wrap._save(); }
    if (e.key === 'Escape') { e.preventDefault(); wrap._cancel(); }
  });
}

/**
 * 编辑事务心跳超时（内联编辑，参考 editTaskTimer）
 * @param {number} taskId - 事务 ID
 * @param {number} currentValue - 当前心跳超时值（秒），0表示使用默认值
 */
function editTaskHeartbeat(taskId, currentValue) {
  const cell = event.target.closest('td');
  if (!cell) return;
  const oldHtml = cell.innerHTML;
  cell.innerHTML = `<span style="display:inline-flex;align-items:center;gap:4px;"><input type="number" min="0" value="${currentValue || 0}" class="provider-priority-input" style="width:60px;"><span style="font-size:0.8em;color:var(--text-secondary);">秒</span><button onclick="this.parentElement._save()" class="provider-priority-input" style="cursor:pointer;">✅</button><button onclick="this.parentElement._cancel()" class="provider-priority-input" style="cursor:pointer;">✖</button></span>`;
  const wrap = cell.firstElementChild;
  const input = wrap.querySelector('input');
  input.focus();
  input.select();

  wrap._cancel = () => { cell.innerHTML = oldHtml; };
  wrap._save = async () => {
    const seconds = parseInt(input.value, 10);
    if (isNaN(seconds) || seconds < 0) {
      showSimpleToast('❌ 请输入有效的非负整数', 'error');
      return;
    }
    try {
      const response = await fetch(`/api/cli/task/${taskId}/heartbeat`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ seconds }),
      });
      const data = await response.json();
      if (data.success) {
        if (seconds > 0) {
          showSimpleToast(`✅ 心跳超时已设置为 ${seconds} 秒`, 'success');
        } else {
          showSimpleToast('✅ 心跳超时已重置为默认值', 'success');
        }
        await showTaskDetails(taskId);
      } else {
        showSimpleToast('❌ 设置失败', 'error');
        cell.innerHTML = oldHtml;
      }
    } catch (error) {
      console.error('设置心跳超时失败:', error);
      showSimpleToast('❌ 设置心跳超时失败', 'error');
      cell.innerHTML = oldHtml;
    }
  };

  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); wrap._save(); }
    if (e.key === 'Escape') { e.preventDefault(); wrap._cancel(); }
  });
}

/**
 * 重置事务心跳超时为默认值
 * @param {number} taskId - 事务 ID
 */
async function resetTaskHeartbeat(taskId) {
  try {
    const response = await fetch(`/api/cli/task/${taskId}/heartbeat`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ delete: true }),
    });
    const data = await response.json();
    if (data.success) {
      showSimpleToast('✅ 心跳超时已重置为默认值', 'success');
      await showTaskDetails(taskId);
    } else {
      showSimpleToast('❌ 重置失败', 'error');
    }
  } catch (error) {
    console.error('重置心跳超时失败:', error);
    showSimpleToast('❌ 重置心跳超时失败', 'error');
  }
}

/**
 * 编辑事务字段（nest/workdir等，内联编辑）
 * @param {number} taskId - 事务 ID
 * @param {string} field - 字段名
 */
function editTaskField(taskId, field) {
  const cell = event.target.closest('td');
  if (!cell) return;
  const oldHtml = cell.innerHTML;
  const labels = { nest: '鹏🐣窝', workdir: '工作目录', branch_name: '分支' };
  const label = labels[field] || field;

  if (field === 'nest') {
    // 从主界面的nest下拉框获取选项列表
    const allNests = window._latestNests || {};
    const nestKeys = Object.keys(allNests);
    if (!nestKeys.length) nestKeys.push('local');

    let optionsHtml = '';
    for (const key of nestKeys) {
      const info = allNests[key] || {};
      const name = info.display_key || key;
      optionsHtml += `<option value="${name}">${name}</option>`;
    }

    // 动态计算select宽度：取最长选项文字长度
    let maxNameLen = 0;
    for (const key of nestKeys) {
      const info = allNests[key] || {};
      const name = info.display_key || key;
      if (name.length > maxNameLen) maxNameLen = name.length;
    }
    const selectWidth = Math.max(120, maxNameLen * 9 + 40); // ~9px/字符 + padding+箭头

    cell.innerHTML = `<span style="display:inline-flex;align-items:center;gap:4px;"><select class="provider-priority-input" style="width:${selectWidth}px;">${optionsHtml}</select><button onclick="this.parentElement._save()" class="provider-priority-input" style="cursor:pointer;">✅</button><button onclick="this.parentElement._cancel()" class="provider-priority-input" style="cursor:pointer;">✖</button></span>`;
    const select = cell.querySelector('select');
    select.focus();
    const wrap3 = cell.firstElementChild;
    wrap3._cancel = () => { cell.innerHTML = oldHtml; };
    wrap3._save = async () => {
      const val = select.value;
      try {
        const response = await fetch(`/api/cli/task/${taskId}/field/${field}`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ value: val }),
        });
        const data = await response.json();
        if (data.success) {
          showSimpleToast(`✅ ${label}已设置`, 'success');
          await showTaskDetails(taskId);
        } else {
          showSimpleToast('❌ 设置失败', 'error');
          cell.innerHTML = oldHtml;
        }
      } catch (error) {
        console.error(`设置${label}失败:`, error);
        showSimpleToast(`❌ 设置${label}失败`, 'error');
        cell.innerHTML = oldHtml;
      }
    };
    select.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); wrap3._save(); }
      if (e.key === 'Escape') { e.preventDefault(); wrap3._cancel(); }
    });
  } else {
    // workdir 或其他文本字段：用输入框
    const currentVal = cell.textContent.replace(/设置$/, '').trim();
    cell.innerHTML = `<span style="display:inline-flex;align-items:center;gap:4px;width:100%;"><input type="text" value="${currentVal}" class="provider-priority-input" style="flex:1;min-width:300px;font-size:inherit;text-align:left;"><button onclick="this.parentElement._save()" class="provider-priority-input" style="cursor:pointer;flex-shrink:0;">✅</button><button onclick="this.parentElement._cancel()" class="provider-priority-input" style="cursor:pointer;flex-shrink:0;">✖</button></span>`;
    const input = cell.querySelector('input');
    input.focus();
    input.select();
    const wrap2 = cell.firstElementChild;
    wrap2._cancel = () => { cell.innerHTML = oldHtml; };
    wrap2._save = async () => {
      const val = input.value.trim();
      try {
        const response = await fetch(`/api/cli/task/${taskId}/field/${field}`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ value: val }),
        });
        const data = await response.json();
        if (data.success) {
          showSimpleToast(`✅ ${label}已设置`, 'success');
          await showTaskDetails(taskId);
        } else {
          showSimpleToast('❌ 设置失败', 'error');
          cell.innerHTML = oldHtml;
        }
      } catch (error) {
        console.error(`设置${label}失败:`, error);
        showSimpleToast(`❌ 设置${label}失败`, 'error');
        cell.innerHTML = oldHtml;
      }
    };
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); wrap2._save(); }
      if (e.key === 'Escape') { e.preventDefault(); wrap2._cancel(); }
    });
  }
}

/**
 * 切换事务注入信息的显示：点击超链接后，替换当前 info 内容为全部 info 列表（已读灰色，未读绿色）
 * @param {number} taskId - 事务 ID
 * @param {HTMLElement} toggleEl - 被点击的标题元素
 */
function toggleTaskInfo(taskId, toggleEl) {
  const section = toggleEl.closest('.task-section');
  if (!section) return;

  const defaultEl = section.querySelector('.task-info-default');
  const fullEl = section.querySelector('.task-info-full');

  if (defaultEl && fullEl) {
    if (defaultEl.style.display !== 'none') {
      // 显示全部 info，隐藏默认内容
      defaultEl.style.display = 'none';
      fullEl.style.display = '';
      // 更新标题：添加收起提示
      const h3 = section.querySelector('.task-info-toggle');
      if (h3) {
        h3.innerHTML = h3.innerHTML.replace('💡 注入信息', '💡 注入信息（收起）');
      }
    } else {
      // 收起全部，显示默认内容（最后已读+所有未读）
      defaultEl.style.display = '';
      fullEl.style.display = 'none';
      // 恢复标题：移除收起提示
      const h3 = section.querySelector('.task-info-toggle');
      if (h3) {
        h3.innerHTML = h3.innerHTML.replace('💡 注入信息（收起）', '💡 注入信息').replace('（点击收起）', '');
      }
    }
  }
}

/**
 * 切换事务上下文的显示：点击超链接后，显示完整对话上下文（markdown高亮）
 * @param {number} taskId - 事务 ID
 * @param {HTMLElement} toggleEl - 被点击的标题元素
 */
function toggleContext(taskId, toggleEl) {
  const section = toggleEl.closest('.task-section');
  if (!section) return;

  const fullEl = section.querySelector('.context-full');
  const h3 = section.querySelector('.context-toggle');

  if (fullEl && h3) {
    if (fullEl.style.display === 'none') {
      // 显示完整上下文
      fullEl.style.display = '';
      // 更新标题：添加收起提示
      h3.innerHTML = h3.innerHTML.replace('📜 当前上下文', '📜 当前上下文（收起）') + '<span style="font-size:0.75em;color:#58a6ff;">(点击收起)</span>';
    } else {
      // 收起完整上下文
      fullEl.style.display = 'none';
      // 恢复标题：移除收起提示，恢复展开提示
      h3.innerHTML = h3.innerHTML.replace('📜 当前上下文（收起）', '📜 当前上下文').replace('(点击收起)', '(点击展开)');
    }
  }
}

/**
 * 删除上下文中指定索引的消息（含其关联的 tool 消息）
 * 操作后直接在 DOM 上标记删除线效果，不重开窗口
 * @param {number} taskId - 事务 ID
 * @param {number} messageIndex - 消息索引（0-based）
 */
async function deleteContextMessage(taskId, messageIndex) {
  try {
    const response = await fetch(`/api/cli/task/${taskId}/context/message`, {
      method: 'DELETE',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message_index: messageIndex }),
    });
    const data = await response.json();
    if (data.success) {
      // 在 DOM 上标记被删除的消息：删除线 + 暗灰色
      const section = document.getElementById(`context-section-${taskId}`);
      if (section) {
        const allMsgs = section.querySelectorAll('.context-message');
        const target = section.querySelector(`.context-message[data-msg-index="${messageIndex}"]`);
        if (target) {
          // 从目标开始，连续标记（user→连带assistant+tool，assistant→连带tool）
          const role = target.querySelector('div')?.textContent?.trim() || '';
          let node = target;
          while (node) {
            node.style.opacity = '0.35';
            node.style.textDecoration = 'line-through';
            // 移除操作按钮
            const actions = node.querySelector('span[style*="float:right"]');
            if (actions) actions.remove();
            // 判断下一条是否需要连带标记
            const next = node.nextElementSibling;
            if (!next || !next.classList.contains('context-message')) break;
            const nextRole = next.querySelector('div')?.textContent?.trim() || '';
            if (role.includes('user') && (nextRole.includes('assistant') || nextRole.includes('tool'))) {
              node = next;
            } else if (role.includes('assistant') && nextRole.includes('tool')) {
              node = next;
            } else {
              break;
            }
          }
        }
      }
      showSimpleToast(`✅ 已删除 ${data.deleted_count} 条消息`, 'success');
    } else {
      showSimpleToast('❌ 删除失败', 'error');
    }
  } catch (error) {
    console.error('删除上下文消息失败:', error);
    showSimpleToast('❌ 删除上下文消息失败', 'error');
  }
}

/**
 * 从指定消息索引开始截断所有后续消息（含该条）
 * 操作后直接在 DOM 上标记截断效果，不重开窗口
 * @param {number} taskId - 事务 ID
 * @param {number} messageIndex - 消息索引（0-based）
 */
async function truncateContextFrom(taskId, messageIndex) {
  try {
    const response = await fetch(`/api/cli/task/${taskId}/context/truncate`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message_index: messageIndex }),
    });
    const data = await response.json();
    if (data.success) {
      // 在 DOM 上标记被截断的消息：从 messageIndex 开始全部标记删除线 + 暗灰色
      const section = document.getElementById(`context-section-${taskId}`);
      if (section) {
        const allMsgs = section.querySelectorAll('.context-message');
        allMsgs.forEach(msg => {
          const idx = parseInt(msg.dataset.msgIndex);
          if (idx >= messageIndex) {
            msg.style.opacity = '0.35';
            msg.style.textDecoration = 'line-through';
            const actions = msg.querySelector('span[style*="float:right"]');
            if (actions) actions.remove();
          }
        });
      }
      showSimpleToast(`✅ 已截断 ${data.removed_count} 条消息`, 'success');
    } else {
      showSimpleToast('❌ 截断失败', 'error');
    }
  } catch (error) {
    console.error('截断上下文失败:', error);
    showSimpleToast('❌ 截断上下文失败', 'error');
  }
}

/**
 * 遍历元素及其所有后代，设置 font-size: inherit，
 * 覆盖 CSS 中硬编码的 rem/px 字体大小，确保遵循用户配置。
 */
function applyInheritFontSize(root) {
  function walk(el) {
    if (el.nodeType !== 1) return;
    // 跳过终端相关
    if (el.classList.contains('output') || el.classList.contains('terminal-container') || el.id === 'terminal-container') return;
    el.style.fontSize = 'inherit';
    for (const child of el.children) {
      walk(child);
    }
  }
  walk(root);
}

/**
 * ANSI 转义序列 → HTML（保留 rich 库输出的全部颜色/样式高亮）
 * 累积相同样式的文本段，避免逐字符包裹 span
 */
function ansiToHtml(text) {
  if (!text) return '';

  // 预处理：去除每行尾随空格
  // Rich 在宽终端(COLUMNS=320)下会用空格 padding 到终端宽度，
  // 这些尾随空格在 CSS pre 模式下会导致内容区域过宽。
  // 注意：ANSI转义码(\x1b...)不包含空格，所以此操作安全。
  text = text.split('\n').map(line => line.replace(/\s+$/, '')).join('\n');

  // 标准16色映射（30-37 前景，40-47 背景，90-97 亮前景）
  const fg16 = {
    30: '#4e4e4e', 31: '#f85149', 32: '#3fb950', 33: '#d29922',
    34: '#58a6ff', 35: '#bc8cff', 36: '#39c5cf', 37: '#d1d5db',
    90: '#8b949e', 91: '#ff7b72', 92: '#56d364', 93: '#e3b341',
    94: '#79c0ff', 95: '#d2a8ff', 96: '#56d4dd', 97: '#f0f6fc',
  };
  const bg16 = {
    40: '#242424', 41: '#6e2323', 42: '#234d23', 43: '#544a16',
    44: '#1a3a5c', 45: '#3c1e50', 46: '#15464a', 47: '#444444',
  };

  // 预构建 256 色表
  const c256 = [];
  const v6 = [0, 95, 135, 175, 215, 255];
  for (let i = 0; i < 16; i++) c256[i] = fg16[30 + (i % 8)] || '#fff';
  for (let r = 0; r < 6; r++)
    for (let g = 0; g < 6; g++)
      for (let b = 0; b < 6; b++)
        c256[16 + r * 36 + g * 6 + b] = `rgb(${v6[r]},${v6[g]},${v6[b]})`;
  for (let i = 0; i < 24; i++) { const v = 8 + i * 10; c256[232 + i] = `rgb(${v},${v},${v})`; }

  // 解析 SGR 参数序列，更新样式状态
  function applyCodes(codes, st) {
    for (let i = 0; i < codes.length; i++) {
      const n = parseInt(codes[i]);
      if (isNaN(n)) continue;
      if (n === 0) { st.fg = null; st.bg = null; st.bold = false; st.dim = false; st.italic = false; st.underline = false; st.strike = false; }
      else if (n === 1) st.bold = true;
      else if (n === 2) st.dim = true;
      else if (n === 3) st.italic = true;
      else if (n === 4) st.underline = true;
      else if (n === 9) st.strike = true;
      else if (n === 22) { st.bold = false; st.dim = false; }
      else if (n === 23) st.italic = false;
      else if (n === 24) st.underline = false;
      else if (n === 29) st.strike = false;
      else if (n === 39) st.fg = null;
      else if (n === 49) st.bg = null;
      else if (n >= 30 && n <= 37) st.fg = fg16[n];
      else if (n >= 90 && n <= 97) st.fg = fg16[n];
      else if (n >= 40 && n <= 47) st.bg = bg16[n];
      else if (n === 38) {
        if (codes[i + 1] === '5' && c256[parseInt(codes[i + 2])]) { st.fg = c256[parseInt(codes[i + 2])]; i += 2; }
        else if (codes[i + 1] === '2') { st.fg = `rgb(${codes[i+2]|0},${codes[i+3]|0},${codes[i+4]|0})`; i += 4; }
      } else if (n === 48) {
        if (codes[i + 1] === '5' && c256[parseInt(codes[i + 2])]) { st.bg = c256[parseInt(codes[i + 2])]; i += 2; }
        else if (codes[i + 1] === '2') { st.bg = `rgb(${codes[i+2]|0},${codes[i+3]|0},${codes[i+4]|0})`; i += 4; }
      }
    }
  }

  // 构建内联 style 字符串
  function styleStr(st) {
    let parts = [];
    if (st.fg) parts.push(`color:${st.fg}`);
    if (st.bg) parts.push(`background:${st.bg}`);
    if (st.bold) parts.push('font-weight:bold');
    if (st.dim) parts.push('opacity:0.5');
    if (st.italic) parts.push('font-style:italic');
    if (st.underline) parts.push('text-decoration:underline');
    if (st.strike) parts.push('text-decoration:line-through');
    return parts.length ? parts.join(';') : '';
  }

  // 转义 HTML 特殊字符
  function esc(s) {
    return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  const st = { fg: null, bg: null, bold: false, dim: false, italic: false, underline: false, strike: false };
  let html = '';
  let buf = '';       // 累积当前样式的文本
  let curStyle = '';  // 当前文本段对应的 style 字符串

  // flush：把 buf 中的文本用 curStyle 包裹输出
  function flush() {
    if (!buf) return;
    const escaped = esc(buf);
    if (curStyle) {
      html += `<span style="${curStyle}">${escaped}</span>`;
    } else {
      html += escaped;
    }
    buf = '';
  }

  // 逐段处理：先按 ANSI 转义序列分割
  const regex = /\x1b\[([0-9;]*)m/g;
  let lastEnd = 0;
  let match;

  while ((match = regex.exec(text)) !== null) {
    // 转义序列之前的普通文本
    const plain = text.substring(lastEnd, match.index);
    if (plain) {
      buf += plain;
    }
    // 处理 SGR 码
    const codes = match[1] ? match[1].split(';') : ['0'];
    // 在样式变化前 flush 旧文本
    flush();
    applyCodes(codes, st);
    curStyle = styleStr(st);
    lastEnd = regex.lastIndex;
  }

  // 尾部剩余文本
  const tail = text.substring(lastEnd);
  if (tail) buf += tail;
  flush();

  // 换行处理：将 \r\n、\r、\n 都转换为 <br>
  // 注意：先替换 \r\n，再替换单独的 \r 和 \n，避免双重替换
  html = html.replace(/\r\n/g, '<br>').replace(/\r/g, '<br>').replace(/\n/g, '<br>');
  // 清理 <br> 前的尾随空格（行内 padding 空格）
  html = html.replace(/ +<br>/g, '<br>');
  return html;
}

// 显示帮助信息到终端
function showHelp() {
  const helpHtml = `<div style="padding:20px;font-size:0.9rem;line-height:1.8;">
    <h3 style="color:var(--accent-color,#58a6ff);margin:0 0 12px;">内置命令</h3>
    <table style="width:100%;border-collapse:collapse;margin-bottom:20px;">
      <tr><td style="padding:4px 12px;color:#d29922;white-space:nowrap;font-family:monospace;">/new</td><td style="padding:4px 8px;">开始新话题</td></tr>
      <tr><td style="padding:4px 12px;color:#d29922;white-space:nowrap;font-family:monospace;">/clear</td><td style="padding:4px 8px;">清空事务输出信息</td></tr>
      <tr><td style="padding:4px 12px;color:#d29922;white-space:nowrap;font-family:monospace;">/reply id [消息]</td><td style="padding:4px 8px;">回复指定话题</td></tr>
      <tr><td style="padding:4px 12px;color:#d29922;white-space:nowrap;font-family:monospace;">/msg [参数]</td><td style="padding:4px 8px;">消息管理</td></tr>
      <tr><td style="padding:4px 12px;color:#d29922;white-space:nowrap;font-family:monospace;">/task [参数]</td><td style="padding:4px 8px;">事务管理</td></tr>
      <tr><td style="padding:4px 12px;color:#d29922;white-space:nowrap;font-family:monospace;">/cmd &lt;命令&gt; [参数]</td><td style="padding:4px 8px;">执行命令行命令</td></tr>
      <tr><td style="padding:4px 12px;color:#d29922;white-space:nowrap;font-family:monospace;">/cd &lt;目录&gt;</td><td style="padding:4px 8px;">切换后端工作目录</td></tr>
      <tr><td style="padding:4px 12px;color:#d29922;white-space:nowrap;font-family:monospace;">/go [提示]</td><td style="padding:4px 8px;">启动/继续执行事务</td></tr>
      <tr><td style="padding:4px 12px;color:#d29922;white-space:nowrap;font-family:monospace;">/restart</td><td style="padding:4px 8px;">清空上下文重启事务</td></tr>
      <tr><td style="padding:4px 12px;color:#d29922;white-space:nowrap;font-family:monospace;">/clear-all-data --force</td><td style="padding:4px 8px;">清空所有数据</td></tr>
      <tr><td style="padding:4px 12px;color:#d29922;white-space:nowrap;font-family:monospace;">/reset-webui</td><td style="padding:4px 8px;">全面清理浏览器数据并刷新页面</td></tr>
      <tr><td style="padding:4px 12px;color:#d29922;white-space:nowrap;font-family:monospace;">/help</td><td style="padding:4px 8px;">显示帮助</td></tr>
    </table>

    <h3 style="color:var(--accent-color,#58a6ff);margin:0 0 12px;">工作目录</h3>
    <div style="margin-bottom:20px;padding-left:8px;">
      <div>• 输入框上方的"工作目录"可设置 ${state.agentName} 的工作目录</div>
      <div>• ${state.agentName} 执行命令和访问文件时基于此目录</div>
      <div>• 支持相对路径和绝对路径（如 ~/work 或 /home/user/work）</div>
    </div>

    <h3 style="color:var(--accent-color,#58a6ff);margin:0 0 12px;">快捷键</h3>
    <table style="width:100%;border-collapse:collapse;margin-bottom:20px;">
      <tr><td style="padding:4px 12px;color:#d29922;white-space:nowrap;font-family:monospace;">Enter</td><td style="padding:4px 8px;">发送消息</td></tr>
      <tr><td style="padding:4px 12px;color:#d29922;white-space:nowrap;font-family:monospace;">Shift+Enter</td><td style="padding:4px 8px;">换行</td></tr>
      <tr><td style="padding:4px 12px;color:#d29922;white-space:nowrap;font-family:monospace;">Ctrl+Enter</td><td style="padding:4px 8px;">发送新消息</td></tr>
      <tr><td style="padding:4px 12px;color:#d29922;white-space:nowrap;font-family:monospace;">Ctrl+Shift+Enter</td><td style="padding:4px 8px;">切换消息发送/事务信息注入模式</td></tr>
    </table>

    <h3 style="color:var(--accent-color,#58a6ff);margin:0 0 12px;">搜索过滤</h3>
    <table style="width:100%;border-collapse:collapse;margin-bottom:20px;">
      <tr><td style="padding:4px 12px;color:#d29922;white-space:nowrap;font-family:monospace;">空格</td><td style="padding:4px 8px;">AND 关键词（含所有关键词）</td></tr>
      <tr><td style="padding:4px 12px;color:#d29922;white-space:nowrap;font-family:monospace;">|</td><td style="padding:4px 8px;">OR 关键词（含任一关键词）</td></tr>
      <tr><td style="padding:4px 12px;color:#d29922;white-space:nowrap;font-family:monospace;">hi hello</td><td style="padding:4px 8px;">含 hi 且含 hello</td></tr>
      <tr><td style="padding:4px 12px;color:#d29922;white-space:nowrap;font-family:monospace;">hi|hello</td><td style="padding:4px 8px;">含 hi 或含 hello</td></tr>
    </table>

    <h3 style="color:var(--accent-color,#58a6ff);margin:0 0 12px;">操作说明</h3>
    <div style="padding-left:8px;">
      <div>• 点击左侧话题可选择回复</div>
      <div>• 点击右侧事务可过滤输出</div>
      <div>• 拖拽边栏可调整宽度</div>
      <div>• 输入框自动获得焦点</div>
    </div>
  </div>`;

  showDetailModalHtml('❓ 帮助', helpHtml, null, true);
}

function closeHelp() {
  // 不再需要，保留兼容
}

async function sendMessage() {
  const input = document.getElementById('message-input');
  if (!input) return;

  let content = input.innerText.trim();
  if (!content && state.attachedFiles.length === 0) return;
  await saveInputHistory(content);

  // 🔧 Task#1449优化：事务模式下发送info，直接调用快速REST API + 立即xterm反馈
  // 之前：转换为 /task info 命令 → /api/command 子进程（~700ms+）
  // 现在：POST /api/cli/task/{taskId}/info（~40ms），且立刻在xterm输出反馈
  if (state.inputMode === 'task' && state.currentTaskId && !content.startsWith("/")) {
    const taskId = state.currentTaskId;
    // 立刻清空输入框和附件
    input.innerHTML = '';
    clearAttachedFiles();
    setLoading(false);
    // 重置历史索引和草稿
    state.historyIndex = -1;
    state.historyDraft = '';
    // 立即在xterm输出反馈，让用户立刻知道信息已发送
    showInOutput(`✅ 注入信息已发送至事务 #${taskId}：${content.length > 60 ? content.substring(0, 60) + '...' : content}`, 'success');
    showSimpleToast('📜  注入信息已发送', 'success');
    // 异步调用快速REST API（不阻塞UI，即使慢也不影响用户体验）
    fetch(`/api/cli/task/${taskId}/info`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ info: content })
    }).then(resp => resp.json()).then(data => {
      if (data.success) {
        console.log(`[Task#1449] info注入成功，task#${taskId}，共${data.count || 0}条`);
      } else {
        showInOutput(`⚠️ 注入信息发送失败：${data.action || '未知错误'}`, 'warning');
      }
    }).catch(err => {
      showInOutput(`❌ 注入信息发送失败：${err.message}`, 'error');
    });
    return;
  }

  // 处理内置命令
  if (content.startsWith("/")) {
    content = textToArgs(content);
  }
  const cmdResult = processBuiltinCommand(content);
  if (cmdResult.handled) {
    // 保存内置命令到输入历史
    if (cmdResult.clear) {
      input.innerHTML = '';
      clearAttachedFiles();
    }
    if (cmdResult.command) {
      await executeBuiltinCommand(cmdResult);
    }
    input.innerHTML = '';
    return;
  }

  // 获取实际要发送的内容和回复目标
  let actualContent = cmdResult.content || content;

  // 如果选中了分支消息，使用它作为回复目标
  const replyTo = state.currentBranchMessageId || state.currentMessageId;

  // 获取输入选项
  const thinkingToggle = document.getElementById('thinking-toggle');
  const thinkingBudgetInput = document.getElementById('thinking-budget');
  const searchToggle = document.getElementById('search-toggle');

  // 思考模式：勾选=跟随全局配置（不传null则用后端全局设置），取消=禁用
  // 注意：thinking_mode 消息级参数后端不消费（无消费者），全局配置由 /api/llm/thinking-mode 管理
  // 这里仅在取消勾选时显式禁用，勾选时保持全局设置（auto/low/high/max/enabled 均尊重）
  const thinkingMode = thinkingToggle && !thinkingToggle.checked ? 'disabled' : null;
  const thinkingBudget = thinkingBudgetInput ? parseInt(thinkingBudgetInput.value) || null : null;
  const searchEnabled = searchToggle ? searchToggle.checked : false;

  // 显示加载状态
  setLoading(true);

  // 设置回复标志（回复消息不自动切换选中状态，保持链首）
  if (replyTo) {
    state.isReplyingMessage = true;
    state._isLocalReply = true;  // 🔧 Task#349：标记本地回复，由 handleMessageUpdate 清除
    // 🔧 Task#349：超时兜底清除（防止 WebSocket 丢失导致 _isLocalReply 永远残留）
    setTimeout(() => { state._isLocalReply = false; }, 5000);
  }

  // 🔧 Task#265：清除上一次的本地创建标记，防止残留导致误匹配
  state._localCreatedMsgId = null;

  try {
    // 如果有文件，先上传文件
    let uploadedFiles = [];
    if (state.attachedFiles.length > 0) {
      uploadedFiles = await uploadFiles();
      if (uploadedFiles.length === 0 && state.attachedFiles.length > 0) {
        showSimpleToast('📎  文件上传失败', 'error');
        setLoading(false);
        return;
      }
    }

    // 根据是否有回复目标决定 URL 和请求体
    // URL中的msg_id使用replyTo（可能是分支消息ID），确保回复正确的消息
    // 回复的目标消息ID通过请求体的reply_to参数传递（与URL中的msg_id一致）
    const url = replyTo
      ? `/api/messages/${replyTo}/reply`  // 使用replyTo（可能是分支消息ID）
      : '/api/messages';

    const requestBody = {
      content: actualContent,
      files: uploadedFiles,
      thinking_mode: thinkingMode,
      thinking_budget: thinkingBudget,
      search_enabled: searchEnabled,
      workdir: state.workdir || null,
      nest: getSelectedNestName() || 'local',
    };

    const response = await fetch(url, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify(requestBody),
    });

    if (!response.ok) {
      let errorMessage = '发送失败';
      try {
        const errorData = await response.json();
        // FastAPI 验证错误返回 detail 为数组
        if (Array.isArray(errorData.detail)) {
          errorMessage = errorData.detail.map(e => e.msg).join(', ') || '发送失败';
        } else if (typeof errorData.detail === 'string') {
          errorMessage = errorData.detail;
        } else if (errorData.error) {
          errorMessage = typeof errorData.error === 'string' ? errorData.error : JSON.stringify(errorData.error);
        }
      } catch {
        // JSON 解析失败，使用默认消息
      }
      throw new Error(errorMessage);
    }

    const result = await response.json();

    // 刷新消息历史（从数据库重新加载）
    refreshMessageHistory();

    // 滚动消息栏到顶部（最新消息，立即跳转，不使用平滑滚动）
    const messageTree = document.getElementById('message-tree');
    if (messageTree) {
      messageTree.scrollTo({ top: 0, behavior: 'instant' });
    }

    // 立刻清空输入框和附件（不等待处理完成）
    input.innerHTML = '';
    clearAttachedFiles();
    setLoading(false);  // 立刻取消加载状态，允许继续输入

    // 重置历史索引和草稿
    state.historyIndex = -1;
    state.historyDraft = '';

    // 设置当前消息ID（新消息或回复链）
    if (result.id) {
      if (!replyTo) {
        // 新消息，设置为当前消息
        state.currentMessageId = result.id;
        state.isCreatingNewMessage = false;  // 发送新消息后，退出新建状态
        // 🔧 Task#265：标记本实例创建的消息ID，用于 handleMessageUpdate 区分本地操作和远程广播
        state._localCreatedMsgId = result.id;
        // 更新输入框提示为回复消息
        updateInputPlaceholder(result.id);
        // 🔧 记录需要滚动到的消息ID（新消息）
        state.scrollToMessageId = result.id;
      } else {
        // 🔧 Task#265：回复消息也标记，防止 handleMessageUpdate 对其他实例误触发自动选中
        state._localCreatedMsgId = result.id;
      }
      // 不清空分支消息ID，让用户可以继续回复同一个分支消息
      // 用户想回复根消息时，可以点击根消息（会调用selectMessage清空currentBranchMessageId）
      // 不清空输出框，输出会通过WebSocket接着显示
      // 只有/new命令才清空（在processBuiltinCommand中处理）
    }

    // 刷新消息列表（会根据state.currentMessageId设置active class）
    await loadMessages(false, true);  // 发送消息后：完整加载

    if (result.id && !replyTo) {
      // 🔧 Task#281修复：先刷新事务列表，确保新消息关联的事务已加载到 state.tasks 中
      // 否则 selectMessage 中的 findTaskByMessageId 会返回 null，导致 state.currentTaskId = null，
      // 后续输出因 taskId 不匹配而被 appendOutput 跳过，造成 xterm 输出停滞
      await loadTasks();
      // 新消息：调用 selectMessage 切换到新消息
      await selectMessage(result.id);
      // 🔧 Task#281修复：selectMessage 后立即清除 _localCreatedMsgId，
      // 防止 handleMessageUpdate 二次调用 selectMessage（会导致xterm清空重新订阅）
      state._localCreatedMsgId = null;

      // 🔧 Task#281修复：如果 selectMessage 中 findTaskByMessageId 返回 null（事务未创建竞态），
      // 标记 _pendingMessageForTask，让 handleTaskUpdate(created) 补偿切换输出订阅
      if (state.currentTaskId === null) {
        state._pendingMessageForTask = result.id;
        console.log(`[Task#281] 事务未创建竞态：设置 _pendingMessageForTask=${result.id}，等待 handleTaskUpdate(created) 补偿`);
      }
    }
    // 🔧 Task#281修复：回复消息时不在此处切换任务（不会创建新task，由链首task处理）
    // currentTaskId 保持不变，输出订阅不变，xterm 继续显示
    // 🔧 Task#1432：在xterm中输出消息发送成功提示，让用户明确知道消息已发出
    if (replyTo) {
      showInOutput(`✅ 已回复消息 #${replyTo}，正在处理...`, 'success');
    } else {
      showInOutput(`✅ 新消息已发送 #${result.id}，正在处理...`, 'success');
    }
    showSimpleToast('📜  消息已发送，处理中...', 'success');
  } catch (error) {
    console.error('发送消息失败:', error);
    showError('发送失败', error);
  } finally {
    setLoading(false);
    // 清除回复标志
    state.isReplyingMessage = false;
    // 🔧 Task#349：不在finally中清除 _isLocalReply，改由 handleMessageUpdate 中清除
    // 原因：finally先于WebSocket handleMessageUpdate执行，导致 _isLocalReply 保护窗口过短
    // handleMessageUpdate 中 isLocalCreation=true 时会检查 _isLocalReply 并清除
  }
}

/**
 * 切换系统提示词的展开/折叠状态
 * @param {number} taskId - 事务 ID
 * @param {HTMLElement} toggleEl - 触发点击的元素（h3标题）
 */
function toggleSystemPrompt(taskId, toggleEl) {
  const section = toggleEl.closest('.task-section');
  if (!section) return;

  const fullEl = section.querySelector('.system-prompt-full');
  const h3 = section.querySelector('.system-prompt-toggle');

  if (fullEl && h3) {
    if (fullEl.style.display === 'none') {
      // 显示完整系统提示词
      fullEl.style.display = '';
      // 更新标题：添加收起提示
      h3.innerHTML = h3.innerHTML.replace('🤖 系统提示词', '🤖 系统提示词（收起）') + '<span style="font-size:0.75em;color:#58a6ff;">(点击收起)</span>';
    } else {
      // 收起完整系统提示词
      fullEl.style.display = 'none';
      // 恢复标题：移除收起提示，恢复展开提示
      h3.innerHTML = h3.innerHTML.replace('🤖 系统提示词（收起）', '🤖 系统提示词').replace('(点击收起)', '(点击展开)');
    }
  }
}

