// 保存折叠展开状态到 localStorage 和数据库
async function saveCollapseStates() {
  try {
    // 保存消息和消息树节点状态到 localStorage（暂不支持到数据库）
    const collapsedMessagesObj = Object.fromEntries(state.collapsedMessages);
    const expandedMessagesObj = Object.fromEntries(state.expandedMessages);
    const collapsedMessageNodesObj = Object.fromEntries(state.collapsedMessageNodes);
    const expandedMessageNodesObj = Object.fromEntries(state.expandedMessageNodes);

    localStorage.setItem('peng-collapsed-messages', JSON.stringify(collapsedMessagesObj));
    localStorage.setItem('peng-expanded-messages', JSON.stringify(expandedMessagesObj));
    localStorage.setItem('peng-collapsed-message-nodes', JSON.stringify(collapsedMessageNodesObj));
    localStorage.setItem('peng-expanded-message-nodes', JSON.stringify(expandedMessageNodesObj));

    // 保存事务状态到数据库
    const collapsedTasks = Array.from(state.collapsedTasks.keys());
    const expandedTasks = Array.from(state.expandedTasks.keys());

    // 保存所有事务的折叠状态
    for (const taskId of collapsedTasks) {
      try {
        await fetch(`/api/tasks/${taskId}/collapsed?collapsed=true`, {
          method: 'POST'
        });
      } catch (e) {
        console.warn(`保存事务 ${taskId} 折叠状态失败:`, e);
      }
    }

    for (const taskId of expandedTasks) {
      try {
        await fetch(`/api/tasks/${taskId}/collapsed?collapsed=false`, {
          method: 'POST'
        });
      } catch (e) {
        console.warn(`保存事务 ${taskId} 展开状态失败:`, e);
      }
    }
  } catch (e) {
    console.error('保存折叠展开状态失败:', e);
  }
}

// 从 localStorage 和数据库恢复折叠展开状态
async function restoreCollapseStates() {
  try {
    // 从 localStorage 恢复消息树节点的折叠状态（暂不支持消息树节点到数据库）
    const collapsedMessageNodesStr = localStorage.getItem('peng-collapsed-message-nodes');
    const expandedMessageNodesStr = localStorage.getItem('peng-expanded-message-nodes');

    if (collapsedMessageNodesStr) {
      state.collapsedMessageNodes = new Map(Object.entries(JSON.parse(collapsedMessageNodesStr)));
    }
    if (expandedMessageNodesStr) {
      state.expandedMessageNodes = new Map(Object.entries(JSON.parse(expandedMessageNodesStr)));
    }

    // 🔧 性能优化：从已加载的消息/事务数据中提取collapsed状态，消除N+1 HTTP请求
    // collapsed字段已在 /api/messages 和 /api/tasks 的响应中包含（to_dict()包含collapsed字段）

    // 从已加载的消息数据恢复消息折叠状态（包括消息树节点）
    if (state.messages && state.messages.length > 0) {
      // 递归遍历消息树节点，从已加载数据中提取collapsed状态
      const applyCollapsedFromTree = (node) => {
        if (!node || !node.message) return;
        const msgId = node.message.id;
        const collapsed = !!node.message.collapsed;

        // 数据库状态优先：先清除可能存在的冲突
        state.collapsedMessageNodes.delete(msgId);
        state.collapsedMessageNodes.delete(String(msgId));
        state.expandedMessageNodes.delete(msgId);
        state.expandedMessageNodes.delete(String(msgId));

        if (collapsed) {
          state.collapsedMessageNodes.set(msgId, Date.now());
        } else {
          state.expandedMessageNodes.set(msgId, Date.now());
        }

        // 递归处理子节点
        if (node.children) {
          node.children.forEach(applyCollapsedFromTree);
        }
      };

      for (const msg of state.messages) {
        // 链首消息的collapsed状态
        if (msg.collapsed) {
          state.collapsedMessages.delete(msg.id);
          state.collapsedMessages.delete(String(msg.id));
          state.expandedMessages.delete(msg.id);
          state.expandedMessages.delete(String(msg.id));
          state.collapsedMessages.set(msg.id, Date.now());
        } else {
          // collapsed=0（数据库默认值）或collapsed=false，标记为展开
          state.collapsedMessages.delete(msg.id);
          state.collapsedMessages.delete(String(msg.id));
          state.expandedMessages.delete(msg.id);
          state.expandedMessages.delete(String(msg.id));
          state.expandedMessages.set(msg.id, Date.now());
        }

        // 消息树节点的collapsed状态
        if (msg.tree && msg.tree.root) {
          applyCollapsedFromTree(msg.tree.root);
        }
      }
    }
  } catch (e) {
    console.error('恢复消息折叠展开状态失败:', e);
  }
}

