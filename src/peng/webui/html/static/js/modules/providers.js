// ==================== API Provider 管理 ====================

/**
 * 优先级更新防抖定时器
 * 每个 provider 对应一个定时器，0.5秒内多次点击只在最后一次执行
 */
const _priorityDebounceTimers = new Map();

/**
 * 防抖版本的 updateProviderPriority
 * 同一个 provider 在 0.5 秒内多次点击时，只在最后一次执行
 */
function debouncedUpdateProviderPriority(providerName, priority) {
  // 清除该 provider 之前的定时器
  if (_priorityDebounceTimers.has(providerName)) {
    clearTimeout(_priorityDebounceTimers.get(providerName));
  }

  // 设置新的定时器，0.5秒后执行
  const timer = setTimeout(() => {
    _priorityDebounceTimers.delete(providerName);
    updateProviderPriorityInternal(providerName, priority);
  }, 500);

  _priorityDebounceTimers.set(providerName, timer);
}

/**
 * 加载所有providers
 */
async function loadProviders() {
  try {
    const response = await fetch('/api/llm/providers');
    if (!response.ok) {
      throw new Error('获取providers失败');
    }
    const data = await response.json();
    state.providers = data.providers || [];
    state.currentProvider = data.current_provider || null;
    // 更新轮选按钮显示当前provider图标
    updateApiProviderButtonIcon();

    // 🔧 后端返回的 current_provider 可能过时，从事务重新同步
    syncCurrentProviderFromTasks();

    // 如果模态框已打开，刷新列表以更新"当前"标记
    const modal = document.getElementById('api-provider-modal');
    if (modal && modal.style.display === 'flex') {
      renderProvidersList();
    }
  } catch (error) {
    console.error('❌ 加载providers失败:', error);
    state.providers = [];
  }
}

/**
 * 从最新活跃事务同步 provider 到大模型按钮图标
 * 用于页面初始化和 loadProviders 后同步
 */
function syncCurrentProviderFromTasks() {
  if (!state.tasks || state.tasks.length === 0 || !state.providers || state.providers.length === 0) return;

  const allTasks = flattenTasks(state.tasks);
  // 找所有有 provider 的事务，按活跃时间降序
  const tasksWithProvider = allTasks.filter(t => t.provider);
  if (tasksWithProvider.length === 0) return;

  tasksWithProvider.sort((a, b) => {
    const timeA = new Date(a.heartbeat_at || a.updated_at || a.created_at || 0).getTime();
    const timeB = new Date(b.heartbeat_at || b.updated_at || b.created_at || 0).getTime();
    return timeB - timeA;
  });

  const latestProvider = tasksWithProvider[0].provider;
  if (!latestProvider || state.currentProvider === latestProvider) return;

  state.currentProvider = latestProvider;
  state.providers.forEach(p => {
    p.is_current = (p.name === latestProvider);
  });
  updateApiProviderButtonIcon();
}

/**
 * 更新API Provider轮选按钮的图标为当前provider的图标
 */
function updateApiProviderButtonIcon() {
  const btn = document.getElementById('api-provider-btn');
  if (!btn || !state.currentProvider) return;

  const currentProvider = state.providers.find(p => p.name === state.currentProvider);
  const icon = currentProvider?.icon || '🤖';
  btn.textContent = `${icon}大模型`;

  // 同时更新模态框标题的图标
  const modalTitle = document.querySelector('#api-provider-modal h2');
  if (modalTitle) {
    modalTitle.textContent = `${icon} 大模型接口设置`;
  }
}

/**
 * 显示API provider设置模态框
 */
let _apiProviderModalResizeHandler = null;

function _updateApiProviderModalWidth() {
  const modalContent = document.getElementById('api-provider-modal-content');
  const mainContent = document.getElementById('main-content');
  if (!modalContent || !mainContent) return;
  const editorWidth = mainContent.clientWidth;
  if (editorWidth > 0) {
    modalContent.style.width = (editorWidth * 0.9) + 'px';
    modalContent.style.maxWidth = 'none';
  }
}

