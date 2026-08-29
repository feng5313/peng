function passFilters(source) {
  // 明确的来源(thinking, bash, tool_call, tool_result, system)分别控制各自的来源
  const explicitSources = ['thinking', 'bash', 'tool_call', 'tool_result', 'system'];
  if (explicitSources.includes(source)) {
    // 如果该来源未勾选，则过滤掉
    if (!state.outputSourceFilters[source]) {
      return false;  // 过滤掉禁用的来源
    }
  } else {
    // 其他所有来源(包括未知source和task)由"事务输出"控制
    // 如果"事务输出"未勾选，则过滤掉
    if (!state.outputSourceFilters['task']) {
      return false;  // 过滤掉禁用的来源
    }
  }
  return true;
}

function appendOutput(content, source = 'output', taskId = null, messageId = null, timestamp = null, fileLineNo = null) {
  // 获取该 taskId 的历史
  const history = getOutputHistory(taskId);

  // 🔧 Fix Task#1008：防重复 —— 行号+时间戳双重去重
  // 存在两条输出路径（_watch_loop 实时推送 + output_batch 增量批量推送），
  // 竞态条件下同一条输出可能通过两条路径都到达 appendOutput，导致 xterm 重复显示。
  // fileLineNo 是文件中的全局唯一行号且单调递增，history 最后一条总是最大行号。
  if (history.length > 0) {
    const last = history[history.length - 1];
    // 主去重：fileLineNo <= 已有最大行号 → 已通过其他路径接收过，跳过
    if (fileLineNo != null && last.fileLineNo != null && fileLineNo <= last.fileLineNo) {
      return;
    }
    // 备用去重：fileLineNo 不可用时，用 timestamp + content 完全匹配判断
    if (timestamp && last.timestamp === timestamp && last.content === content) {
      return;
    }
  }

  // 存储到历史 - 只存储到对应 taskId 的历史中
  const outputItem = {
    content,
    source,
    taskId: taskId || 0,
    messageId,
    timestamp: timestamp,
    fileLineNo: fileLineNo,
  };

  history.push(outputItem);

  // 🔧 Fix #1064: 保持 _totalLines 与实际输出行数同步
  // appendOutput 走的是 'output' WebSocket消息路径（非 output_batch），
  // 此路径不携带 totalLines，必须在此更新，否则 getPageInfo 页数计算错误、
  // loadNextPage 的 isLastPage 判断错误，导致翻页混乱和无法回到尾部
  if (fileLineNo && fileLineNo > state._totalLines) {
    state._totalLines = fileLineNo;
  }

  const isCurrentTask = state.currentTaskId === taskId;
  if (!isCurrentTask) {
    // 非当前事务的输出：只缓存，不显示
    return;
  }

  // 🔧 Fix Task#1108: 安全检查 —— 打破 userScrolledUp 死锁
  // 当 buffer 达到 scrollback 上限时，buffer.length 不随 write() 变化，
  // _lastBufferLength 检测失效 → onScroll 可能误设 userScrolledUp=true。
  // 一旦误设，appendOutput 跳过写入 → 无 scroll 事件 → userScrolledUp 永不重置 → 死锁。
  // 修复：如果 userScrolledUp=true 但终端实际在底部（且非翻页模式），重置为 false。
  if (state.userScrolledUp && !state._browsingHistory && state.output && isExactlyAtBottom()) {
    state.userScrolledUp = false;
  }

  // 🔧 修复Task#219：重构 shouldAppend 判断逻辑
  // 🔧 Task#1725：在底部时永远写入（消灭"底部不更新"）——
  // userScrolledUp 只应在用户真正上移视口时阻止写入；isExactlyAtBottom 说明视口就在底部，
  // 即使存在残留的 userScrolledUp（时间窗口竞态），也必须写入，否则底部卡死不更新。
  const atExactBottom = state.output && isExactlyAtBottom();
  const shouldSkipWrite = state._browsingHistory || (state.userScrolledUp && !atExactBottom);

  // 更新时间戳指针：只在非翻页模式下更新（currentNewestTimestamp 表示"当前显示的最新"）
  if (!state._browsingHistory && !state.userScrolledUp) {
    if (state.currentNewestTimestamp === null || outputItem.timestamp > state.currentNewestTimestamp) {
      state.currentNewestTimestamp = outputItem.timestamp;
    }
  }
  // 🔧 修复：首次接收实时输出时，初始化 currentOldestTimestamp
  if (state.currentOldestTimestamp === null) {
    state.currentOldestTimestamp = outputItem.timestamp;
  }

  if (shouldSkipWrite) {
    // 🔧 修复：翻页浏览历史时，不更新分页按钮状态（避免干扰翻页逻辑）
    if (!state._browsingHistory) {
      updatePaginationButtons();
    }
    return;
  }

  // 写入 xterm 终端（自动处理 ANSI 颜色）
  if (passFilters(source)) {
    // 🔧 Fix #1064: 不再使用 _pagingRecoveryUntil（时间窗口在持续输出场景下永远激活，
    // 会禁用所有用户滚动检测）。改为依赖 onScroll 中的 buffer长度追踪：
    // write() 使 buffer 增长 → onScroll 检测到 buffer.length 变化 → 跳过（不误设 userScrolledUp）
    // 用户滚动 → onScroll 检测到 buffer.length 未变 → 正常设置 userScrolledUp
    state.output.write(formatLineNoPrefix(fileLineNo) + convertFilePathsToLinks(content) + '\r\n');
    // 🔧 Task#1725：更新 xterm 实际内容最新行号（回底时判定内容是否落后）
    if (fileLineNo && fileLineNo > (state._xtermNewestLineNo || 0)) {
      state._xtermNewestLineNo = fileLineNo;
    }
    // 🔧 修复：写入后确保滚动到底部，防止buffer增长后viewport位置滞后导致isAtBottom误判
    // userScrolledUp已在上面检查为false，此处安全滚动
    // 🔧 Fix #1428: rAF 回调可能在用户滚轮事件之后才执行，
    // 此时 userScrolledUp 可能已被 wheel handler 设置为 true，
    // 必须在 rAF 中重新检查（而非依赖上面的检查结果）
    requestAnimationFrame(() => {
      if (state.output && !state.userScrolledUp
          && !(state._userScrollIntentUntil && Date.now() < state._userScrollIntentUntil)) {
        state.output.scrollToBottom();
      }
    });
  }
}

function updateOutputTitle(taskId, messageId) {
  // 只更新状态文本，不覆盖整个header（保留操作按钮）

  let statusText = '选择话题查看输出';  // 默认提示（/new 后的状态）

  if (messageId !== null && messageId !== undefined) {
    if (taskId !== null && taskId !== undefined) {
      statusText = `消息📜${messageId} 事务🚩${taskId}的信息输出`;
    } else {
      statusText = `消息📜${messageId}的信息输出`;
    }
  }

  // 更新所有布局的输出头部信息（使用id精确匹配）
  ['output-msg-info-default', 'output-msg-info-2', 'output-msg-info-3'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.textContent = statusText;
  });
}

