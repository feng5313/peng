// ==================== Coding Plan 用量徽章 ====================
// 底部状态栏显示智谱 Coding Plan 的周额度使用率
// 每分钟刷新，仅在 GLM provider enable 时显示
// 容错：单次失败保留上次数据；连续多次失败（约5分钟）才隐藏

let _cpPollTimer = null;
let _cpHasGlm = false;  // 是否有 enabled 的 GLM provider
let _cpFailCount = 0;   // 连续失败计数
const _CP_MAX_FAILS = 5; // 连续失败5次（约5分钟）才隐藏徽章

async function _cpFetchUsage() {
  try {
    const resp = await fetch('/api/llm/coding-plan-usage');
    if (!resp.ok) return null;
    const data = await resp.json();
    return data.success ? data : null;
  } catch {
    return null;
  }
}

async function _cpCheckGlmEnabled() {
  // 检查是否有 enabled 的 GLM 智谱 provider
  try {
    const resp = await fetch('/api/llm/providers');
    if (!resp.ok) return false;
    const data = await resp.json();
    const providers = data.providers || [];
    return providers.some(
      (p) => p.enable && (
        p.name?.startsWith('z-') ||  // z-glm5.2 etc
        p.name?.toLowerCase().includes('glm') ||
        p.name?.toLowerCase().includes('zhipu')
      )
    );
  } catch {
    return false;
  }
}

function _cpColorForPct(pct) {
  if (pct >= 90) return '#f44336';
  if (pct >= 70) return '#ff9800';
  return '#4caf50';
}

function _cpRender(data) {
  const el = document.getElementById('coding-plan-usage');
  if (!el) return;

  const wp = data.weekly_percentage;
  if (wp == null) {
    el.style.display = 'none';
    return;
  }

  const level = (data.level || '').toUpperCase();
  const color = _cpColorForPct(wp);
  const resetStr = data.weekly_reset ? ` ♻️${data.weekly_reset}` : '';
  // 后端 stale-if-error 降级数据：加 ⚠️ 提示数据更新延迟
  const staleMark = data.stale ? ' ⚠️' : '';
  const staleTitle = data.stale ? ' title="数据更新延迟（上游暂时不可用），显示最近一次成功数据"' : '';

  const icon = '<img src="/static/img/bigmodel.png" alt="bigmodel" ' +
    'style="width:14px;height:14px;vertical-align:middle;margin-right:4px">';

  el.innerHTML =
    `${icon}<span>GLM套餐用量</span> ` +
    (level ? `<span style="color:#42a5f5;font-weight:600">${level}</span>` : '') +
    `<b style="color:${color}">${wp}%</b>` +
    `<span class="cp-bar"><span class="cp-bar-fill" style="width:${Math.min(wp, 100)}%;background:${color}"></span></span>` +
    `<span style="color:${color}"${staleTitle}>${staleMark}${resetStr}</span>`;
  el.style.display = 'inline-flex';
}

function _cpHide() {
  const el = document.getElementById('coding-plan-usage');
  if (el) el.style.display = 'none';
}

async function _cpTick() {
  if (!_cpHasGlm) {
    _cpHide();
    return;
  }
  const data = await _cpFetchUsage();
  if (data) {
    _cpFailCount = 0;
    _cpRender(data);
  } else {
    // 失败容忍：保留上次显示，连续多次失败才隐藏
    _cpFailCount++;
    if (_cpFailCount >= _CP_MAX_FAILS) _cpHide();
  }
}

function initCodingPlanBadge() {
  // 初始隐藏
  _cpHide();

  // 首次检查 GLM provider 状态
  _cpCheckGlmEnabled().then((has) => {
    _cpHasGlm = has;
    if (has) _cpTick();
  });

  // 每60秒刷新
  _cpPollTimer = setInterval(async () => {
    // 重新检查 provider 状态（用户可能切换了 provider）
    _cpHasGlm = await _cpCheckGlmEnabled();
    await _cpTick();
  }, 60000);

  // 监听 provider 变更事件（providers 面板操作后触发）
  document.addEventListener('providers-changed', async () => {
    _cpHasGlm = await _cpCheckGlmEnabled();
    await _cpTick();
  });
}

document.addEventListener('DOMContentLoaded', initCodingPlanBadge);
