function _positionTooltip(el, targetEl) {
  const rect = targetEl.getBoundingClientRect ? targetEl.getBoundingClientRect() : { left: 0, top: 0, bottom: 0, right: 0, width: 0, height: 0 };
  const elW = el.offsetWidth;
  const elH = el.offsetHeight;
  // 默认显示在元素下方居中
  let left = rect.left + rect.width / 2 - elW / 2;
  let top = rect.bottom + 4;
  // 边界检查
  if (left < 4) left = 4;
  if (left + elW > window.innerWidth - 4) left = window.innerWidth - elW - 4;
  if (top + elH > window.innerHeight - 4) {
    top = rect.top - elH - 4;  // 改为显示在上方
  }
  el.style.left = left + 'px';
  el.style.top = top + 'px';
}

// ==================== 点击处理函数(带文本选择检查) ====================

// 从消息的 workdir 更新工作目录（用户主动切换消息时调用）
function _updateWorkdirFromMessage(msgId) {
  const msg = findMessageByIdRecursive(state.messages, msgId);
  if (msg && msg.workdir) {
    const workdirInput = document.getElementById('workdir-input');
    if (workdirInput) {
      workdirInput.value = toNativeDisplayPath(msg.workdir);
      validateAndSetWorkdir(msg.workdir);
    }
  }
}

/**
 * 🔧 在 validateAndSetWorkdir 之前同步 nest 状态（确保 state.remoteNest 正确）
 * 
 * 关键修复：选中消息/事务时，必须先设置 state.remoteNest，
 * 否则 validateAndSetWorkdir 会走错误的分支（本地 vs 远程），
 * 导致远程路径（如 d:\work\peng）被当作本地路径验证失败而回退。
 */
function _prepareNestState(nestName) {
  if (nestName && nestName !== 'local') {
    // 远程 nest 模式：设置 state.remoteNest 和 remoteNestRoot
    state.remoteNest = nestName;
    // 从缓存的 nests 数据中获取 workdir（如果有的话）
    const nests = window._latestNests || {};
    const nestInfo = nests[nestName];
    if (nestInfo && nestInfo.workdir) {
      state.remoteNestRoot = nestInfo.workdir;
    }
  } else if (!nestName || nestName === 'local') {
    // 本地模式：清除远程 nest 状态
    state.remoteNest = null;
    state.remoteNestRoot = null;
  }
}

// 从 nest 名称同步下拉框（选中消息/事务时调用）
// 双向同步：更改下拉框值会触发 change 事件 → /api/nest/select → 保存到 config.webui.nest
function _syncNestDropdown(nestName) {
  const targetNest = nestName || 'local';
  // 🔧 检查下拉列表中是否存在该 nest
  const items = document.querySelectorAll('#nest-dropdown-list .nest-dropdown-item');
  let found = false;
  for (const item of items) {
    if (item.dataset.value === targetNest) {
      found = true;
      break;
    }
  }
  if (found && typeof _syncNestDropdownValue === 'function') {
    _syncNestDropdownValue(targetNest);
  }
}

// 点击消息链首（可切换折叠）
async function selectMessageHead(msgId, event) {
  // 如果有文本被选中（用户正在划选），不触发任何操作
  if (hasSelectedText()) {
    return;
  }

  // 如果点击的是编辑图标或超链接（文件路径/图片链接），不触发折叠
  if (event && (event.target.classList.contains('edit-text-icon') || event.target.closest('a'))) {
    return;
  }

  // 如果已选中分支消息，点击树根应该清除分支选中并选中树根
  if (state.currentBranchMessageId !== null && state.currentMessageId === msgId) {
    await selectMessage(msgId);
    _updateWorkdirFromMessage(msgId);
    _syncNestDropdown(findMessageByIdRecursive(state.messages, msgId)?.nest);
    return;
  }

  // 如果点击的是已选中的消息，切换折叠状态
  if (state.currentMessageId === msgId) {
    // 确保切换到消息模式（selectTask 会设置 currentMessageId，导致再次点击根消息走此分支）
    switchInputMode('message');
    // 如果超过3秒才点击已选中的消息，不触发折叠状态改变（但仍更新时间戳，恢复快速点击能力）
    const msgLastTime = state.lastSelectedTimestamps['msg_' + msgId];
    if (msgLastTime && (Date.now() - msgLastTime > state.collapseToggleTimeout)) {
      state.lastSelectedTimestamps['msg_' + msgId] = Date.now();
      return;
    }
    // 更新时间戳，确保连续快速折叠/展开不会因超过3秒阈值而被阻止
    state.lastSelectedTimestamps['msg_' + msgId] = Date.now();
    toggleMessageCollapse(msgId);
    return;
  }

  // 否则选中该消息
  await selectMessage(msgId);
  _updateWorkdirFromMessage(msgId);
  _syncNestDropdown(findMessageByIdRecursive(state.messages, msgId)?.nest);
}

