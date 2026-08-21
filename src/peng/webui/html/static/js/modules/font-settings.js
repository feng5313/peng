const FONT_SETTINGS = {
  fontFamily: 'JetBrains Mono, Fira Code, Consolas, Monaco, monospace',
  fontSize: 14,
  fontSizeXterm: 14,
  fontSizeUI: 14,
  fontSizeEditor: 14,
  emojiTextMode: false  // 🔧 Task#418：emoji文字模式，默认关闭(彩色emoji)
};

/**
 * 将字体族字符串包装为包含 CJK 中文字体 fallback 的完整 font-family 值。
 * 等宽英文字体不包含中文字形，浏览器会逐个 fallback，最终走到系统默认字体，
 * 导致用户选择的字体对中文无效。此函数在末尾追加常见中文字体，确保中文也受控。
 */
function buildFontFamilyWithCJK(fontFamily) {
  // 如果已经包含中文字体，不再重复追加
  if (/Microsoft YaHei|SimHei|SimSun|PingFang|Noto Sans CJK|WenQuanYi/i.test(fontFamily)) {
    return fontFamily;
  }
  return `${fontFamily}, "Microsoft YaHei", "PingFang SC", "Noto Sans CJK SC", "SimHei", sans-serif`;
}

function loadFontSettings() {
  try {
    // 优先从 webuiConfig 加载（来自 config/webui.json）
    const cfg = state.webuiConfig.font;
    if (cfg) {
      FONT_SETTINGS.fontFamily = cfg.family || FONT_SETTINGS.fontFamily;
      FONT_SETTINGS.fontSize = cfg.size || FONT_SETTINGS.fontSize;
      FONT_SETTINGS.fontSizeXterm = cfg.size_xterm || FONT_SETTINGS.fontSizeXterm;
      FONT_SETTINGS.fontSizeUI = cfg.size || FONT_SETTINGS.fontSizeUI;
      // 🔧 Task#1035：编辑器字号独立设置，若未配置则回退到通用字号
      FONT_SETTINGS.fontSizeEditor = cfg.size_editor || FONT_SETTINGS.fontSize;
    }
    // 🔧 Task#418：读取编辑器配置（emoji_text_mode等）
    const editorCfg = state.webuiConfig.editor;
    if (editorCfg) {
      FONT_SETTINGS.emojiTextMode = !!editorCfg.emoji_text_mode;
    }
    // localStorage 可覆盖（用户个性化调整优先）
    const saved = localStorage.getItem('peng-font-settings');
    if (saved) {
      const settings = JSON.parse(saved);
      Object.assign(FONT_SETTINGS, settings);
    }
    applyFontSettings();
  } catch (error) {
    console.error('加载字体设置失败:', error);
  }
}

function saveFontSettings() {
  try {
    localStorage.setItem('peng-font-settings', JSON.stringify(FONT_SETTINGS));
  } catch (error) {
    console.error('保存字体设置失败:', error);
  }
}

/** 同步字体设置到后端 config/webui.json */
function _syncFontSettingsToBackend() {
  try {
    fetch('/api/config/ui-settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        font: {
          family: FONT_SETTINGS.fontFamily,
          size: FONT_SETTINGS.fontSize,
          size_xterm: FONT_SETTINGS.fontSizeXterm,
          size_editor: FONT_SETTINGS.fontSizeEditor
        }
      })
    }).catch(err => console.warn('同步字体设置到后端失败:', err));
  } catch (error) {
    console.warn('同步字体设置到后端失败:', error);
  }
}

/** 同步侧边栏宽度到后端 config/webui.json */
function _syncSidebarWidthToBackend() {
  try {
    const leftSidebar = document.getElementById('sidebar-left');
    const rightSidebar = document.getElementById('sidebar-right');
    const settings = {};
    if (leftSidebar) settings.sidebar_left_width = leftSidebar.offsetWidth;
    if (rightSidebar) settings.sidebar_right_width = rightSidebar.offsetWidth;
    if (Object.keys(settings).length > 0) {
      fetch('/api/config/ui-settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(settings)
      }).catch(err => console.warn('同步侧边栏宽度到后端失败:', err));
    }
  } catch (error) {
    console.warn('同步侧边栏宽度到后端失败:', error);
  }
}

