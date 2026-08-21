// ==================== 拖拽功能 ====================
// 完全基于 mouse 事件实现，不使用浏览器 HTML5 drag-and-drop API
// 彻底避免 Firefox 将 text/plain 数据当作 URL/搜索词打开新标签页的问题

// 拖拽内部状态
let _dragMouseDown = null;   // mousedown 时记录的起始信息
let _dragGhost = null;       // 拖拽预览幽灵元素
const _DRAG_THRESHOLD = 5;   // 触发拖拽的鼠标移动阈值（像素）

/**
 * 更新所有可拖拽元素的样式类（不再设置 draggable 属性）
 */
function updateDraggableState() {
  const enabled = state.enableDrag;
  document.querySelectorAll('.message-chain[data-msg-id]').forEach(el => {
    el.classList.toggle('drag-enabled', enabled);
  });
  document.querySelectorAll('.message-tree-node[data-msg-id]').forEach(el => {
    el.classList.toggle('drag-enabled', enabled);
  });
  document.querySelectorAll('.task-chain[data-task-id]').forEach(el => {
    el.classList.toggle('drag-enabled', enabled);
  });
}

/**
 * 初始化拖拽功能（基于 mouse 事件，非 HTML5 drag API）
 */
function initDragAndDrop() {
  const messageTree = document.getElementById('message-tree');
  const taskTree = document.getElementById('task-tree');

  if (messageTree) {
    messageTree.addEventListener('mousedown', _handleMouseDown);
  }
  if (taskTree) {
    taskTree.addEventListener('mousedown', _handleMouseDown);
  }

  document.addEventListener('mousemove', _handleMouseMove);
  document.addEventListener('mouseup', _handleMouseUp);
}

/**
 * mousedown：记录拖拽起始信息（不立即开始拖拽，等 mousemove 超过阈值才触发）
 */
function _handleMouseDown(e) {
  if (!state.enableDrag) return;
  if (e.button !== 0) return; // 只处理左键

  const target = e.target;
  // 排除点击按钮/链接等交互元素
  if (target.closest('button, a, input, .action-btn, .message-fold-icon, .edit-text-icon')) return;

  const messageNode = target.closest('.message-tree-node');
  const messageChain = target.closest('.message-chain');
  const taskNode = target.closest('.task-chain');

  let dragInfo = null;

  if (messageNode) {
    const msgId = parseInt(messageNode.dataset.msgId);
    const rootMsgId = parseInt(messageNode.dataset.rootMsgId);
    dragInfo = { type: 'message', id: msgId, sourceRootId: rootMsgId, sourceEl: messageNode };
  } else if (messageChain) {
    const depth = parseInt(messageChain.dataset.depth);
    if (depth !== 0) return; // 只允许拖拽根消息
    const msgId = parseInt(messageChain.dataset.msgId);
    dragInfo = { type: 'message', id: msgId, sourceRootId: msgId, sourceEl: messageChain };
  } else if (taskNode) {
    const taskId = parseInt(taskNode.dataset.taskId);
    dragInfo = { type: 'task', id: taskId, sourceRootId: null, sourceEl: taskNode };
  }

  if (dragInfo) {
    _dragMouseDown = { ...dragInfo, startX: e.clientX, startY: e.clientY };
    // 不阻止默认行为，允许正常的文本选择等（只有真正开始拖拽时才阻止）
  }
}

/**
 * mousemove：超过阈值后启动拖拽，拖拽中更新 drag-over 高亮
 */
function _handleMouseMove(e) {
  // 尚未开始拖拽，检测是否超过阈值
  if (_dragMouseDown && !state.dragState.isDragging) {
    const dx = e.clientX - _dragMouseDown.startX;
    const dy = e.clientY - _dragMouseDown.startY;
    if (Math.abs(dx) < _DRAG_THRESHOLD && Math.abs(dy) < _DRAG_THRESHOLD) return;

    // 超过阈值，正式开始拖拽
    _startDrag(e);
  }

  if (!state.dragState.isDragging) return;

  // 阻止默认行为（防止文本选择等）
  e.preventDefault();

  // 更新幽灵元素位置
  if (_dragGhost) {
    _dragGhost.style.left = e.clientX + 'px';
    _dragGhost.style.top = e.clientY + 'px';
  }

  // 用 elementFromPoint 检测鼠标下方的拖放目标
  _updateDragOver(e.clientX, e.clientY);
}