// 切换消息树节点的折叠状态并选中
async function toggleMessageNodeCollapseAndSelect(event, rootMsgId, msgId) {
  // 如果有文本被选中（用户正在划选），不触发任何操作
  if (hasSelectedText()) {
    return;
  }

  // 如果点击的是编辑图标或超链接（文件路径/图片链接），不触发折叠
  if (event && (event.target.classList.contains('edit-text-icon') || event.target.closest('a'))) {
    return;
  }

  // 阻止事件冒泡
  if (event) {
    event.stopPropagation();
  }

  // 如果点击的是已选中的分支消息，切换折叠状态
  if (state.currentBranchMessageId === msgId && state.currentMessageId === rootMsgId) {
    // 确保切换到消息模式
    switchInputMode('message');
    // 如果超过3秒才点击已选中的分支消息，不触发折叠状态改变（但仍更新时间戳，恢复快速点击能力）
    const branchMsgLastTime = state.lastSelectedTimestamps['branchMsg_' + msgId];
    if (branchMsgLastTime && (Date.now() - branchMsgLastTime > state.collapseToggleTimeout)) {
      // 恢复焦点到主输入框
      const mainInput = document.getElementById('message-input');
      if (mainInput) {
        mainInput.focus();
      }
      state.lastSelectedTimestamps['branchMsg_' + msgId] = Date.now();
      return;
    }
    // 更新时间戳，确保连续快速折叠/展开不会因超过3秒阈值而被阻止
    state.lastSelectedTimestamps['branchMsg_' + msgId] = Date.now();
    toggleMessageNodeCollapse(msgId);
    // 恢复焦点到主输入框（因为 stopPropagation 阻止了事件冒泡）
    const mainInput = document.getElementById('message-input');
    if (mainInput) {
      mainInput.focus();
    }
    return;
  }

  // 否则选中该消息
  await selectMessageBranch(rootMsgId, msgId, event);
}

/**
 * 选中分支消息（与selectTask保持一致的处理方式）
 *
 * @param {Number} rootMsgId - 根消息ID（用于选中根消息）
 * @param {Number} branchMsgId - 分支消息ID
 * @param {Event} event - 事件对象
 */
