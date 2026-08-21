// ==================== 焦点管理 ====================
// 🔧 Task#1092：全面修复消息输入框焦点丢失问题
// 主要改动：
// 1. 添加 IME 输入法组合状态保护（compositionstart/end），组合期间不抢焦点
// 2. 避免对已有焦点的 mainInput 重复调用 .focus()（防止光标重置/IME中断）
// 3. 将焦点恢复延迟从 10ms 降低到 0ms（用微任务/立即执行）
// 4. 添加 blur 兜底机制：焦点意外离开 mainInput 到 body 时自动恢复
// 🔧 Task#1098：修复 blur 兜底机制破坏消息/事务栏/xterm终端文字划选
// 用户在消息/事务/xterm区域 mousedown 划选文字时，mainInput 失焦触发 blur，
// 此时 hasSelectedText() 尚为 false（刚开始划），blur handler 会抢回焦点，
// 破坏正在进行的文字选择。解决：跟踪鼠标在划选区域的按下状态。

function initFocusManagement() {
  const searchInput = document.getElementById('message-search');
  const taskSearchInput = document.getElementById('task-search');
  const mainInput = document.getElementById('message-input');
  const outputEl = document.getElementById('output');
  const workdirInput = document.getElementById('workdir-input');
  const thinkingBudgetInput = document.getElementById('thinking-budget');
  const thinkingSlider = document.getElementById('thinking-budget-slider');

  // 🔧 Task#1098：跟踪鼠标在文字划选区域（消息树/事务树/xterm终端）的按下状态
  // 用户在这些区域 mousedown 划选文字时，mainInput 失焦触发 blur，
  // 此时 hasSelectedText() 尚为 false（刚开始划），blur handler 会抢回焦点，
  // 破坏正在进行的文字选择。解决：跟踪鼠标在划选区域的按下状态。
  // 时序：mousedown→blur(setTimeout0中检查标志=true→不抢焦点)→drag→mouseup(清除标志)→click(正常恢复焦点)
  let _mouseDownInSelectionArea = false;
  document.addEventListener('mousedown', (e) => {
    if (e.target.closest('#message-tree') || e.target.closest('#task-tree') ||
        e.target.closest('#output') || e.target.closest('.terminal-instance') ||
        e.target.closest('#rollback-confirm-dialog') ||  // 回滚确认框：划选文本自动复制，需同样保护
        e.target.closest('#detail-modal')) {  // 消息/事务详情窗口：划选文本自动复制，需同样保护
      _mouseDownInSelectionArea = true;
    }
  }, true);  // 捕获阶段，确保最先执行
  document.addEventListener('mouseup', () => {
    _mouseDownInSelectionArea = false;
  }, true);

  // 🔧 Task#1092：IME 输入法组合状态跟踪
  // 用户正在用输入法打字时，绝对不能抢焦点，否则会中断组合、丢失输入
  let isComposing = false;
  if (mainInput) {
    mainInput.addEventListener('compositionstart', () => { isComposing = true; });
    mainInput.addEventListener('compositionend', () => {
      // 延迟清除标志，因为 compositionend 后浏览器可能还有后续处理
      setTimeout(() => { isComposing = false; }, 100);
    });
  }

  // 默认聚焦到输入框
  if (mainInput) {
    mainInput.focus();
  }

  // 🔧 Task#1092：安全聚焦函数 —— 仅在必要时调用 .focus()
  // 避免对已有焦点的元素重复调用 .focus()，防止光标位置重置
  function safeFocusMainInput() {
    if (!mainInput) return;
    if (document.activeElement === mainInput) return;  // 已有焦点，不需要重复
    if (isComposing) return;  // IME 组合中，不抢焦点
    if (window._suppressFocusManagement) return;
    if (_mouseDownInSelectionArea) return;  // 🔧 Task#1098：鼠标在划选区域按下时，不抢焦点（允许文字划选）
    mainInput.focus();
  }

  // 允许保持焦点的元素（这些输入框可以获得焦点）
  function isAllowedFocusTarget(target) {
    if (!target) return false;
    return target === searchInput ||
      target === taskSearchInput ||
      target === workdirInput ||
      target === thinkingBudgetInput ||
      target === thinkingSlider ||
      target.classList?.contains('output-keyword-highlight-input') ||  // xterm关键词高亮输入框
      target.tagName === 'SELECT' ||
      target.classList?.contains('file-browser-filename-input') ||
      target.classList?.contains('file-browser-newdir-input') ||
      target.classList?.contains('provider-priority-input') ||
      target.closest('.terminal-instance') ||  // 允许远程终端获得焦点
      target.closest('.modal') ||  // 允许模态框内的输入框获得焦点
      target.closest('.font-settings-dialog');  // 允许字体设置对话框内的控件获得焦点
  }

  // 检查是否在编辑器内
  function isInEditor(target) {
    if (!target) return false;
    // 🔧 Bug2修复：非默认布局下editorView是hidden的，但Monaco编辑器在#editor-groups-container中
    // 需要同时检查editor-groups-container和editor-group
    // 优先检查Monaco相关元素（所有布局通用）
    if (target.closest('.monaco-editor') || target.closest('.monaco-list') || target.closest('.editor-group') || target.closest('#editor-groups-container')) {
      return true;
    }
    // 默认布局下检查editor-view（包含editor-tabs和monaco-editor-container）
    const editorView = document.getElementById('editor-view');
    if (editorView && editorView.style.display !== 'none' && target.closest('#editor-view')) {
      return true;
    }
    // 检查布局面板中的编辑器标签栏
    if (target.closest('#layout-editor-panel')) {
      return true;
    }
    // 🔧 Task#184修复：默认布局下编辑器头部工具栏（#editor-header）不在#editor-view内，
    // 点击编辑器按钮（保存、另存为、分拆、历史等）时焦点不应切到消息输入框
    if (target.closest('#editor-header')) {
      return true;
    }
    // 检查编辑器标签页切换按钮
    if (target.closest('.view-tab[data-view="editor"]')) {
      return true;
    }
    return false;
  }

  // 点击页面任意位置时，检查焦点
  document.addEventListener('click', (e) => {
    const target = e.target;

    // 如果有文本被选中（用户正在划选），不要强制切回焦点
    if (hasSelectedText()) {
      return;
    }

    // 如果编辑器已打开，且点击在编辑器内，允许保持焦点
    if (isInEditor(target)) {
      return;
    }

    // 如果点击的是允许的输入框，保持焦点
    if (isAllowedFocusTarget(target)) {
      return;
    }

    // 如果点击的是主输入框本身，保持焦点
    if (target === mainInput) {
      return;
    }

    // 🔧 Task#1402：点击 xterm 终端区域时不再 early return，
    // 让无选中文本的点击正常恢复焦点到消息输入框。
    // 划选文本的场景由 hasSelectedText() 检查（line 118）和
    // _mouseDownInSelectionArea 标志（safeFocusMainInput 中检查）保护。

    // 其他所有点击（包括按钮、checkbox、消息/事务项等），焦点回到主输入框
    // 🔧 Task#1092：使用 setTimeout(0) 替代 setTimeout(10)，减少焦点恢复延迟
    setTimeout(() => {
      if (!hasSelectedText()) {
        safeFocusMainInput();
      }
      // 收起所有临时展开的消息
      collapseAllTempExpandedMessages();
    }, 0);
  });

  // 监听焦点变化
  document.addEventListener('focusin', (e) => {
    const target = e.target;

    // 🔧 Task #146：如果正在执行编辑器复制操作（fallbackCopy等），禁止焦点管理器抢焦点
    if (window._suppressFocusManagement) {
      return;
    }

    // 🔧 Task#1092：IME 组合期间不处理任何焦点变化
    if (isComposing) {
      return;
    }

    // 如果有文本被选中（用户正在划选），不要强制切回焦点
    if (hasSelectedText()) {
      return;
    }

    // 如果编辑器已打开，且焦点在编辑器内，允许保持焦点
    if (isInEditor(target)) {
      return;
    }

    // 允许特定输入框获得焦点
    if (isAllowedFocusTarget(target)) {
      return;
    }

    // 主输入框本身可以获得焦点
    if (target === mainInput) {
      return;
    }

    // 如果点击的是消息树节点或事务链等可点击元素，不触发焦点切换
    // 这些元素的点击会在 click 事件中处理焦点切换
    if (target.closest('.message-tree-node') ||
      target.closest('.message-chain-head') ||
      target.closest('.task-chain-head') ||
      target.closest('.task-chain') ||
      target.closest('.tree-node-main')) {
      return;
    }

    // 如果当前焦点已经在主输入框，且焦点短暂变化后又回到主输入框，不做处理
    // 这避免了点击分支消息等操作时的"闪烁"效果
    if (document.activeElement === mainInput) {
      return;
    }

    // 其他元素获得焦点时，立即切换到主输入框
    // 🔧 Task#1092：从 setTimeout(10) 改为 setTimeout(0)
    // 10ms 延迟在快速输入时会导致字符丢失（焦点在错误元素上停留太久）
    setTimeout(() => {
      // 🔧 Task #146：延迟回调中也要检查抑制标志
      if (window._suppressFocusManagement) {
        return;
      }
      // 🔧 Task#1092：IME 组合期间不恢复焦点
      if (isComposing) {
        return;
      }
      // 再次检查：如果焦点已经回到主输入框，不做处理
      if (document.activeElement === mainInput) {
        return;
      }
      // 再次检查是否有文本被选中
      if (!hasSelectedText() && !isAllowedFocusTarget(document.activeElement)) {
        safeFocusMainInput();
      }
    }, 0);
  });

  // 🔧 Task#1092：添加 mainInput 的 blur 兜底机制
  // 当 mainInput 失去焦点且新焦点不在任何允许的元素上时，自动恢复焦点
  // 这处理了 focusin 事件无法捕获的情况（如焦点直接跳到 body）
  if (mainInput) {
    mainInput.addEventListener('blur', () => {
      // IME 组合中不处理（如点击候选词时短暂失焦）
      if (isComposing) return;
      if (window._suppressFocusManagement) return;

      // 延迟检查，给 click/focusin 事件处理时间
      setTimeout(() => {
        if (isComposing) return;
        if (window._suppressFocusManagement) return;
        if (document.activeElement === mainInput) return;  // 已恢复
        if (hasSelectedText()) return;  // 用户在选文本

        const active = document.activeElement;
        // 如果焦点在 body 或其他非交互元素上，恢复到 mainInput
        if (!active || active === document.body ||
            (!isAllowedFocusTarget(active) && !isInEditor(active) &&
             active.tagName !== 'BUTTON' && active.tagName !== 'INPUT' &&
             active.tagName !== 'SELECT' && active.tagName !== 'TEXTAREA' &&
             !active.isContentEditable)) {
          safeFocusMainInput();
        }
      }, 0);
    });
  }
}