/** 🔧 Task#153：同步选中的消息/事务ID到后端 config/webui.json */
let _saveSelectionTimer = null;
function _saveSelectionToBackend() {
  // 防抖：合并短时间内的多次选中操作
  if (_saveSelectionTimer) clearTimeout(_saveSelectionTimer);
  _saveSelectionTimer = setTimeout(() => {
    try {
      fetch('/api/config/ui-settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          selected_message_id: state.currentMessageId,
          selected_task_id: state.currentTaskId,
          selected_branch_message_id: state.currentBranchMessageId,
        })
      }).catch(err => console.warn('同步选中状态到后端失败:', err));
    } catch (error) {
      console.warn('同步选中状态到后端失败:', error);
    }
  }, 500);  // 500ms 防抖
}

function applyFontSettings() {
  // 应用到xterm（主输出终端）
  if (state.output) {
    state.output.options.fontSize = FONT_SETTINGS.fontSizeXterm;
    state.output.options.fontFamily = FONT_SETTINGS.fontFamily;
  }

  // 应用到所有终端标签页
  state.terminalTabs.forEach(tab => {
    if (tab.xterm) {
      try {
        tab.xterm.options.fontSize = FONT_SETTINGS.fontSizeXterm;
        tab.xterm.options.fontFamily = FONT_SETTINGS.fontFamily;
      } catch (error) {
        console.warn(`[applyFontSettings] 更新终端 ${tab.id} 字体设置失败:`, error);
      }
    }
  });

  // 应用到UI元素（输入框、文本等）
  // 🔧 修复：同时更新 --font-mono 变量（CSS中大部分地方使用 var(--font-mono) 而非 var(--font-family)）
  // 🔧 修复：追加 CJK 中文字体 fallback，确保中文也受字体设置影响
  const fontFamilyWithCJK = buildFontFamilyWithCJK(FONT_SETTINGS.fontFamily);
  document.documentElement.style.setProperty('--font-family', fontFamilyWithCJK);
  document.documentElement.style.setProperty('--font-mono', fontFamilyWithCJK);
  // 🔧 Task#415：Monaco 编辑器专用字体变量，不包含 CJK fallback。
  // ZyfunHuge 字体缺少空格字形(U+0020/U+00A0)，.notdef宽度为全角(1024)，
  // 不含 CJK fallback 能确保缺失字形时浏览器正确 fallback 到下一个等宽字体，
  // 而不是用 .notdef 或 CJK 字体的空格。
  document.documentElement.style.setProperty('--font-mono-editor', FONT_SETTINGS.fontFamily);
  document.documentElement.style.setProperty('--font-size-ui', `${FONT_SETTINGS.fontSizeUI}px`);
  document.documentElement.style.setProperty('--font-size-xterm', `${FONT_SETTINGS.fontSizeXterm}px`);

  // 设置根元素字体大小：CSS 中所有 rem 单位和 var(--font-size-ui) 都会自动按比例缩放。
  // 🔧 Task#1069 修复：不再递归遍历 DOM 强制设置 inherit，那会破坏 CSS 中 rem 定义的差异化字号比例。
  // 正确做法是只设置根元素 font-size，让 CSS 的 rem/rem-based 比例自然生效。
  document.documentElement.style.fontSize = `${FONT_SETTINGS.fontSizeUI}px`;
  document.documentElement.style.fontFamily = fontFamilyWithCJK;

  // 🔧 修复：更新Monaco Editor字体设置（Task#1035：编辑器字号独立设置）
  if (monacoEditor) {
    try {
      monacoEditor.updateOptions({
        fontSize: FONT_SETTINGS.fontSizeEditor,
        fontFamily: FONT_SETTINGS.fontFamily
      });
    } catch (error) {
      console.warn('[applyFontSettings] 更新Monaco Editor字体设置失败:', error);
    }
  }

  // 🔧 Task#418：应用emoji文字模式（通过body class切换CSS）
  // 开启后emoji使用等宽字体字形渲染(单色)，宽度与CJK一致
  // 关闭则使用系统彩色emoji字体（默认）
  if (FONT_SETTINGS.emojiTextMode) {
    document.body.classList.add('emoji-text-mode');
  } else {
    document.body.classList.remove('emoji-text-mode');
  }

  // 🔧 Task#1028：字体变更后必须重新fit，否则cellWidth已变但cols不变，导致文字溢出右侧
  setTimeout(() => {
    if (typeof fitAllTerminals === 'function') {
      fitAllTerminals();
    }
  }, 50);
}