async function selectMessageBranch(rootMsgId, branchMsgId, event) {
  // 阻止事件冒泡
  if (event) {
    event.stopPropagation();
  }

  // 清除回复标志（用户主动切换消息）
  state.isReplyingMessage = false;

  // 记录选中的分支消息ID
  state.currentBranchMessageId = branchMsgId;
  state.isCreatingNewMessage = false;
  state.lastSelectedTimestamps['branchMsg_' + branchMsgId] = Date.now();

  // 🔧 获取分支消息
  const branchMsg = findMessageByIdRecursive(state.messages, branchMsgId);

  // 🔧 关键修复：先同步 nest 状态（设置 state.remoteNest），再设置 workdir
  // 否则远程 nest 路径（如 d:\work\peng）会被当作本地路径验证失败而回退
  _prepareNestState(branchMsg?.nest);

  // 🔧 获取分支消息的 workdir 并更新输入框（异步执行，不阻塞UI响应）
  if (branchMsg && branchMsg.workdir) {
    const workdirInput = document.getElementById('workdir-input');
    if (workdirInput) {
      workdirInput.value = toNativeDisplayPath(branchMsg.workdir);
      validateAndSetWorkdir(branchMsg.workdir);  // 不 await，避免阻塞消息切换
    }
  } else if (state.projectRoot) {
    // workdir 不存在，回退到项目目录
    const workdirInput = document.getElementById('workdir-input');
    if (workdirInput) {
      workdirInput.value = toNativeDisplayPath(state.projectRoot);
      validateAndSetWorkdir(state.projectRoot);
    }
  }

  // 🔧 同步 nest 下拉框 UI（从分支消息的 nest 字段）
  _syncNestDropdown(branchMsg?.nest);

  // 判断是否是同一消息树
  const isSameTree = state.currentMessageId === rootMsgId;

  // 更新根消息ID（如果不是同一消息树）
  if (!isSameTree) {
    state.currentMessageId = rootMsgId;
  }

  // 记录展开状态（不覆盖从数据库恢复的折叠状态）
  if (!state.collapsedMessages.has(rootMsgId)) {
    state.expandedMessages.set(rootMsgId, Date.now());
  }

  // 如果消息链是折叠状态且没有被手动折叠过，展开它
  const msgChain = document.querySelector(`.message-chain-head[data-id="${rootMsgId}"]`)?.closest('.message-chain');
  if (msgChain?.classList.contains('collapsed') && !state.collapsedMessages.has(rootMsgId)) {
    msgChain.classList.remove('collapsed');
  }

  // 更新UI：高亮选中的分支消息，清除其他
  document.querySelectorAll('.message-tree-node').forEach(el => {
    const isActive = el.dataset.msgId == branchMsgId;
    el.classList.toggle('active', isActive);

    // 即时添加/移除编辑图标
    const contentEl = el.querySelector('.tree-node-content');
    if (contentEl) {
      const existingEditIcon = contentEl.querySelector('.edit-text-icon');
      if (isActive && !existingEditIcon) {
        // 选中时添加编辑图标（分支消息）
        const rawText = contentEl.getAttribute('data-raw-text') || '';
        const editIcon = document.createElement('span');
        editIcon.className = 'edit-text-icon';
        editIcon.title = '编辑';
        editIcon.setAttribute('data-edit-type', 'branch-message');
        editIcon.setAttribute('data-edit-id', el.dataset.msgId);
        editIcon.setAttribute('data-root-msg-id', rootMsgId);
        editIcon.setAttribute('data-raw-text', rawText);
        editIcon.textContent = '📝';
        contentEl.appendChild(editIcon);
      } else if (!isActive && existingEditIcon) {
        // 未选中时移除编辑图标
        existingEditIcon.remove();
      }
    }
  });

  // 清除消息链的选中状态（分支消息选中时不显示根消息的选中状态）
  document.querySelectorAll('.message-chain-head').forEach(el => {
    el.classList.remove('active');
  });

  switchInputMode('message')

  // 如果是同一消息树，只更新placeholder，不做其他操作
  if (isSameTree) {
    updateInputPlaceholder(branchMsgId);
    // 恢复焦点到主输入框（因为 stopPropagation 阻止了事件冒泡，document 的 click 监听器不会触发）
    const mainInput = document.getElementById('message-input');
    if (mainInput) {
      mainInput.focus();
    }
    return;
  }
  // 先查找关联事务（需要在渲染前定义）
  const relatedTask = findTaskByMessageId(rootMsgId);

  // 更新关联事务的UI
  if (relatedTask) {
    state.currentTaskId = relatedTask.id;

    document.querySelectorAll('.task-chain').forEach(el => {
      const head = el.querySelector('.task-chain-head');
      if (head) {
        el.classList.toggle('active', head.dataset.id == relatedTask.id);
      }
    });
  } else {
    state.currentTaskId = null;
    document.querySelectorAll('.task-chain').forEach(el => {
      el.classList.remove('active');
    });
  }

  // 更新输入框提示
  updateInputPlaceholder(branchMsgId);

  // 更新输出过滤状态
  updateOutputTitle(relatedTask?.id || null, rootMsgId);

  // 恢复焦点到主输入框（因为 stopPropagation 阻止了事件冒泡，document 的 click 监听器不会触发）
  const mainInput = document.getElementById('message-input');
  if (mainInput) {
    mainInput.focus();
  }

  // 🔧 修复Task#219：保存旧事务的最新输出时间戳
  const prevTaskId3 = state._prevTaskIdForSwitch;
  const prevNewestTs3 = state.currentNewestTimestamp;
  if (prevTaskId3 != null && prevNewestTs3 != null) {
    state.lastViewedTimestamps.set(prevTaskId3, prevNewestTs3);
  }

  // 🔧 修复：切换时立即清空 xterm 并重置时间戳指针
  if (state.output) {
    state.output.clear(); state.currentColor = '';
  }
  state.currentOldestTimestamp = null;
  state.currentNewestTimestamp = null;
  // 🔧 修复#307：只在无记录时初始化为true，避免覆盖后端已确认的false值
  if (!state.hasMoreHistory.has(state.currentTaskId)) {
    state.hasMoreHistory.set(state.currentTaskId, true);
  }

  // 🔧 修复Task#219：切回时增量获取
  const newTaskId3 = state.currentTaskId;
  const hasCache3 = hasOutputHistoryCache(newTaskId3);
  const lastViewedTs3 = newTaskId3 != null ? state.lastViewedTimestamps.get(newTaskId3) : null;
  state._prevTaskIdForSwitch = newTaskId3;

  closeEditor();
  // 🔧 Fix Task#1433：有缓存时立即从缓存全量渲染（不延迟！）
  if (hasCache3 && newTaskId3 != null) {
    renderFromCache(newTaskId3);
  }
  // 🔧 异步订阅输出（不阻塞UI响应）
  if (hasCache3 && lastViewedTs3) {
    subscribeOutput(0, true, null, lastViewedTs3);
  } else {
    subscribeOutput(0, hasCache3);
  }
  closeEditor();

  // 🔧 Task#153：保存选中状态到后端配置（刷新页面后恢复）
  _saveSelectionToBackend();
}

