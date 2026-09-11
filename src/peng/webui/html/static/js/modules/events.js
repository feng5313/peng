// ==================== 事件监听 ====================

// 🔧 事务#1545修复：undo栈持久化防抖（模块级工具）
// 设计要点：
// 1. 连续输入/删除时只在停顿800ms后写一次 localStorage，避免每键 O(栈大小×文本长度) 的同步序列化阻塞主线程
// 2. 页面隐藏（visibilitychange）或卸载（beforeunload/pagehide）时立即落盘，保证刷新/关闭不丢失撤销历史
// 3. 写入失败静默降级（undo 功能本身在内存中仍完整可用）
const UNDO_PERSIST_DEBOUNCE_MS = 800;
let _undoPersistTimer = null;
function persistUndoStackNow() {
  try {
    // Range对象无法直接JSON序列化，只保存文本
    const textOnlyStack = state.undoStack.map(item => ({ text: item.text }));
    localStorage.setItem('peng_undo_stack', JSON.stringify(textOnlyStack.slice(-500))); // 最多保存500条
  } catch (e) { console.warn('[undo] 保存失败:', e); }
}
function scheduleUndoPersist() {
  if (_undoPersistTimer) clearTimeout(_undoPersistTimer);
  _undoPersistTimer = setTimeout(() => {
    _undoPersistTimer = null;
    persistUndoStackNow();
  }, UNDO_PERSIST_DEBOUNCE_MS);
}
// 页面即将隐藏/卸载时立即落盘（用户可能直接刷新或关标签页）
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'hidden' && _undoPersistTimer) {
    clearTimeout(_undoPersistTimer);
    _undoPersistTimer = null;
    persistUndoStackNow();
  }
});
window.addEventListener('beforeunload', () => {
  if (_undoPersistTimer) {
    clearTimeout(_undoPersistTimer);
    _undoPersistTimer = null;
    persistUndoStackNow();
  }
});
window.addEventListener('pagehide', persistUndoStackNow);

