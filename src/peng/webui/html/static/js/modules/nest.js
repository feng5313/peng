// ==================== Nest 自定义下拉选择器（支持图标） ====================

/**
 * 获取 nest 的图标 HTML
 * local 用 🪹 emoji，远程用平台对应的 SVG/img 图标
 */
function _getNestIconHTML(nestKey, nestInfo) {
  if (nestKey === 'local' || !nestInfo) {
    return '🪹';
  }
  const platform = nestInfo.platform || '';
  const linuxId = nestInfo.linux_id || '';
  return getPlatformIconHTML(platform, linuxId) || '💻';
}

/**
 * 获取 nest 的显示文本
 */
function _getNestDisplayText(nestKey, nestInfo) {
  if (!nestInfo || !nestInfo.connected) {
    return nestKey;
  }
  const platform = nestInfo.platform || '?';
  const hostname = nestInfo.hostname || '?';
  const platformDisplay = platform === 'Linux' && nestInfo.linux_name ? nestInfo.linux_name : platform;
  return `${nestKey} (${platformDisplay}/${hostname})`;
}

async function initNestSelector() {
  const dropdownList = document.getElementById('nest-dropdown-list');
  const hiddenInput = document.getElementById('nest-select');
  if (!dropdownList || !hiddenInput) return;

  try {
    const resp = await fetch('/api/nest/list');
    const data = await resp.json();

    const prevValue = hiddenInput.value;
    // await之后重新读取，因为await期间可能已改变
    const currentSelectValue = hiddenInput.value || prevValue;

    const nests = data.nests || {};
    const defaultNest = data.default_nest || 'local';

    // 🔧 修复性能：数据比较，无变化时跳过DOM重建
    const currentDataJson = JSON.stringify(nests);
    if (window._lastNestsDataJson === currentDataJson) {
      return; // 数据无变化，跳过DOM重建
    }
    window._lastNestsDataJson = currentDataJson;

    // 按连接状态排序：已连接的在前
    const sortedEntries = Object.entries(nests).sort((a, b) => {
      const aConnected = a[1].connected ? 0 : 1;
      const bConnected = b[1].connected ? 0 : 1;
      return aConnected - bConnected;
    });

    // 确定目标选中值
    let targetValue;
    if (!window._nestInited) {
      targetValue = defaultNest;
    } else if (currentSelectValue && nests.hasOwnProperty(currentSelectValue)) {
      targetValue = currentSelectValue;
    } else if (prevValue && nests.hasOwnProperty(prevValue)) {
      targetValue = prevValue;
    } else {
      targetValue = defaultNest;
    }

    // 渲染下拉列表
    dropdownList.innerHTML = '';
    for (const [key, val] of sortedEntries) {
      const item = document.createElement('div');
      item.className = 'nest-dropdown-item' + (key === targetValue ? ' selected' : '');
      item.dataset.value = key;

      const iconHTML = _getNestIconHTML(key, val);
      const displayText = _getNestDisplayText(key, val);

      item.innerHTML = `<span class="item-icon">${iconHTML}</span><span class="item-text">${displayText}</span>`;
      item.addEventListener('click', () => _onNestItemSelected(key));
      dropdownList.appendChild(item);
    }

    window._nestInited = true;

    // 更新选中项的显示
    _updateNestDropdownDisplay(targetValue, nests);

    // 缓存最新 nests 数据
    window._latestNests = nests;

    // 控制显示/隐藏
    const dropdown = document.getElementById('nest-dropdown');
    if (Object.keys(nests).length < 1) {
      dropdown.style.display = 'none';
    } else {
      dropdown.style.display = '';
    }

    // 首次初始化时同步 nest 状态
    if (!window._nestStateInited) {
      const currentNestInfo = nests[targetValue];
      if (currentNestInfo && currentNestInfo.connected) {
        _updateWorkdirForNest(targetValue, nests);
      }
      window._nestStateInited = true;
    } else if (prevValue && targetValue !== prevValue && !nests.hasOwnProperty(prevValue)) {
      console.warn(`[NEST] 检测到 nest "${prevValue}" 已断开被删除，自动切换到 "${targetValue}"，更新工作目录`);
      const nestInfo = nests[targetValue];
      if (nestInfo) {
        _updateWorkdirForNest(targetValue, nests);
      }
    }

    // 绑定下拉框开关事件（仅在第一次绑定时）
    if (!window._nestDropdownBindDone) {
      window._nestDropdownBindDone = true;
      _bindDropdownEvents();
    }
  } catch (e) {
    console.warn('加载 Nest 列表失败:', e);
  }
}

/**
 * 更新下拉框选中项的显示（图标+文字）
 */
function _updateNestDropdownDisplay(selectedNest, nests) {
  const iconEl = document.getElementById('nest-dropdown-icon');
  const textEl = document.getElementById('nest-dropdown-text');
  const hiddenInput = document.getElementById('nest-select');
  if (!iconEl || !textEl || !hiddenInput) return;

  const nestInfo = (nests || {})[selectedNest] || {};
  const iconHTML = _getNestIconHTML(selectedNest, nestInfo);
  const displayText = _getNestDisplayText(selectedNest, nestInfo);

  iconEl.innerHTML = iconHTML;
  textEl.textContent = displayText;
  hiddenInput.value = selectedNest;

  // 更新列表中的 selected 状态
  const items = document.querySelectorAll('#nest-dropdown-list .nest-dropdown-item');
  items.forEach(item => {
    item.classList.toggle('selected', item.dataset.value === selectedNest);
  });
}