// 递归展开事务树的所有子事务
function expandTaskTreeRecursively(taskId) {
  // 标记该事务树已完全展开
  state.fullyExpandedTasks.add(taskId);
  state.expandedTasks.set(taskId, Date.now());
  state.collapsedTasks.delete(taskId);

  // 查找该事务的所有子事务（递归）
  const expandChildren = (parentTask) => {
    if (parentTask.children && parentTask.children.length > 0) {
      for (const child of parentTask.children) {
        // 展开子事务
        state.expandedTasks.set(child.id, Date.now());
        state.collapsedTasks.delete(child.id);
        // 递归展开孙子事务
        expandChildren(child);
      }
    }
  };

  // 递归查找事务
  const findTaskByIdRecursive = (tasks, targetId) => {
    for (const task of tasks) {
      if (task.id === targetId) return task;
      if (task.children && task.children.length > 0) {
        const found = findTaskByIdRecursive(task.children, targetId);
        if (found) return found;
      }
    }
    return null;
  };

  // 从事务树中找到该事务并展开其所有子事务
  const task = findTaskByIdRecursive(state.tasks, taskId);
  if (task) {
    expandChildren(task);
  }
}

async function selectMessage(msgId) {
  // 🔍 调试日志

  // 清除回复标志（用户主动切换消息）
  state.isReplyingMessage = false;

  // 清除分支消息ID（用户点击了根消息，不再回复分支消息）
  state.currentBranchMessageId = null;

  state.currentMessageId = msgId;
  state.isCreatingNewMessage = false;
  state.lastSelectedTimestamps['msg_' + msgId] = Date.now();

  // 🔧 获取消息
  const msg = findMessageByIdRecursive(state.messages, msgId);

  // 🔧 关键修复：先同步 nest 状态（设置 state.remoteNest），再设置 workdir
  // 否则远程 nest 路径（如 d:\work\peng）会被当作本地路径验证失败而回退
  _prepareNestState(msg?.nest);

  // 🔧 获取消息的 workdir 并切换工作目录（自动选中时也切换后端状态）
  // 如果消息没有 workdir 或 workdir 路径不存在，回退到项目目录
  if (msg && msg.workdir) {
    const workdirInput = document.getElementById('workdir-input');
    if (workdirInput) {
      workdirInput.value = toNativeDisplayPath(msg.workdir);
    }
    validateAndSetWorkdir(msg.workdir);  // 切换后端工作目录
  } else if (state.projectRoot) {
    // workdir 不存在，回退到项目目录
    const workdirInput = document.getElementById('workdir-input');
    if (workdirInput) {
      workdirInput.value = toNativeDisplayPath(state.projectRoot);
    }
    validateAndSetWorkdir(state.projectRoot);
  }

  // 🔧 同步 nest 下拉框 UI（从消息的 nest 字段）
  _syncNestDropdown(msg?.nest);

  // 记录选中的消息和时间戳，但不覆盖从数据库恢复的折叠状态
  // 如果消息之前被手动折叠过，保持折叠状态；否则标记为展开
  if (!state.collapsedMessages.has(msgId)) {
    // 检查是否在折叠状态Map中（包括字符串版本）
    if (!state.collapsedMessages.has(String(msgId))) {
      state.expandedMessages.set(msgId, Date.now());
    }
  }

  // 如果消息链是折叠状态且没有被手动折叠过，直接展开（不调用renderMessageTree）
  // 注意：这里要尊重从数据库恢复的折叠状态，只有非手动折叠的消息才强制展开
  const msgChain = document.querySelector(`.message-chain-head[data-id="${msgId}"]`)?.closest('.message-chain');
  if (msgChain?.classList.contains('collapsed') && !state.collapsedMessages.has(msgId)) {
    msgChain.classList.remove('collapsed');
  }

  // 更新 UI：清除消息树节点选中状态，更新消息链选中状态
  document.querySelectorAll('.message-tree-node').forEach(el => {
    el.classList.remove('active');  // 清除所有消息树节点的选中状态
    // 移除消息树节点的复制图标
    const contentEl = el.querySelector('.tree-node-content');
    if (contentEl) {
      const existingCopyIcon = contentEl.querySelector('.edit-text-icon');
      if (existingCopyIcon) {
        existingCopyIcon.remove();
      }
    }
  });

  // 更新消息链的选中状态和复制图标
  document.querySelectorAll('.message-chain-head').forEach(el => {
    const isActive = el.dataset.id == msgId;
    el.classList.toggle('active', isActive);

    // 即时添加/移除复制图标
    const contentEl = el.querySelector('.message-chain-content');
    if (contentEl) {
      const existingCopyIcon = contentEl.querySelector('.edit-text-icon');
      const rawText = contentEl.getAttribute('data-raw-text') || '';
      const messageId = el.dataset.id;  // 获取消息ID
      if (isActive && !existingCopyIcon) {
        // 选中时添加编辑图标
        const editIcon = document.createElement('span');
        editIcon.className = 'edit-text-icon';
        editIcon.title = '编辑';
        editIcon.setAttribute('data-edit-type', 'message');
        editIcon.setAttribute('data-edit-id', messageId);
        editIcon.setAttribute('data-raw-text', rawText);
        editIcon.textContent = '📝';
        contentEl.appendChild(editIcon);
      } else if (!isActive && existingCopyIcon) {
        // 未选中时移除编辑图标
        existingCopyIcon.remove();
      }
    }
  });
  // 先查找关联事务（需要在渲染前定义）
  const relatedTask = findTaskByMessageId(msgId);

  // 关联事务已在前文查找，这里只更新UI
  if (relatedTask) {
    state.currentTaskId = relatedTask.id;

    document.querySelectorAll('.task-chain').forEach(el => {
      const head = el.querySelector('.task-chain-head');
      if (head) {
        el.classList.toggle('active', head.dataset.id == relatedTask.id);
      }
    });

    // 滚动事务列表使选中的事务可见
    const taskHead = document.querySelector(`.task-chain-head[data-id="${relatedTask.id}"]`);
    if (taskHead) {
      taskHead.scrollIntoView({ behavior: 'instant', block: 'nearest' });
    }
  } else {
    state.currentTaskId = null;
    document.querySelectorAll('.task-chain').forEach(el => {
      el.classList.remove('active');
    });
  }

  // 🔧 修复Task#219：切换时保存旧事务的最新输出时间戳，用于切回时增量获取
  const prevTaskId = state._prevTaskIdForSwitch;
  const prevNewestTs = state.currentNewestTimestamp;
  if (prevTaskId != null && prevNewestTs != null) {
    state.lastViewedTimestamps.set(prevTaskId, prevNewestTs);
  }

  // 🔧 修复：切换时立即清空 xterm 并重置时间戳指针
  // 确保切换后 xterm 不会残留旧事务的输出内容
  if (state.output) {
    state.output.clear(); state.currentColor = '';
  }
  state.userScrolledUp = false;  // 🔧 Task #92：重置滚动状态
  state._browsingHistory = false;  // 🔧 修复#305：切换消息时重置翻页状态，确保scrollToNewest生效
  state._pagingStartLine = -1;  // 🔧 Fix Task#840：清除翻页行号
  state._pagingEndLine = -1;
  state._totalLines = 0;  // 🔧 Fix Task#840：切换消息重置总行数
  state._pagingAnchorNewestTs = null;
  state._pagingPageNewestTs = null;
  state.currentOldestTimestamp = null;
  state.currentNewestTimestamp = null;
  // 🔧 Task#1119: 清除输出搜索状态
  if (typeof _clearSearch === 'function') _clearSearch();
  // 🔧 修复#307：只在无记录时初始化为true，避免覆盖后端已确认的false值
  if (!state.hasMoreHistory.has(state.currentTaskId)) {
    state.hasMoreHistory.set(state.currentTaskId, true);
  }

  // 🔧 修复Task#219：切回时用 sinceTime 增量获取切换期间的新输出
  // 如果有缓存且记录了上次查看的时间戳，用 sinceTime 只获取增量数据
  const newTaskId = state.currentTaskId;
  const hasCache = hasOutputHistoryCache(newTaskId);
  const lastViewedTs = newTaskId != null ? state.lastViewedTimestamps.get(newTaskId) : null;

  if (hasCache && lastViewedTs) {
    // 有缓存且知道上次看到哪里：请求增量数据（sinceTime）
    subscribeOutput(0, true, null, lastViewedTs);
  } else {
    // 无缓存或首次查看：正常订阅
    subscribeOutput(0, hasCache);
  }
  // 记录当前 taskId，下次切换时保存其时间戳
  state._prevTaskIdForSwitch = newTaskId;

  // 🔧 Fix Task#1433：有缓存时立即从缓存全量渲染（不延迟！）
  // 根因：之前用 setTimeout(scrollToNewest, 100) 延迟渲染，但在这100ms窗口内
  // _watch_loop 的实时输出通过 appendOutput 写入了几行并设置了 currentNewestTimestamp，
  // 导致 scrollToNewest 误判"已在最新"跳过缓存渲染，只显示几行实时输出。
  if (hasCache && newTaskId != null) {
    renderFromCache(newTaskId);
  }
  setTimeout(() => {
    // 延迟 scrollToBottom 以处理 fit() 异步链（不涉及内容渲染）
    if (hasCache && state.output) state.output.scrollToBottom();
    closeEditor();
    // 更新输入框提示
    updateInputPlaceholder(msgId);
    // 更新输出过滤状态
    updateOutputTitle(relatedTask?.id || null, msgId);
    switchInputMode('message');
  }, 100);

  // 🔧 Task#153：保存选中状态到后端配置（刷新页面后恢复）
  _saveSelectionToBackend();
}