function updateConnectionStatus(status) {
  const statusElement = document.getElementById('connection-status');
  if (statusElement) {
    statusElement.textContent = status;
  }
}

// ==================== 消息管理 ====================

// 消息数据缓存（用于增量更新比较）
let lastMessagesData = null;

// 从消息数据中剔除易变字段，用于变更检测比较
// heartbeat_at: 心跳时间戳，处理中的消息每秒更新
// locked_at: 锁定时间戳
// locked_by: 锁定者
// 这些字段不影响UI展示，但会导致JSON比较永远不相等
function stripVolatileFieldsForCompare(data) {
  const volatileKeys = ['heartbeat_at', 'locked_at', 'locked_by'];
  return JSON.stringify(data, (key, value) => {
    if (volatileKeys.includes(key)) return undefined;
    return value;
  });
}

/**
 * 加载消息列表
 * @param {boolean} forceRefresh - 强制刷新（跳过数据比较）
 * @param {boolean} fullData - 为true时获取完整数据（含tree），用于首次加载和手动操作后
 *                            默认false：轮询模式，只获取元数据（排除tree），大幅减少传输量
 */
async function loadMessages(forceRefresh = false, fullData = false) {
  try {
    // 搜索模式下（有关键词）强制包含已归档消息，否则根据 showArchived 状态决定
    const includeArchived = state._searchIncludedArchived || state.showArchived;

    // 🔧 Task#249：性能优化 — 区分轮询模式和完整加载模式
    // - 轮询模式(fullData=false)：exclude_tree=true，只获取消息元数据（几KB），用于变更检测
    // - 完整模式(fullData=true)：获取含tree的完整数据（100KB+），用于首次加载和手动操作
    // 检测到消息变化后，自动触发一次完整加载以获取tree数据
    let url = '/api/messages';
    const params = [];
    if (includeArchived) {
      params.push('include_archived=true', 'limit=999');
    }
    if (!fullData) {
      params.push('exclude_tree=true');
    }
    if (params.length > 0) {
      url += '?' + params.join('&');
    }

    const response = await fetch(url);
    if (!response.ok) throw new Error('获取消息失败');

    const messages = await response.json();

    // 🔧 Task#249：轮询模式(exclude_tree)的轻量级变更检测
    // 只比较消息元数据（id/status/timestamp/sender/content），不含tree
    if (!fullData) {
      // 轻量级签名：只用id、status、timestamp、sender、content、is_archived
      // 避免对整个消息列表做JSON.stringify（含tree时100KB+）
      const oldIds = new Set((state.messages || []).map(m => m.id));
      const newIds = new Set(messages.map(m => m.id));

      // 快速路径：ID集合完全相同且无forceRefresh，检查元数据是否变化
      let hasChanges = forceRefresh;
      if (!hasChanges) {
        if (oldIds.size !== newIds.size) {
          hasChanges = true;
        } else {
          // ID集合相同，逐条比较关键字段
          const oldMsgMap = new Map((state.messages || []).map(m => [m.id, m]));
          for (const msg of messages) {
            const old = oldMsgMap.get(msg.id);
            if (!old || old.status !== msg.status ||
                old.timestamp !== msg.timestamp ||
                old.sender !== msg.sender ||
                old.content !== msg.content ||
                old.is_archived !== msg.is_archived) {
              hasChanges = true;
              break;
            }
          }
        }
      }

      if (hasChanges) {
        // 检测到变化，触发一次完整加载（含tree）
        console.log('[Task#249] 轮询检测到消息变化，触发完整加载');
        await loadMessages(forceRefresh, true);
      }
      // 无变化则跳过，节省CPU
      return;
    }

    // 以下是完整加载模式（fullData=true）的逻辑
    // 比较数据，只有变化时才更新DOM（避免闪烁）
    const messagesJson = stripVolatileFieldsForCompare(messages);
    if (forceRefresh || messagesJson !== lastMessagesData) {
      // 保存旧的消息ID列表（用于检测是否首次加载）
      const oldMessageIds = new Set(state.messages.map(m => m.id));

      state.messages = messages;
      lastMessagesData = messagesJson;

      // 清理过期的展开记录（超过1小时的选中记录）
      cleanupExpandedMessages(messages);

      // 渲染消息树
      renderMessageTree();

      // 检查消息列表是否为空，如果为空则清空xterm和状态
      if (messages.length === 0) {
        state.currentMessageId = null;
        state.currentTaskId = null;
        state.currentBranchMessageId = null;
        state.isCreatingNewMessage = false;
        if (state.output) {
          state.output.clear(); state.currentColor = '';
        }
        updateOutputTitle(null, null);
        updateInputPlaceholder(null);
        return;  // 消息列表为空，不再执行后续逻辑
      }

      // 恢复选中状态（因为重新渲染会清除选中状态）
      // 1. 如果选中的是分支消息，恢复分支消息的选中状态
      if (state.currentBranchMessageId) {
        document.querySelectorAll('.message-tree-node').forEach(el => {
          el.classList.toggle('active', el.dataset.msgId == state.currentBranchMessageId);
        });
        // 清除消息链的选中状态（不显示根消息的选中状态）
        document.querySelectorAll('.message-chain-head').forEach(el => {
          el.classList.remove('active');
        });
      }
      // 2. 如果选中的是根消息，恢复根消息的选中状态
      else if (state.currentMessageId) {
        document.querySelectorAll('.message-chain-head').forEach(el => {
          el.classList.toggle('active', el.dataset.id == state.currentMessageId);
        });
        // 清除消息树节点的选中状态
        document.querySelectorAll('.message-tree-node').forEach(el => {
          el.classList.remove('active');
        });
      }

      // 只在首次加载（没有旧消息）时自动选中最新消息
      // 其他时候不自动选中，避免干扰用户
      // 🔧 Task#153：如果有配置中的选中消息待恢复，跳过自动选中（由 DOMContentLoaded 统一处理）
      if (oldMessageIds.size === 0 && state.currentMessageId === null && messages.length > 0 && !state.selectedMessageIdFromConfig) {
        const sorted = [...messages].sort((a, b) =>
          new Date(b.timestamp || b.created_at) - new Date(a.timestamp || a.created_at)
        );
        if (sorted.length > 0) {
          selectMessage(sorted[0].id);  // 不 await，避免 subscribeOutput 等待 WebSocket 阻塞
        }
      }
    }
  } catch (error) {
    console.error('加载消息失败:', error);
    // 不显示错误toast，避免频繁打扰
  }
}