// 从已加载的事务数据恢复事务折叠状态（需在 loadTasks 之后调用）
function restoreTaskCollapseStates() {
  try {
    if (state.tasks && state.tasks.length > 0) {
      const allTasks = flattenTasks(state.tasks);
      for (const task of allTasks) {
        if (task.collapsed) {
          state.collapsedTasks.set(task.id, Date.now());
        } else {
          // collapsed=0（数据库默认值）或collapsed=false，标记为展开
          state.expandedTasks.set(task.id, Date.now());
        }
      }
    }
  } catch (e) {
    console.error('恢复事务折叠展开状态失败:', e);
  }
}

// 根据 message_id 查找关联的事务
function findTaskByMessageId(messageId) {
  if (!messageId) return null;
  const allTasks = flattenTasks(state.tasks);
  return allTasks.find(task => task.message_id === messageId) || null;
}

// 根据 task_id 查找事务
function findTaskById(tasks, taskId) {
  if (!taskId) return null;
  const findRecursive = (taskList, targetId) => {
    for (const task of taskList) {
      if (task.id === targetId) return task;
      if (task.children && task.children.length > 0) {
        const found = findRecursive(task.children, targetId);
        if (found) return found;
      }
    }
    return null;
  };
  return findRecursive(tasks, taskId);
}

// 更新输入框提示文本
function updateInputPlaceholder(msgId) {
  const input = document.getElementById('message-input');
  if (!input) return;
  if (state.inputMode === 'task') {
    input.setAttribute('data-placeholder', `Enter发送提示给事务#${state.currentTaskId}，Ctrl+Enter发送新消息，Ctrl+Shift+Enter回复消息#${state.currentMessageId}`);
  } else if (msgId) {
    input.setAttribute('data-placeholder', `Enter回复消息#${msgId}，Ctrl+Enter发送新消息，Ctrl+Shift+Enter指示事务#${state.currentTaskId}`);
  } else {
    input.setAttribute('data-placeholder', '输入新消息，Enter发送，Shift+Enter换行...');
  }
}

// 切换消息输入框模式
function switchInputMode(mode = '') {
  const input = document.getElementById('message-input');
  if (mode !== 'task' && mode !== 'message') {
    if (state.inputMode === 'message')
      mode = 'task';
    else
      mode = 'message'
  }

  if (mode === 'task') {
    state.inputMode = 'task';
    input.classList.add('input-mode-task');
  } else {
    state.inputMode = 'message';
    input.classList.remove('input-mode-task');
  }
  updateInputPlaceholder(state.currentBranchMessageId || state.currentMessageId);
}

// 🔧 Task#228：切换消息顶置，同步关联根事务
async function toggleMessageTop(msgId) {
  try {
    const response = await fetch(`/api/messages/${msgId}/top`, { method: 'POST' });
    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw new Error(error.detail || '切换顶置失败');
    }
    const data = await response.json();

    // 同步更新 state（消息+关联事务）
    state.toppedMessageIds = new Set(data.topped_message_ids || []);
    state.toppedTaskIds = new Set(data.topped_task_ids || []);

    showSimpleToast(`${data.topped ? '★' : '☆'} 消息 #${msgId} ${data.topped ? '已顶置' : '已取消顶置'}`, 'success');

    // 重新渲染消息和事务列表
    renderMessageTree(true);
    renderTaskTree();
  } catch (error) {
    showError('操作失败', error);
  }
}