function showFontSettings() {
  // 创建字体设置对话框
  const dialog = document.createElement('div');
  dialog.className = 'font-settings-dialog';
  dialog.innerHTML = `
    <div class="font-settings-content">
      <h3>字体设置</h3>
      <div class="font-setting-row">
        <label>字体:</label>
        <select id="font-family-select" style="flex: 1; padding: 4px; background: var(--input-bg, #1f2328); border: 1px solid var(--border-color, #30363d); color: var(--text-color, #e6edf3); border-radius: 4px;">
          ${state.webuiConfig.font.family.split(',').map(f => {
    const trimmed = f.trim();
    const isSelected = FONT_SETTINGS.fontFamily.split(',')[0].trim() === trimmed ? 'selected' : '';
    return `<option value="${trimmed}">${trimmed}</option>`;
  }).join('')}
        </select>
      </div>
      <div class="font-setting-row">
        <label>字体大小:</label>
        <input type="range" id="font-size-slider" min="10" max="24" step="0.5" value="${FONT_SETTINGS.fontSize}">
        <input type="number" id="font-size-input" min="10" max="24" step="0.5" value="${FONT_SETTINGS.fontSize}">
        <span>px</span>
      </div>
      <div class="font-setting-row">
        <label>编辑器字体大小:</label>
        <input type="range" id="font-size-editor-slider" min="10" max="24" step="0.5" value="${FONT_SETTINGS.fontSizeEditor}">
        <input type="number" id="font-size-editor-input" min="10" max="24" step="0.5" value="${FONT_SETTINGS.fontSizeEditor}">
        <span>px</span>
      </div>
      <div class="font-setting-row">
        <label>终端字体大小:</label>
        <input type="range" id="font-size-xterm-slider" min="10" max="24" step="0.5" value="${FONT_SETTINGS.fontSizeXterm}">
        <input type="number" id="font-size-xterm-input" min="10" max="24" step="0.5" value="${FONT_SETTINGS.fontSizeXterm}">
        <span>px</span>
      </div>
      <div class="font-setting-buttons">
        <button id="font-settings-cancel">取消</button>
        <button id="font-settings-apply">应用</button>
      </div>
    </div>
  `;

  // 添加样式
  if (!document.querySelector('#font-settings-style')) {
    const style = document.createElement('style');
    style.id = 'font-settings-style';
    style.textContent = `
      .font-settings-dialog {
        position: fixed;
        top: 0;
        left: 0;
        right: 0;
        bottom: 0;
        background: rgba(0, 0, 0, 0.5);
        display: flex;
        justify-content: center;
        align-items: center;
        z-index: 10000;
      }
      .font-settings-content {
        background: var(--background-color, #0d1117);
        padding: 20px;
        border-radius: 8px;
        box-shadow: 0 4px 12px rgba(0, 0, 0, 0.3);
        min-width: 400px;
      }
      .font-settings-content h3 {
        margin-top: 0;
        margin-bottom: 20px;
        color: var(--text-color, #e6edf3);
      }
      .font-setting-row {
        display: flex;
        align-items: center;
        margin-bottom: 15px;
        gap: 10px;
      }
      .font-setting-row label {
        width: 120px;
        color: var(--text-color, #e6edf3);
      }
      .font-setting-row input[type="range"] {
        flex: 1;
      }
      .font-setting-row input[type="number"] {
        width: 60px;
        padding: 4px;
        background: var(--input-bg, #1f2328);
        border: 1px solid var(--border-color, #30363d);
        color: var(--text-color, #e6edf3);
        border-radius: 4px;
      }
      .font-setting-buttons {
        display: flex;
        justify-content: flex-end;
        gap: 10px;
        margin-top: 20px;
      }
      .font-setting-buttons button {
        padding: 8px 16px;
        border-radius: 4px;
        cursor: pointer;
        border: none;
        font-size: 14px;
      }
      #font-settings-cancel {
        background: var(--button-secondary, #21262d);
        color: var(--text-color, #e6edf3);
      }
      #font-settings-apply {
        background: var(--button-primary, #238636);
        color: white;
      }
      .light-theme .font-settings-content {
        background: #ffffff;
      }
      .light-theme .font-setting-row label {
        color: #1f2328;
      }
      .light-theme .font-setting-row input[type="number"] {
        background: #f6f8fa;
        border-color: #d0d7de;
        color: #1f2328;
      }
      .light-theme #font-settings-cancel {
        background: #f6f8fa;
        color: #1f2328;
      }
    `;
    document.head.appendChild(style);
  }

  document.body.appendChild(dialog);

  // 事件绑定
  const fontSizeSlider = document.getElementById('font-size-slider');
  const fontSizeInput = document.getElementById('font-size-input');
  const fontSizeEditorSlider = document.getElementById('font-size-editor-slider');
  const fontSizeEditorInput = document.getElementById('font-size-editor-input');
  const fontSizeXtermSlider = document.getElementById('font-size-xterm-slider');
  const fontSizeXtermInput = document.getElementById('font-size-xterm-input');
  const fontFamilySelect = document.getElementById('font-family-select');
  const cancelButton = document.getElementById('font-settings-cancel');
  const applyButton = document.getElementById('font-settings-apply');

  // 同步slider和input
  fontSizeSlider.addEventListener('input', () => {
    fontSizeInput.value = fontSizeSlider.value;
  });
  fontSizeInput.addEventListener('input', () => {
    fontSizeSlider.value = fontSizeInput.value;
  });
  fontSizeEditorSlider.addEventListener('input', () => {
    fontSizeEditorInput.value = fontSizeEditorSlider.value;
  });
  fontSizeEditorInput.addEventListener('input', () => {
    fontSizeEditorSlider.value = fontSizeEditorInput.value;
  });
  fontSizeXtermSlider.addEventListener('input', () => {
    fontSizeXtermInput.value = fontSizeXtermSlider.value;
  });
  fontSizeXtermInput.addEventListener('input', () => {
    fontSizeXtermSlider.value = fontSizeXtermInput.value;
  });

  // 取消按钮
  cancelButton.addEventListener('click', () => {
    document.body.removeChild(dialog);
  });

  // 应用按钮
  applyButton.addEventListener('click', () => {
    const selectedFont = fontFamilySelect.value;
    // 将选中的字体移到 family 候选列表第一位，作为实际使用的字体
    const candidates = state.webuiConfig.font.family.split(',').map(f => f.trim());
    const idx = candidates.indexOf(selectedFont);
    if (idx > 0) {
      candidates.splice(idx, 1);
      candidates.unshift(selectedFont);
    }
    const newFamily = candidates.join(', ');
    FONT_SETTINGS.fontFamily = newFamily;
    state.webuiConfig.font.family = newFamily;
    FONT_SETTINGS.fontSize = parseFloat(fontSizeInput.value);
    FONT_SETTINGS.fontSizeEditor = parseFloat(fontSizeEditorInput.value);
    FONT_SETTINGS.fontSizeXterm = parseFloat(fontSizeXtermInput.value);
    FONT_SETTINGS.fontSizeUI = parseFloat(fontSizeInput.value);
    state.webuiConfig.font.size_editor = FONT_SETTINGS.fontSizeEditor;

    applyFontSettings();
    saveFontSettings();
    // 同步到后端 config（family 整体同步，选中的已移到第一位）
    _syncFontSettingsToBackend();
    document.body.removeChild(dialog);
  });

  // 点击外部关闭
  dialog.addEventListener('click', (e) => {
    if (e.target === dialog) {
      document.body.removeChild(dialog);
    }
  });
}