// 清理过期的展开/折叠记录
function cleanupExpandedMessages(messages) {
  const messageIds = new Set(messages.map(m => m.id));

  // 只移除不存在的消息记录，保留用户的主动操作（展开或折叠）
  for (const msgId of state.expandedMessages.keys()) {
    if (!messageIds.has(msgId)) {
      state.expandedMessages.delete(msgId);
    }
  }
  for (const msgId of state.collapsedMessages.keys()) {
    if (!messageIds.has(msgId)) {
      state.collapsedMessages.delete(msgId);
    }
  }
}

function renderMessageTree(forceFullRender = false) {
  const container = document.getElementById('message-tree');
  if (!container) return;

  const rawSearchTerm = (state._messageSearchTerm || state.searchTerm || '').trim();

  // 过滤消息：有关键词时搜索所有消息（包括已归档），无关键词时只显示未归档
  let filteredMessages;
  if (rawSearchTerm) {
    // 解析搜索语法：空格=AND，|=OR
    const { andTerms, orTerms } = parseSearchTerms(rawSearchTerm);

    filteredMessages = state.messages.filter(msg => {
      // 获取搜索文本（主消息和回复）
      const texts = getSearchableTexts(msg);

      // OR 条件：满足任一 OR 关键词
      let orMatch = true;
      if (orTerms.length > 0) {
        orMatch = orTerms.some(term =>
          texts.some(text => text.toLowerCase().includes(term.toLowerCase()))
        );
      }

      // AND 条件：满足所有 AND 关键词
      let andMatch = true;
      if (andTerms.length > 0) {
        andMatch = andTerms.every(term =>
          texts.some(text => text.toLowerCase().includes(term.toLowerCase()))
        );
      }

      return andMatch && orMatch;
    });
  } else {
    // 无关键词：根据 showArchived 标志决定是否显示已归档消息
    if (state.showArchived) {
      filteredMessages = state.messages;  // 显示所有消息
    } else {
      filteredMessages = state.messages.filter(msg => !msg.is_archived);  // 只显示未归档消息
    }
  }

  // 保存当前显示的消息ID列表（供事务过滤使用）
  state.displayedMessageIds = new Set(filteredMessages.map(m => m.id));
  // 标记消息树已渲染过（loadTasks 据此决定是否使用前端消息ID过滤事务）
  state._messageTreeRendered = true;

  if (filteredMessages.length === 0) {
    container.innerHTML = rawSearchTerm
      ? `<div class="loading">未找到匹配 "${escapeHtml(state.searchTerm)}" 的消息</div>`
      : '<div class="loading">暂无消息</div>';
    return;
  }

  // 按时间从新到老排序（上新下旧），但消息#1始终在最上面
  // 🔧 Task#228：顶置消息排在#1之后，其余按时间排序
  const message1 = filteredMessages.find(m => m.id === 1);
  const otherMessages = filteredMessages.filter(m => m.id !== 1);
  const sortedOthers = otherMessages.sort((a, b) =>
    new Date(b.timestamp || b.created_at) - new Date(a.timestamp || a.created_at)
  );

  // 分离顶置和非顶置消息（保持时间排序）
  const toppedOthers = sortedOthers.filter(m => state.toppedMessageIds.has(m.id));
  const nonToppedOthers = sortedOthers.filter(m => !state.toppedMessageIds.has(m.id));
  const sorted = message1 ? [message1, ...toppedOthers, ...nonToppedOthers] : [...toppedOthers, ...nonToppedOthers];

  // 高亮关键词
  const highlightTerms = [...(rawSearchTerm ? parseSearchTerms(rawSearchTerm).andTerms : []), ...(rawSearchTerm ? parseSearchTerms(rawSearchTerm).orTerms : [])];

  // 检查是否需要全量渲染（首次加载、容器为空、或搜索词变化、或强制全量渲染）
  const existingMessages = container.querySelectorAll('.message-chain-head');
  const lastSearchTerm = container.dataset.searchTerm || '';
  const searchChanged = lastSearchTerm !== rawSearchTerm;

  if (forceFullRender || existingMessages.length === 0 || searchChanged) {
    // 全量渲染
    container.dataset.searchTerm = rawSearchTerm;
    container.innerHTML = sorted.map(msg => renderMessageChain(msg, highlightTerms)).join('');
    return;
  }

  // 增量更新：只更新有变化的消息
  const existingIds = new Set();
  existingMessages.forEach(el => {
    existingIds.add(parseInt(el.dataset.id));
  });

  const newIds = new Set(sorted.map(m => m.id));

  // 删除不存在的消息
  existingIds.forEach(id => {
    if (!newIds.has(id)) {
      const el = container.querySelector(`.message-chain-head[data-id="${id}"]`);
      if (el) {
        const chain = el.closest('.message-chain');
        if (chain) chain.remove();
      }
    }
  });

  // 更新或添加消息（按倒序插入）
  sorted.forEach((msg, index) => {
    const existing = container.querySelector(`.message-chain-head[data-id="${msg.id}"]`);
    if (existing) {
      // 更新现有消息
      updateMessageElement(existing, msg, highlightTerms);
    } else {
      // 添加新消息（插入到正确位置）
      const html = renderMessageChain(msg, highlightTerms);
      if (index === 0) {
        container.insertAdjacentHTML('afterbegin', html);
      } else {
        // 找到前一个消息，插入到它后面
        const prevMsg = sorted[index - 1];
        const prevEl = container.querySelector(`.message-chain-head[data-id="${prevMsg.id}"]`);
        if (prevEl) {
          prevEl.closest('.message-chain').insertAdjacentHTML('afterend', html);
        } else {
          container.insertAdjacentHTML('beforeend', html);
        }
      }
    }
  });
}