async function showApiProviderModal() {
  const modal = document.getElementById('api-provider-modal');
  if (!modal) {
    console.error('API provider模态框未找到');
    return;
  }

  // 先显示模态框（用户立刻看到反馈）
  modal.style.display = 'flex';

  // 动态设置宽度为编辑器容器宽度的90%
  _updateApiProviderModalWidth();
  // 监听窗口大小变化，保持自适应
  if (!_apiProviderModalResizeHandler) {
    _apiProviderModalResizeHandler = () => _updateApiProviderModalWidth();
    window.addEventListener('resize', _apiProviderModalResizeHandler);
  }

  // 如果已有缓存的providers数据，立即渲染，无需等待网络请求
  if (state.providers && state.providers.length > 0) {
    renderProvidersList();
  } else {
    const container = document.getElementById('api-provider-list');
    if (container) {
      container.innerHTML = '<div class="loading" style="text-align: center; padding: 20px; color: var(--text-secondary);">加载中...</div>';
    }
  }

  // 后台异步刷新最新providers数据（不阻塞显示）
  loadProviders().then(() => {
    renderProvidersList();
  });
}

/**
 * 关闭API provider设置模态框
 */
function closeApiProviderModal() {
  const modal = document.getElementById('api-provider-modal');
  if (modal) {
    modal.style.display = 'none';
  }
  // 清理resize监听器
  if (_apiProviderModalResizeHandler) {
    window.removeEventListener('resize', _apiProviderModalResizeHandler);
    _apiProviderModalResizeHandler = null;
  }
}

/**
 * 渲染providers列表
 */
/**
 * 获取 provider 吞吐率显示 HTML（用于设置窗口）
 * 格式：小的等宽字体显示 tokens/s，无吞吐率时不显示
 */
function getThroughputDisplayHtml(providerName) {
  const tps = state.providerThroughputs[providerName];
  if (!tps || tps <= 0) return '';
  let tpsStr;
  if (tps < 10) {
    tpsStr = tps.toFixed(1);
  } else if (tps < 1000) {
    tpsStr = Math.round(tps).toString();
  } else {
    tpsStr = (tps / 1000).toFixed(1) + 'k';
  }
  return `<span style="font-size: 10px; font-family: 'JetBrains Mono', monospace; color: var(--accent-color); min-width: 40px; text-align: center;" title="${Math.round(tps)} tokens/s">${tpsStr} tok/s</span>`;
}

function renderProvidersList() {
  const container = document.getElementById('api-provider-list');
  if (!container) {
    console.error('API provider列表容器未找到');
    return;
  }

  if (state.providers.length === 0) {
    container.innerHTML = '<div class="no-providers" style="color: var(--text-secondary); text-align: center; padding: 20px;">暂无大模型接口</div>';
    return;
  }

  container.innerHTML = state.providers.map(provider => `
    <label class="filter-checkbox" style="display: flex; align-items: center; padding: 8px 12px; margin: 4px 0; border-radius: 6px; cursor: pointer; transition: background-color 0.2s;" 
           onmouseover="this.style.backgroundColor='var(--bg-hover)'" 
           onmouseout="this.style.backgroundColor='transparent'">
      <input type="checkbox" 
             id="provider-${provider.name}" 
             ${provider.enable ? 'checked' : ''} 
             onchange="toggleProvider('${provider.name}', this.checked)"
             style="margin-right: 10px;">
      <span style="flex: 1; display: flex; align-items: center; gap: 8px;">
        <span style="font-size: 16px;">${provider.icon || '🤖'}</span>
        <span style="font-weight: 500;">${provider.name}</span>
        <span style="color: var(--text-secondary); font-size: 12px;">${provider.model || ''}</span>
        ${provider.name === state.currentProvider ? '<span style="background: var(--accent-color); color: white; font-size: 11px; padding: 2px 6px; border-radius: 4px;">当前</span>' : ''}
      </span>
      <span style="display: flex; align-items: center; gap: 4px; margin-left: 8px;">
        ${getThroughputDisplayHtml(provider.name)}
        <span style="font-size: 11px; color: var(--text-secondary);">优先级</span>
        <input type="number" 
               id="provider-priority-${provider.name}"
               value="${provider.priority !== undefined ? provider.priority : 50}" 
               min="0" max="99" 
               onchange="debouncedUpdateProviderPriority('${provider.name}', this.value)"
               onclick="event.stopPropagation()"
               class="provider-priority-input">
        <button onclick="event.preventDefault(); event.stopPropagation(); editProvider('${provider.name}')" 
                title="编辑" 
                style="background: none; border: none; cursor: pointer; font-size: 14px; padding: 2px 4px; border-radius: 4px; color: var(--text-secondary); transition: all 0.2s;"
                onmouseover="this.style.color='var(--accent-color)'; this.style.backgroundColor='var(--bg-hover)'"
                onmouseout="this.style.color='var(--text-secondary)'; this.style.backgroundColor='transparent'">✏️</button>
        <button onclick="event.preventDefault(); event.stopPropagation(); deleteProvider('${provider.name}')" 
                title="删除" 
                style="background: none; border: none; cursor: pointer; font-size: 14px; padding: 2px 4px; border-radius: 4px; color: var(--text-secondary); transition: all 0.2s;"
                onmouseover="this.style.color='#ff6b6b'; this.style.backgroundColor='var(--bg-hover)'"
                onmouseout="this.style.color='var(--text-secondary)'; this.style.backgroundColor='transparent'">✕</button>
      </span>
    </label>
  `).join('');
}