/**
 * 处理下拉项选中
 */
async function _onNestItemSelected(selectedNest) {
  // 关闭下拉列表
  _closeNestDropdown();

  const hiddenInput = document.getElementById('nest-select');
  if (hiddenInput.value === selectedNest) return;

  // 🔧 Task#259：移除 _skipWorkdirUpdate 标志机制
  // 用户手动点击切换nest时，始终更新workdir

  try {
    const resp = await fetch('/api/nest/select', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ nest: selectedNest }),
    });
    const result = await resp.json();
    if (result.success) {
      console.log(`Nest 切换成功: ${selectedNest}`);
      _updateNestDropdownDisplay(selectedNest, window._latestNests);
      // 🔧 Task#259：用户手动切换nest时，始终更新工作目录
      _updateWorkdirForNest(selectedNest, window._latestNests);
      // 触发 change 事件，保持与旧 select 兼容
      hiddenInput.value = selectedNest;
      hiddenInput.dispatchEvent(new Event('change'));
    } else {
      console.error(`Nest 切换失败: ${result.error}`);
    }
  } catch (e) {
    console.error('Nest 切换请求失败:', e);
  }
}

/**
 * 绑定下拉框的开关事件
 */
function _bindDropdownEvents() {
  const selected = document.getElementById('nest-dropdown-selected');
  const dropdownList = document.getElementById('nest-dropdown-list');

  if (!selected || !dropdownList) return;

  // 点击选中框切换下拉
  selected.addEventListener('click', (e) => {
    e.stopPropagation();
    const isOpen = dropdownList.classList.contains('open');
    if (isOpen) {
      _closeNestDropdown();
    } else {
      dropdownList.classList.add('open');
    }
  });

  // 键盘支持
  selected.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      selected.click();
    } else if (e.key === 'Escape') {
      _closeNestDropdown();
    }
  });

  // 点击外部关闭
  document.addEventListener('click', (e) => {
    const dropdown = document.getElementById('nest-dropdown');
    if (dropdown && !dropdown.contains(e.target)) {
      _closeNestDropdown();
    }
  });
}

function _closeNestDropdown() {
  const dropdownList = document.getElementById('nest-dropdown-list');
  if (dropdownList) {
    dropdownList.classList.remove('open');
  }
}

/**
 * 同步外部设置的下拉值（供 _syncNestDropdown 等调用）
 * 同步更新显示、hidden input 值，并通知后端
 */
function _syncNestDropdownValue(nestName) {
  const hiddenInput = document.getElementById('nest-select');
  const currentValue = hiddenInput ? hiddenInput.value : '';
  if (currentValue === nestName) return;

  if (hiddenInput) {
    hiddenInput.value = nestName;
  }
  _updateNestDropdownDisplay(nestName, window._latestNests);
  // 同步后端状态并保存到 config.webui.nest
  fetch('/api/nest/select', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ nest: nestName }),
  }).catch(e => console.warn('同步nest选择失败:', e));
}

/**
 * 根据 nest 更新工作目录
 * 直接从nest连接信息中读取workdir（由远程nest注册时传过来）
 */
function _updateWorkdirForNest(selectedNest, nests) {
  const nestInfo = (nests || {})[selectedNest];
  const platform = nestInfo?.platform || 'Linux';
  const defaultRoot = (platform === 'Windows') ? 'C:/' : '/';
  const remoteWorkdir = nestInfo?.workdir || defaultRoot;

  console.warn('[WORKDIR] _updateWorkdirForNest:', selectedNest, '-> workdir:', remoteWorkdir);

  state.remoteNest = selectedNest;
  state.remoteNestRoot = remoteWorkdir;
  state.workdir = remoteWorkdir;
  const workdirInput = document.getElementById('workdir-input');
  if (workdirInput) {
    workdirInput.value = toNativeDisplayPath(remoteWorkdir);
  }
  // 同步到 host 端 config.webui.workdir（save_only 跳过路径验证）
  fetch(`/api/set-workdir?path=${encodeURIComponent(remoteWorkdir)}&save_only=true`, { method: 'POST' }).catch(e => console.warn('保存本地workdir配置失败:', e));
}

/**
 * 更新nest下拉框旁边的图标（保持向后兼容）
 */
function _updateNestLabelIcon(selectedNest) {
  // 图标现在集成在下拉框内，此处仅做兼容调用
  _updateNestDropdownDisplay(selectedNest, window._latestNests);
}

// 定期刷新 Nest 选择器（每10秒，以发现新连接的 nest）
// 🔧 修复性能：从1秒改为10秒，减少不必要的DOM操作和网络请求
setInterval(initNestSelector, 10000);

// 初始化 Nest 选择器
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', initNestSelector);
} else {
  initNestSelector();
}