// 更新单个消息元素（只更新变化的属性）
function updateMessageElement(headEl, msg, highlightTerms = []) {
  const isActive = msg.id === state.currentMessageId;
  const chain = headEl.closest('.message-chain');

  // 计算需要更新的属性
  const statusClass = getStatusClass(msg.status);
  const statusIcon = getStatusIcon(msg.status);
  const timestamp = msg.timestamp || msg.created_at || '';
  const timeStr = timestamp ? timestamp.substring(0, 19).replace('T', ' ') : '';

  // 检查折叠状态是否需要更新（只检查用户操作和active状态，不重新计算时间规则）
  const currentCollapsed = chain.classList.contains('collapsed');
  let expectedCollapsed = currentCollapsed; // 默认保持当前状态

  if (state.collapsedMessages.has(msg.id)) {
    expectedCollapsed = true;
  } else if (state.expandedMessages.has(msg.id)) {
    expectedCollapsed = false;
  } else if (isActive) {
    expectedCollapsed = false;
  }
  // 否则保持当前状态（不重新计算时间规则）

  // 如果折叠状态不一致，需要重渲染
  if (currentCollapsed !== expectedCollapsed) {
    const newHtml = renderMessageChain(msg, highlightTerms);
    chain.outerHTML = newHtml;
    return;
  }

  // 检查消息内容/状态/回复是否变化（用签名比较）
  // 只包含稳定的业务数据，不包含不稳定的对象引用
  const repliesData = msg.replies ? msg.replies.map(r => ({ id: r.id, content: r.content, sender: r.sender })).sort((a, b) => a.id - b.id) : [];
  // 提取消息树签名数据（用于检测分支消息的变化）
  const treeData = extractTreeSignatureData(msg.tree);
  const newSignatureJson = JSON.stringify({
    content: msg.content,
    status: msg.status,
    sender: msg.sender,
    replies: repliesData,
    tree: treeData,
    is_archived: msg.is_archived
  });
  const newSignature = btoa(encodeURIComponent(newSignatureJson));
  const oldSignature = chain.dataset.signature || '';

  if (oldSignature !== newSignature) {
    // 签名变化，重新渲染整个消息链
    chain.dataset.signature = newSignature;
    const newHtml = renderMessageChain(msg, highlightTerms);
    chain.outerHTML = newHtml;
    return;
  }

  // 签名没变，只更新 active 状态和 meta 信息（不触发重渲染）
  headEl.classList.toggle('active', isActive);

  const meta = headEl.querySelector('.message-chain-meta');
  if (meta) {
    const timeEl = meta.querySelector('.message-chain-time');
    if (timeEl && timeEl.textContent !== timeStr) {
      timeEl.textContent = timeStr;
    }

    const senderEl = meta.querySelector('.message-chain-sender');
    const newSender = msg.sender || state.masterName;
    if (senderEl && senderEl.textContent !== newSender) {
      senderEl.textContent = newSender;
    }

    const statusEl = meta.querySelector('.message-chain-status');
    if (statusEl) {
      if (statusEl.textContent !== statusIcon) {
        statusEl.textContent = statusIcon;
      }
      statusEl.className = `message-chain-status ${statusClass}`;
      statusEl.title = getStatusTitle(msg.status);
      // 为running状态添加随机动画延迟（如果元素没有动画延迟的话）
      if (msg.status === 'running' && !statusEl.style.animationDelay) {
        statusEl.style.animationDelay = `-${Math.random() * 3}s`;
      }
      // 动态调整running状态的动画速度（基于关联事务的provider吞吐率）
      if (msg.status === 'running') {
        const runningTask = state.tasks.find(t => t.message_id === msg.id && t.status === 'running');
        if (runningTask?.provider && state.providerFrameRates[runningTask.provider] !== undefined) {
          statusEl.style.animationDuration = `${state.providerFrameRates[runningTask.provider]}s`;
        }
      } else {
        statusEl.style.animationDuration = '';
      }
    }
  }

  // 更新编辑图标的显示状态
  const contentEl = headEl.querySelector('.message-chain-content');
  if (contentEl) {
    const existingEditIcon = contentEl.querySelector('.edit-text-icon');
    const msgContent = msg.content || '';
    if (isActive && !existingEditIcon) {
      // 需要显示编辑图标但不存在，添加
      const editIcon = document.createElement('span');
      editIcon.className = 'edit-text-icon';
      editIcon.title = '编辑';
      editIcon.setAttribute('data-edit-type', 'message');
      editIcon.setAttribute('data-edit-id', msg.id);
      editIcon.setAttribute('data-raw-text', msgContent);
      editIcon.textContent = '📝';
      contentEl.appendChild(editIcon);
    } else if (!isActive && existingEditIcon) {
      // 不需要显示编辑图标但存在，移除
      existingEditIcon.remove();
    } else if (isActive && existingEditIcon) {
      // 需要显示且存在，更新文本内容和类型
      existingEditIcon.setAttribute('data-edit-type', 'message');
      existingEditIcon.setAttribute('data-raw-text', msgContent);
      existingEditIcon.setAttribute('data-edit-id', msg.id);
    }
  }
}

// 解析搜索关键词
function parseSearchTerms(searchTerm) {
  // 用 | 分割 OR 组
  const orGroups = searchTerm.split('|');
  const andTerms = [];
  const orTerms = [];

  orGroups.forEach((group, index) => {
    const terms = group.trim().split(/\s+/).filter(t => t);
    if (index === 0 && orGroups.length === 1) {
      // 没有 | 分隔符，全部作为 AND
      andTerms.push(...terms);
    } else if (index === 0) {
      // 第一个组，如果没有前置的 AND 关键词
      orTerms.push(...terms);
    } else {
      orTerms.push(...terms);
    }
  });

  // 如果没有 | 分隔符，所有空格分隔的词都是 AND
  if (!searchTerm.includes('|')) {
    return { andTerms: searchTerm.split(/\s+/).filter(t => t), orTerms: [] };
  }

  return { andTerms: [], orTerms: orGroups.flatMap(g => g.trim().split(/\s+/).filter(t => t)) };
}

// 获取消息的可搜索文本
function getSearchableTexts(msg) {
  const texts = [];

  // 主消息
  if (msg.content) texts.push(msg.content);
  if (msg.sender) texts.push(msg.sender);
  if (String(msg.id)) texts.push(String(msg.id));

  // 回复
  if (msg.replies) {
    msg.replies.forEach(reply => {
      if (reply.content) texts.push(reply.content);
      if (reply.sender) texts.push(reply.sender);
    });
  }

  // 分支消息（从消息树中递归获取）
  if (msg.tree && msg.tree.root && msg.tree.root.children) {
    const traverseTree = (node) => {
      if (node.message) {
        const branchMsg = node.message;
        if (branchMsg.content) texts.push(branchMsg.content);
        if (branchMsg.sender) texts.push(branchMsg.sender);
        if (String(branchMsg.id)) texts.push(String(branchMsg.id));
      }
      if (node.children) {
        node.children.forEach(child => traverseTree(child));
      }
    };
    msg.tree.root.children.forEach(child => traverseTree(child));
  }

  return texts;
}

/**
 * 提取消息树签名数据（用于检测分支消息的变化）
 * 递归提取每个节点的消息ID列表
 * @param {Object} tree - 消息树对象
 * @returns {Array} 签名数据数组
 */
function extractTreeSignatureData(tree) {
  if (!tree || !tree.root) return [];

  const nodeData = [];

  function traverseNode(node) {
    if (node && node.message) {
      nodeData.push({
        id: node.message.id,
        reply_to: node.message.reply_to || null,
        content: node.message.content || ''
      });
    }
    if (node && node.children) {
      node.children.forEach(child => traverseNode(child));
    }
  }

  traverseNode(tree.root);
  return nodeData.sort((a, b) => a.id - b.id);
}