/**
 * 切换provider的enable状态
 */
async function toggleProvider(providerName, enable) {
  try {
    const response = await fetch(`/api/llm/providers/${encodeURIComponent(providerName)}/enable`, {
      method: 'PUT',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({ enable }),
    });

    if (!response.ok) {
      const error = await response.json();
      throw new Error(error.detail || '更新失败');
    }

    const data = await response.json();

    // 更新本地状态
    const provider = state.providers.find(p => p.name === providerName);
    if (provider) {
      provider.enable = enable;
    }

    // 显示提示
    showSimpleToast(`✅ ${data.message}`, 'success');
  } catch (error) {
    console.error('❌ 切换provider状态失败:', error);
    showSimpleToast(`❌ 切换provider状态失败: ${error.message}`, 'error');

    // 恢复checkbox状态
    const checkbox = document.getElementById(`provider-${providerName}`);
    if (checkbox) {
      checkbox.checked = !enable;
    }
  }
}

/**
 * 更新provider的优先级（内部实现，由防抖函数调用）
 */
async function updateProviderPriorityInternal(providerName, priority) {
  const priorityValue = parseInt(priority, 10);
  if (isNaN(priorityValue) || priorityValue < 0 || priorityValue > 99) {
    showSimpleToast('❌ 优先级必须在0-99范围内', 'error');
    // 恢复原值
    const provider = state.providers.find(p => p.name === providerName);
    if (provider) {
      const input = document.getElementById(`provider-priority-${providerName}`);
      if (input) input.value = provider.priority !== undefined ? provider.priority : 50;
    }
    return;
  }

  try {
    const response = await fetch(`/api/llm/providers/${encodeURIComponent(providerName)}/priority`, {
      method: 'PUT',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({ priority: priorityValue }),
    });

    if (!response.ok) {
      const error = await response.json();
      throw new Error(error.detail || '更新失败');
    }

    const data = await response.json();

    // 更新本地状态
    const provider = state.providers.find(p => p.name === providerName);
    if (provider) {
      provider.priority = priorityValue;
    }

    showSimpleToast(`✅ ${data.message}`, 'success');
  } catch (error) {
    console.error('❌ 更新provider优先级失败:', error);
    showSimpleToast(`❌ 更新优先级失败: ${error.message}`, 'error');

    // 恢复原值
    const provider = state.providers.find(p => p.name === providerName);
    if (provider) {
      const input = document.getElementById(`provider-priority-${providerName}`);
      if (input) input.value = provider.priority !== undefined ? provider.priority : 50;
    }
  }
}


// ==================== 新建 Provider（模态框） ====================

/** 新建 provider 模态框中缓存的 providers 详情 */
let _providersDetailCache = [];

/** 新建 provider 表单是否已初始化过（保留用户填写内容） */
let _createProviderFormInitialized = false;

/**
 * 打开新建 Provider 模态框
 */
