// ==================== 主题管理 ====================

function initTheme() {
  const savedTheme = localStorage.getItem('peng-theme') || 'dark';
  setTheme(savedTheme);
}

function setTheme(theme) {
  state.theme = theme;
  document.body.className = theme === 'dark' ? 'dark-theme' : 'light-theme';

  // 同步更新 xterm 终端主题（必须使用 spread 创建新对象引用才能触发更新）
  if (state.output && XTERM_THEMES[theme]) {
    state.output.options.theme = { ...XTERM_THEMES[theme] };
  }

  // 同步更新所有终端标签页的主题
  if (state.terminalTabs && XTERM_THEMES[theme]) {
    state.terminalTabs.forEach(tab => {
      if (tab.xterm) {
        tab.xterm.options.theme = { ...XTERM_THEMES[theme] };
      }
    });
  }

  // 应用主题配置（CSS 变量 + 图标）
  applyThemeConfig(theme);

  // 同步更新 Monaco Editor 主题（在applyThemeConfig之后，确保themeConfig已加载）
  if (monacoEditor && window.monaco) {
    applyMonacoTheme(theme);
  }

  localStorage.setItem('peng-theme', theme);
}

function toggleTheme() {
  setTheme(state.theme === 'dark' ? 'light' : 'dark');
}

// ==================== 全屏切换 ====================

function toggleFullscreen() {
  if (!document.fullscreenElement) {
    // 进入全屏
    document.documentElement.requestFullscreen().then(() => {
    }).catch(err => {
      console.error('进入全屏失败:', err);
    });
  } else {
    // 退出全屏
    if (document.exitFullscreen) {
      document.exitFullscreen().then(() => {
        showSimpleToast('退出全屏', "info");
      }).catch(err => {
        showSimpleToast('退出全屏失败:', 'error');
      });
    }
  }
}

// 监听全屏变化事件
document.addEventListener('fullscreenchange', () => {
  const fullscreenToggle = document.getElementById('fullscreen-toggle');
  if (fullscreenToggle) {
    if (document.fullscreenElement) {
      fullscreenToggle.textContent = '⛶';
      fullscreenToggle.title = '退出全屏 (ESC)';
    } else {
      fullscreenToggle.textContent = '⛶';
      fullscreenToggle.title = '全屏';
    }
  }
});

// 终端全屏切换
function toggleTerminalFullscreen() {
  const terminalView = document.getElementById('terminal-view');
  const terminalFullscreenBtn = document.getElementById('terminal-fullscreen');

  if (terminalView) {
    terminalView.classList.toggle('fullscreen');

    if (terminalFullscreenBtn) {
      if (terminalView.classList.contains('fullscreen')) {
        terminalFullscreenBtn.textContent = '⛶';
        terminalFullscreenBtn.title = '退出全屏 (ESC)';
      } else {
        terminalFullscreenBtn.textContent = '⛶';
        terminalFullscreenBtn.title = '满页面显示';
      }
    }

    // 调整终端大小
    const currentTab = state.terminalTabs.find(tab => tab.id === state.currentTerminalTabId);
    if (currentTab && currentTab.fit) {
      setTimeout(() => {
        currentTab.fit.fit();
      }, 100);
    }
  }
}

/** 编辑器满页面显示切换 */
let _editorFullscreenEscHandler = null;
function toggleEditorFullscreen() {
  const mainContent = document.querySelector('.main-content');
  const fullscreenBtn1 = document.getElementById('editor-fullscreen-btn');
  const fullscreenBtn2 = document.getElementById('editor-fullscreen-btn-default');

  if (!mainContent) return;

  const isFullscreen = mainContent.classList.toggle('editor-fullscreen');

  // 更新按钮提示
  [fullscreenBtn1, fullscreenBtn2].forEach(btn => {
    if (btn) btn.title = isFullscreen ? '退出满页面显示 (ESC)' : '满页面显示';
  });

  // ESC键退出
  if (isFullscreen) {
    _editorFullscreenEscHandler = (e) => {
      if (e.key === 'Escape' && mainContent.classList.contains('editor-fullscreen')) {
        toggleEditorFullscreen();
      }
    };
    document.addEventListener('keydown', _editorFullscreenEscHandler);
  } else if (_editorFullscreenEscHandler) {
    document.removeEventListener('keydown', _editorFullscreenEscHandler);
    _editorFullscreenEscHandler = null;
  }

  // 重新布局Monaco编辑器
  setTimeout(() => {
    layoutAllMonacoEditors();
  }, 100);
}

// ==================== 字体设置 ====================