// 归档消息
async function archiveMessage(msgId) {
  try {
    // 查找消息对象，检查当前归档状态
    const msg = state.messages.find(m => m.id === msgId);
    const isCurrentlyArchived = msg ? msg.is_archived : false;

    // 根据当前状态调用对应的接口
    const endpoint = isCurrentlyArchived
      ? `/api/messages/${msgId}/unarchive`
      : `/api/messages/${msgId}/archive`;

    const response = await fetch(endpoint, {
      method: 'POST',
    });

    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw new Error(error.detail || (isCurrentlyArchived ? '取消归档失败' : '归档失败'));
    }

    showSimpleToast(`♻️  消息 #${msgId} ${isCurrentlyArchived ? '已取消归档' : '已归档'}`, 'success');
    state.output.clear(); state.currentColor = '';

    // 🔧 Task#1346：跳过下一次轮询，避免与手动 loadMessages 竞争
    skipNextMessagesPoll = true;

    // 如果归档的是当前选中的消息，清除选中状态，并记录需要自动选中第一条消息
    const needSelectFirst = (state.currentMessageId === msgId && !isCurrentlyArchived);
    if (needSelectFirst) {
      state.currentMessageId = null;
    }

    // 刷新消息列表
    await loadMessages(false, true);

    // 🔧 Task#1518：loadMessages 更新了 displayedMessageIds 后，
    // 必须同步刷新事务列表，否则事务栏不会跟随归档消失
    lastTasksData = '';  // 清除去重缓存，确保 loadTasks 不被跳过
    loadTasks();

    // 如果归档的是当前选中的消息，且消息列表不为空，自动选中第一条消息
    if (needSelectFirst && state.messages.length > 0) {
      await selectMessage(state.messages[0].id);
    }

  } catch (error) {
    showError('操作失败', error);
  }
}

// 删除消息（递归）
async function deleteMessage(msgId, isRoot, rootMsgId = null) {
  try {
    // 🔧 Task#1346：移除删除前的 selectMessageBranch 预选中操作
    // 旧代码先 await selectMessageBranch 再发删除请求，导致用户多等一次输出订阅+DOM渲染
    // 删除后 loadMessages 会刷新列表并 selectMessage(rootMsgId)，无需预选中

    // 删除树根消息前，找到上一条消息（用于删除后自动选中）
    let prevMsgId = null;
    if (isRoot) {
      // 按时间从新到老排序，与 renderMessageTree 保持一致
      const sorted = [...state.messages].sort((a, b) =>
        new Date(b.timestamp || b.created_at) - new Date(a.timestamp || a.created_at)
      );
      const currentIndex = sorted.findIndex(m => m.id === msgId);
      if (currentIndex > 0) {
        prevMsgId = sorted[currentIndex - 1].id;
      }
    }

    // 树根消息：使用原有的删除端点（递归删除事务）
    // 分支消息：使用新的递归删除端点（递归删除消息并重新生成上下文）
    const endpoint = isRoot ? `/api/messages/${msgId}` : `/api/messages/${msgId}/recursive`;
    const response = await fetch(endpoint, {
      method: 'DELETE',
    });

    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw new Error(error.detail || '删除失败');
    }

    const result = await response.json();
    showSimpleToast(`📜 消息 #${msgId} ${result.message}`, 'success');

    // 清除选中状态
    if (state.currentMessageId === msgId) {
      state.currentMessageId = null;
    }
    if (state.currentBranchMessageId === msgId) {
      state.currentBranchMessageId = null;
    }

    // 🔧 Task#1346：跳过下一次轮询，避免与手动 loadMessages 竞争
    skipNextMessagesPoll = true;

    // 刷新消息列表
    await loadMessages(false, true);

    // 🔧 Task#1518：loadMessages 更新了 displayedMessageIds 后，
    // 必须同步刷新事务列表，否则事务栏不会跟随删除消失
    lastTasksData = '';  // 清除去重缓存，确保 loadTasks 不被跳过
    loadTasks();

    // 如果消息列表为空，清空终端
    if (state.messages.length === 0) {
      if (state.output) {
        state.output.clear(); state.currentColor = '';
      }
    } else {
      // 删除分支消息后自动选中根消息
      if (!isRoot && rootMsgId) {
        await selectMessage(rootMsgId);
      }
      // 删除树根消息后自动选中第一条消息
      else if (isRoot) {
        if (prevMsgId) {
          await selectMessage(prevMsgId);
        } else {
          // 如果没有上一条消息，选中第一条消息
          await selectMessage(state.messages[0].id);
        }
      }
    }

  } catch (error) {
    showError('删除失败', error);
  }
}

