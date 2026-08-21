// ==================== 状态管理 ====================

function handleStatusUpdate(data) {
  const statusText = document.getElementById('status-text');
  const statusIndicator = document.getElementById('status-indicator');

  if (statusText) {
    statusText.textContent = data.status === 'running' ? '服务运行中' : data.status;
  }

  if (statusIndicator) {
    statusIndicator.className = `status-dot ${data.status}`;
  }
}

// ==================== 实时时钟 ====================

const _weekdays = ['星期日', '星期一', '星期二', '星期三', '星期四', '星期五', '星期六'];

function updateClock() {
  const el = document.getElementById('status-clock');
  if (!el) return;
  const d = new Date();
  const text = `${d.getFullYear()}年${d.getMonth() + 1}月${d.getDate()}日 ` +
    `${_weekdays[d.getDay()]} ` +
    `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}:${String(d.getSeconds()).padStart(2, '0')}`;
  el.textContent = text;
}

updateClock();
setInterval(updateClock, 1000);

