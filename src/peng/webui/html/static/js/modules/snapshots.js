// ==================== 快照管理 ====================

// 快照状态
let snapshots = [];
let selectedSnapshotId = null;

// 加载快照列表
async function loadSnapshots() {
  try {
    const response = await fetch('/api/snapshots');
    const data = await response.json();

    if (data.snapshots) {
      snapshots = data.snapshots;
      updateSnapshotSelect();
    }
  } catch (error) {
    console.error('❌ 加载快照列表失败:', error);
  }
}

// 更新快照选择器
function updateSnapshotSelect() {
  const select = document.getElementById('snapshot-select');
  if (!select) return;

  // 保存当前选中的值
  const currentValue = select.value;

  // 清空选项
  select.innerHTML = '<option value="">选择快照...</option>';

  // 添加快照选项
  snapshots.forEach(snapshot => {
    const option = document.createElement('option');
    option.value = snapshot.id;
    option.textContent = `${snapshot.name} (${snapshot.created_at})`;
    select.appendChild(option);
  });

  // 恢复选中状态
  if (currentValue && snapshots.some(s => s.id === parseInt(currentValue))) {
    select.value = currentValue;
  }

  // 更新按钮状态
  updateSnapshotButtons();
}

// 更新快照按钮状态
function updateSnapshotButtons() {
  const restoreBtn = document.getElementById('snapshot-restore-btn');
  const deleteBtn = document.getElementById('snapshot-delete-btn');
  const select = document.getElementById('snapshot-select');

  const hasSelection = select && select.value !== '';

  if (restoreBtn) restoreBtn.disabled = !hasSelection;
  if (deleteBtn) deleteBtn.disabled = !hasSelection;

  // 更新快照信息显示
  updateSnapshotInfo();
}

// 更新快照信息显示
function updateSnapshotInfo() {
  const infoDiv = document.getElementById('snapshot-info');
  const select = document.getElementById('snapshot-select');

  if (!infoDiv || !select) return;

  const snapshotId = select.value;
  if (!snapshotId) {
    infoDiv.style.display = 'none';
    return;
  }

  const snapshot = snapshots.find(s => s.id === parseInt(snapshotId));
  if (!snapshot) {
    infoDiv.style.display = 'none';
    return;
  }

  // 格式化大小
  const sizeMB = (snapshot.size_bytes / (1024 * 1024)).toFixed(2);

  infoDiv.innerHTML = `
    <div class="snapshot-info-item">
      <span class="label">描述:</span>
      <span class="value">${snapshot.description || '无'}</span>
    </div>
    <div class="snapshot-info-item">
      <span class="label">WAL LSN:</span>
      <span class="value">${snapshot.wal_lsn}</span>
    </div>
    <div class="snapshot-info-item">
      <span class="label">大小:</span>
      <span class="value">${sizeMB} MB</span>
    </div>
  `;
  infoDiv.style.display = 'flex';
}

// 创建快照
async function createSnapshot() {
  const name = prompt('请输入快照名称:', `snapshot_${new Date().toISOString().slice(0, 10)}`);
  if (!name) return;

  const description = prompt('请输入快照描述（可选）:', '') || '';

  try {
    const response = await fetch('/api/snapshots', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, description })
    });

    const data = await response.json();

    if (data.success) {
      await loadSnapshots(); // 重新加载快照列表
    } else {
      alert('创建快照失败: ' + (data.message || '未知错误'));
    }
  } catch (error) {
    console.error('❌ 创建快照失败:', error);
    alert('创建快照失败: ' + error.message);
  }
}

// 恢复快照
async function restoreSnapshot() {
  const select = document.getElementById('snapshot-select');
  if (!select || !select.value) {
    alert('请先选择一个快照');
    return;
  }

  const snapshotId = parseInt(select.value);
  const snapshot = snapshots.find(s => s.id === snapshotId);

  if (!confirm(`确定要恢复到快照 "${snapshot.name}" 吗？\n\n恢复过程中数据库将不可用，请确保当前没有正在运行的事务。`)) {
    return;
  }

  try {
    const response = await fetch(`/api/snapshots/${snapshotId}/restore`, {
      method: 'POST'
    });

    const data = await response.json();

    if (data.success) {
      // 恢复成功后刷新页面
      setTimeout(() => {
        location.reload();
      }, 2000);
    } else {
      alert('恢复快照失败: ' + (data.message || '未知错误'));
    }
  } catch (error) {
    console.error('❌ 恢复快照失败:', error);
    alert('恢复快照失败: ' + error.message);
  }
}

// 删除快照
async function deleteSnapshot() {
  const select = document.getElementById('snapshot-select');
  if (!select || !select.value) {
    alert('请先选择一个快照');
    return;
  }

  const snapshotId = parseInt(select.value);
  const snapshot = snapshots.find(s => s.id === snapshotId);

  if (!confirm(`确定要删除快照 "${snapshot.name}" 吗？\n\n此操作不可撤销！`)) {
    return;
  }

  try {
    const response = await fetch(`/api/snapshots/${snapshotId}`, {
      method: 'DELETE'
    });

    const data = await response.json();

    if (data.success) {
      await loadSnapshots(); // 重新加载快照列表
    } else {
      alert('删除快照失败: ' + (data.message || '未知错误'));
    }
  } catch (error) {
    console.error('❌ 删除快照失败:', error);
    alert('删除快照失败: ' + error.message);
  }
}

// 初始化快照管理
function initSnapshotManager() {
  // 绑定按钮事件
  const createBtn = document.getElementById('snapshot-create-btn');
  const restoreBtn = document.getElementById('snapshot-restore-btn');
  const deleteBtn = document.getElementById('snapshot-delete-btn');
  const refreshBtn = document.getElementById('snapshot-refresh-btn');
  const select = document.getElementById('snapshot-select');

  if (createBtn) {
    createBtn.addEventListener('click', createSnapshot);
  }

  if (restoreBtn) {
    restoreBtn.addEventListener('click', restoreSnapshot);
  }

  if (deleteBtn) {
    deleteBtn.addEventListener('click', deleteSnapshot);
  }

  if (refreshBtn) {
    refreshBtn.addEventListener('click', loadSnapshots);
  }

  if (select) {
    select.addEventListener('change', updateSnapshotButtons);
  }

  // 加载快照列表
  loadSnapshots();
}

// 页面加载完成后初始化快照管理
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', initSnapshotManager);
} else {
  initSnapshotManager();
}