/**
 * 正式启动拖拽
 */
function _startDrag(e) {
  const info = _dragMouseDown;
  state.dragState = {
    isDragging: true,
    type: info.type,
    id: info.id,
    sourceRootId: info.sourceRootId
  };

  // 给被拖拽元素添加 dragging 样式
  info.sourceEl.classList.add('dragging');

  // 创建幽灵预览元素
  _dragGhost = document.createElement('div');
  _dragGhost.className = 'peng-drag-ghost';
  const labelText = info.type === 'message' ? `消息 #${info.id}` : `事务 #${info.id}`;
  _dragGhost.textContent = labelText;
  _dragGhost.style.cssText = `
    position: fixed; pointer-events: none; z-index: 99999;
    padding: 4px 12px; border-radius: 4px; font-size: 12px;
    background: var(--accent-color, #4a9); color: #fff;
    box-shadow: 0 2px 8px rgba(0,0,0,0.3); opacity: 0.9;
    transform: translate(8px, 8px); white-space: nowrap;
  `;
  _dragGhost.style.left = e.clientX + 'px';
  _dragGhost.style.top = e.clientY + 'px';
  document.body.appendChild(_dragGhost);
}

/**
 * 更新拖拽悬停高亮（用 elementFromPoint 代替 dragover 事件）
 */
function _updateDragOver(clientX, clientY) {
  // 临时隐藏幽灵元素以获取下方真正元素
  if (_dragGhost) _dragGhost.style.display = 'none';
  const elBelow = document.elementFromPoint(clientX, clientY);
  if (_dragGhost) _dragGhost.style.display = '';

  // 清除上一次的 drag-over 高亮
  document.querySelectorAll('.drag-over').forEach(el => el.classList.remove('drag-over'));

  if (!elBelow) return;

  const messageNode = elBelow.closest('.message-tree-node');
  const messageChain = elBelow.closest('.message-chain');
  const taskNode = elBelow.closest('.task-chain');
  const messageTree = elBelow.closest('#message-tree');

  if (state.dragState.type === 'message') {
    let targetMsgId = null;
    if (messageNode) {
      targetMsgId = parseInt(messageNode.dataset.msgId);
    } else if (messageChain) {
      targetMsgId = parseInt(messageChain.dataset.msgId);
    }

    if ((messageNode || messageChain) && targetMsgId !== state.dragState.id &&
        !isDescendant(targetMsgId, state.dragState.id, 'message')) {
      (messageNode || messageChain).classList.add('drag-over');
    } else if (messageTree && !messageNode && !messageChain) {
      // 在消息树空白处，允许移动到根（不特殊高亮）
    }
  } else if (state.dragState.type === 'task') {
    if (taskNode) {
      const targetTaskId = parseInt(taskNode.dataset.taskId);
      if (targetTaskId !== state.dragState.id && !isDescendant(targetTaskId, state.dragState.id, 'task')) {
        taskNode.classList.add('drag-over');
      }
    }
  }
}

/**
 * mouseup：执行 drop 或取消拖拽
 */