async function showCreateProviderModal() {
  const modal = document.getElementById('create-provider-modal');
  if (!modal) return;

  // 确保不在编辑模式
  _editingProviderName = null;

  // 恢复名称输入框可编辑状态
  const nameInput = document.getElementById('cp-name');
  nameInput.readOnly = false;
  nameInput.style.opacity = '1';

  // 恢复继承选择可见
  const cloneGroup = document.getElementById('cp-clone')?.closest('.form-group');
  if (cloneGroup) cloneGroup.style.display = '';

  // 恢复标题和按钮
  const modalTitle = document.querySelector('#create-provider-modal h2');
  if (modalTitle) modalTitle.textContent = ' ✨ 新建大模型接口';
  const saveBtn = document.getElementById('create-provider-save-btn');
  if (saveBtn) saveBtn.textContent = '保存';

  // 只在首次打开时重置表单，之后保留用户已填写的内容
  if (!_createProviderFormInitialized) {
    resetCreateProviderForm();
    _createProviderFormInitialized = true;
  }

  // 填充 clone 下拉列表（保留当前选中的值）
  const cloneSelect = document.getElementById('cp-clone');
  const currentCloneValue = cloneSelect.value;
  const providers = state.providers || [];
  cloneSelect.innerHTML = '<option value="">-- 不继承 --</option>' +
    providers.map(p => `<option value="${p.name}">${p.icon || '🤖'} ${p.name} (${p.model || ''})</option>`).join('');
  cloneSelect.value = currentCloneValue;

  // 预加载 providers 详情（含完整配置）
  try {
    const response = await fetch('/api/llm/providers?detail=true');
    if (response.ok) {
      const data = await response.json();
      _providersDetailCache = data.providers || [];
    }
  } catch (e) {
    console.error('加载 providers 详情失败:', e);
  }

  // 绑定 clone 下拉变化事件
  cloneSelect.onchange = onCloneProviderChange;

  // 绑定图标预览
  document.getElementById('cp-icon').oninput = function () {
    document.getElementById('cp-icon-preview').textContent = this.value || '🤖';
  };

  // 显示模态框
  modal.style.display = 'flex';
}

/**
 * 关闭新建 Provider 模态框
 */
function closeCreateProviderModal() {
  const modal = document.getElementById('create-provider-modal');
  if (modal) {
    modal.style.display = 'none';
  }
}

/**
 * 重置新建 provider 表单（仅在首次打开或保存成功后调用）
 */
function resetCreateProviderForm() {
  document.getElementById('cp-name').value = '';
  document.getElementById('cp-clone').value = '';
  document.getElementById('cp-icon').value = '🤖';
  document.getElementById('cp-icon-preview').textContent = '🤖';
  document.getElementById('cp-api_key').value = '';
  document.getElementById('cp-base_url').value = '';
  document.getElementById('cp-model').value = '';
  document.getElementById('cp-small_model').value = '';
  document.getElementById('cp-priority').value = '50';
  document.getElementById('cp-enable').checked = true;
  document.getElementById('cp-thinking_type').value = '';
  document.getElementById('cp-thinking_mode').value = 'auto';
  document.getElementById('cp-max_tokens').value = '';
  document.getElementById('cp-compact_threshold').value = '';
  document.getElementById('cp-price_input_hit').value = '';
  document.getElementById('cp-price_input_miss').value = '';
  document.getElementById('cp-price_output').value = '';
  document.getElementById('cp-vision').value = 'no';
  const statusEl = document.getElementById('create-provider-status');
  if (statusEl) {
    statusEl.style.display = 'none';
    statusEl.textContent = '';
  }
}

/**
 * Clone 下拉选择变化时自动填充表单
 */
function onCloneProviderChange() {
  const cloneName = document.getElementById('cp-clone').value;
  if (!cloneName) return;

  const source = _providersDetailCache.find(p => p.name === cloneName);
  if (!source) {
    // 回退到 state.providers 基本信息
    const basicSource = state.providers.find(p => p.name === cloneName);
    if (basicSource) {
      fillCreateProviderFields({
        model: basicSource.model || '',
        icon: basicSource.icon || '🤖',
        priority: basicSource.priority ?? 50,
        enable: basicSource.enable ?? true,
      });
    }
    return;
  }

  fillCreateProviderFields(source);
}

/**
 * 填充新建 provider 表单字段
 */
