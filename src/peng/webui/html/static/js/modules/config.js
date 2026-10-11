// ==================== QRCode 懒加载 ====================

// 🔧 性能优化：QRCode.js 按需加载，避免阻塞页面初始化（从CDN加载超时可达15s+）
let _qrcodeLoaded = false;
let _qrcodeLoading = null;  // Promise，防止重复加载

async function ensureQRCode() {
  if (typeof QRCode !== 'undefined') { _qrcodeLoaded = true; return; }
  if (_qrcodeLoading) return _qrcodeLoading;
  _qrcodeLoading = new Promise((resolve, reject) => {
    const script = document.createElement('script');
    script.src = '/static/vendor/qrcode.min.js';
    script.onload = () => { _qrcodeLoaded = true; resolve(); };
    script.onerror = () => { _qrcodeLoading = null; reject(new Error('QRCode.js 加载失败')); };
    document.head.appendChild(script);
  });
  return _qrcodeLoading;
}

// ==================== 配置加载 ====================

// 🔧 性能优化：配置缓存，避免重复请求 /api/config
let _cachedConfig = null;

async function _fetchConfig() {
  if (_cachedConfig) return _cachedConfig;
  const response = await fetch('/api/config');
  if (!response.ok) throw new Error('获取配置失败');
  _cachedConfig = await response.json();
  return _cachedConfig;
}

async function loadConfig() {
  try {
    const config = await _fetchConfig();

    // 加载鹏配置
    if (config.agent_name) {
      state.agentName = config.agent_name;
    }

    // 加载master配置
    if (config.master_name) {
      state.masterName = config.master_name;
    }

    // 加载后端运行平台（用于路径超链接匹配策略）
    if (config.platform) {
      state.platform = config.platform;
      // 🔧 事务#2156：config到位后刷新local nest平台缓存
      // 修复初始化竞态：nest.js的/api/nest/list可能先于本config到达，
      // _updateWorkdirForNest会用尚未加载的state.platform固化state.nestPlatform为错误值
      if (typeof _resolveNestPlatform === 'function' && (!state.remoteNest || state.remoteNest === 'local')) {
        state.nestPlatform = _resolveNestPlatform(state.remoteNest || 'local');
      }
    }

    // 同步全局 thinking_mode 到勾选框
    // 思考级别支持: auto/enabled/disabled/low/medium/high/xhigh/max
    // 勾选框反映"是否思考"：disabled=未勾选，其他=勾选（级别值记忆在 window._lastThinkingMode）
    const thinkingMode = config.thinking_mode || 'auto';
    window._lastThinkingMode = thinkingMode;
    const thinkingToggle = document.getElementById('thinking-toggle');
    if (thinkingToggle) {
      thinkingToggle.checked = (thinkingMode !== 'disabled');
    }

    // 同步全局 thinking_budget 到滑块和输入框（与配置保持一致，非WebUI覆盖配置）
    const thinkingBudget = config.thinking_budget || 0;
    const thinkingBudgetSlider = document.getElementById('thinking-budget-slider');
    const thinkingBudgetInput = document.getElementById('thinking-budget');
    if (thinkingBudgetSlider) {
      thinkingBudgetSlider.value = thinkingBudget;
    }
    if (thinkingBudgetInput) {
      thinkingBudgetInput.value = thinkingBudget;
    }

    // 🔧 Task#262：同步大文件加载配置到 state.webuiConfig
    if (config.large_file_threshold) state.webuiConfig.large_file_threshold = config.large_file_threshold;
    if (config.large_file_parallel_threshold) state.webuiConfig.large_file_parallel_threshold = config.large_file_parallel_threshold;
    if (config.large_file_parallel_concurrency) state.webuiConfig.large_file_parallel_concurrency = config.large_file_parallel_concurrency;
    if (config.large_file_chunk_size) state.webuiConfig.large_file_chunk_size = config.large_file_chunk_size;
    // 🔧 输入历史记录最大条数（前后端统一配置）
    if (config.input_history_limit) state.webuiConfig.input_history_limit = config.input_history_limit;

    // 🔧 Task#418：同步编辑器配置到 state.webuiConfig
    if (config.editor) {
      if (!state.webuiConfig.editor) state.webuiConfig.editor = {};
      if (config.editor.emoji_text_mode !== undefined) state.webuiConfig.editor.emoji_text_mode = config.editor.emoji_text_mode;
      if (config.editor.auto_create_empty_doc !== undefined) state.webuiConfig.editor.auto_create_empty_doc = config.editor.auto_create_empty_doc;
    }

    // 恢复拖动功能状态
    try {
      const savedEnableDrag = localStorage.getItem('peng_enable_drag');
      if (savedEnableDrag !== null) {
        state.enableDrag = JSON.parse(savedEnableDrag);
      }
    } catch (error) {
      console.warn('恢复拖动状态失败:', error);
      state.enableDrag = false;
    }

  } catch (error) {
    console.error('❌ 加载配置失败:', error);
    // 使用默认值
    state.agentName = '鹏🐣';
    state.masterName = 'master';
  }
}