// 切换消息折叠状态
async function toggleMessageCollapse(msgId) {
  const now = Date.now();

  // 查找当前消息元素
  const msgChain = document.querySelector(`.message-chain-head[data-id="${msgId}"]`)?.closest('.message-chain');

  if (msgChain?.classList.contains('collapsed')) {
    // 当前是折叠状态，切换为展开：记录展开状态，清除折叠状态
    state.expandedMessages.set(msgId, now);
    state.collapsedMessages.delete(msgId);
    // 调用API保存到数据库
    try {
      await fetch(`/api/messages/${msgId}/collapsed?collapsed=false`, {
        method: 'POST'
      });
    } catch (e) {
      console.warn(`保存消息 ${msgId} 折叠状态失败:`, e);
    }
  } else {
    // 当前是展开状态，切换为折叠：记录折叠状态，清除展开状态
    state.collapsedMessages.set(msgId, now);
    state.expandedMessages.delete(msgId);
    // 调用API保存到数据库
    try {
      await fetch(`/api/messages/${msgId}/collapsed?collapsed=true`, {
        method: 'POST'
      });
    } catch (e) {
      console.warn(`保存消息 ${msgId} 折叠状态失败:`, e);
    }
  }

  // 使用 requestAnimationFrame 延迟渲染，避免阻塞 UI
  requestAnimationFrame(() => {
    renderMessageTree();
  });

  // 保存折叠展开状态到 localStorage（用于消息树节点）
  saveCollapseStates();
}

