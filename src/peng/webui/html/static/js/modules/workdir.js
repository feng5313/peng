// ==================== 工作目录验证 ====================

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
    workdirInput.value = toNativeDisplayPath(safePath);
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
  // 规范化路径：移除尾部斜杠后比较
  const normalizedNewPath = path.replace(/\/+$/, '');
  const normalizedCurrentPath = (state.workdir || '').replace(/\/+$/, '');
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
      // 只有路径实质不同时才更新输入框（忽略尾部斜杠差异）
      const normalizedInput = path.replace(/\/+$/, '');
      const normalizedResult = (result.path || '').replace(/\/+$/, '');
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