function _handleMouseUp(e) {
  if (!state.dragState.isDragging) {
    _dragMouseDown = null;
    return;
  }

  // 获取鼠标下方的目标元素
  if (_dragGhost) _dragGhost.style.display = 'none';
  const elBelow = document.elementFromPoint(e.clientX, e.clientY);

  // 解析放置目标
  let dropTarget = null;
  if (elBelow) {
    const messageNode = elBelow.closest('.message-tree-node');
    const messageChain = elBelow.closest('.message-chain');
    const taskNode = elBelow.closest('.task-chain');
    const messageTree = elBelow.closest('#message-tree');

    if (state.dragState.type === 'message') {
      if (messageNode) {
        const targetMsgId = parseInt(messageNode.dataset.msgId);
        if (targetMsgId !== state.dragState.id && !isDescendant(targetMsgId, state.dragState.id, 'message')) {
          dropTarget = { type: 'message', targetId: targetMsgId };
        }
      } else if (messageChain) {
        const targetMsgId = parseInt(messageChain.dataset.msgId);
        if (targetMsgId !== state.dragState.id && !isDescendant(targetMsgId, state.dragState.id, 'message')) {
          dropTarget = { type: 'message', targetId: targetMsgId };
        }
      } else if (messageTree) {
        dropTarget = { type: 'message', targetId: null };
      }
    } else if (state.dragState.type === 'task' && taskNode) {
      const targetTaskId = parseInt(taskNode.dataset.taskId);
      if (targetTaskId !== state.dragState.id && !isDescendant(targetTaskId, state.dragState.id, 'task')) {
        dropTarget = { type: 'task', targetId: targetTaskId };
      }
    }
  }

  // 执行放置
  if (dropTarget) {
    _executeDrop(dropTarget);
  }

  // 清理
  _cleanupDrag();
  _dragMouseDown = null;
}

/**
 * 执行实际的 API 调用（从原 handleDrop 提取）
 */
async function _executeDrop(dropTarget) {
  try {
    if (dropTarget.type === 'message') {
      const msgId = state.dragState.id;
      const response = await fetch(`/api/messages/${msgId}/move`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ target_id: dropTarget.targetId })
      });
      if (response.ok) {
        const result = await response.json();
        showSimpleToast(`✅ ${result.message}`, 'success');
        await loadMessages(true);
      } else {
        const error = await response.json();
        showSimpleToast(`❌ 移动失败: ${error.detail || error.message}`, 'error');
      }
    } else if (dropTarget.type === 'task') {
      const taskId = state.dragState.id;
      const response = await fetch(`/api/tasks/${taskId}/move`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ target_id: dropTarget.targetId })
      });
      if (response.ok) {
        const result = await response.json();
        showSimpleToast(`✅ ${result.message}`, 'success');
        await loadTasks();
      } else {
        const error = await response.json();
        showSimpleToast(`❌ 移动失败: ${error.detail || error.message}`, 'error');
      }
    }
  } catch (error) {
    console.error('拖放失败:', error);
    showSimpleToast(`❌ 拖放失败: ${error.message}`, 'error');
  }
}

/**
 * 清理拖拽状态和 UI
 */
function _cleanupDrag() {
  document.querySelectorAll('.dragging, .drag-over').forEach(el => {
    el.classList.remove('dragging', 'drag-over');
  });
  if (_dragGhost) {
    _dragGhost.remove();
    _dragGhost = null;
  }
  state.dragState.isDragging = false;
  state.dragState.type = null;
  state.dragState.id = null;
  state.dragState.sourceRootId = null;
}

/**
 * 判断目标节点是否是源节点的后代
 */
function isDescendant(targetId, sourceId, type) {
  if (type === 'message') {
    const msg = state.messages.find(m => m.id === sourceId);
    if (!msg) return false;
    function checkDescendants(msgId) {
      const children = state.messages.filter(m => m.reply_to === msgId);
      for (const child of children) {
        if (child.id === targetId) return true;
        if (checkDescendants(child.id)) return true;
      }
      return false;
    }
    return checkDescendants(sourceId);
  } else if (type === 'task') {
    function checkTaskDescendants(taskId, tasks) {
      for (const task of tasks) {
        if (task.parent_task_id === taskId) {
          if (task.id === targetId) return true;
          if (checkTaskDescendants(task.id, tasks)) return true;
        }
      }
      return false;
    }
    return checkTaskDescendants(sourceId, state.tasks);
  }
  return false;
}
