// ==================== 消息/事务操作 撤销/重做系统（Task#1893） ====================
//
// 命令模式（Command Pattern）：每条历史记录 entry 描述一次用户操作的逆/正执行方式：
//   {
//     kind: 'edit'|'move'|'swap'|'archive'|'delete',
//     targetType: 'message'|'task',   // 归属哪个栏的历史栈
//     targetId: number,
//     label: string,                  // toast 描述
//     ts: number,
//     undo: {...}, redo: {...}        // 由 _execOpAction 解释执行
//   }
//
// kind 说明：
//   edit    content编辑。undo/redo = { content } → PUT /api/cli/{msg|task}/{id}/field/content
//           （后端会自动双向同步根消息↔根事务 content）
//   move    拖动移动。undo/redo = { parentId(或null=变根) } → POST /api/{messages|tasks}/{id}/move
//   swap    消息拖到自己的子孙时后端执行"内容交换"。undo/redo = { contents: {msgId: 旧content} }
//           → 逐条 PUT 恢复旧内容（交换幂等，用记录值更精确）
//   archive 归档/取消归档。undo/redo = { archive: bool } → POST archive/unarchive
//   delete  删除。undo = { backupFile, isRoot } → POST /api/undelete/restore
//           redo = { isRoot } → 重新 DELETE（新 backup_file 写回 entry.undo，保证再次撤销恢复最新状态）
//
// 历史栈独立于 state.undoStack/redoStack（那是输入框字符级撤销专用）。

const OP_HISTORY_LIMIT = 50;  // 每个栈最多保留条数

// 双栏历史栈：{ message: {undo:[], redo:[]}, task: {undo:[], redo:[]} }
const opHistory = {
  message: { undo: [], redo: [] },
  task: { undo: [], redo: [] },
};
let _opBusy = false;  // 撤销/重做执行中防重入

// ==================== 持久化 ====================

function _persistOpHistory() {
  try {
    for (const t of ['message', 'task']) {
      const stack = opHistory[t];
      localStorage.setItem(
        `peng-op-history-${t}`,
        JSON.stringify({ undo: stack.undo.slice(-OP_HISTORY_LIMIT), redo: stack.redo.slice(-OP_HISTORY_LIMIT) })
      );
    }
  } catch (e) {
    console.warn('[op-undo] 持久化失败:', e);
  }
}

function _restoreOpHistory() {
  for (const t of ['message', 'task']) {
    try {
      const raw = localStorage.getItem(`peng-op-history-${t}`);
      if (raw) {
        const data = JSON.parse(raw);
        opHistory[t].undo = Array.isArray(data.undo) ? data.undo : [];
        opHistory[t].redo = Array.isArray(data.redo) ? data.redo : [];
      }
    } catch (e) {
      console.warn('[op-undo] 恢复失败:', e);
      opHistory[t].undo = [];
      opHistory[t].redo = [];
    }
  }
}

// ==================== 入栈 ====================

/**
 * 记录一条可撤销操作（在正向操作成功后调用）
 * @param {'message'|'task'} targetType 操作发起的栏（历史归属）
 * @param {object} entry { kind, targetId, label, undo, redo }
 */
function pushOpHistory(targetType, entry) {
  const stack = opHistory[targetType] || (opHistory[targetType] = { undo: [], redo: [] });
  stack.undo.push({ ts: Date.now(), targetType, ...entry });
  if (stack.undo.length > OP_HISTORY_LIMIT) stack.undo.shift();
  stack.redo = [];  // 新操作使重做分支失效
  _persistOpHistory();
  updateOpUndoRedoButtons();
}

// ==================== 执行器 ====================

async function _opFetch(url, options) {
  const response = await fetch(url, options);
  let data = null;
  try { data = await response.json(); } catch (_) { /* 无body */ }
  if (!response.ok) {
    throw new Error((data && (data.detail || data.message)) || `HTTP ${response.status}`);
  }
  return data;
}