// ==================== 消息临时展开功能（展开链接） ====================

/**
 * 切换消息/事务的临时展开/收起状态
 * @param {Event} event - 点击事件
 * @param {number} msgId - 消息 ID 或事务 ID
 * @param {string} type - 类型：'chain'（消息链）, 'node'（消息树节点）, 'task'（事务）
 */
/**
 * 从 content 元素中提取发送者前缀的 HTML（<span class="sender-*">...</span>）
 * @param {HTMLElement} contentEl - 内容容器元素
 * @returns {string} 发送者前缀 HTML，无发送者时返回空字符串
 */
function extractSenderPrefixHtml(contentEl) {
  const senderSpan = contentEl.querySelector('.sender-master, .sender-peng');
  if (senderSpan) {
    return senderSpan.outerHTML;
  }
  return '';
}

/**
 * 展开/收起消息内容后，如果该消息/事务处于选中状态，重新添加编辑图标
 * （因为 toggleMessageExpand 中设置 innerHTML 会覆盖掉已有的编辑图标）
 */
function restoreEditIconIfNeeded(contentEl, msgId, type, containerEl) {
  let shouldShowEditIcon = false;
  let editType = '';
  let rootMsgId = '';

  if (type === 'chain') {
    // 消息链：当前选中消息且没有选中分支消息时显示编辑图标
    shouldShowEditIcon = state.currentMessageId === msgId && !state.currentBranchMessageId;
    editType = 'message';
  } else if (type === 'node') {
    // 分支消息树节点：当前选中的分支消息时显示编辑图标
    rootMsgId = containerEl.getAttribute('data-root-msg-id') || '';
    shouldShowEditIcon = state.currentMessageId == rootMsgId && state.currentBranchMessageId == msgId;
    editType = 'branch-message';
  } else if (type === 'task') {
    // 事务：当前选中的事务时显示编辑图标
    shouldShowEditIcon = state.currentTaskId === msgId;
    editType = 'task';
  }

  if (shouldShowEditIcon) {
    const rawText = contentEl.getAttribute('data-raw-text') || '';
    const editIcon = document.createElement('span');
    editIcon.className = 'edit-text-icon';
    editIcon.title = '编辑';
    editIcon.setAttribute('data-edit-type', editType);
    editIcon.setAttribute('data-edit-id', msgId);
    editIcon.setAttribute('data-raw-text', rawText);
    if (editType === 'branch-message' && rootMsgId) {
      editIcon.setAttribute('data-root-msg-id', rootMsgId);
    }
    editIcon.textContent = '📝';
    contentEl.appendChild(editIcon);
  }
}

function toggleMessageExpand(event, msgId, type) {
  event.stopPropagation();
  event.preventDefault();

  let selector;
  if (type === 'chain') {
    selector = `.message-chain[data-msg-id="${msgId}"]`;
  } else if (type === 'node') {
    selector = `.message-tree-node[data-msg-id="${msgId}"]`;
  } else if (type === 'task') {
    selector = `.task-chain[data-task-id="${msgId}"]`;
  }

  const containerEl = document.querySelector(selector);

  if (!containerEl) return;

  const contentEl = containerEl.querySelector('.message-chain-content, .tree-node-content, .task-chain-content');
  if (!contentEl) return;

  const isExpanded = containerEl.classList.contains('temp-expanded');
  const rawText = contentEl.dataset.rawText;

  // 先提取发送者前缀（展开前提取，避免innerHTML被覆盖后丢失）
  const senderPrefix = (type !== 'task') ? extractSenderPrefixHtml(contentEl) : '';

  if (isExpanded) {
    // 收起：恢复截断内容
    containerEl.classList.remove('temp-expanded');
    if (type === 'task') {
      // 事务不需要发送者前缀
      const truncated = truncateText(rawText, null, msgId, type, true);
      contentEl.innerHTML = convertFilePathsToHtmlLinks(truncated);
    } else {
      // 消息需要发送者前缀
      const truncated = truncateText(rawText, null, msgId, type, true);
      contentEl.innerHTML = senderPrefix + convertFilePathsToHtmlLinks(truncated);
    }
  } else {
    // 展开：显示完整原始内容（使用 convertFilePathsToHtmlLinks 转换文件路径为超链接）
    containerEl.classList.add('temp-expanded');
    if (type === 'task') {
      // 事务不需要发送者前缀
      const fullContent = convertFilePathsToHtmlLinks(rawText);
      contentEl.innerHTML = fullContent;
    } else {
      // 消息需要发送者前缀
      const fullContent = convertFilePathsToHtmlLinks(rawText);
      contentEl.innerHTML = senderPrefix + fullContent;
    }
  }

  // 展开/收起后，如果该消息/事务处于选中状态，重新添加编辑图标（innerHTML覆盖会丢失编辑图标）
  restoreEditIconIfNeeded(contentEl, msgId, type, containerEl);
}