// 暂停消息（通过暂停关联的事务）
async function pauseMessage(msgId) {
  try {
    const relatedTask = findTaskByMessageId(msgId);
    if (!relatedTask) {
      showSimpleToast(`📜  消息 #${msgId} 没有关联的事务`, 'error');
      return;
    }

    const response = await fetch(`/api/tasks/${relatedTask.id}/action`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({ action: 'pause' }),
    });

    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw new Error(error.detail || '暂停失败');
    }

    showSimpleToast(`📜 消息 #${msgId} 已暂停`, 'success');
    loadMessages(false, true);
    loadTasks();

  } catch (error) {
    showError('暂停失败', error);
  }
}

// 暂停当前选中的事务分支
async function pauseCurrentTaskBranch() {
  try {
    // 优先使用当前选中的事务ID
    if (state.currentTaskId) {
      const response = await fetch(`/api/tasks/${state.currentTaskId}/action`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({ action: 'pause' }),
      });

      if (!response.ok) {
        const error = await response.json().catch(() => ({}));
        throw new Error(error.detail || '暂停失败');
      }

      showSimpleToast(`🚩  事务 #${state.currentTaskId} 已暂停`, 'success');
      loadMessages(false, true);
      loadTasks();
      return;
    }

    // 如果没有选中的事务，尝试通过消息查找关联的事务
    const msgId = state.currentBranchMessageId || state.currentMessageId;
    if (msgId) {
      const relatedTask = findTaskByMessageId(msgId);
      if (relatedTask) {
        const response = await fetch(`/api/tasks/${relatedTask.id}/action`, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({ action: 'pause' }),
        });

        if (!response.ok) {
          const error = await response.json().catch(() => ({}));
          throw new Error(error.detail || '暂停失败');
        }

        showSimpleToast(`🚩  事务 #${relatedTask.id} 已暂停`, 'success');
        loadMessages(false, true);
        loadTasks();
        return;
      }
    }

    showSimpleToast('🚩 没有可暂停的事务', 'warning');

  } catch (error) {
    showError('暂停失败', error);
  }
}

// 取消消息（通过取消关联的事务）
async function cancelMessage(msgId) {
  try {
    const relatedTask = findTaskByMessageId(msgId);
    if (!relatedTask) {
      showSimpleToast(`📜 消息 #${msgId} 没有关联的事务`, 'error');
      return;
    }

    const response = await fetch(`/api/tasks/${relatedTask.id}/action`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({ action: 'cancel' }),
    });

    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw new Error(error.detail || '取消失败');
    }

    showSimpleToast(`📜  消息 #${msgId} 已取消`, 'success');

    // 🔧 修复：取消后保留选中状态，不清除 currentMessageId，避免输出区域被清空
    // 用户取消消息后仍应能查看该消息的输出内容

    loadMessages(false, true);
    loadTasks();

  } catch (error) {
    showError('取消失败', error);
  }
}

// 恢复消息（通过恢复关联的事务）
async function resumeMessage(msgId) {
  try {
    const relatedTask = findTaskByMessageId(msgId);
    if (!relatedTask) {
      showSimpleToast(`📜  消息 #${msgId} 没有关联的事务`, 'error');
      return;
    }

    const response = await fetch(`/api/tasks/${relatedTask.id}/action`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({ action: 'resume' }),
    });

    if (!response.ok) {
      const errorData = await response.json().catch(() => ({}));
      const err = new Error(errorData.detail || '恢复失败');
      err.response = { status: response.status, data: errorData };
      throw err;
    }

    showSimpleToast(`📜  消息 #${msgId} 已恢复`, 'success');
    // 🔧 修复：重置滚动状态，确保新输出能显示到xterm
    state.userScrolledUp = false;
    loadMessages(false, true);
    loadTasks();

  } catch (error) {
    console.error('❌ resumeMessage 失败:', error);
    showError('恢复失败', error);
  }
}