function fillCreateProviderFields(source) {
  if (source.api_key !== undefined && source.api_key !== null) {
    document.getElementById('cp-api_key').value = source.api_key;
  }
  if (source.base_url) document.getElementById('cp-base_url').value = source.base_url;
  if (source.model) document.getElementById('cp-model').value = source.model;
  if (source.small_model) document.getElementById('cp-small_model').value = source.small_model;
  if (source.icon) {
    document.getElementById('cp-icon').value = source.icon;
    document.getElementById('cp-icon-preview').textContent = source.icon;
  }
  if (source.priority !== undefined && source.priority !== null) {
    document.getElementById('cp-priority').value = source.priority;
  }
  if (source.enable !== undefined && source.enable !== null) {
    document.getElementById('cp-enable').checked = source.enable;
  }
  if (source.thinking_type) {
    document.getElementById('cp-thinking_type').value = source.thinking_type;
  }
  if (source.thinking_mode) {
    document.getElementById('cp-thinking_mode').value = source.thinking_mode;
  }
  if (source.max_tokens !== undefined && source.max_tokens !== null) {
    document.getElementById('cp-max_tokens').value = source.max_tokens;
  }
  if (source.compact_threshold !== undefined && source.compact_threshold !== null) {
    document.getElementById('cp-compact_threshold').value = source.compact_threshold;
  }
  if (source.price_input_hit !== undefined && source.price_input_hit !== null) {
    document.getElementById('cp-price_input_hit').value = source.price_input_hit;
  }
  if (source.price_input_miss !== undefined && source.price_input_miss !== null) {
    document.getElementById('cp-price_input_miss').value = source.price_input_miss;
  }
  if (source.price_output !== undefined && source.price_output !== null) {
    document.getElementById('cp-price_output').value = source.price_output;
  }
  if (source.vision !== undefined && source.vision !== null) {
    // 布尔值兼容：true->yes, false->no
    let val = source.vision;
    if (val === true) val = 'yes';
    if (val === false) val = 'no';
    document.getElementById('cp-vision').value = val;
  }
}

/**
 * 保存新建 Provider
 */
async function saveNewProvider() {
  const statusEl = document.getElementById('create-provider-status');
  statusEl.style.display = 'none';
  statusEl.textContent = '';
  statusEl.className = '';

  const name = document.getElementById('cp-name').value.trim();
  if (!name) {
    statusEl.style.display = 'block';
    statusEl.className = 'error';
    statusEl.textContent = 'Provider 名称不能为空';
    return;
  }

  // 编辑模式下不检查名称格式（名称不可更改）
  if (!_editingProviderName && !/^[a-zA-Z0-9_\-.]+$/.test(name)) {
    statusEl.style.display = 'block';
    statusEl.className = 'error';
    statusEl.textContent = 'Provider 名称只能包含字母、数字、下划线、中划线和点';
    return;
  }

  const clone = document.getElementById('cp-clone').value || null;

  const body = {
    model: document.getElementById('cp-model').value.trim() || null,
    icon: document.getElementById('cp-icon').value.trim() || null,
    priority: parseInt(document.getElementById('cp-priority').value) || 50,
    enable: document.getElementById('cp-enable').checked,
  };

  // 收集可选字段
  const optionalFields = {
    api_key: document.getElementById('cp-api_key').value.trim(),
    base_url: document.getElementById('cp-base_url').value.trim(),
    small_model: document.getElementById('cp-small_model').value.trim(),
    thinking_type: document.getElementById('cp-thinking_type').value || null,
    thinking_mode: document.getElementById('cp-thinking_mode').value || null,
    max_tokens: document.getElementById('cp-max_tokens').value ? parseInt(document.getElementById('cp-max_tokens').value) : null,
    compact_threshold: document.getElementById('cp-compact_threshold').value ? parseInt(document.getElementById('cp-compact_threshold').value) : null,
    price_input_hit: document.getElementById('cp-price_input_hit').value ? parseFloat(document.getElementById('cp-price_input_hit').value) : null,
    price_input_miss: document.getElementById('cp-price_input_miss').value ? parseFloat(document.getElementById('cp-price_input_miss').value) : null,
    price_output: document.getElementById('cp-price_output').value ? parseFloat(document.getElementById('cp-price_output').value) : null,
    vision: document.getElementById('cp-vision').value !== 'no' ? document.getElementById('cp-vision').value : null,
  };
  for (const [key, val] of Object.entries(optionalFields)) {
    if (val !== '' && val !== null && val !== undefined) {
      body[key] = val;
    }
  }

  const isEdit = !!_editingProviderName;

  try {
    let url, method;
    if (isEdit) {
      // 编辑模式：PUT 更新
      url = `/api/llm/providers/${encodeURIComponent(_editingProviderName)}`;
      method = 'PUT';
      body.clone = clone;
    } else {
      // 新建模式：POST 创建
      url = '/api/llm/providers';
      method = 'POST';
      body.name = name;
      body.clone = clone;
    }

    const response = await fetch(url, {
      method: method,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });

    const data = await response.json();

    if (!response.ok) {
      throw new Error(data.detail || (isEdit ? '更新失败' : '创建失败'));
    }

    const providerName = isEdit ? _editingProviderName : name;
    showSimpleToast(`✅ ${isEdit ? '更新' : '创建'}成功: ${providerName}`, 'success');

    // 刷新 providers 列表
    await loadProviders();

    // 重置编辑状态
    _editingProviderName = null;

    // 重置表单，下次打开时是全新的
    resetCreateProviderForm();
    _createProviderFormInitialized = false;

    // 关闭模态框
    closeCreateProviderModal();

  } catch (error) {
    statusEl.style.display = 'block';
    statusEl.className = 'error';
    statusEl.textContent = (isEdit ? '更新失败: ' : '创建失败: ') + error.message;
  }
}