/**
 * 加载项目目录结构（用于路径超链接二次过滤）
 * 从后端 /api/project-structure 获取 projectDirs 和 projectFiles，
 * 存储到 state.projectDirs 和 state.projectFiles 中。
 * 
 * 🔧 优化：缓存已加载的工作目录，避免来回切换时重复加载
 */
let _loadedProjectStructureWorkdir = null;  // 已加载项目结构的工作目录

async function loadProjectStructure() {
  // 🔧 优化：如果当前工作目录与已加载的相同，跳过重新加载
  const currentWorkdir = (state.workdir || '').replace(/\/+$/, '');
  const loadedWorkdir = (_loadedProjectStructureWorkdir || '').replace(/\/+$/, '');
  if (currentWorkdir === loadedWorkdir && state.projectDirs.size > 0) {
    // 已加载过该目录的项目结构，无需重复加载
    return;
  }

  try {
    const response = await fetch('/api/project-structure');
    if (!response.ok) return;
    const data = await response.json();
    if (data.projectDirs) {
      state.projectDirs = new Set(data.projectDirs);
    }
    if (data.projectFiles) {
      state.projectFiles = new Set(data.projectFiles);
    }
    // 🔧 记录已加载的工作目录
    _loadedProjectStructureWorkdir = state.workdir;
  } catch (error) {
    console.warn('加载项目结构失败:', error);
  }
}

/**
 * 判断路径是否是有效的项目路径（用于超链接二次过滤）
 *
 * 过滤逻辑：
 * 1. 绝对路径（以 '/' 开头）→ 直接保留
 * 2. 家目录路径（以 '~/' 开头）→ 直接保留
 * 3. 包含 '/' 的相对路径 → 检查第一级目录是否在 projectDirs 中
 * 4. 不包含 '/' 的路径 → 检查是否在 projectFiles 中
 * 5. 如果 projectDirs/projectFiles 未加载（为空Set），不过滤（保持原行为）
 *
 * @param {string} path - 待检查的路径字符串
 * @returns {boolean} true 表示是有效项目路径，应该生成超链接
 */
function isValidProjectPath(path) {
  // 如果项目结构未加载，不过滤
  if (state.projectDirs.size === 0 && state.projectFiles.size === 0) {
    return true;
  }

  // 🔧 修复 Windows 平台：支持反斜杠路径和绝对路径
  // Windows 绝对路径（如 D:\work\peng\src\app.py）直接保留，与 Linux 以 / 开头一致
  if (/^[a-zA-Z]:[\\/]/.test(path)) {
    return true;
  }

  // Unix 绝对路径直接保留
  if (path.startsWith('/')) return true;
  // 家目录路径直接保留
  if (path.startsWith('~/')) return true;
  // Windows 相对路径（.\ 或 ..\ 开头）直接保留
  if (/^\.{1,2}[\\/]/.test(path)) return true;

  // 统一分隔符：将反斜杠转为正斜杠进行验证
  const normalizedPath = path.replace(/\\/g, '/');
  const hasSlash = normalizedPath.includes('/');

  if (hasSlash) {
    // 提取第一级目录名（如 "src/peng/app.py" → "src"）
    const firstSegment = normalizedPath.split('/')[0];
    // 检查第一级目录是否在 projectDirs 中
    if (state.projectDirs.has(firstSegment)) {
      return true;
    }
    // 也检查完整路径前缀是否在 projectDirs 中（更精确匹配）
    // 如路径 "src/peng/webui" 检查 "src/peng" 是否在 dirs 中
    const lastSlash = normalizedPath.lastIndexOf('/');
    if (lastSlash > 0) {
      const dirPart = normalizedPath.substring(0, lastSlash);
      if (state.projectDirs.has(dirPart)) {
        return true;
      }
    }
    return false;
  } else {
    // 不包含 '/' 的纯文件名，检查是否在根目录文件列表中
    return state.projectFiles.has(path);
  }
}

function getStackTrace() {
  try {
    throw new Error();
  } catch (e) {
    return e.stack;
  }
};