// 渲染消息链
function renderMessageChain(msg, highlightTerms = [], hoveredId = null) {
  const isActive = msg.id === state.currentMessageId;
  const isHovered = String(msg.id) === String(hoveredId);
  const statusClass = getStatusClass(msg.status);
  const statusIcon = getStatusIcon(msg.status);
  const timestamp = msg.timestamp || msg.created_at || '';
  const timeStr = timestamp ? timestamp.substring(0, 19).replace('T', ' ') : '';

  // 为running状态添加随机动画延迟，避免所有图标动画完全同步
  // 同时根据当前活跃事务的 provider 吞吐率动态调整动画速度
  let msgAnimStyle = '';
  if (msg.status === 'running') {
    // 消息没有直接的 provider，尝试从关联事务获取
    const runningTask = state.tasks.find(t => t.message_id === msg.id && t.status === 'running');
    const provider = runningTask?.provider;
    if (provider && state.providerFrameRates[provider] !== undefined) {
      const duration = state.providerFrameRates[provider];
      msgAnimStyle = `animation-delay: -${Math.random() * 3}s; animation-duration: ${duration}s;`;
    } else {
      msgAnimStyle = `animation-delay: -${Math.random() * 3}s;`;
    }
  }
  const randomAnimationDelay = msgAnimStyle;

  // 从配置中提取消息图标（去除文字）
  const themeConfig = state.themeConfig?.[state.theme];
  const messageIcon = extractIcon(themeConfig?.icon_messages_title, '📜');

  // 判断是否需要折叠：
  // 优先级：主动折叠 > 主动展开 > isActive > 时间规则
  // 时间计算：优先使用心跳更新时间 heartbeat_at，回退到创建时间 created_at
  const msgTime = new Date(msg.heartbeat_at || timestamp).getTime();
  const oneHourAgo = Date.now() - 60 * 60 * 1000;
  const isManuallyCollapsed = state.collapsedMessages.has(msg.id);
  const isManuallyExpanded = state.expandedMessages.has(msg.id);
  let shouldCollapse = false;

  if (isManuallyCollapsed) {
    // 用户主动折叠的消息要折叠（优先级最高）
    shouldCollapse = true;
  } else if (isManuallyExpanded) {
    // 用户主动展开的消息不折叠
    shouldCollapse = false;
  } else if (isActive) {
    // 当前选中的消息不折叠
    shouldCollapse = false;
  } else {
    // 默认规则：1小时前的消息折叠
    shouldCollapse = msgTime < oneHourAgo;
  }

  // 计算签名用于增量更新比较（用base64编码避免HTML属性问题）
  // 只包含稳定的业务数据，不包含不稳定的对象引用
  const repliesData = msg.replies ? msg.replies.map(r => ({ id: r.id, content: r.content, sender: r.sender })).sort((a, b) => a.id - b.id) : [];
  // 提取消息树签名数据（用于检测分支消息的变化）
  const treeData = extractTreeSignatureData(msg.tree);
  const signatureJson = JSON.stringify({
    content: msg.content,
    status: msg.status,
    sender: msg.sender,
    replies: repliesData,
    tree: treeData
  });
  const signature = btoa(encodeURIComponent(signatureJson));

  // 计算发送者样式类
  const sender = msg.sender;
  const senderClass = sender === state.masterName ? 'sender-master' : 'sender-peng';

  // 构建发送者HTML
  const senderHtml = `<span class="${senderClass}">${escapeHtml(sender)}：</span>`;

  // 判断是否是根消息（无 reply_to）
  const isRoot = !msg.reply_to;

  // 计算根消息（depth=0）的未读边框颜色：底色向纯绿色偏移35%
  const baseR = 48, baseG = 128, baseB = 192;
  const rootUnreadBorderR = Math.round(baseR * 0.65);
  const rootUnreadBorderG = Math.round(baseG + (255 - baseG) * 0.35);
  const rootUnreadBorderB = Math.round(baseB * 0.65);
  const rootUnreadBorderRgba = `rgba(${rootUnreadBorderR}, ${rootUnreadBorderG}, ${rootUnreadBorderB}, 0.38)`;
  const status = msg.status || 'unread';
  const isUnread = status === 'unread';

  // 判断是否有子消息
  const hasChildren = (msg.tree && msg.tree.root && msg.tree.root.children && msg.tree.root.children.length > 0) ||
    (msg.replies && msg.replies.length > 0);

  // 生成摘要（使用消息内容的前10行）
  const content = msg.content || '';
  // 🔧 先截断纯文本，再转换文件路径为HTML超链接
  const truncatedContent = truncateText(content, null, msg.id, 'chain', true);
  const displayContent = convertFilePathsToHtmlLinks(truncatedContent);
  const truncatedSummary = truncateText(content, null, null, 'chain', false);
  const summary = convertFilePathsToHtmlLinks(truncatedSummary); // 摘要不需要展开链接

  // 根节点底色从主题配置读取
  const rootMsgBgColor = themeConfig?.ui?.root_message_bg_color;

  let html = `
        <div class="message-chain ${shouldCollapse ? 'collapsed' : ''} ${hasChildren ? 'has-children' : 'no-children'} ${isUnread ? 'unread' : ''} ${state.enableDrag ? 'drag-enabled' : ''}"
             data-signature="${signature}" data-msg-id="${msg.id}" data-depth="0" data-has-children="${hasChildren}" data-root-msg-id="${msg.id}"
             style="${isUnread ? '--unread-border-color: ' + rootUnreadBorderRgba + '; ' : ''}${rootMsgBgColor ? '--level-color-dark: ' + rootMsgBgColor + '; --level-color-light: ' + rootMsgBgColor + '; ' : ''}">
            ${hasChildren && shouldCollapse ? `<span class="message-fold-icon" onclick="event.stopPropagation(); expandMessageChainFully(${msg.id})">▶</span>` : ''}
            <div class="message-chain-head ${isActive ? 'active' : ''} ${isHovered ? 'preserve-hover' : ''}"
                 data-id="${msg.id}">
                  <div class="message-chain-main" onclick="selectMessageHead(${msg.id}, event)">
                       <div class="message-chain-meta">
                          <span class="message-chain-time">${escapeHtml(timeStr)}</span>
                          <span class="message-chain-id" onclick="event.stopPropagation(); showMessageDetails(${msg.id})">${messageIcon}${msg.id}</span>
                            ${statusIcon ? `<span class="message-chain-status ${statusClass}" title="${getStatusTitle(msg.status)}"${randomAnimationDelay ? ` style="${randomAnimationDelay}"` : ''}>${statusIcon}</span>` : ''}
                             ${isRoot ? (msg.id === 1 ? '<span class="action-btn top-btn topped" title="系统消息，永远顶置">★</span>' : `<span class="action-btn top-btn${state.toppedMessageIds.has(msg.id) ? ' topped' : ''}" onclick="event.stopPropagation(); toggleMessageTop(${msg.id})" title="${state.toppedMessageIds.has(msg.id) ? '已顶置，点击取消' : '顶置'}">${state.toppedMessageIds.has(msg.id) ? '★' : '☆'}</span>`) : ''}
                             <span class="message-chain-actions">
                                ${renderMessageActions(msg)}
                                <span class="action-spacer"></span><span class="action-btn archive-btn${msg.is_archived ? ' archived' : ''}" onclick="event.stopPropagation(); archiveMessage(${msg.id})" title="${msg.is_archived ? '取消归档' : '归档'}">♻️</span>
                                ${state.showDeleteButtons ? `<span class="action-btn delete-btn" onclick="event.stopPropagation(); deleteMessage(${msg.id}, true)" title="删除">❌</span>` : ''}
                            </span>
                      </div>
                      <div class="message-chain-content" data-raw-text="${escapeHtml(content).replace(/"/g, '&quot;')}">${senderHtml}${highlightTerms.length > 0 ? highlightSearch(displayContent, highlightTerms.join('|')) : displayContent}${isActive ? '<span class="edit-text-icon" title="编辑" data-edit-type="message" data-edit-id="' + msg.id + '" data-raw-text="' + escapeHtml(content).replace(/"/g, '&quot;') + '">📝</span>' : ''}</div>
                      <div class="message-chain-summary">${senderHtml}${highlightTerms.length > 0 ? highlightSearch(summary, highlightTerms.join('|')) : summary}</div>
                  </div>
            </div>
    `;

  // 折叠时不渲染消息树，选中或展开时才渲染
  if (!shouldCollapse) {
    // 优先使用tree字段（消息树），否则使用replies字段（向后兼容）
    if (msg.tree && msg.tree.root) {
      // 获取消息树容器的宽度，计算最大缩进（容器宽度的1/4）
      const messageTreeContainer = document.getElementById('message-tree');
      const containerWidth = messageTreeContainer ? messageTreeContainer.offsetWidth : 400;
      const maxIndent = Math.floor(containerWidth / 4);
      // 递归渲染消息树
      html += renderMessageTreeNode(msg.tree.root, highlightTerms, 0, msg.id, maxIndent);
    } else if (msg.replies && msg.replies.length > 0) {
      // 向后兼容：使用旧的链式结构
      for (const reply of msg.replies) {
        const replyTime = reply.timestamp || reply.created_at || '';
        const replyTimeStr = replyTime ? replyTime.substring(0, 19).replace('T', ' ') : '';

        // 计算回复发送者样式类
        const replySender = reply.sender;
        const replySenderClass = replySender === state.masterName ? 'sender-master' : 'sender-peng';
        const replySenderHtml = `<span class="${replySenderClass}">${escapeHtml(replySender)}：</span>`;

        // 如果回复有reply_to字段，显示 -> messageIcon#reply_to_id
        const replyToHtml = reply.reply_to ? `<span class="reply-to"> → ${messageIcon}${reply.reply_to}</span>` : '';

        // 🔧 在显示回复内容前先转换文件路径为HTML超链接
        const replyContentWithLinks = convertFilePathsToHtmlLinks(reply.content || '');
        const replyContentDisplay = highlightTerms.length > 0 ? highlightSearch(replyContentWithLinks, highlightTerms.join('|')) : replyContentWithLinks;

        html += `
                    <div class="message-reply-item" onclick="selectMessage(${msg.id})">
                        <div class="reply-meta">
                            <span class="reply-time">${escapeHtml(replyTimeStr)}</span>
                            <span class="reply-id">${messageIcon}${reply.id}${replyToHtml}</span>
                        </div>
                        <div class="reply-content">${replySenderHtml}${replyContentDisplay}</div>
                    </div>
                `;
      }
    }
  }

  html += '</div>';
  return html;
}