/**
 * 收起所有临时展开的消息和事务
 */
function collapseAllTempExpandedMessages() {
  // 收起消息链
  document.querySelectorAll('.message-chain.temp-expanded').forEach(el => {
    el.classList.remove('temp-expanded');
    const msgId = el.dataset.msgId;
    const contentEl = el.querySelector('.message-chain-content');
    if (msgId && contentEl) {
      const rawText = contentEl.dataset.rawText;
      const senderPrefix = extractSenderPrefixHtml(contentEl);
      const truncated = truncateText(rawText, null, msgId, 'chain', true);
      contentEl.innerHTML = senderPrefix + convertFilePathsToHtmlLinks(truncated);
      restoreEditIconIfNeeded(contentEl, msgId, 'chain', el);
    }
  });
  // 收起消息树节点
  document.querySelectorAll('.message-tree-node.temp-expanded').forEach(el => {
    el.classList.remove('temp-expanded');
    const msgId = el.dataset.msgId;
    const contentEl = el.querySelector('.tree-node-content');
    if (msgId && contentEl) {
      const rawText = contentEl.dataset.rawText;
      const senderPrefix = extractSenderPrefixHtml(contentEl);
      const truncated = truncateText(rawText, null, msgId, 'node', true);
      contentEl.innerHTML = senderPrefix + convertFilePathsToHtmlLinks(truncated);
      restoreEditIconIfNeeded(contentEl, msgId, 'node', el);
    }
  });
  // 收起事务
  document.querySelectorAll('.task-chain.temp-expanded').forEach(el => {
    el.classList.remove('temp-expanded');
    const taskId = el.dataset.taskId;
    const contentEl = el.querySelector('.task-chain-content');
    if (taskId && contentEl) {
      const rawText = contentEl.dataset.rawText;
      const truncated = truncateText(rawText, null, taskId, 'task', true);
      contentEl.innerHTML = convertFilePathsToHtmlLinks(truncated);
      restoreEditIconIfNeeded(contentEl, taskId, 'task', el);
    }
  });
}

// 切换消息树节点的折叠状态
async function toggleMessageNodeCollapse(msgId) {
  const now = Date.now();

  const isCurrentlyCollapsed = state.collapsedMessageNodes.has(msgId);
  const newCollapsedState = !isCurrentlyCollapsed;

  if (isCurrentlyCollapsed) {
    // 当前是折叠状态，切换为展开
    state.expandedMessageNodes.set(msgId, now);
    state.collapsedMessageNodes.delete(msgId);
  } else {
    // 当前是展开状态，切换为折叠
    state.collapsedMessageNodes.set(msgId, now);
    state.expandedMessageNodes.delete(msgId);
  }

  // 保存当前选中的分支消息ID（渲染后需要恢复）
  const currentBranchMessageId = state.currentBranchMessageId;

  // 使用 requestAnimationFrame 延迟渲染，避免阻塞 UI
  requestAnimationFrame(() => {
    renderMessageTree(true); // 强制全量渲染

    // 渲染后恢复分支消息的选中状态和复制图标
    if (currentBranchMessageId !== null) {
      document.querySelectorAll('.message-tree-node').forEach(el => {
        const isActive = el.dataset.msgId == currentBranchMessageId;
        el.classList.toggle('active', isActive);

        // 即时添加/移除复制图标
        const contentEl = el.querySelector('.tree-node-content');
        if (contentEl) {
          const existingCopyIcon = contentEl.querySelector('.edit-text-icon');
          if (isActive && !existingCopyIcon) {
            // 选中时添加复制图标
            const rawText = contentEl.getAttribute('data-raw-text') || '';
            const copyIcon = document.createElement('span');
            copyIcon.className = 'edit-text-icon';
            copyIcon.title = '编辑';
            copyIcon.setAttribute('data-raw-text', rawText);
            copyIcon.textContent = '📝';
            contentEl.appendChild(copyIcon);
          } else if (!isActive && existingCopyIcon) {
            // 未选中时移除复制图标
            existingCopyIcon.remove();
          }
        }
      });
      // 清除消息链的选中状态（不显示根消息的选中状态）
      document.querySelectorAll('.message-chain-head').forEach(el => {
        el.classList.remove('active');
        // 移除消息链的复制图标
        const contentEl = el.querySelector('.message-chain-content');
        if (contentEl) {
          const existingCopyIcon = contentEl.querySelector('.edit-text-icon');
          if (existingCopyIcon) {
            existingCopyIcon.remove();
          }
        }
      });
    }
  });

  // 异步保存折叠展开状态到 localStorage（不阻塞UI）
  setTimeout(() => {
    try {
      const collapsedMessageNodesObj = Object.fromEntries(state.collapsedMessageNodes);
      const expandedMessageNodesObj = Object.fromEntries(state.expandedMessageNodes);
      localStorage.setItem('peng-collapsed-message-nodes', JSON.stringify(collapsedMessageNodesObj));
      localStorage.setItem('peng-expanded-message-nodes', JSON.stringify(expandedMessageNodesObj));
    } catch (e) {
      console.error('保存折叠展开状态到localStorage失败:', e);
    }
  }, 0);

  // 调用API保存到数据库
  try {
    await fetch(`/api/messages/${msgId}/collapsed?collapsed=${newCollapsedState}`, {
      method: 'POST'
    });
  } catch (e) {
    console.warn(`保存消息节点 ${msgId} 折叠状态失败:`, e);
  }
}