function initEventListeners() {
  // 🔧 Task#178 Bug3修复：编辑器区域禁用浏览器默认右键菜单
  // 关键：不在 .monaco-editor 内部阻止默认行为，因为Monaco使用自己的右键菜单
  // 在Edge浏览器中，捕获阶段的preventDefault会阻止Monaco上下文菜单
  // 所以只在编辑器标签栏、空白容器等非Monaco区域阻止
  const isEditorNonMonacoArea = (e) => {
    // 如果点击在Monaco编辑器内部（包括Monaco的上下文菜单、建议列表等），不阻止
    if (e.target.closest('.monaco-editor, .monaco-list, .monaco-menu, .monaco-context-view')) {
      return false;
    }
    // 在编辑器标签栏、空白容器等区域，阻止默认右键菜单
    return e.target.closest('.editor-group, #editor-groups-container, .editor-view, #editor-view, .editor-container, .editor-tabs, #editor-tabs');
  };
  // 捕获阶段：只在非Monaco区域阻止默认行为
  document.addEventListener('contextmenu', (e) => {
    if (isEditorNonMonacoArea(e)) e.preventDefault();
  }, true);
  // 冒泡阶段：再次检查（某些浏览器在冒泡阶段才显示原生菜单）
  document.addEventListener('contextmenu', (e) => {
    if (isEditorNonMonacoArea(e)) e.preventDefault();
  }, false);

  // 编辑图标点击事件（事件委托）
  document.addEventListener('click', (e) => {
    if (e.target.classList.contains('edit-text-icon')) {
      e.stopPropagation();

      // 直接根据DOM结构判断类型并调用对应函数
      const editIcon = e.target.closest('.edit-text-icon');
      const branchMsgNode = e.target.closest('.message-tree-node');
      const taskNode = e.target.closest('.task-chain');

      let rawText = '';
      if (editIcon) {
        // 从编辑图标获取data-root-msg-id来判断是分支消息还是消息
        const rootMsgId = editIcon.getAttribute('data-root-msg-id');
        const editType = editIcon.getAttribute('data-edit-type');
        const editId = editIcon.getAttribute('data-edit-id');
        rawText = editIcon.getAttribute('data-raw-text') || '';

        // 使用统一的打开编辑器函数（传递rootMsgId）
        openEditorForContent(editType, editId, rawText, rootMsgId);
      }

      // 保持输入框焦点
      const mainInput = document.getElementById('message-input');
      if (mainInput) {
        setTimeout(() => mainInput.focus(), 10);
      }
    }
  });


  // 终端头部按钮
  const terminalFullscreenBtn = document.getElementById('terminal-fullscreen');
  if (terminalFullscreenBtn) {
    terminalFullscreenBtn.addEventListener('click', () => {
      toggleTerminalFullscreen();
    });
  }

  // 全屏切换
  const fullscreenToggle = document.getElementById('fullscreen-toggle');
  if (fullscreenToggle) {
    fullscreenToggle.addEventListener('click', toggleFullscreen);
  }

  // 主题切换
  const themeToggle = document.getElementById('theme-toggle');
  if (themeToggle) {
    themeToggle.addEventListener('click', toggleTheme);
  }

  // 字体设置
  const fontToggle = document.getElementById('font-toggle');
  if (fontToggle) {
    fontToggle.addEventListener('click', showFontSettings);
  }

  // 发送按钮
  const sendBtn = document.getElementById('send-btn');
  if (sendBtn) {
    sendBtn.addEventListener('click', sendMessage);
  }

  // 帮助按钮
  const helpBtn = document.getElementById('help-btn');
  if (helpBtn) {
    helpBtn.addEventListener('click', showHelp);
  }

  // API provider按钮
  const apiProviderBtn = document.getElementById('api-provider-btn');
  if (apiProviderBtn) {
    apiProviderBtn.addEventListener('click', showApiProviderModal);
  }

  // API provider模态框关闭按钮
  const apiProviderClose = document.getElementById('api-provider-close');
  const apiProviderCloseBtn = document.getElementById('api-provider-close-btn');
  if (apiProviderClose) {
    apiProviderClose.addEventListener('click', closeApiProviderModal);
  }
  if (apiProviderCloseBtn) {
    apiProviderCloseBtn.addEventListener('click', closeApiProviderModal);
  }

  // API provider新建按钮
  const apiProviderCreateBtn = document.getElementById('api-provider-create-btn');
  if (apiProviderCreateBtn) {
    apiProviderCreateBtn.addEventListener('click', showCreateProviderModal);
  }

  // 新建provider模态框按钮
  const createProviderClose = document.getElementById('create-provider-close');
  const createProviderCancelBtn = document.getElementById('create-provider-cancel-btn');
  const createProviderSaveBtn = document.getElementById('create-provider-save-btn');
  if (createProviderClose) {
    createProviderClose.addEventListener('click', closeCreateProviderModal);
  }
  if (createProviderCancelBtn) {
    createProviderCancelBtn.addEventListener('click', closeCreateProviderModal);
  }
  if (createProviderSaveBtn) {
    createProviderSaveBtn.addEventListener('click', saveNewProvider);
  }
  const createProviderModal = document.getElementById('create-provider-modal');
  if (createProviderModal) {
    createProviderModal.addEventListener('click', (e) => {
      if (e.target === createProviderModal) {
        closeCreateProviderModal();
      }
    });
  }

  // 点击模态框背景关闭
  const apiProviderModal = document.getElementById('api-provider-modal');
  if (apiProviderModal) {
    apiProviderModal.addEventListener('click', (e) => {
      if (e.target === apiProviderModal) {
        closeApiProviderModal();
      }
    });
  }

  // ===== Channel 配置模态框事件 =====
  initChannelModals();

  // 新消息按钮
  const newMsgBtn = document.getElementById('new-msg-btn');
  if (newMsgBtn) {
    newMsgBtn.addEventListener('click', () => {
      processBuiltinCommand('/new');
    });
  }

  // 输入框快捷键
  const input = document.getElementById('message-input');
  if (input) {
    // 🔧 修复：添加捕获阶段的Esc键监听器，在浏览器处理之前拦截
    input.addEventListener('keydown', (e) => {
      // 🔧 Task#1432：IME 组合期间不拦截任何按键，让浏览器/IME自然处理
      // （组合中按 Esc 应取消 IME 组合，而非关闭历史列表）
      if (e.isComposing || e.keyCode === 229) return;
      if (e.key === 'Escape') {
        e.preventDefault();
        e.stopPropagation();
        hideInputHistoryList();
      }
    }, { capture: true });  // 使用捕获阶段

    input.addEventListener('keydown', (e) => {
      // 🔧 Task#1432：IME 组合输入期间（如中文输入法选词/确认），不拦截任何按键
      // 根因：用户用IME输入文字后按Enter确认候选词时，e.isComposing===true，
      //       如果不跳过，preventDefault 会阻止IME组合提交，导致：
      //       1) IME组合文本未写入DOM
      //       2) sendMessage读到空内容直接return
      //       3) input.innerHTML='' 清空一切
      //       用户体验：按了回车，什么都没发生（消息丢失！）
      // keyCode 229 是老浏览器/部分IME的兼容补充（某些IME不设isComposing但发229）
      if (e.isComposing || e.keyCode === 229) return;

      // 🔧 历史列表可见时，除了纯上下键以外，其他任何操作都应关闭历史列表
      // 仅无修饰键的 ArrowUp/ArrowDown 用于历史列表导航，不在此时关闭
      // Tab 和 Delete 需要对历史列表做特殊处理（选中/删除），在其各自分支中单独关闭
      //
      // 🔧 事务#1545优化：字符键（含 Backspace）不再立即关闭列表。
      // 此前逻辑：字符键先立即 hide 列表，随后 input 事件又 50ms 防抖重新 show →
      //   1) 列表每键闪烁一次（hide/unbind + show/rebind + 全量重渲染）
      //   2) 渲染开销翻倍，加剧 Backspace 删除卡顿
      // 现在：字符编辑键交由 input 事件的防抖逻辑统一处理（内容变化→重新过滤渲染；
      //   匹配为0/为空/超2行→隐藏）。命令键（Esc/Enter/PageUp/PageDown/方向翻页等）
      //   与修饰键组合仍立即关闭。Enter/Ctrl+Enter/Ctrl+Shift+Enter 分支中也各自 hide。
      const isPlainArrowNav = (e.key === 'ArrowUp' || e.key === 'ArrowDown') && !e.altKey && !e.shiftKey && !e.ctrlKey;
      // 字符编辑键：单字符可见键 + Backspace + Delete（无修饰键）—— 文本将变化，由 input 防抖接管
      const isPlainCharEdit = (e.key.length === 1 && !e.ctrlKey && !e.altKey && !e.metaKey) ||
                               (e.key === 'Backspace' && !e.ctrlKey && !e.altKey && !e.metaKey) ||
                               (e.key === 'Delete' && !e.ctrlKey && !e.altKey && !e.metaKey && !e.shiftKey);
      if (!isPlainArrowNav && e.key !== 'Tab' && e.key !== 'Delete' && !isPlainCharEdit) {
        hideInputHistoryList();
      }

      // 输入新内容时xterm回到底部
      if (input.innerText.trim() === '' && (e.key.length === 1 || e.key === 'Enter')) {
        scrollToNewest();
      }
      if (e.key === 'PageUp') {
        if (!isAtTop()) {
          state.output.scrollPages(-1);
        } else
          loadPrevPage();
        return;
      }
      if (e.key === 'PageDown') {
        if (!isAtBottom()) {
          state.output.scrollPages(1);
        } else
          loadNextPage();
        return;
      }

      // Ctrl+Shift+Enter: 切换输入模式 或 按新模式发送
      if (e.key === 'Enter' && e.ctrlKey && e.shiftKey) {
        e.preventDefault();
        hideInputHistoryList();
        const hadContent = input.innerText.trim().length > 0;
        switchInputMode()
        if (hadContent) sendMessage();
        input.innerHTML = '';
        return;
      }
      // Ctrl+Enter: 直接发送新消息（相当于 /new + 发送）
      if (e.key === 'Enter' && e.ctrlKey) {
        e.preventDefault();
        state.inputMode = 'message';
        input.classList.remove('input-mode-task');
        hideInputHistoryList();  // 隐藏历史记录列表
        // 先执行 /new 逻辑
        state.currentMessageId = null;
        state.currentTaskId = null;
        state.currentBranchMessageId = null;  // 清空分支消息选中状态，确保发送新消息链
        state.isCreatingNewMessage = true;
        document.querySelectorAll('.message-chain-head').forEach(el => {
          el.classList.remove('active');
        });
        document.querySelectorAll('.task-chain').forEach(el => {
          el.classList.remove('active');
        });
        updateInputPlaceholder(null);
        // 清空终端
        if (state.output) {
          state.output.clear(); state.currentColor = '';
        }
        // 然后发送消息
        sendMessage();
        input.innerHTML = '';
        return;
      }
      // Enter: 关闭历史列表，直接发送输入框内容（Shift+Enter 换行）
      if (e.key === 'Enter' && !e.shiftKey && !e.altKey && !e.ctrlKey) {
        e.preventDefault();
        hideInputHistoryList();  // 隐藏历史记录列表
        sendMessage();
        input.innerHTML = '';
        return;
      }
      // Shift+Delete: 清空输入框
      if (e.key === 'Delete' && e.shiftKey) {
        e.preventDefault();
        input.innerHTML = '';
        state.historyIndex = -1;
        hideInputHistoryList();
        return;
      }
      // Delete: 如果历史列表弹出 且 光标在末尾 且 有选中的历史项，删除该历史项；否则执行默认删除功能
      if (e.key === 'Delete' && !e.shiftKey && !e.ctrlKey && !e.altKey) {
        const selection = window.getSelection();
        const selectedText = selection.toString();

        // 如果有选中文本，执行正常的删除操作（不preventDefault）
        if (selectedText) {
          return;
        }

        // 检查历史列表是否可见
        const historyList = document.getElementById('input-history-list');
        const isHistoryListVisible = historyList && historyList.style.display !== 'none';

        // 检查光标是否在输入框末尾
        const selectionRange = selection.getRangeAt(0);
        const isCursorAtEnd = isCursorAtInputEnd(selectionRange, input);

        // 只有当历史列表可见、光标在末尾、且有选中历史项时，才删除历史项
        const dd = getMessageHistoryDropdown();
        if (isHistoryListVisible && isCursorAtEnd && dd && dd.getFilteredIndex() >= 0) {
          e.preventDefault();
          deleteSelectedHistoryItem();
          return;
        }
        // 其他情况，执行默认的删除功能（不preventDefault），并关闭历史列表
        hideInputHistoryList();
      }
      // Shift+Backspace: 删半句话（往前删直到碰到标点符号）
      if (e.key === 'Backspace' && e.shiftKey) {
        e.preventDefault();
        deleteHalfSentence(input);
        return;
      }
      // Tab: 如果有高亮的历史项，填入输入框；否则切换inputMode
      if (e.key === 'Tab') {
        e.preventDefault();
        // 如果历史列表有选中项，把选中项填入输入框
        const dd = getMessageHistoryDropdown();
        if (dd && dd.getFilteredIndex() >= 0) {
          applySelectedHistoryItem();
        } else {
          // 🔧 历史列表无选中项时，关闭列表并切换inputMode
          hideInputHistoryList();
          switchInputMode();
        }
        return;
      }
      // 🔧 事务#1732修复：上/下箭头按输入框行数分流
      // - 多行输入（≥2行）：上下键用于光标在行间移动（换行编辑），不拦截（让浏览器默认行为生效），
      //   同时关闭历史列表（用户在编辑文本，而非查找历史）。此前无条件拦截导致多行文本无法换行移动光标。
      // - 单行输入：维持终端式历史导航（列表可见时高亮移动；列表隐藏时 navigate 的 render 会弹出列表）
      // 行数计算与 input 事件防抖逻辑保持一致（innerText.trim() 统计换行符）
      const isPlainArrowKey = (e.key === 'ArrowUp' || e.key === 'ArrowDown') && !e.altKey && !e.shiftKey && !e.ctrlKey;
      if (isPlainArrowKey) {
        const inputLineCount = (input.innerText.trim().match(/\n/g) || []).length + 1;
        if (inputLineCount >= 2) {
          hideInputHistoryList();
          return;  // 不 preventDefault，浏览器默认移动光标到上一行/下一行
        }
        e.preventDefault();
        navigateFilteredHistory(e.key === 'ArrowUp' ? -1 : 1);  // -1 往上（更老），1 往下（更新）
        return;
      }
      // Ctrl+C: 如果没有选中文字，暂停当前选中的事务分支
      if (e.key === 'c' && e.ctrlKey && !e.shiftKey && !e.altKey) {
        const selection = window.getSelection();
        const selectedText = selection.toString();
        if (!selectedText) {
          // 没有选中文字，暂停事务
          e.preventDefault();
          pauseCurrentTaskBranch();
          return;
        }
        // 有选中文字，执行正常的复制操作（不preventDefault）
      }
      // 🔧 Ctrl+Z: 无限 Undo（撤销所有字符变化历史）
      if (e.key === 'z' && e.ctrlKey && !e.shiftKey && !e.altKey) {
        e.preventDefault();
        if (state.undoStack.length > 0) {
          // 保存当前光标位置
          const selection = window.getSelection();
          const cursorRange = selection.rangeCount > 0 ? selection.getRangeAt(0).cloneRange() : null;

          // 当前状态保存到 redo 栈
          state.redoStack.push({
            text: input.innerText,
            range: cursorRange
          });
          // 从 undo 栈恢复
          const prev = state.undoStack.pop();
          state.isUndoing = true;
          input.innerText = prev.text;

          // 恢复光标位置
          if (prev.range) {
            selection.removeAllRanges();
            selection.addRange(prev.range);
          }

          state.isUndoing = false;
        }
        return;
      }
      // 🔧 Ctrl+Y 或 Ctrl+Shift+Z: 无限 Redo
      if ((e.key === 'y' && e.ctrlKey && !e.shiftKey && !e.altKey) ||
        (e.key === 'z' && e.ctrlKey && e.shiftKey && !e.altKey)) {
        e.preventDefault();
        if (state.redoStack.length > 0) {
          // 保存当前光标位置
          const selection = window.getSelection();
          const cursorRange = selection.rangeCount > 0 ? selection.getRangeAt(0).cloneRange() : null;

          // 当前状态保存到 undo 栈
          state.undoStack.push({
            text: input.innerText,
            range: cursorRange
          });
          // 从 redo 栈恢复
          const next = state.redoStack.pop();
          state.isUndoing = true;
          input.innerText = next.text;

          // 恢复光标位置
          if (next.range) {
            selection.removeAllRanges();
            selection.addRange(next.range);
          }

          state.isUndoing = false;
        }
        return;
      }
      // 其他按键时重置历史索引（用户开始输入新内容）
      if (e.key.length === 1 && state.historyIndex === -1) {
        // 保存当前输入作为草稿
        state.historyDraft = input.innerText;
      }
    });

    // 点击输入框时隐藏历史记录列表 + 初始化 undo 跟踪
    input.addEventListener('focus', () => {
      hideInputHistoryList();
      // 🔧 初始化 lastInputValue（用于跟踪变化）
      state.lastInputValue = input.innerText;
      // contenteditable不支持selectionStart，暂时不保存光标位置
      state.lastInputCursor = 0;
    });

    // 用户输入时：记录到 undo 栈 + 自动弹出历史列表 + 滚动 xterm 到底部
    input.addEventListener('input', () => {
      // 🔧 事务#1545优化：innerText 每次访问都要对 contenteditable 做全文序列化（可能强制布局），
      // 本处理器内多处使用，统一读取一次复用（此前每次按键读取 4 次）
      const currentText = input.innerText;

      // 🔧 记录字符变化到 undo 栈（避免递归）
      if (!state.isUndoing) {
        // 如果值确实变化了，记录旧值到 undo 栈
        if (state.lastInputValue !== currentText) {
          // 保存当前光标位置
          const selection = window.getSelection();
          const cursorRange = selection.rangeCount > 0 ? selection.getRangeAt(0).cloneRange() : null;

          state.undoStack.push({
            text: state.lastInputValue,
            range: cursorRange
          });
          // 🔧 修复内存泄漏：内存中的undoStack也要限制大小（与localStorage一致）
          // 超过500条时裁剪旧条目，防止长时间运行后内存无限增长导致卡顿
          if (state.undoStack.length > 500) {
            state.undoStack = state.undoStack.slice(-500);
          }
          // 新输入时清空 redo 栈
          state.redoStack = [];

          // 🔧 事务#1545修复：undo栈持久化改为防抖（800ms）
          // 根因：此前每次按键都同步 JSON.stringify(整个undoStack≤500条×全文快照)
          // 并同步写 localStorage，长文本连续输入/删除时主线程被 O(N²) 序列化阻塞，
          // 导致 Backspace 删除极慢（即使无 history 匹配也慢）
          scheduleUndoPersist();
        }
        // 更新记录（用于下次比较）
        state.lastInputValue = currentText;
      }

      // 自动弹出历史列表（带防抖：防止 execCommand 等操作触发多次 input 事件导致重复渲染）
      const inputValue = currentText.trim();
      const inputLineCount = (inputValue.match(/\n/g) || []).length + 1;
      if (inputValue.length > 0 && inputLineCount <= 2) {
        // 🔧 超过2行的文本不匹配history（多行文本匹配性能差且无实际用途）
        clearTimeout(state._historyListTimer);
        state._historyListTimer = setTimeout(() => {
          showInputHistoryList();
          // 🔧 不预选任何项（index=-1），让 ArrowUp 直接选中最后一项（最新），ArrowDown 选中第一项
          const dd = getMessageHistoryDropdown();
          if (dd) dd.setFilteredIndex(-1);
        }, 50);
      } else {
        // 输入框为空或超过2行时，隐藏历史列表
        clearTimeout(state._historyListTimer);
        hideInputHistoryList();
      }

      // 🔧 Task#1262修复：不再自动删除用户输入的空白字符（空格、制表符等）
      // 仅当 innerText 为空或仅含换行符（来自浏览器插入的 <br> 占位节点）时才清空，
      // 以确保 placeholder 能正确显示。用户输入的空格等字符不再被误删。
      const isEmptyOrBrOnly = currentText.length === 0 || currentText.replace(/\n/g, '').length === 0;
      if (isEmptyOrBrOnly && input.childNodes.length > 0) {
        input.innerHTML = '';
      }
    });

    // 粘贴事件：转换为纯文本
    input.addEventListener('paste', (e) => {
      e.preventDefault(); // 阻止默认粘贴行为

      // 获取剪贴板中的纯文本
      const plainText = e.clipboardData.getData('text/plain');

      // 🔧 修复：弃用 document.execCommand('insertText')，因为它对多行文本
      // 会触发多次 input 事件（每行/片段一次），导致 renderHistoryList 被反复调用卡死
      // 改用 Selection API 直接插入文本节点，只触发一次 input 事件
      const selection = window.getSelection();
      if (selection.rangeCount > 0) {
        const range = selection.getRangeAt(0);
        if (input.contains(range.commonAncestorContainer)) {
          range.deleteContents(); // 删除选中内容
          const textNode = document.createTextNode(plainText);
          range.insertNode(textNode);
          // 将光标移到插入文本之后
          range.setStartAfter(textNode);
          range.collapse(true);
          selection.removeAllRanges();
          selection.addRange(range);
          // 手动触发一次 input 事件（contenteditable 的 input 事件是浏览器自动的，
          // 但直接操作 DOM 后不会自动触发，需要手动派发以保持 undo 栈等功能正常）
          input.dispatchEvent(new Event('input', { bubbles: true }));
        }
      }
    });
  }

  // 消息输入框清空按钮（参考关键词过滤框的清空按钮交互）
  // 有内容时按钮淡入（CSS :has(#message-input:not(:empty)) 控制），点击清空全部输入
  const messageClearBtn = document.getElementById('message-clear-btn');
  if (messageClearBtn && input) {
    messageClearBtn.addEventListener('click', () => {
      const currentText = input.innerText;
      // 无内容（可能仅剩 br 占位节点）时无需处理
      if (currentText.trim().length === 0) return;
      // 清空前的全文压入 undo 栈，支持 Ctrl+Z 撤销误清（与键盘撤销机制一致）
      state.undoStack.push({ text: currentText, range: null });
      if (state.undoStack.length > 500) state.undoStack = state.undoStack.slice(-500);
      scheduleUndoPersist();
      state.redoStack = [];
      state.lastInputValue = '';
      // 清空输入框、附件、历史索引与草稿，恢复 placeholder
      input.innerHTML = '';
      clearAttachedFiles();
      state.historyIndex = -1;
      state.historyDraft = '';
      hideInputHistoryList();
      input.focus();
    });
  }

  // 思维链滑块和输入框同步，并自动更新思考模式复选框
  const thinkingSlider = document.getElementById('thinking-budget-slider');
  const thinkingInput = document.getElementById('thinking-budget');
  const thinkingToggle = document.getElementById('thinking-toggle');

  if (thinkingSlider && thinkingInput && thinkingToggle) {
    // 保存上次的非零thinking_budget值
    let lastThinkingBudget = parseInt(thinkingInput.value) || 56000;

    // 滑块变化 -> 更新输入框和复选框
    thinkingSlider.addEventListener('input', (e) => {
      thinkingInput.value = e.target.value;
      thinkingToggle.checked = parseInt(e.target.value) > 0;
      if (parseInt(e.target.value) > 0) {
        lastThinkingBudget = parseInt(e.target.value);
      }
    });

    // 输入框变化 -> 更新滑块和复选框
    thinkingInput.addEventListener('input', (e) => {
      thinkingSlider.value = e.target.value;
      thinkingToggle.checked = parseInt(e.target.value) > 0;
      if (parseInt(e.target.value) > 0) {
        lastThinkingBudget = parseInt(e.target.value);
      }
    });

    // 复选框变化 -> 更新输入框和滑块，同步全局配置
    thinkingToggle.addEventListener('change', (e) => {
      if (!e.target.checked) {
        // 取消勾选，保存当前值并清零
        const currentVal = parseInt(thinkingInput.value);
        if (currentVal > 0) {
          lastThinkingBudget = currentVal;
        }
        thinkingInput.value = 0;
        thinkingSlider.value = 0;
      } else if (parseInt(thinkingInput.value) === 0) {
        // 勾选但预算为0，恢复上次保存的值
        thinkingInput.value = lastThinkingBudget;
        thinkingSlider.value = lastThinkingBudget;
      }
      // 同步全局 thinking_mode 配置
      // 注意：思考级别模式（low/medium/high/xhigh/max）下勾选框只反映"是否思考"，
      // 勾选恢复为 enabled（标准档），取消勾选 disabled；auto 模式不通过勾选框设置
      const prevMode = window._lastThinkingMode || 'auto';
      const nextMode = e.target.checked
        ? (prevMode === 'auto' ? 'auto' : (prevMode === 'disabled' ? 'enabled' : prevMode))
        : 'disabled';
      window._lastThinkingMode = nextMode;
      fetch('/api/llm/thinking-mode', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ thinking_mode: nextMode }),
      }).catch(err => console.warn('同步thinking_mode失败:', err));
    });

    // 🔧 注意：不再在此处初始化 thinkingToggle.checked
    // 因为 loadConfig() 已经在第 417-420 行根据配置文件的 thinking_mode 正确设置了状态
    // 此处如果用 thinkingInput.value（默认 56000）来设置，会覆盖配置文件的状态
  }

  // 消息搜索框（有关键词时强制包含已归档消息，并联动刷新事务栏）
  const searchInput = document.getElementById('message-search');
  if (searchInput) {
    let searchTimeout = null;
    searchInput.addEventListener('input', (e) => {
      clearTimeout(searchTimeout);
      // 立即更新状态，确保 renderMessageTree() 使用最新值
      const oldSearchIncludedArchived = state._searchIncludedArchived;
      const oldTaskSearchIncludedArchived = state._taskSearchIncludedArchived;
      state.searchTerm = e.target.value;
      const hasKeyword = state.searchTerm.trim().length > 0;
      // 搜索框有内容时，高亮边框
      searchInput.classList.toggle('has-content', hasKeyword);
      // 有关键词时，强制从后端加载包含已归档的消息，确保搜索范围覆盖所有消息
      // 无关键词时，恢复根据 showArchived 标志决定是否加载已归档消息
      // 消息搜索时同步更新 _taskSearchIncludedArchived，使事务栏跟随消息栏状态
      if (hasKeyword !== oldSearchIncludedArchived) {
        state._searchIncludedArchived = hasKeyword;
        state._taskSearchIncludedArchived = hasKeyword;
        // 🔧 修复#282：先同步渲染消息树更新 displayedMessageIds，再加载事务列表
        // 否则 loadTasks() 会使用旧的 displayedMessageIds，导致事务栏不刷新
        renderMessageTree();
        loadMessages(false, true);  // 搜索状态变化：完整加载
        loadTasks();
      } else {
        renderMessageTree();
        // 消息搜索过滤时联动刷新事务栏
        loadTasks();
      }
      searchTimeout = setTimeout(() => {
        // 在 debounce 中更新状态（使用当前值）
        if (hasKeyword !== state._searchIncludedArchived) {
          state._searchIncludedArchived = hasKeyword;
          state._taskSearchIncludedArchived = hasKeyword;
          loadMessages(false, true);  // 搜索状态变化：完整加载
          loadTasks();
        }
      }, 300);
    });
    // 清空按钮点击
    const searchClearBtn = searchInput.parentElement.querySelector('.search-clear-btn');
    if (searchClearBtn) {
      searchClearBtn.addEventListener('click', () => {
        searchInput.value = '';
        searchInput.dispatchEvent(new Event('input'));
        searchInput.focus();
      });
    }
    // 🔧 修复：页面刷新后，如果搜索输入框已有内容，应用 has-content 类
    const hasKeyword = searchInput.value.trim().length > 0;
    if (hasKeyword) {
      searchInput.classList.add('has-content');
    }
  }

  // 显示已归档消息切换
  const showArchivedToggle = document.getElementById('show-archived-toggle');
  if (showArchivedToggle) {
    showArchivedToggle.addEventListener('change', async (e) => {
      state.showArchived = e.target.checked;
      await loadMessages(false, true);  // 归档状态切换：完整加载
      await loadTasks();  // 同步刷新事务栏
    });
  }

  // 拖动功能切换（消息栏）
  const enableDragToggle = document.getElementById('enable-drag-toggle');
  if (enableDragToggle) {
    enableDragToggle.addEventListener('change', async (e) => {
      state.enableDrag = e.target.checked;
      // 同步更新事务栏拖动勾选框
      const enableTaskDragToggle = document.getElementById('enable-task-drag-toggle');
      if (enableTaskDragToggle) {
        enableTaskDragToggle.checked = state.enableDrag;
      }
      // 更新所有可拖拽元素的属性
      updateDraggableState();
      // 保存到 localStorage
      try {
        localStorage.setItem('peng_enable_drag', JSON.stringify(state.enableDrag));
      } catch (error) {
        console.warn('保存拖动状态失败:', error);
      }
    });
  }

  // 拖动功能切换（事务栏）
  const enableTaskDragToggle = document.getElementById('enable-task-drag-toggle');
  if (enableTaskDragToggle) {
    enableTaskDragToggle.addEventListener('change', async (e) => {
      state.enableDrag = e.target.checked;
      // 同步更新消息栏拖动勾选框
      const enableDragToggle = document.getElementById('enable-drag-toggle');
      if (enableDragToggle) {
        enableDragToggle.checked = state.enableDrag;
      }
      // 更新所有可拖拽元素的属性
      updateDraggableState();
      // 保存到 localStorage
      try {
        localStorage.setItem('peng_enable_drag', JSON.stringify(state.enableDrag));
      } catch (error) {
        console.warn('保存拖动状态失败:', error);
      }
    });
  }

  // 显示删除按钮切换（消息栏和事务栏同步）
  const showDeleteButtonsToggle = document.getElementById('show-delete-buttons-toggle');
  const showTaskDeleteButtonsToggle = document.getElementById('show-task-delete-buttons-toggle');
  function syncDeleteToggle(checked) {
    state.showDeleteButtons = checked;
    state.showTaskDeleteButtons = checked;
    if (showDeleteButtonsToggle) showDeleteButtonsToggle.checked = checked;
    if (showTaskDeleteButtonsToggle) showTaskDeleteButtonsToggle.checked = checked;
    renderMessageTree(true);  // 强制全量渲染
    renderTaskTree();  // 强制全量渲染
  }
  if (showDeleteButtonsToggle) {
    showDeleteButtonsToggle.addEventListener('change', async (e) => {
      syncDeleteToggle(e.target.checked);
    });
  }
  if (showTaskDeleteButtonsToggle) {
    showTaskDeleteButtonsToggle.addEventListener('change', async (e) => {
      syncDeleteToggle(e.target.checked);
    });
  }

  // 事务搜索输入框（有关键词时强制包含已归档事务）
  const taskSearchInput = document.getElementById('task-search');
  if (taskSearchInput) {
    let taskSearchTimeout = null;
    taskSearchInput.addEventListener('input', (e) => {
      clearTimeout(taskSearchTimeout);
      // 立即更新状态，确保 renderTaskTree() 使用最新值
      const oldTaskSearchIncludedArchived = state._taskSearchIncludedArchived;
      state.taskSearchTerm = e.target.value;
      const hasKeyword = state.taskSearchTerm.trim().length > 0;
      // 搜索框有内容时，高亮边框
      taskSearchInput.classList.toggle('has-content', hasKeyword);
      // 更新 _taskSearchIncludedArchived，确保 loadTasks() 读取到最新值
      state._taskSearchIncludedArchived = hasKeyword;
      // 有关键词时，强制从后端加载包含已归档的事务，确保搜索范围覆盖所有事务
      // 无关键词时，恢复根据 showArchived 标志决定是否加载已归档事务
      if (hasKeyword !== oldTaskSearchIncludedArchived) {
        loadTasks();
      } else {
        renderTaskTree();
      }
      taskSearchTimeout = setTimeout(() => {
        // 在 debounce 中确保 _taskSearchIncludedArchived 正确（防御性更新）
        if (hasKeyword !== state._taskSearchIncludedArchived) {
          state._taskSearchIncludedArchived = hasKeyword;
          loadTasks();
        }
      }, 300);
    });
    // 清空按钮点击
    const taskSearchClearBtn = taskSearchInput.parentElement.querySelector('.search-clear-btn');
    if (taskSearchClearBtn) {
      taskSearchClearBtn.addEventListener('click', () => {
        taskSearchInput.value = '';
        taskSearchInput.dispatchEvent(new Event('input'));
        taskSearchInput.focus();
      });
    }
    // 🔧 修复：页面刷新后，如果事务搜索输入框已有内容，应用 has-content 类
    const taskHasKeyword = taskSearchInput.value.trim().length > 0;
    if (taskHasKeyword) {
      taskSearchInput.classList.add('has-content');
    }
  }

  // ==================== 拖拽功能 ====================
  initDragAndDrop();

  // 初始化拖拽状态（根据enableDrag设置元素的draggable属性）
  // 延迟执行，等待消息树和事务树渲染完成
  setTimeout(() => {
    updateDraggableState();
  }, 500);

  // 工作目录输入框
  const workdirInput = document.getElementById('workdir-input');
  if (workdirInput) {
    // 防抖验证
    let workdirTimeout = null;

    // 输入时实时验证（带防抖）
    workdirInput.addEventListener('input', (e) => {
      clearTimeout(workdirTimeout);
      workdirTimeout = setTimeout(() => {
        validateAndSetWorkdir(e.target.value);
      }, 500);
    });

    // 失去焦点时立即验证并更新
    workdirInput.addEventListener('blur', (e) => {
      clearTimeout(workdirTimeout);
      validateAndSetWorkdir(e.target.value);
    });

    // 初始化时验证当前值
    if (workdirInput.value) {
      validateAndSetWorkdir(workdirInput.value);
    }
  }

  // ==================== 文件浏览器 ====================

  const fileBrowserBtn = document.getElementById('file-browser-btn');
  const fileBrowserModal = document.getElementById('file-browser-modal');
  const fileBrowserClose = document.getElementById('file-browser-close');
  const fileBrowserCancel = document.getElementById('file-browser-cancel');
  const fileBrowserSelect = document.getElementById('file-browser-select');
  const fileBrowserSetWorkdir = document.getElementById('file-browser-set-workdir');
  const fileBrowserTree = document.getElementById('file-browser-tree');
  const fileBrowserCurrentPath = document.getElementById('file-browser-current-path');
  const fileBrowserCreateDir = document.getElementById('file-browser-create-dir');
  const fileBrowserNewdirWrapper = document.getElementById('file-browser-newdir-wrapper');
  const fileBrowserNewdirName = document.getElementById('file-browser-newdir-name');
  const fileBrowserNewdirConfirm = document.getElementById('file-browser-newdir-confirm');
  const fileBrowserNewdirCancel = document.getElementById('file-browser-newdir-cancel');

  let currentFileBrowserPath = state.workdir || '~/';
  let selectedFileItem = null;

  // 路径输入框回车跳转功能 + 历史列表
  // 🔧 路径历史数据（localStorage 持久化，按 nest 分开存储）
  let pathHistory = [];
  const PATH_HISTORY_PREFIX = 'peng_path_history_';
  const PATH_HISTORY_MAX = 200;

  /** 获取当前 nest 对应的 localStorage key */
  function getPathHistoryKey() {
    const nestSelect = document.getElementById('nest-select');
    const nestName = nestSelect ? nestSelect.value : 'local';
    return PATH_HISTORY_PREFIX + (nestName || 'local');
  }

  /** 从 localStorage 加载当前 nest 的路径历史 */
  function loadPathHistory() {
    try {
      const raw = localStorage.getItem(getPathHistoryKey());
      pathHistory = raw ? JSON.parse(raw) : [];
    } catch (e) {
      pathHistory = [];
    }
  }

  /** 保存路径历史到 localStorage（当前 nest） */
  function savePathHistory(history) {
    pathHistory = history;
    try {
      localStorage.setItem(getPathHistoryKey(), JSON.stringify(history));
    } catch (e) {
      console.warn('保存路径历史失败:', e);
    }
  }

  /** 添加路径到历史（去重） */
  function addPathHistory(path) {
    if (!path || !path.trim()) return;
    const trimmed = path.trim();
    // 去重
    const idx = pathHistory.findIndex(item => item.content === trimmed);
    if (idx !== -1) pathHistory.splice(idx, 1);
    pathHistory.push({ content: trimmed, timestamp: Date.now() });
    if (pathHistory.length > PATH_HISTORY_MAX) pathHistory.shift();
    savePathHistory(pathHistory);
  }

  // 初始化加载路径历史
  loadPathHistory();

  // 🔧 创建路径历史下拉组件（复用 createHistoryDropdown）
  let pathHistoryDropdown = null;
  function getPathHistoryDropdown() {
    if (pathHistoryDropdown) return pathHistoryDropdown;

    const pathInputEl = document.getElementById('file-browser-current-path');
    const pathContainer = document.querySelector('.file-browser-path');
    if (!pathInputEl || !pathContainer) return null;

    pathHistoryDropdown = createHistoryDropdown({
      inputEl: pathInputEl,
      listId: 'path-history-list',
      listClassName: 'path-history-list',
      containerSelector: pathContainer,
      position: 'below',
      getHistory: () => pathHistory,
      setHistory: (h) => { savePathHistory(h); },
      saveHistory: async (h) => { savePathHistory(h); },
      getInputText: () => {
        const el = document.getElementById('file-browser-current-path');
        return el ? el.value.trim() : '';
      },
      setInputText: (text) => {
        const el = document.getElementById('file-browser-current-path');
        if (el) el.value = toNativeDisplayPath(text);
      },
      applyCallback: (text) => {
        // 选中历史路径后，自动跳转到该路径
        loadFileBrowser(text);
      },
    });

    return pathHistoryDropdown;
  }

  if (fileBrowserCurrentPath) {
    fileBrowserCurrentPath.addEventListener('keydown', (e) => {
      const dd = getPathHistoryDropdown();

      // 🔧 历史列表可见时，除了纯上下键以外，其他任何操作都应关闭历史列表
      // Tab/Enter/Escape/Delete 有各自的特殊处理，在其分支中单独关闭
      const isPlainArrowNav = (e.key === 'ArrowUp' || e.key === 'ArrowDown') && !e.altKey && !e.shiftKey && !e.ctrlKey;
      if (!isPlainArrowNav && e.key !== 'Tab' && e.key !== 'Delete' && e.key !== 'Enter' && e.key !== 'Escape') {
        if (dd) dd.hide();
      }

      // 上箭头：在历史列表中导航
      if (e.key === 'ArrowUp' && !e.altKey && !e.shiftKey && !e.ctrlKey) {
        if (dd && dd.getListEl() && dd.getListEl().style.display !== 'none') {
          e.preventDefault();
          dd.navigate(-1);
          return;
        }
      }
      // 下箭头：在历史列表中导航
      if (e.key === 'ArrowDown' && !e.altKey && !e.shiftKey && !e.ctrlKey) {
        if (dd && dd.getListEl() && dd.getListEl().style.display !== 'none') {
          e.preventDefault();
          dd.navigate(1);
          return;
        }
      }
      // Tab: 选中历史项（无选中项则关闭列表）
      if (e.key === 'Tab') {
        if (dd && dd.getFilteredIndex() >= 0) {
          e.preventDefault();
          dd.applySelected();
          return;
        }
        if (dd) dd.hide();
      }
      // Delete: 删除选中的历史项（无选中项则关闭列表后执行默认删除）
      if (e.key === 'Delete' && !e.shiftKey && !e.ctrlKey && !e.altKey) {
        if (dd && dd.getListEl() && dd.getListEl().style.display !== 'none' && dd.getFilteredIndex() >= 0) {
          e.preventDefault();
          dd.deleteSelected();
          return;
        }
        if (dd) dd.hide();
      }
      // Enter: 跳转路径 + 记录历史
      if (e.key === 'Enter') {
        e.preventDefault();
        const inputPath = fileBrowserCurrentPath.value.trim();
        if (inputPath) {
          // 🔧 统一使用正斜杠存储到历史，避免 Windows 平台正反斜杠导致重复条目
          addPathHistory(inputPath.replace(/\\/g, '/'));
          loadFileBrowser(inputPath);
        }
        if (dd) dd.hide();
        fileBrowserCurrentPath.blur();
        return;
      }
      // Escape键恢复原始路径 + 隐藏历史
      if (e.key === 'Escape') {
        e.preventDefault();
        fileBrowserCurrentPath.value = toNativeDisplayPath(currentFileBrowserPath);
        if (dd) dd.hide();
        fileBrowserCurrentPath.blur();
        return;
      }
    });

    // 聚焦时全选文本 + 弹出历史列表
    fileBrowserCurrentPath.addEventListener('focus', () => {
      setTimeout(() => fileBrowserCurrentPath.select(), 0);
      // 弹出路径历史列表
      const dd = getPathHistoryDropdown();
      if (dd) {
        dd.setFilteredIndex(-1);
        dd.show();
      }
    });

    // 失焦时隐藏历史列表
    fileBrowserCurrentPath.addEventListener('blur', () => {
      setTimeout(() => {
        const dd = getPathHistoryDropdown();
        if (dd) dd.hide();
      }, 150);
    });

    // 输入时自动过滤历史列表
    fileBrowserCurrentPath.addEventListener('input', () => {
      const dd = getPathHistoryDropdown();
      if (dd) {
        dd.setFilteredIndex(-1);
        dd.show();
      }
    });
  }

  // 获取文件浏览器初始路径：优先使用激活编辑器标签页的文件所在目录，否则使用工作目录
  const getInitialFileBrowserPath = () => {
    // 🔧 修复：优先查找活跃group中的当前tab（多分拆列时需从group获取）
    let currentTab = null;
    if (getActiveEditorTabId()) {  // 🔧 Task#334
      currentTab = findTabById(getActiveEditorTabId());
      if (!currentTab && state.editorGroups) {
        for (const group of state.editorGroups) {
          const found = group.tabs.find(t => t.id === getActiveEditorTabId());  // 🔧 Task#334
          if (found) { currentTab = found; break; }
        }
      }
    }
    if (currentTab && currentTab.path) {
      // 取文件所在目录（去掉文件名部分）
      const lastSlash = currentTab.path.lastIndexOf('/');
      if (lastSlash > 0) {
        return currentTab.path.substring(0, lastSlash);
      }
    }
    return state.workdir || '~/';
  };

  // 🔧 获取当前应该使用的文件浏览路径（考虑远程nest）
  const getCurrentFileSystemPath = () => {
    if (state.remoteNest && state.remoteNestRoot) {
      return state.remoteNestRoot;
    }
    return state.workdir || '~/';
  };

  // 打开文件浏览器
  const openFileBrowser = async () => {
    if (fileBrowserModal) {
      currentFileBrowserPath = state.remoteNest ? (state.remoteNestRoot || '/') : getInitialFileBrowserPath();
      // 🔧 切换 nest 后重新加载对应的路径历史
      loadPathHistory();
      fileBrowserModal.classList.add('show');
      await loadFileBrowser(currentFileBrowserPath);
    }
  };

  // 从编辑器打开文件浏览器
  window.openFileBrowserFromEditor = async () => {
    state.isFileBrowserFromEditor = true;

    // 获取当前标签页信息
    const currentTab = findTabById(getActiveEditorTabId());  // 🔧 Task#334

    // 🔧 优化：如果是另存为模式，更新标题和按钮文本
    if (state.isSaveAsMode) {
      const fileBrowserTitle = document.getElementById('file-browser-title');
      const fileBrowserSelect = document.getElementById('file-browser-select');
      const fileBrowserFilenameWrapper = document.getElementById('file-browser-filename-wrapper');
      const fileBrowserFilenameInput = document.getElementById('file-browser-filename');

      if (fileBrowserTitle) {
        fileBrowserTitle.textContent = '💾 另存为 - 选择保存位置';
      }
      if (fileBrowserSelect) {
        fileBrowserSelect.textContent = '保存';
      }
      if (fileBrowserFilenameWrapper) {
        fileBrowserFilenameWrapper.style.display = 'flex';
      }
      if (fileBrowserFilenameInput) {
        // 🔧 修复：预填充当前标签页的文件名和路径
        if (currentTab && currentTab.path) {
          const pathParts = currentTab.path.split('/');
          fileBrowserFilenameInput.value = pathParts.pop() || currentTab.name || 'untitled.txt';
        } else if (currentTab && currentTab.name) {
          fileBrowserFilenameInput.value = currentTab.name;
        } else {
          fileBrowserFilenameInput.value = 'untitled.txt';
        }
        // 聚焦到文件名输入框
        setTimeout(() => fileBrowserFilenameInput.focus(), 100);
      }
    }

    // 隐藏"设为工作目录"按钮和"新建目录"按钮
    if (fileBrowserSetWorkdir) {
      fileBrowserSetWorkdir.style.display = 'none';
    }
    if (fileBrowserCreateDir) {
      fileBrowserCreateDir.style.display = 'none';
    }
    // 🔧 修复：另存为时，默认路径为当前标签页文件所在目录
    if (state.isSaveAsMode && currentTab && currentTab.path) {
      const pathParts = currentTab.path.split('/');
      pathParts.pop(); // 移除文件名，保留目录
      if (pathParts.length > 0) {
        currentFileBrowserPath = pathParts.join('/');
      } else {
        currentFileBrowserPath = state.remoteNest ? (state.remoteNestRoot || '/') : getInitialFileBrowserPath();
      }
    } else {
      currentFileBrowserPath = state.remoteNest ? (state.remoteNestRoot || '/') : getInitialFileBrowserPath();
    }
    // 🔧 切换 nest 后重新加载对应的路径历史
    loadPathHistory();
    if (fileBrowserModal) {
      fileBrowserModal.classList.add('show');
      await loadFileBrowser(currentFileBrowserPath);
    }
  };

  // 🔧 Task#992：通过路径超链接打开文件浏览器，定位到指定目录
  window.openPathInFileBrowser = async (dirPath, nestName) => {
    if (!fileBrowserModal) return;

    // 切换 nest-select 到正确的 nest（loadFileBrowser 依赖它确定 nest 通道）
    if (nestName) {
      const nestSelect = document.getElementById('nest-select');
      if (nestSelect && nestSelect.value !== nestName) {
        nestSelect.value = nestName;
        // 触发 change 事件让其他模块感知 nest 切换
        nestSelect.dispatchEvent(new Event('change'));
      }
    }

    state.isFileBrowserFromEditor = false;
    state.isSaveAsMode = false;
    loadPathHistory();
    currentFileBrowserPath = dirPath;
    fileBrowserModal.classList.add('show');
    await loadFileBrowser(dirPath);
  };

  // 关闭文件浏览器
  const closeFileBrowser = () => {
    if (fileBrowserModal) {
      fileBrowserModal.classList.remove('show');
      selectedFileItem = null;
      state.isFileBrowserFromEditor = false;
      state.isSaveAsMode = false;

      // 🔧 恢复标题和按钮文本
      const fileBrowserTitle = document.getElementById('file-browser-title');
      const fileBrowserSelect = document.getElementById('file-browser-select');
      const fileBrowserFilenameWrapper = document.getElementById('file-browser-filename-wrapper');
      if (fileBrowserTitle) {
        fileBrowserTitle.textContent = '📁 文件浏览器';
      }
      if (fileBrowserSelect) {
        fileBrowserSelect.textContent = '打开/选择';
      }
      if (fileBrowserFilenameWrapper) {
        fileBrowserFilenameWrapper.style.display = 'none';
      }

      // 恢复"设为工作目录"按钮的显示
      if (fileBrowserSetWorkdir) {
        fileBrowserSetWorkdir.style.display = '';
      }

      // 恢复"新建目录"按钮的显示
      if (fileBrowserCreateDir) {
        fileBrowserCreateDir.style.display = '';
      }

      // 隐藏新建目录输入框并重置状态
      if (fileBrowserNewdirWrapper) {
        fileBrowserNewdirWrapper.style.display = 'none';
      }
      if (fileBrowserNewdirName) {
        fileBrowserNewdirName.value = '';
      }
      if (fileBrowserCreateDir) {
        fileBrowserCreateDir.disabled = false;
      }

      // 🔧 关闭时隐藏路径历史列表
      const dd = getPathHistoryDropdown();
      if (dd) dd.hide();
    }
  };

  // 格式化文件大小为人类可读格式
  const formatFileSize = (bytes) => {
    if (bytes === 0) return '0 B';
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    const i = Math.floor(Math.log(bytes) / Math.log(1024));
    const size = (bytes / Math.pow(1024, i)).toFixed(i === 0 ? 0 : 1);
    return size + ' ' + units[i];
  };

  // 加载文件浏览器内容
  const loadFileBrowser = async (path) => {
    if (!fileBrowserTree) return;

    fileBrowserTree.innerHTML = '<div class="loading">加载中...</div>';

    try {
      // 🔧 获取当前选中的nest
      const nestSelect = document.getElementById('nest-select');
      const currentNest = nestSelect ? nestSelect.value : '';
      // 🔧 "local" 也发送 nest 参数，走 local nest 通道（支持盘符切换等功能）
      const nestParam = currentNest ? `&nest=${encodeURIComponent(currentNest)}` : '';

      // 调用API获取文件列表
      const response = await fetch(`/api/files/list?path=${encodeURIComponent(path)}${nestParam}`);
      if (!response.ok) {
        throw new Error(`HTTP error! status: ${response.status}`);
      }

      const data = await response.json();
      if (!data.success) {
        throw new Error(data.error || '加载文件列表失败');
      }

      const files = data.files || [];
      currentFileBrowserPath = data.path || path;

      // 🔧 记录导航到的路径到历史
      if (currentFileBrowserPath) {
        addPathHistory(currentFileBrowserPath);
      }

      // 更新路径显示
      if (fileBrowserCurrentPath) {
        fileBrowserCurrentPath.value = toNativeDisplayPath(currentFileBrowserPath) || '🗃️ 选择硬盘';
      }

      // 先显示目录，再显示文件
      const folders = files.filter(f => f.type === 'directory');
      const regularFiles = files.filter(f => f.type !== 'directory');

      let html = '';

      // 添加返回上级目录选项（如果不是根目录）
      // 注意：parent_path 为 "" 表示盘符列表（Windows），也是有效parent
      if (data.parent_path !== null && data.parent_path !== undefined) {
        html += `<div class="file-item folder" data-path="${data.parent_path}" data-type="directory" data-name="..">
          <span class="file-icon">⬆️</span>
          <span class="file-name">..</span>
        </div>`;
      }

      // 如果目录为空，显示提示信息
      if (files.length === 0) {
        html += '<div class="empty">此目录为空</div>';
      } else {
        const isDriveList = !currentFileBrowserPath; // 盘符列表模式
        const renderItem = (item) => {
          // 🔧 盘符列表模式用🗃️图标
          const icon = isDriveList && item.type === 'directory' ? '💽' : (item.type === 'directory' ? '📁' : getFileIcon(item.name));
          const itemClass = item.type === 'directory' ? 'file-item folder' : 'file-item';
          // 格式化文件大小
          const sizeStr = item.size != null ? formatFileSize(item.size) : '';
          const mtimeStr = item.mtime || '';
          return `<div class="${itemClass}" data-path="${item.path}" data-type="${item.type}" data-name="${item.name}">
            <span class="file-icon">${icon}</span>
            <span class="file-name">${item.name}</span>
            <span class="file-meta">
              <span class="file-size">${sizeStr}</span>
              <span class="file-mtime">${mtimeStr}</span>
              <span class="file-delete-btn" data-path="${item.path}" data-type="${item.type}" data-name="${item.name}" title="删除">&nbsp;🗑️</span>
            </span>
          </div>`;
        };

        // 渲染目录和文件
        folders.forEach(item => { html += renderItem(item); });
        regularFiles.forEach(item => { html += renderItem(item); });
      }

      fileBrowserTree.innerHTML = html;

      // 添加点击事件
      const fileItems = fileBrowserTree.querySelectorAll('.file-item');
      fileItems.forEach(item => {
        item.addEventListener('click', () => handleFileItemClick(item));
      });

      // 添加删除按钮点击事件（阻止冒泡到file-item）
      const deleteBtns = fileBrowserTree.querySelectorAll('.file-delete-btn');
      deleteBtns.forEach(btn => {
        btn.addEventListener('click', (e) => {
          e.stopPropagation();
          e.preventDefault();
          handleFileBrowserDelete(btn);
        });
      });

    } catch (error) {
      console.error('加载文件浏览器失败:', error);
      // 🔧 加载失败时自动回退到上一级或盘符列表，不卡在错误页面
      if (currentFileBrowserPath && currentFileBrowserPath !== '') {
        // 有上级路径，尝试回退到上级
        const stripped = currentFileBrowserPath.replace(/\\/g, '/').replace(/\/+$/, '');
        const lastSlash = stripped.lastIndexOf('/');
        if (lastSlash > 0) {
          currentFileBrowserPath = stripped.substring(0, lastSlash + 1);
        } else if (lastSlash === 0) {
          currentFileBrowserPath = '/';
        } else {
          // 可能是盘符根目录，回退到盘符列表
          currentFileBrowserPath = '';
        }
      }
      // 自动重试加载回退路径
      try {
        await loadFileBrowser(currentFileBrowserPath);
      } catch (retryError) {
        // 重试也失败，显示盘符列表作为兜底
        console.error('重试加载也失败:', retryError);
        currentFileBrowserPath = '';
        fileBrowserTree.innerHTML = '<div class="empty">目录无法访问，请选择其他目录</div>';
        if (fileBrowserCurrentPath) {
          fileBrowserCurrentPath.value = '🗃️ 选择硬盘';
        }
      }
    }
  };

  // 处理文件项点击
  const handleFileItemClick = async (item) => {
    const path = item.dataset.path;
    const type = item.dataset.type;

    // 如果是从编辑器打开的文件浏览器
    if (state.isFileBrowserFromEditor && type === 'file') {
      // 🔧 修复：另存为模式下只选中文件，不直接打开
      if (state.isSaveAsMode) {
        // 清除之前的选中状态
        const allItems = fileBrowserTree.querySelectorAll('.file-item');
        allItems.forEach(i => i.classList.remove('selected'));

        // 选中文件
        item.classList.add('selected');
        selectedFileItem = item;

        // 更新文件名输入框为点击的文件名
        const fileBrowserFilenameInput = document.getElementById('file-browser-filename');
        if (fileBrowserFilenameInput) {
          const fileName = path.split('/').pop();
          fileBrowserFilenameInput.value = fileName;
        }
        return;
      }

      // 非另存为模式下，打开文件
      // 🖼️ 图片文件：用内置图片查看器打开
      if (isImageFile(path)) {
        openImageViewer(path);
        closeFileBrowser();
        return;
      }
      await openEditorForFile(path);
      closeFileBrowser();
      return;
    }

    // 🖼️ 图片文件：直接用图片查看器打开
    if (type === 'file' && isImageFile(path)) {
      openImageViewer(path);
      return;
    }

    // 📝 非图片文件：点击一次直接打开编辑器
    if (type === 'file') {
      await openEditorForFile(path);
      closeFileBrowser();
      return;
    }

    // 📁 目录：进入目录
    if (type === 'directory') {
      await loadFileBrowser(path);
    }
  };

  // 静默删除文件/目录（移动到临时目录/deleted），即时刷新列表
  const handleFileBrowserDelete = async (btn) => {
    const filePath = btn.dataset.path;
    if (!filePath) return;

    // 获取当前nest
    const nestSelect = document.getElementById('nest-select');
    const currentNest = nestSelect ? nestSelect.value : '';
    const nestParam = currentNest ? `?nest=${encodeURIComponent(currentNest)}` : '';

    btn.style.opacity = '0.5';
    btn.style.pointerEvents = 'none';

    try {
      const response = await fetch(`/api/files/delete${nestParam}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: filePath }),
      });
      if (!response.ok) {
        const errData = await response.json().catch(() => ({}));
        throw new Error(errData.detail || '删除失败');
      }
      const result = await response.json();

      // 记录到删除历史（供"最近打开的文件"列表恢复使用）
      _recordDeletedFile({
        nest: currentNest || 'local',
        originalPath: result.original_path || filePath,
        backupPath: result.backup_path || '',
        name: btn.dataset.name || '',
        type: btn.dataset.type || 'file',
        deletedAt: Date.now(),
      });

      // 静默刷新文件浏览器列表
      await loadFileBrowser(currentFileBrowserPath);
    } catch (error) {
      console.error('删除文件失败:', error);
      showSimpleToast('删除失败: ' + error.message, 'error');
      btn.style.opacity = '';
      btn.style.pointerEvents = '';
    }
  };

  // 文件图标已统一使用全局 getFileIcon（lang-icons.js）

  // 绑定文件浏览器按钮事件
  if (fileBrowserBtn) {
    fileBrowserBtn.addEventListener('click', openFileBrowser);
  }

  // 绑定关闭按钮事件
  if (fileBrowserClose) {
    fileBrowserClose.addEventListener('click', closeFileBrowser);
  }

  if (fileBrowserCancel) {
    fileBrowserCancel.addEventListener('click', closeFileBrowser);
  }

  // 点击模态框外部关闭
  if (fileBrowserModal) {
    fileBrowserModal.addEventListener('click', (e) => {
      if (e.target === fileBrowserModal) {
        closeFileBrowser();
      }
    });
  }

  // Esc键关闭文件浏览器（监听document，因为modal本身可能不获得焦点）
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && fileBrowserModal && fileBrowserModal.classList.contains('show')) {
      // 检查焦点是否在输入框中（如文件名输入框、新建目录输入框）
      const activeEl = document.activeElement;
      if (activeEl && (activeEl.classList.contains('file-browser-filename-input') || activeEl.classList.contains('file-browser-newdir-input'))) {
        // 输入框中按Esc先清空输入框焦点，不关闭浏览器
        activeEl.blur();
        return;
      }
      e.preventDefault();
      e.stopPropagation();
      closeFileBrowser();
    }
  });

  // 打开/选择按钮（根据选中的是文件还是目录来决定）
  if (fileBrowserSelect) {
    fileBrowserSelect.addEventListener('click', async () => {
      // 检查是否处于"另存为"模式
      if (state.isSaveAsMode) {
        await handleSaveAsMode();
        return;
      }

      // 原有的打开文件逻辑
      if (selectedFileItem) {
        // 如果选中的是文件，打开编辑器
        const filePath = selectedFileItem.dataset.path;
        const fileType = selectedFileItem.dataset.type;

        if (fileType === 'file') {
          // 打开编辑器
          await openEditorForFile(filePath);
          closeFileBrowser();
        } else if (fileType === 'directory') {
          // 如果选中的是目录，进入目录
          await loadFileBrowser(filePath);
        }
      } else {
        // 如果没有选中任何项，则将当前目录设为工作目录
        if (workdirInput) {
          workdirInput.value = toNativeDisplayPath(currentFileBrowserPath);
          validateAndSetWorkdir(currentFileBrowserPath);
        }
        closeFileBrowser();
      }
    });
  }

  // 处理"另存为"模式下的文件选择
  async function handleSaveAsMode() {
    const currentTab = findTabById(getActiveEditorTabId());  // 🔧 Task#334
    if (!currentTab) {
      showSimpleToast('当前没有打开的文件标签', 'warning');
      closeFileBrowser();
      return;
    }

    // 获取文件名输入框的值
    const fileBrowserFilenameInput = document.getElementById('file-browser-filename');
    const fileName = fileBrowserFilenameInput ? fileBrowserFilenameInput.value.trim() : '';

    if (!fileName) {
      showSimpleToast('请输入文件名', 'warning');
      if (fileBrowserFilenameInput) {
        fileBrowserFilenameInput.focus();
      }
      return;
    }

    // 始终使用当前浏览目录 + 输入的文件名来构建路径
    let filePath;
    const dirPath = currentFileBrowserPath;
    filePath = dirPath.endsWith('/') ? dirPath + fileName : dirPath + '/' + fileName;

    // 执行保存
    const success = await performSaveAs(filePath);

    if (success) {
      // 保存成功，关闭文件浏览器
      closeFileBrowser();
    }
    // 如果保存失败，不关闭文件浏览器，让用户可以重新选择
  }

  // 设为工作目录按钮
  if (fileBrowserSetWorkdir) {
    fileBrowserSetWorkdir.addEventListener('click', () => {
      if (selectedFileItem && selectedFileItem.dataset.type === 'directory') {
        // 如果选中的是目录，设置为工作目录
        const dirPath = selectedFileItem.dataset.path;
        if (workdirInput) {
          workdirInput.value = toNativeDisplayPath(dirPath);
          validateAndSetWorkdir(dirPath);
        }
        closeFileBrowser();
      } else {
        // 如果没有选中目录，则将当前目录设为工作目录
        if (workdirInput) {
          workdirInput.value = toNativeDisplayPath(currentFileBrowserPath);
          validateAndSetWorkdir(currentFileBrowserPath);
        }
        closeFileBrowser();
      }
    });
  }

  // 新建目录功能
  if (fileBrowserCreateDir) {
    fileBrowserCreateDir.addEventListener('click', () => {
      // 显示新建目录输入框
      if (fileBrowserNewdirWrapper) {
        fileBrowserNewdirWrapper.style.display = 'flex';
        // 清空输入框并聚焦
        if (fileBrowserNewdirName) {
          fileBrowserNewdirName.value = '';
          fileBrowserNewdirName.focus();
        }
        // 禁用"新建目录"按钮
        fileBrowserCreateDir.disabled = true;
      }
    });
  }

  // 取消新建目录
  if (fileBrowserNewdirCancel) {
    fileBrowserNewdirCancel.addEventListener('click', () => {
      // 隐藏新建目录输入框
      if (fileBrowserNewdirWrapper) {
        fileBrowserNewdirWrapper.style.display = 'none';
        // 清空输入框
        if (fileBrowserNewdirName) {
          fileBrowserNewdirName.value = '';
        }
        // 启用"新建目录"按钮
        if (fileBrowserCreateDir) {
          fileBrowserCreateDir.disabled = false;
        }
      }
    });
  }

  // 确认新建目录
  const createDirectory = async () => {
    const dirName = fileBrowserNewdirName ? fileBrowserNewdirName.value.trim() : '';

    if (!dirName) {
      showSimpleToast('请输入目录名', 'warning');
      if (fileBrowserNewdirName) {
        fileBrowserNewdirName.focus();
      }
      return;
    }

    try {
      const nestParam = getNestParam();
      const response = await fetch(`/api/files/create-directory${nestParam ? '?' + nestParam.slice(1) : ''}`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          parent_path: currentFileBrowserPath,
          dir_name: dirName
        })
      });

      const result = await response.json();

      if (!response.ok || !result.success) {
        const errMsg = result.detail || result.message || `创建目录失败 (${response.status})`;
        showSimpleToast(`❌ ${errMsg}`, 'error');
        return;
      }

      showSimpleToast(`✅ ${result.message}`, 'success');
      // 刷新文件浏览器
      await loadFileBrowser(currentFileBrowserPath);
      // 隐藏新建目录输入框
      if (fileBrowserNewdirWrapper) {
        fileBrowserNewdirWrapper.style.display = 'none';
        if (fileBrowserNewdirName) {
          fileBrowserNewdirName.value = '';
        }
      }
      // 启用"新建目录"按钮
      if (fileBrowserCreateDir) {
        fileBrowserCreateDir.disabled = false;
      }
    } catch (error) {
      console.error('创建目录失败:', error);
      showSimpleToast(`❌ 创建目录失败: ${error.message}`, 'error');
    }
  };

  if (fileBrowserNewdirConfirm) {
    fileBrowserNewdirConfirm.addEventListener('click', createDirectory);
  }

  // 目录名输入框按Enter键创建
  if (fileBrowserNewdirName) {
    // 🔧 修复：使用keydown + 捕获阶段，确保Esc键在浏览器处理之前被拦截
    fileBrowserNewdirName.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        createDirectory();
      } else if (e.key === 'Escape') {
        e.preventDefault();
        e.stopPropagation();
        // 按Escape键取消
        if (fileBrowserNewdirWrapper) {
          fileBrowserNewdirWrapper.style.display = 'none';
          if (fileBrowserNewdirName) {
            fileBrowserNewdirName.value = '';
          }
        }
        if (fileBrowserCreateDir) {
          fileBrowserCreateDir.disabled = false;
        }
      }
    }, { capture: true });  // 使用捕获阶段
  }

  // ==================== 文字划选自动复制 ====================

  // 消息栏、事务栏、回滚确认框和事务详情窗口文字划选自动复制
  const handleSelectionCopy = (e) => {
    // 只在指定区域内处理
    const target = e.target;
    const isInMessageList = target.closest('#message-tree');
    const isInTaskList = target.closest('#task-tree');
    const isInRollbackDialog = target.closest('#rollback-confirm-dialog');
    const isInDetailModal = target.closest('#detail-modal');

    if (!isInMessageList && !isInTaskList && !isInRollbackDialog && !isInDetailModal) {
      return;  // 不在指定区域内，不处理
    }

    // 延迟一点时间，确保 selection 已更新
    setTimeout(() => {
      const selection = window.getSelection();
      const selectedText = selection.toString().trim();

      if (selectedText) {
        // 尝试使用 Clipboard API
        if (navigator.clipboard && typeof navigator.clipboard.writeText === 'function') {
          navigator.clipboard.writeText(selectedText).then(() => {
            showSimpleToast('📝  已复制选中文本到剪贴板', 'success');
          }).catch(err => {
            console.warn('Clipboard API 复制失败，使用备用方法:', err);
            fallbackCopy(selectedText);
          });
        } else {
          fallbackCopy(selectedText);
        }
      }
    }, 50);
  };

  // 备用复制方法：使用 textarea 和 execCommand
  function fallbackCopy(text) {
    const textArea = document.createElement('textarea');
    textArea.value = text;
    textArea.style.position = 'fixed';
    textArea.style.left = '-999999px';
    textArea.style.top = '-999999px';
    document.body.appendChild(textArea);
    textArea.focus();
    textArea.select();

    try {
      const successful = document.execCommand('copy');
      if (successful) {
        showSimpleToast('📝  已复制选中文本到剪贴板', 'success');
      } else {
        console.warn('备用复制方法失败');
      }
    } catch (err) {
      console.error('复制失败:', err);
    }

    document.body.removeChild(textArea);
  }

  // 添加鼠标松开事件监听器
  document.addEventListener('mouseup', handleSelectionCopy);

  // 🔧 修复：需求#7 - 页面卸载前同步所有终端屏幕到后端
  window.addEventListener('beforeunload', () => {
    // 🔧 Task#178 Bug6修复：页面卸载前保存编辑器状态，确保刷新后能恢复
    // 🔧 Task#327 DEBUG: 记录beforeunload时的activeGroupId
    try { localStorage.setItem('__peng_debug_bu', JSON.stringify({activeGroupId: state._lastActiveGroupId, ts: Date.now()})); } catch(e) {}
    saveEditorTabs();
    state.terminalTabs.forEach(tab => {
      if (tab.stopScreenSync) {
        tab.stopScreenSync();
      }
    });
  });
}