// ==================== 编辑 / 删除 Provider ====================

/** 当前正在编辑的 provider 名称（null 表示新建模式） */
let _editingProviderName = null;

/**
 * 打开编辑 Provider 模态框
 * 复用新建模态框，预填充现有配置
 */
async function editProvider(providerName) {
  _editingProviderName = providerName;

  // 获取 provider 详情
  try {
    const response = await fetch(`/api/llm/providers?detail=true`);
    if (!response.ok) throw new Error('获取provider详情失败');
    const data = await response.json();
    const provider = (data.providers || []).find(p => p.name === providerName);
    if (!provider) throw new Error(`Provider '${providerName}' 不存在`);

    // 重置并填充表单
    resetCreateProviderForm();
    _createProviderFormInitialized = true;

    // 名称只读
    const nameInput = document.getElementById('cp-name');
    nameInput.value = provider.name;
    nameInput.readOnly = true;
    nameInput.style.opacity = '0.6';

    // 填充 clone 下拉列表（排除自身，不可继承自己）
    const cloneSelect = document.getElementById('cp-clone');
    const providers = state.providers || [];
    cloneSelect.innerHTML = '<option value="">-- 不继承 --</option>' +
      providers.filter(p => p.name !== providerName).map(p => `<option value="${p.name}">${p.icon || '🤖'} ${p.name} (${p.model || ''})</option>`).join('');
    // 设置当前 clone 值
    if (provider.clone) {
      cloneSelect.value = provider.clone;
    } else {
      cloneSelect.value = '';
    }

    // 填充字段
    fillCreateProviderFields(provider);

    // 修改标题和按钮
    const modalTitle = document.querySelector('#create-provider-modal h2');
    if (modalTitle) modalTitle.textContent = ` ✏️ 编辑接口 - ${provider.name}`;
    const saveBtn = document.getElementById('create-provider-save-btn');
    if (saveBtn) saveBtn.textContent = '更新';

    // 显示模态框
    const modal = document.getElementById('create-provider-modal');
    if (modal) modal.style.display = 'flex';
  } catch (error) {
    console.error('❌ 打开编辑模态框失败:', error);
    showSimpleToast(`❌ ${error.message}`, 'error');
  }
}

/**
 * 删除 Provider（带确认）
 */
async function deleteProvider(providerName) {
  // 检查是否为当前正在使用的 provider
  if (providerName === state.currentProvider) {
    showSimpleToast(`❌ '${providerName}' 当前正在使用，无法删除`, 'error');
    return;
  }

  if (!confirm(`确定要删除接口 '${providerName}' 吗？此操作不可撤销。`)) return;

  try {
    const response = await fetch(`/api/llm/providers/${encodeURIComponent(providerName)}`, {
      method: 'DELETE',
    });

    if (!response.ok) {
      const error = await response.json();
      throw new Error(error.detail || '删除失败');
    }

    showSimpleToast(`✅ '${providerName}' 已删除`, 'success');
    await loadProviders();
    renderProvidersList();
  } catch (error) {
    console.error('❌ 删除provider失败:', error);
    showSimpleToast(`❌ 删除失败: ${error.message}`, 'error');
  }
}


// ==================== 拖拽功能 ====================