// 🔧 Task#1449优化：通知分支消息，直接调用快速REST API（不再走子进程）
async function notifyBranchMessage(rootMsgId, msgId, content) {
  try {
    // 查找分支消息关联的事务（通过根消息ID查找）
    const relatedTask = findTaskByMessageId(rootMsgId);
    if (!relatedTask) {
      showSimpleToast(`📜  消息链 #${rootMsgId} 没有关联的事务`, 'error');
      return;
    }

    // 直接调用快速REST API（~40ms），不再走 /api/command 子进程（~700ms+）
    const response = await fetch(`/api/cli/task/${relatedTask.id}/info`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({ info: content }),
    });

    if (!response.ok) {
      showSimpleToast(`❌ 无法发送提醒至事务 #${relatedTask.id}`, 'error');
    } else {
      const data = await response.json();
      if (data.success) {
        showSimpleToast(`🛎️ 已发送提醒至事务 #${relatedTask.id}`, 'success');
        // 🔧 Task#1462：发送成功后立刻在 xterm 输出提示
        showInOutput(`🛎️ 已向事务#${relatedTask.id}发送关于消息#${msgId}的提醒，事务将在下个迭代处理。`, 'success');
      } else {
        showSimpleToast(`❌ 无法发送提醒至事务 #${relatedTask.id}`, 'error');
      }
    }
  } catch (error) {
    console.error('❌ notifyBranchMessage 失败:', error);
    showError('发送提醒失败', error);
  }
}

// 重启消息（通过后端MessageManager.restart_message确保行为统一）
async function restartMessage(msgId) {
  try {
    // 🔧 Task#1326：不再前端主动删除输出文件
    // 后端 restart_message → restart_task 保留 output 文件不删除，
    // 并通过 WebSocket "restarted" 事件通知前端清除缓存。
    // 前端无需预先调用 clearOutput(deleteFile=true)。

    // 调用后端 /messages/{msg_id}/restart 端点
    // 后端会执行完整的清理流程：
    // 1. 递归删除所有回复消息及其关联事务树
    // 2. 递归删除根事务的所有子事务
    // 3. 重启根事务（output 文件保留不删除）
    // 4. 更新消息状态为processing
    const response = await fetch(`/api/messages/${msgId}/restart`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
    });

    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw new Error(error.detail || '重启失败');
    }

    showSimpleToast(`📜  消息 #${msgId} 已重启`, 'success');
    loadMessages(false, true);
    loadTasks();

  } catch (error) {
    showError('重启失败', error);
  }
}

// 根据消息状态渲染操作按钮
function renderMessageActions(msg) {
  const status = msg.status;
  // 消息状态：processing/paused/interrupted/done/completed/failed/cancelled等

  // 可暂停状态：processing（处理中）
  const canPause = ['processing'].includes(status);
  // 可取消状态：processing/paused/interrupted
  const canCancel = ['processing', 'paused', 'interrupted'].includes(status);
  // 可恢复状态：paused/interrupted
  const canResume = ['paused', 'interrupted'].includes(status);
  // 可重启状态：done/completed/failed/cancelled/paused/interrupted
  const canRestart = ['done', 'completed', 'failed', 'cancelled', 'paused', 'interrupted'].includes(status);

  let html = '';

  // 按钮顺序：恢复 > 暂停 > [spacer] > 重启 > 取消
  // 注意：删除按钮在 renderMessageChain 中单独渲染，避免重复
  // spacer将安全操作（恢复/暂停）与危险操作（重启/取消）拉开距离
  let hasSafeAction = false;
  if (canResume) {
    html += `<span class="action-btn resume-btn" onclick="event.stopPropagation(); resumeMessage(${msg.id})" title="恢复">▶️</span>`;
    hasSafeAction = true;
  }
  if (canPause) {
    html += `<span class="action-btn pause-btn" onclick="event.stopPropagation(); pauseMessage(${msg.id})" title="暂停">⏸️</span>`;
    hasSafeAction = true;
  }
  if (canRestart) {
    if (hasSafeAction) html += '<span class="action-spacer"></span>';
    html += `<span class="action-btn restart-btn" onclick="event.stopPropagation(); restartMessage(${msg.id})" title="重启">🔄</span>`;
  }
  if (canCancel) {
    if (!canRestart && hasSafeAction) html += '<span class="action-spacer"></span>';
    html += `<span class="action-btn cancel-btn" onclick="event.stopPropagation(); cancelMessage(${msg.id})" title="取消">❎</span>`;
  }

  return html;
}