/** 编辑撤销/重做后按原保存逻辑通知关联事务（与 saveEditorContent 行为一致） */
async function _notifyEditChange(entry, content) {
  try {
    if (entry.targetType === 'task') {
      if (typeof _notifyTaskContentChange === 'function') {
        _notifyTaskContentChange(entry.targetId, `事务#${entry.targetId}的需求描述已变更：\n${content}`);
      }
      return;
    }
    const msg = typeof findMessageByIdRecursive === 'function'
      ? findMessageByIdRecursive(state.messages, entry.targetId) : null;
    const rootMsgId = msg ? (msg.chain_head_id || msg.id) : null;
    const relatedTask = rootMsgId && typeof findTaskByMessageId === 'function'
      ? findTaskByMessageId(rootMsgId) : null;
    if (relatedTask && relatedTask.id && msg && msg.receiver === state.agentName &&
        typeof _notifyTaskContentChange === 'function') {
      _notifyTaskContentChange(relatedTask.id, `消息#${entry.targetId}的需求描述已变更：\n${content}`);
    }
  } catch (e) {
    console.warn('[op-undo] 通知事务失败:', e);
  }
}

/** PUT 消息/事务 content（后端自动双向同步根消息↔根事务） */
async function _putContent(targetType, id, content) {
  const base = targetType === 'task' ? '/api/cli/task' : '/api/cli/msg';
  await _opFetch(`${base}/${id}/field/content`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ value: content }),
  });
}

/** 执行 entry 的 undo 或 redo 动作。成功返回 true；delete 的 redo 会写回新 backupFile */
async function _execOpAction(entry, dir) {
  const action = entry[dir] || {};
  const id = entry.targetId;

  switch (entry.kind) {
    case 'edit': {
      await _putContent(entry.targetType, id, action.content);
      await _notifyEditChange(entry, action.content);
      return true;
    }

    case 'move': {
      const base = entry.targetType === 'task' ? '/api/tasks' : '/api/messages';
      await _opFetch(`${base}/${id}/move`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ target_id: action.parentId ?? null }),
      });
      return true;
    }

    case 'swap': {
      // 按记录的旧内容逐条恢复（两个消息各 PUT 一次）
      const contents = action.contents || {};
      for (const msgId of Object.keys(contents)) {
        await _putContent('message', parseInt(msgId, 10), contents[msgId]);
      }
      return true;
    }

    case 'archive': {
      const endpoint = action.archive ? 'archive' : 'unarchive';
      await _opFetch(`/api/messages/${id}/${endpoint}`, { method: 'POST' });
      return true;
    }

    case 'delete': {
      if (dir === 'undo') {
        const data = await _opFetch('/api/undelete/restore', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ backup_file: action.backupFile }),
        });
        return true;
      }
      // redo：重新执行删除（使用与首次相同的端点），新快照写回 undo 以支持再次撤销
      let url;
      if (entry.targetType === 'task') {
        url = `/api/tasks/${id}`;
      } else if (action.isRoot) {
        url = `/api/messages/${id}`;
      } else {
        url = `/api/messages/${id}/recursive`;
      }
      const data = await _opFetch(url, { method: 'DELETE' });
      if (data && data.backup_file) {
        entry.undo.backupFile = data.backup_file;  // 写回最新快照
      } else {
        entry.undo.backupFile = null;  // 后端未备份，本条不可再次撤销
      }
      return true;
    }

    default:
      console.warn('[op-undo] 未知操作类型:', entry.kind);
      return false;
  }
}

/** 撤销/重做后的列表刷新（必须 await loadMessages 完成后再 loadTasks：
 *  loadTasks 依赖 renderMessageTree 更新的 displayedMessageIds，
 *  否则恢复的消息/事务不在请求范围内，事务列表不会立刻显示） */
async function _refreshAfterOp(entry) {
  skipNextMessagesPoll = true;  // 跳过下一次轮询，避免与手动刷新竞争
  await loadMessages(false, true);
  lastTasksData = '';  // 清除去重缓存，确保 loadTasks 不被跳过
  loadTasks();
}

