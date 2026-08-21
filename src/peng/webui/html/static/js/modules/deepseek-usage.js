// ==================== DeepSeek 用量徽章 ====================
// 底部状态栏显示 DeepSeek provider 的余额、今日费用、累计费用、高峰状态
// 每分钟刷新，仅在存在 DeepSeek provider 时显示

let _dsPollTimer = null;
let _dsHasDeepseek = false;

async function _dsFetchUsage() {
  try {
    const resp = await fetch('/api/llm/deepseek-usage');
    if (!resp.ok) return null;
    const data = await resp.json();
    return data.success ? data : null;
  } catch {
    return null;
  }
}

async function _dsCheckProvider() {
  try {
    const resp = await fetch('/api/llm/providers');
    if (!resp.ok) return false;
    const data = await resp.json();
    const providers = data.providers || [];
    // 只要存在 deepseek provider 就显示（不要求 enable）
    return providers.some((p) => p.name?.toLowerCase().includes('deepseek'));
  } catch {
    return false;
  }
}

function _dsFmt(n) {
  if (n == null) return '--';
  return n.toFixed(2);
}

function _dsRender(data) {
  const el = document.getElementById('deepseek-usage');
  if (!el) return;

  const parts = [];

  // 高峰时段标记
  if (data.is_peak) {
    parts.push('<span style="color:#f44336;font-weight:600" title="高峰时段">🐋高峰</span>');
  } else {
    parts.push('<span style="color:#4caf50;font-weight:600" title="非高峰时段">🐋低谷</span>');
  }

  // 余额
  if (data.balance_cny != null) {
    const balColor = data.balance_cny < 5 ? '#f44336' : (data.balance_cny < 20 ? '#ff9800' : '#4caf50');
    parts.push(`<span style="color:var(--text-secondary)">余额</span><b style="color:${balColor}" title="DeepSeek 余额">¥${_dsFmt(data.balance_cny)}</b>`);
  }

  // 今日费用
  if (data.today_cost > 0) {
    parts.push(`<span style="color:var(--text-secondary)">今日</span><b>¥${_dsFmt(data.today_cost)}</b>`);
  }

  // 累计费用（可点击编辑）
  if (data.total_cost > 0) {
    parts.push(`<span style="color:var(--text-secondary)">累计</span><b id="ds-total-cost" style="color:var(--accent-color);cursor:pointer;text-decoration:underline dotted" title="点击更正" onclick="_dsEditTotalCost(${data.total_cost})">¥${_dsFmt(data.total_cost)}</b>`);
  }

  el.innerHTML = parts.join(' ');
  el.style.display = 'inline-flex';
}

function _dsHide() {
  const el = document.getElementById('deepseek-usage');
  if (el) el.style.display = 'none';
}

function _dsEditTotalCost(currentVal) {
  const el = document.getElementById('ds-total-cost');
  if (!el) return;

  // 创建内联编辑器替换显示
  const wrapper = document.createElement('span');
  wrapper.style.display = 'inline-flex';
  wrapper.style.alignItems = 'center';
  wrapper.style.gap = '2px';
  wrapper.innerHTML = '¥';

  const input = document.createElement('input');
  input.type = 'number';
  input.step = '0.01';
  input.min = '0';
  input.value = currentVal.toFixed(2);
  input.style.width = '70px';
  input.style.fontSize = 'inherit';
  input.style.fontFamily = 'inherit';
  input.style.border = '1px solid var(--accent-color)';
  input.style.borderRadius = '3px';
  input.style.padding = '0 2px';
  input.style.background = 'var(--bg-input)';
  input.style.color = 'var(--text-primary)';

  const saveBtn = document.createElement('span');
  saveBtn.textContent = '✅';
  saveBtn.style.cursor = 'pointer';
  saveBtn.title = '保存';

  const cancelBtn = document.createElement('span');
  cancelBtn.textContent = '❌';
  cancelBtn.style.cursor = 'pointer';
  cancelBtn.title = '取消';

  wrapper.appendChild(input);
  wrapper.appendChild(saveBtn);
  wrapper.appendChild(cancelBtn);

  el.replaceWith(wrapper);
  input.focus();
  input.select();

  function restore() {
    wrapper.replaceWith(el);
  }

  function doSave() {
    const val = parseFloat(input.value);
    if (isNaN(val) || val < 0) {
      showSimpleToast('请输入有效的非负数字', 'error');
      restore();
      return;
    }
    fetch('/api/llm/deepseek-usage/correct', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ total_cost: val }),
    })
      .then(r => r.json())
      .then(data => {
        if (data.success) {
          showSimpleToast(`✅ 累计消费已修正为 ¥${val.toFixed(2)}`, 'success');
          _dsTick();
        } else {
          showSimpleToast(`❌ 修正失败: ${data.detail || ''}`, 'error');
          restore();
        }
      })
      .catch(e => {
        showSimpleToast(`❌ 修正失败: ${e}`, 'error');
        restore();
      });
  }

  saveBtn.onclick = doSave;
  cancelBtn.onclick = restore;
  input.onkeydown = (e) => {
    if (e.key === 'Enter') doSave();
    if (e.key === 'Escape') restore();
  };
}

async function _dsTick() {
  if (!_dsHasDeepseek) {
    _dsHide();
    return;
  }
  const data = await _dsFetchUsage();
  if (data) {
    _dsRender(data);
  } else {
    _dsHide();
  }
}

function initDeepSeekBadge() {
  _dsHide();

  _dsCheckProvider().then((has) => {
    _dsHasDeepseek = has;
    if (has) _dsTick();
  });

  _dsPollTimer = setInterval(async () => {
    _dsHasDeepseek = await _dsCheckProvider();
    await _dsTick();
  }, 60000);

  document.addEventListener('providers-changed', async () => {
    _dsHasDeepseek = await _dsCheckProvider();
    await _dsTick();
  });
}

document.addEventListener('DOMContentLoaded', initDeepSeekBadge);