// 暂停事务
async function pauseTask(taskId) {
  try {
    const response = await fetch(`/api/tasks/${taskId}/action`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({ action: 'pause' }),
    });

    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw new Error(error.detail || '暂停失败');
    }

    showSimpleToast(`🚩  事务 #${taskId} 已暂停`, 'success');
    loadTasks();

  } catch (error) {
    showError('暂停失败', error);
  }
}

// 取消事务
async function cancelTask(taskId) {
  try {
    const response = await fetch(`/api/tasks/${taskId}/action`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({ action: 'cancel' }),
    });

    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw new Error(error.detail || '取消失败');
    }

    showSimpleToast(`🚩  事务 #${taskId} 已取消`, 'success');

    // 🔧 修复：取消后保留选中状态，不清除 currentTaskId，避免输出区域被清空
    // 用户取消事务后仍应能查看该事务的输出内容

    loadTasks();

  } catch (error) {
    showError('取消失败', error);
  }
}

// 删除事务（递归删除子事务）
async function deleteTask(taskId) {
  try {
    const response = await fetch(`/api/tasks/${taskId}`, {
      method: 'DELETE',
    });

    if (!response.ok) {
      const errorData = await response.json().catch(() => ({}));
      throw new Error(errorData.detail || '删除失败');
    }

    const data = await response.json();
    const deletedCount = data.deleted_count || 1;
    showSimpleToast(`🚩  事务 #${taskId} 已删除，共 ${deletedCount} 个事务`, 'success');

    // 如果删除的是当前选中的事务，清除选中状态
    if (state.currentTaskId === taskId) {
      state.currentTaskId = null;
      state.currentMessageId = null;
    }

    // 重新加载事务列表
    loadTasks();

  } catch (error) {
    showError('删除失败', error);
  }
}

// 恢复事务
async function resumeTask(taskId) {
  try {
    const response = await fetch(`/api/tasks/${taskId}/action`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({ action: 'resume' }),
    });

    if (!response.ok) {
      const errorData = await response.json().catch(() => ({}));
      const err = new Error(errorData.detail || '恢复失败');
      // 添加响应信息到错误
      err.response = { status: response.status, data: errorData };
      throw err;
    }

    showSimpleToast(`🚩  事务 #${taskId} 已恢复`, 'success');
    // 🔧 修复：重置滚动状态，确保新输出能显示到xterm
    state.userScrolledUp = false;
    loadTasks();

  } catch (error) {
    console.error('❌ resumeTask 失败:', error);
    showError('恢复失败', error);
  }
}

// 开始执行事务（pending -> running）
async function startTask(taskId) {
  try {
    const response = await fetch(`/api/tasks/${taskId}/action`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({ action: 'start' }),
    });

    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw new Error(error.detail || '开始执行失败');
    }

    showSimpleToast(`🚩  事务 #${taskId} 已开始执行`, 'success');
    loadTasks();

  } catch (error) {
    showError('开始执行失败', error);
  }
}

