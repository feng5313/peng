// ==================== 工作目录验证 ====================

/**
 * 剥离路径末尾的分隔符（正斜杠和反斜杠，兼容Windows/Linux路径）
 * 🔧 事务#2156：原正则 /\/+$/ 只剥正斜杠，Windows用户输入的末尾反斜杠（如
 * D:\AI\ComfyUI\）不被剥离，而后端 Path.resolve() 返回的普通目录路径无尾分隔符，
 * 比较误判为"实质不同"→用后端值重写输入框→末尾反斜杠被"自动删除"（约1秒，防抖+fetch往返）
 * 盘符根特例：'D:\'/'D:/' 剥离后为 'D:'，与后端 resolve 保留的 'D:\' 剥离后一致，正确跳过重写
 * @param {string} p - 路径
 * @returns {string} 剥离尾部斜杠后的路径
 */
function _stripTrailingPathSep(p) {
  return p.replace(/[\/\\]+$/, '');
}

/**
 * 验证并设置工作目录（实际切换到后端）
 * @param {string} path - 工作目录路径
 */
async function validateAndSetWorkdir(path) {
  const workdirInput = document.getElementById('workdir-input');
  if (!workdirInput) return;

  // 空路径视为无效
  if (!path || path.trim() === '') {
    workdirInput.classList.add('invalid');
    state.workdir = null;
    return;
  }

  // 🔧 关键修复：远程nest模式下，保存workdir到远程nest配置
  if (state.remoteNest) {
    workdirInput.classList.remove('invalid');
    console.warn('[WORKDIR-DEBUG] 走远程分支, remoteNest:', state.remoteNest);
    // 🔧 内部统一使用正斜杠，仅UI显示时转换为平台本机格式
    const safePath = path.replace(/\\/g, '/');
    state.remoteNestRoot = safePath;
    state.workdir = safePath;
    // 🔧 事务#2156：仅当显示值实际变化时才写输入框——
    // 值相同时重写会导致光标跳到末尾（用户输入中被打断），且是末尾反斜杠被"删除"的显式入口
    const displayPath = toNativeDisplayPath(safePath);
    if (displayPath !== workdirInput.value) {
      workdirInput.value = displayPath;
    }
    // 🔧 保存到本地 config.webui.workdir（save_only 跳过路径验证，远程路径本地不存在）
    try {
      await fetch(`/api/set-workdir?path=${encodeURIComponent(safePath)}&save_only=true`, {
        method: 'POST',
      });
    } catch (e) {
      console.warn('保存本地workdir配置失败:', e);
    }
    // 🔧 如果是远程nest（非local），同步到远程端
    if (state.remoteNest !== 'local') {
      try {
        await fetch(`/api/set-workdir?path=${encodeURIComponent(safePath)}&nest=${encodeURIComponent(state.remoteNest)}`, {
          method: 'POST',
        });
      } catch (e) {
        console.warn('保存远程workdir失败:', e);
      }
      try {
        await fetch('/api/nest/set-workdir', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ nest: state.remoteNest, workdir: safePath }),
        });
      } catch (e) {
        console.warn('同步workdir到远程nest.json失败:', e);
      }
    }
    return;
  }

  // 🔧 修复：如果工作目录与当前相同，跳过整个函数（避免频繁调用 loadProjectStructure）
  // 规范化路径：移除尾部斜杠（/和\）后比较
  const normalizedNewPath = _stripTrailingPathSep(path);
  const normalizedCurrentPath = _stripTrailingPathSep(state.workdir || '');
  if (normalizedNewPath === normalizedCurrentPath) {
    // 路径相同，无需切换，直接返回
    return;
  }

  try {
    // 调用后端实际切换工作目录
    const response = await fetch(`/api/set-workdir?path=${encodeURIComponent(path)}`, {
      method: 'POST',
    });
    const result = await response.json();

    if (result.success) {
      workdirInput.classList.remove('invalid');
      state.workdir = result.path;  // 使用解析后的绝对路径
      // 只有路径实质不同时才更新输入框（忽略尾部斜杠差异，/和\均忽略）
      // 🔧 事务#2156：末尾反斜杠必须纳入忽略范围，否则Windows路径每次都会被
      // 后端resolve()的无尾分隔符结果重写，造成"末尾反斜杠被自动删除"
      const normalizedInput = _stripTrailingPathSep(path);
      const normalizedResult = _stripTrailingPathSep(result.path || '');
      if (normalizedResult && normalizedResult !== normalizedInput) {
        workdirInput.value = toNativeDisplayPath(result.path);
      }
      // 🔧 工作目录切换后重新加载项目结构
      loadProjectStructure();
    } else {
      // 🔧 路径无效（不存在或不是目录），只标红显示，不自动回退
      workdirInput.classList.add('invalid');
      state.workdir = null;
    }
  } catch (error) {
    console.error('设置工作目录失败:', error);
    // 网络错误时标记为无效
    workdirInput.classList.add('invalid');
  }
}