/** 删除恢复的快照失效（404）时清理引用 */
function _invalidateDeleteEntry(entry) {
  if (entry.kind === 'delete' && entry.undo) entry.undo.backupFile = null;
}

// ==================== 撤销 / 重做 ====================

async function performOpUndo(targetType) {
  if (_opBusy) return;
  const stack = opHistory[targetType];
  if (!stack || stack.undo.length === 0) return;

  const entry = stack.undo[stack.undo.length - 1];
  // 删除类且快照凭据已失效：直接跳过该条（不可撤销）
  if (entry.kind === 'delete' && !entry.undo.backupFile) {
    stack.undo.pop();
    _persistOpHistory();
    updateOpUndoRedoButtons();
    showSimpleToast('↩️ 该删除操作的撤销凭据已失效，已跳过', 'warning');
    return performOpUndo(targetType);
  }

  _opBusy = true;
  updateOpUndoRedoButtons();
  try {
    await _execOpAction(entry, 'undo');
    stack.undo.pop();
    stack.redo.push(entry);
    _persistOpHistory();
    await _refreshAfterOp(entry);
    showSimpleToast(`↩️ 已撤销：${entry.label}`, 'success');
  } catch (error) {
    console.error('[op-undo] 撤销失败:', error);
    if (String(error.message || '').includes('快照不存在')) {
      _invalidateDeleteEntry(entry);
    }
    showSimpleToast(`❌ 撤销失败：${error.message || error}`, 'error');
  } finally {
    _opBusy = false;
    updateOpUndoRedoButtons();
  }
}

async function performOpRedo(targetType) {
  if (_opBusy) return;
  const stack = opHistory[targetType];
  if (!stack || stack.redo.length === 0) return;

  const entry = stack.redo[stack.redo.length - 1];
  _opBusy = true;
  updateOpUndoRedoButtons();
  try {
    await _execOpAction(entry, 'redo');
    stack.redo.pop();
    stack.undo.push(entry);
    _persistOpHistory();
    await _refreshAfterOp(entry);
    showSimpleToast(`↪️ 已重做：${entry.label}`, 'success');
  } catch (error) {
    console.error('[op-undo] 重做失败:', error);
    showSimpleToast(`❌ 重做失败：${error.message || error}`, 'error');
  } finally {
    _opBusy = false;
    updateOpUndoRedoButtons();
  }
}

// ==================== 按钮状态与初始化 ====================

function updateOpUndoRedoButtons() {
  for (const t of ['message', 'task']) {
    const undoBtn = document.getElementById(`${t === 'message' ? 'msg' : 'task'}-op-undo-btn`);
    const redoBtn = document.getElementById(`${t === 'message' ? 'msg' : 'task'}-op-redo-btn`);
    const stack = opHistory[t];
    if (undoBtn) {
      const hasUndo = stack && stack.undo.some(e => !(e.kind === 'delete' && !e.undo.backupFile));
      undoBtn.disabled = !hasUndo || _opBusy;
    }
    if (redoBtn) {
      redoBtn.disabled = !(stack && stack.redo.length > 0) || _opBusy;
    }
  }
}

function initOpUndoRedo() {
  _restoreOpHistory();

  const msgUndo = document.getElementById('msg-op-undo-btn');
  const msgRedo = document.getElementById('msg-op-redo-btn');
  const taskUndo = document.getElementById('task-op-undo-btn');
  const taskRedo = document.getElementById('task-op-redo-btn');

  if (msgUndo) msgUndo.addEventListener('click', () => performOpUndo('message'));
  if (msgRedo) msgRedo.addEventListener('click', () => performOpRedo('message'));
  if (taskUndo) taskUndo.addEventListener('click', () => performOpUndo('task'));
  if (taskRedo) taskRedo.addEventListener('click', () => performOpRedo('task'));

  updateOpUndoRedoButtons();
}