// 重启事务
async function restartTask(taskId) {
  try {
    // 🔧 Task#1326：不再前端主动删除输出文件
    // 后端 restart_task 保留 output 文件不删除，
    // 并通过 WebSocket "restarted" 事件通知前端清除缓存。
    const response = await fetch(`/api/tasks/${taskId}/action`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({ action: 'restart' }),
    });

    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw new Error(error.detail || '重启失败');
    }

    showSimpleToast(`🚩  事务 #${taskId} 已重启`, 'success');
    loadTasks();

  } catch (error) {
    showError('重启失败', error);
  }
}

// 🔧 Task#228：切换根事务顶置，同步关联根消息
async function toggleTaskTop(taskId) {
  try {
    const response = await fetch(`/api/tasks/${taskId}/top`, { method: 'POST' });
    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw new Error(error.detail || '切换顶置失败');
    }
    const data = await response.json();

    // 同步更新 state（事务+关联消息）
    state.toppedTaskIds = new Set(data.topped_task_ids || []);
    state.toppedMessageIds = new Set(data.topped_message_ids || []);

    showSimpleToast(`${data.topped ? '★' : '☆'} 事务 #${taskId} ${data.topped ? '已顶置' : '已取消顶置'}`, 'success');

    // 重新渲染消息和事务列表
    renderTaskTree();
    renderMessageTree(true);
  } catch (error) {
    showError('操作失败', error);
  }
}

function renderTaskActions(task) {
  const status = task.status;
  // 事务状态只有：pending/running/accepting/accepted/paused/interrupted/completed/failed/cancelled
  // 注意：processing 是消息状态，不是事务状态！事务执行中是 running

  // 可暂停状态：running, accepting（事务执行中或验收中）
  const canPause = ['running', 'accepting'].includes(status);
  // 可取消状态：running, accepting, pending（执行中或待定）
  const canCancel = ['running', 'accepting', 'pending'].includes(status);
  // 可恢复状态：paused, interrupted, completed, failed（已暂停或已中断或已完成或失败）
  const canResume = ['paused', 'interrupted', 'completed', 'failed'].includes(status);
  // 可开始执行状态：pending（从待定开始执行）
  const canStart = status === 'pending';
  // 可重启状态：paused, cancelled, interrupted, completed, failed（不包括 pending 和 running）
  const canRestart = ['paused', 'cancelled', 'interrupted', 'completed', 'accepted', 'failed'].includes(status);
  // 可删除状态：非根事务（有 parent_task_id）→ 调用 deleteTask
  const canDelete = task.parent_task_id !== null && task.parent_task_id !== 0 && state.showTaskDeleteButtons;
  // 根事务删除：无 parent_task_id 且有 message_id → 调用根消息的 deleteMessage
  const canDeleteRootTask = task.parent_task_id === null && task.message_id && state.showTaskDeleteButtons;

  let html = '';

  // 按钮顺序：开始 > 暂停 > 恢复 > [spacer] > 重启 > 取消 > 删除
  // spacer将安全操作（开始/暂停/恢复）与危险操作（重启/取消/删除）拉开距离
  let hasSafeAction = false;
  if (canStart) {
    html += `<span class="action-btn start-btn" onclick="event.stopPropagation(); startTask(${task.id})" title="开始执行">▶️</span>`;
    hasSafeAction = true;
  }
  if (canPause) {
    html += `<span class="action-btn pause-btn" onclick="event.stopPropagation(); pauseTask(${task.id})" title="暂停">⏸️</span>`;
    hasSafeAction = true;
  }
  if (canResume) {
    html += `<span class="action-btn resume-btn" onclick="event.stopPropagation(); resumeTask(${task.id})" title="恢复执行">▶️</span>`;
    hasSafeAction = true;
  }
  if (canRestart) {
    if (hasSafeAction) html += '<span class="action-spacer"></span>';
    html += `<span class="action-btn restart-btn" onclick="event.stopPropagation(); restartTask(${task.id})" title="重启">🔄</span>`;
  }
  if (canCancel) {
    if (!canRestart && hasSafeAction) html += '<span class="action-spacer"></span>';
    html += `<span class="action-btn cancel-btn" onclick="event.stopPropagation(); cancelTask(${task.id})" title="取消">❎</span>`;
  }
  if (canDelete) {
    if (!canRestart && !canCancel && hasSafeAction) html += '<span class="action-spacer"></span>';
    html += `<span class="action-btn delete-btn" onclick="event.stopPropagation(); deleteTask(${task.id})" title="删除事务">❌</span>`;
  }
  if (canDeleteRootTask) {
    if (!canRestart && !canCancel && !canDelete && hasSafeAction) html += '<span class="action-spacer"></span>';
    html += `<span class="action-btn delete-btn" onclick="event.stopPropagation(); deleteMessage(${task.message_id}, true)" title="删除根消息">❌</span>`;
  }

  return html;
}