/**
 * 递归渲染消息树节点
 *
 * @param {Object} node - 树节点
 * @param {Array} highlightTerms - 高亮关键词
 * @param {Number} depth - 当前深度
 * @param {Number} rootMsgId - 根消息ID（用于区分根消息和回复）
 * @param {Number} maxIndent - 最大缩进宽度（px），用于限制深度过深时的缩进
 */
function renderMessageTreeNode(node, highlightTerms, depth, rootMsgId, maxIndent = null) {
  const msg = node.message;
  const timestamp = msg.timestamp || '';
  const timeStr = timestamp ? timestamp.substring(0, 19).replace('T', ' ') : '';
  const isRoot = depth === 0;
  const indent = maxIndent !== null ? Math.min(depth * 8, maxIndent) : depth * 8; // 每层缩进8px，但不超过最大缩进
  const hasChildren = node.children && node.children.length > 0;

  // 从配置中提取消息图标（去除文字）
  const themeConfig = state.themeConfig?.[state.theme];
  const messageIcon = extractIcon(themeConfig?.icon_messages_title, '📜');

  // 动态计算层级底色（与事务树相同的逻辑）：
  // - level 0（根事务）：保持原样
  // - 奇数层（1, 3, 5...）：绿色分量固定加35%
  // - 偶数层（2, 4, 6...）：红色和蓝色分量固定加35%，绿色分量减30%
  const baseR = 48, baseG = 128, baseB = 192;
  const r = (depth > 0 && depth % 2 === 0) ? Math.min(255, Math.round(baseR * 1.35)) : baseR; // 偶数层红色+35%
  const g = depth % 2 === 1 ? Math.min(255, Math.round(baseG * 1.35)) : Math.max(0, Math.round(baseG * 0.7)); // 奇数层绿色+35%，偶数层绿色-30%
  const b = (depth > 0 && depth % 2 === 0) ? Math.min(255, Math.round(baseB * 1.35)) : baseB; // 偶数层蓝色+35%
  const opacity = 0.08 + depth * 0.02; // 透明度也随层级递增
  // 根节点底色从主题配置读取，子节点使用动态计算
  const rootMsgBgColor = themeConfig?.ui?.root_message_bg_color;
  const isRootNode = depth === 0;

  // 计算未读消息边框颜色：底色向纯绿色偏移35%
  const unreadBorderR = Math.round(r * 0.65);
  const unreadBorderG = Math.round(g + (255 - g) * 0.35);
  const unreadBorderB = Math.round(b * 0.65);
  const unreadBorderRgba = `rgba(${unreadBorderR}, ${unreadBorderG}, ${unreadBorderB}, ${opacity + 0.3})`;
  const status = msg.status || 'unread';
  const isUnread = status === 'unread';

  // 跳过根节点渲染（已经在 message-chain 中渲染过）
  if (isRoot) {
    // 如果根节点被手动折叠，不渲染子节点
    if (state.collapsedMessageNodes.has(msg.id)) {
      return '';
    }
    // 递归渲染子节点
    if (hasChildren) {
      let html = '';
      for (const child of node.children) {
        html += renderMessageTreeNode(child, highlightTerms, depth + 1, rootMsgId, maxIndent);
      }
      return html;
    }
    return '';
  }

  // 判断是否需要折叠消息树节点
  const isManuallyCollapsed = state.collapsedMessageNodes.has(msg.id);
  const isManuallyExpanded = state.expandedMessageNodes.has(msg.id);
  let shouldCollapseNode = false;

  if (isManuallyCollapsed) {
    shouldCollapseNode = true;
  } else if (isManuallyExpanded) {
    shouldCollapseNode = false;
  }

  // 计算发送者样式类
  const sender = msg.sender;
  const senderClass = sender === state.masterName ? 'sender-master' : 'sender-peng';
  const senderHtml = `<span class="${senderClass}">${escapeHtml(sender)}：</span>`;

  // 构建缩进线
  const indentStyle = isRoot ? '' : `margin-left: ${indent}px;`;

  // 如果回复有reply_to字段，显示 -> messageIcon#reply_to_id（可点击）
  const replyToHtml = msg.reply_to ? `<span class="reply-to"> → <span class="reply-to-id" onclick="event.stopPropagation(); showMessageDetails(${msg.reply_to})">${messageIcon}${msg.reply_to}</span></span>` : '';

  // 生成摘要（折叠时显示）- 取前10行
  const content = msg.content || '';
  // 🔧 先截断纯文本，再转换文件路径为HTML超链接
  const truncatedContent = truncateText(content, null, msg.id, 'node', true);
  const displayContent = convertFilePathsToHtmlLinks(truncatedContent);
  const contentLines = content.split('\n');
  const lineCount = contentLines.length;
  const truncatedSummary = truncateText(content, null, null, 'node', false);
  const summary = convertFilePathsToHtmlLinks(truncatedSummary); // 摘要不需要展开链接

  // 判断是否可以折叠（有子节点 或 超过3行或内容太长）
  // 注意：内容太长也会导致CSS自动换行成多行，需要估算实际显示的行数
  const contentLength = content.length;
  // 估算实际显示的行数：按每行80字符估算（考虑CSS自动换行）
  const estimatedLines = Math.ceil(contentLength / 80);
  const actualLineCount = Math.max(lineCount, estimatedLines);
  const canCollapse = hasChildren || actualLineCount > 3;

  // 判断当前消息节点是否被选中（分支消息选中）
  const isSelectedBranch = state.currentMessageId === rootMsgId && state.currentBranchMessageId === msg.id;
  // 编辑图标HTML（仅在选中时显示）
  const editIconHtml = isSelectedBranch ? '<span class="edit-text-icon" title="编辑" data-edit-type="branch-message" data-edit-id="' + msg.id + '" data-root-msg-id="' + rootMsgId + '" data-raw-text="' + escapeHtml(content).replace(/"/g, '&quot;') + '">📝</span>' : '';

  // 获取通知图标（从配置中）
  const notifyIcon = extractIcon(themeConfig?.icon_action_notify, '🛎️');

  // 通知图标HTML（仅分支消息显示，传递rootMsgId以便查找关联事务）
  // 🔧 Task#1212: 修复Windows路径反斜杠丢失问题
  // 原代码未转义反斜杠，导致JS引擎将 `\d` `\想` 等非法转义序列中的反斜杠丢弃
  // 正确转义顺序：先转义所有反斜杠(\\→\\\\)，再转义单引号，再转义换行，最后HTML转义双引号
  const escapedNotifyContent = escapeHtml(content)
    .replace(/\\/g, '\\\\')       // 1. 反斜杠转义（最关键，必须最先做）
    .replace(/'/g, "\\'")          // 2. 单引号转义（JS字符串用单引号包裹）
    .replace(/\n/g, '\\n')         // 3. 真实换行转义
    .replace(/"/g, '&quot;');      // 4. 双引号HTML转义（HTML属性用双引号包裹）
  const notifyIconHtml = '<span class="tree-node-notify-btn" onclick="event.stopPropagation(); notifyBranchMessage(' + rootMsgId + ', ' + msg.id + ', \'' + escapedNotifyContent + '\')" title="发送提醒">' + notifyIcon + '</span>';

  let html = `
                   <div class="message-tree-node ${hasChildren ? 'has-children' : 'no-children'} ${canCollapse ? 'can-collapse' : 'cannot-collapse'} ${shouldCollapseNode ? 'node-collapsed' : 'node-expanded'} ${isUnread ? 'unread' : ''} ${state.enableDrag ? 'drag-enabled' : ''}"
                      style="${indentStyle} --level-color: ${isRootNode && rootMsgBgColor ? rootMsgBgColor : 'rgba(' + r + ', ' + g + ', ' + b + ', ' + opacity + ')'}; --level-color-dark: ${isRootNode && rootMsgBgColor ? rootMsgBgColor : 'rgba(' + r + ', ' + g + ', ' + b + ', ' + (opacity + 0.02) + ')'}; --level-color-light: ${isRootNode && rootMsgBgColor ? rootMsgBgColor : 'rgba(' + r + ', ' + g + ', ' + b + ', ' + (opacity - 0.02) + ')'}; ${isUnread ? '--unread-border-color: ' + unreadBorderRgba : ''};"
                     data-msg-id="${msg.id}" data-depth="${depth}" data-has-children="${hasChildren}" data-can-collapse="${canCollapse}" data-status="${status}" data-root-msg-id="${rootMsgId}">
                    ${canCollapse && shouldCollapseNode ? '<span class="message-node-fold-icon" onclick="event.stopPropagation(); expandMessageNodeFully(' + msg.id + ')">▶</span>' : ''}
                     <div class="tree-node-main" onclick="toggleMessageNodeCollapseAndSelect(event, ${rootMsgId}, ${msg.id})">
                         <div class="tree-node-meta">
                             <span class="tree-node-time">${escapeHtml(timeStr)}</span>
                             <span class="tree-node-id" onclick="event.stopPropagation(); showMessageDetails(${msg.id})">${messageIcon}${msg.id}</span>${replyToHtml}${notifyIconHtml}
                             ${state.showDeleteButtons ? '<span class="tree-node-delete-btn" onclick="event.stopPropagation(); deleteMessage(' + msg.id + ', false, ' + rootMsgId + ')" title="删除">❌</span>' : ''}
                         </div>
                         <div class="tree-node-content" data-raw-text="${escapeHtml(content).replace(/"/g, '&quot;')}">${senderHtml}${highlightTerms.length > 0 ? highlightSearch(displayContent, highlightTerms.join('|')) : displayContent}${editIconHtml}</div>
                         <div class="tree-node-summary">${senderHtml}${highlightTerms.length > 0 ? highlightSearch(summary, highlightTerms.join('|')) : summary}</div>
                    </div>
             </div>
  `;

  // 递归渲染子节点（折叠时不渲染）
  if (!shouldCollapseNode && hasChildren) {
    for (const child of node.children) {
      html += renderMessageTreeNode(child, highlightTerms, depth + 1, rootMsgId, maxIndent);
    }
  }

  return html;
}

// 高亮关键词
function highlightSearch(text, terms) {
  if (!terms) return text;

  // 解析 OR 分隔的关键词
  const termList = terms.split('|').filter(t => t.trim());
  if (termList.length === 0) return text;

  // 🔧 事务#1545优化：合并为单个 alternation 正则，一次遍历完成全部高亮
  // 此前对每个关键词分别执行一次全文 replace，N 个词 = N 次全文扫描+N次字符串重建；
  // 历史列表（≤500项）每键重渲染时该开销被放大 500 倍，是卡顿元凶之一。
  // 按长度降序排列保证长词优先匹配（与多次 replace 的先后语义一致，且避免短词"截断"长词高亮）
  const sorted = termList.slice().sort((a, b) => b.length - a.length);
  const pattern = sorted.map(t => `(${escapeRegex(t)})`).join('|');
  const regex = new RegExp(pattern, 'gi');
  return text.replace(regex, '<mark style="background: var(--warning-color); color: #000; padding: 0 2px; border-radius: 2px;">$&</mark>');
}

function escapeRegex(string) {
  return string.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

// ==================== 文本选择和复制功能 ====================

/**
 * 检查是否有文本被选中
 * @returns {boolean} 是否有文本被选中
 */
function hasSelectedText() {
  const selection = window.getSelection();
  return selection && selection.toString().trim().length > 0;
}

/**
 * 复制选中的文本到剪贴板
 * @returns {Promise<boolean>} 是否成功复制
 */
async function copySelectedText() {
  const selection = window.getSelection();
  if (!selection || selection.toString().trim().length === 0) {
    return false;
  }

  const selectedText = selection.toString().trim();

  try {
    // 尝试使用 Clipboard API
    if (navigator.clipboard && typeof navigator.clipboard.writeText === 'function') {
      await navigator.clipboard.writeText(selectedText);
      showSimpleToast('📝  已复制选中文本到剪贴板', 'success');
      return true;
    }
  } catch (err) {
    console.warn('Clipboard API 复制失败，使用备用方法:', err);
  }

  // 备用复制方法：使用 textarea 和 execCommand
  return fallbackCopy(selectedText);
}

/**
 * 备用复制方法
 * @param {string} text - 要复制的文本
 * @returns {boolean} 是否成功复制
 */
function fallbackCopy(text) {
  const textArea = document.createElement('textarea');
  textArea.value = text;
  textArea.style.position = 'fixed';
  textArea.style.top = '0';
  textArea.style.left = '0';
  textArea.style.width = '2em';
  textArea.style.height = '2em';
  textArea.style.padding = '0';
  textArea.style.border = 'none';
  textArea.style.outline = 'none';
  textArea.style.boxShadow = 'none';
  textArea.style.background = 'transparent';
  textArea.style.opacity = '0';
  document.body.appendChild(textArea);
  textArea.focus();
  textArea.select();
  try {
    const success = document.execCommand('copy');
    if (success) {
      showSimpleToast('📝  已复制选中文本到剪贴板', 'success');
    } else {
      showSimpleToast('❌  复制失败', 'error');
    }
    document.body.removeChild(textArea);
    return success;
  } catch (err) {
    console.warn('execCommand 复制失败:', err);
    showSimpleToast('❌  复制失败: ' + err.message, 'error');
    document.body.removeChild(textArea);
    return false;
  }
}

/**
 * 文本选择自动复制功能已由全局 handleSelectionCopy 统一处理（见文件末尾）
 * 此函数保留为空，避免重复绑定导致多次弹toast
 */
function initTextSelectionCopy() {
  // 全局 handleSelectionCopy (document mouseup) 已覆盖消息树和事务树的文本选择复制
}

/**
 * 自定义tooltip系统
 * 替代浏览器原生title属性tooltip，使tooltip字体跟随全局字体设置和主题。
 * 样式复用 history-hint-bubble 的风格（.peng-tooltip）。
 *
 * 原理：
 * 1. 使用MutationObserver监听DOM变化，对带title属性的元素：
 *    - 将title移到data-peng-title属性（阻止原生tooltip）
 *    - 绑定mouseenter/mouseleave事件显示自定义tooltip
 * 2. 自定义tooltip是一个fixed定位的div，跟随鼠标位置显示
 */
let _pengTooltipEl = null;
let _pengTooltipTimer = null;
let _pengTooltipTarget = null;  // 记住触发tooltip的目标元素

function initCustomTooltip() {
  // 事件委托：在document上监听mouseenter（捕获阶段）
  // 选择器同时匹配 [title]（首次）和 [data-peng-title]（后续hover）
  document.addEventListener('mouseenter', (e) => {
    const target = e.target.closest('[title], [data-peng-title]');
    if (!target) return;
    // 保存title到data-peng-title，清除原生title
    // 如果title被JS动态更新，同步更新data-peng-title
    if (target.title) {
      target.dataset.pengTitle = target.title;
      target.removeAttribute('title');
    }
    const text = target.dataset.pengTitle;
    if (!text) return;

    // 延迟显示tooltip
    clearTimeout(_pengTooltipTimer);
    _pengTooltipTimer = setTimeout(() => {
      _showPengTooltip(text, target);
    }, 400);
  }, true);

  document.addEventListener('mouseleave', (e) => {
    const target = e.target.closest('[data-peng-title]');
    if (!target) return;
    clearTimeout(_pengTooltipTimer);
    _hidePengTooltip();
  }, true);

  // 鼠标移动时不更新位置（tooltip固定在目标元素旁边），只检查目标是否还在DOM
  document.addEventListener('mousemove', (e) => {
    if (_pengTooltipEl && _pengTooltipTarget) {
      // 目标元素被移除时隐藏tooltip
      if (!document.body.contains(_pengTooltipTarget)) {
        _hidePengTooltip();
      }
    }
  });

  // 点击时立即隐藏tooltip（防止点击按钮后tooltip闪现）
  document.addEventListener('mousedown', () => {
    _hidePengTooltip();
    clearTimeout(_pengTooltipTimer);
  }, true);
}

function _showPengTooltip(text, targetEl) {
  _hidePengTooltip();
  const el = document.createElement('div');
  el.className = 'peng-tooltip';
  el.textContent = text;
  el.style.visibility = 'hidden';
  document.body.appendChild(el);
  _positionTooltip(el, targetEl);
  el.style.visibility = '';
  _pengTooltipEl = el;
  _pengTooltipTarget = targetEl;
}

function _hidePengTooltip() {
  if (_pengTooltipEl) {
    _pengTooltipEl.remove();
    _pengTooltipEl = null;
  }
  _pengTooltipTarget = null;
}