// 完全展开消息树节点（递归展开所有子节点）
async function expandMessageNodeFully(msgId) {
  const now = Date.now();
  const visited = new Set(); // 防止循环引用

  // 递归查找并展开所有子节点
  const expandRecursive = (node) => {
    if (!node || visited.has(node.message.id)) return;
    visited.add(node.message.id);

    // 展开当前节点
    state.expandedMessageNodes.set(node.message.id, now);
    state.collapsedMessageNodes.delete(node.message.id);

    // 递归展开子节点
    if (node.children && node.children.length > 0) {
      for (const child of node.children) {
        expandRecursive(child);
      }
    }
  };

  // 在消息树中查找并展开
  for (const msg of state.messages) {
    if (msg.tree && msg.tree.root) {
      expandRecursive(msg.tree.root);
    }
  }

  // 使用 requestAnimationFrame 延迟渲染
  requestAnimationFrame(() => {
    renderMessageTree(true); // 强制全量渲染
  });

  // 异步保存到 localStorage
  setTimeout(() => {
    try {
      const collapsedMessageNodesObj = Object.fromEntries(state.collapsedMessageNodes);
      const expandedMessageNodesObj = Object.fromEntries(state.expandedMessageNodes);
      localStorage.setItem('peng-collapsed-message-nodes', JSON.stringify(collapsedMessageNodesObj));
      localStorage.setItem('peng-expanded-message-nodes', JSON.stringify(expandedMessageNodesObj));
    } catch (e) {
      console.error('保存展开状态失败:', e);
    }
  }, 0);
}

// 完全展开消息链（根消息）
async function expandMessageChainFully(msgId) {
  const now = Date.now();

  // 展开根消息
  state.expandedMessages.set(msgId, now);
  state.collapsedMessages.delete(msgId);

  // 递归展开所有消息树节点
  await expandMessageNodeFully(msgId);

  // 调用API保存到数据库
  try {
    await fetch(`/api/messages/${msgId}/collapsed?collapsed=false`, {
      method: 'POST'
    });
  } catch (e) {
    console.warn(`保存消息 ${msgId} 展开状态失败:`, e);
  }
}

// 完全展开事务树（递归展开所有子事务）
async function expandTaskFully(taskId) {
  const now = Date.now();

  // 递归查找并展开所有子事务
  const expandRecursive = (task) => {
    if (!task) return;

    // 展开当前事务
    state.expandedTasks.set(task.id, now);
    state.collapsedTasks.delete(task.id);

    // 递归展开子事务
    if (task.children && task.children.length > 0) {
      for (const child of task.children) {
        expandRecursive(child);
      }
    }
  };

  // 在事务树中查找并展开
  const findAndExpand = (tasks) => {
    for (const task of tasks) {
      if (task.id === taskId) {
        expandRecursive(task);
        return true;
      }
      if (task.children && task.children.length > 0) {
        if (findAndExpand(task.children)) {
          return true;
        }
      }
    }
    return false;
  };

  findAndExpand(state.tasks);

  // 重新渲染事务树
  renderTaskTree();

  // 保存到数据库（异步，不阻塞UI）
  setTimeout(async () => {
    const expandedTasks = Array.from(state.expandedTasks.keys());
    for (const tId of expandedTasks) {
      try {
        await fetch(`/api/tasks/${tId}/collapsed?collapsed=false`, {
          method: 'POST'
        });
      } catch (e) {
        console.warn(`保存事务 ${tId} 展开状态失败:`, e);
      }
    }
  }, 0);
}