// showMessageInOutput 已废弃
// 信息显示框只显示进程输出，不显示消息内容
// 消息内容在左边栏消息列表中显示

// 🔧 Task#867：消息更新防抖定时器
// 避免短时间内收到大量 message_update 广播时并发 loadMessages 导致竞态
let _messageUpdateDebounceTimer = null;
let _pendingMessageUpdateData = null;

async function handleMessageUpdate(data) {
  // 🔧 Task#867：防抖处理，短时间内多次 message_update 只触发一次 loadMessages
  // 避免批量删除/创建消息时并发请求导致竞态（旧数据覆盖新数据）
  // 但 'created' 事件需要立即处理（自动选中逻辑），不走防抖
  if (data.action !== 'created') {
    _pendingMessageUpdateData = data;  // 保存最新的 data
    if (_messageUpdateDebounceTimer) {
      return;  // 已有待执行的防抖任务，跳过
    }
    _messageUpdateDebounceTimer = setTimeout(async () => {
      _messageUpdateDebounceTimer = null;
      const pendingData = _pendingMessageUpdateData;
      _pendingMessageUpdateData = null;
      if (pendingData) {
        await loadMessages(true, true);
        skipNextMessagesPoll = true;
      }
    }, 150);  // 150ms 防抖窗口
    return;
  }

  // 'created' 事件：立即处理（需要自动选中）
  // 🔧 Task#287：重新加载消息列表，forceRefresh=true 确保不跳过DOM更新
  await loadMessages(true, true);
  // 节流：跳过下一次消息轮询（WebSocket已触发加载，500ms窗口内轮询会重复）
  skipNextMessagesPoll = true;

  // 🔧 Task#265：自动选中逻辑 — 只对本实例发起的消息操作生效
  // 通过 state._localCreatedMsgId 区分：只有发送消息的 webui 实例才有此标记
  // 其他 webui 实例收到广播后不会自动切换选中状态，保持各自的浏览状态
  const isLocalCreation = data.action === 'created' && data.msg_id
    && state._localCreatedMsgId === data.msg_id;

  // 🔧 Task#349：本地回复消息时跳过自动选择逻辑（修复选中状态消失问题）
  // _isLocalReply 由 sendMessage 设置，在此处清除（而非 sendMessage finally），
  // 确保即使 WebSocket 延迟到达，保护窗口仍然有效
  if (isLocalCreation && state._isLocalReply) {
    state._isLocalReply = false;
    state._localCreatedMsgId = null;
    return;
  }

  // 🔧 Task#349：无论是否 isLocalCreation，都清除 _isLocalReply（安全兜底）
  // 防止 _isLocalReply 残留影响后续操作
  if (state._isLocalReply) {
    state._isLocalReply = false;
  }

  if (isLocalCreation && !state.isReplyingMessage && state.currentMessageId !== data.msg_id) {
    const newMessageId = data.msg_id;
    // 清除标记（一次性使用）
    state._localCreatedMsgId = null;

    // 等待消息列表加载完成后，选中并订阅新消息
    setTimeout(() => {
      selectMessage(newMessageId);
    }, 500);  // 延迟500ms，确保消息列表已加载
  } else if (isLocalCreation) {
    // 发送方实例已由 sendMessage 处理选中，或正在回复（保持链首），清除标记
    state._localCreatedMsgId = null;
  }
}

