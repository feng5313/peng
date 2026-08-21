// ==================== 文件上传 ====================

function initFileUpload() {
  // 创建隐藏的文件输入框
  const fileInput = document.createElement('input');
  fileInput.type = 'file';
  fileInput.id = 'file-input';
  fileInput.multiple = true;
  fileInput.style.display = 'none';
  document.body.appendChild(fileInput);

  // 绑定附件按钮点击事件
  const attachBtn = document.getElementById('attach-btn');
  if (attachBtn) {
    attachBtn.addEventListener('click', () => fileInput.click());
  }

  // 文件选择事件
  fileInput.addEventListener('change', handleFileSelect);

  // 拖放支持
  const inputContainer = document.querySelector('.input-container');
  if (inputContainer) {
    inputContainer.addEventListener('dragover', (e) => {
      e.preventDefault();
      inputContainer.classList.add('dragover');
    });

    inputContainer.addEventListener('dragleave', () => {
      inputContainer.classList.remove('dragover');
    });

    inputContainer.addEventListener('drop', (e) => {
      e.preventDefault();
      inputContainer.classList.remove('dragover');

      const files = Array.from(e.dataTransfer.files);
      addFiles(files);
    });
  }
}

function handleFileSelect(e) {
  const files = Array.from(e.target.files);
  addFiles(files);
  e.target.value = ''; // 重置以允许选择相同文件
}

function addFiles(files) {
  const maxSize = 10 * 1024 * 1024; // 10MB
  const allowedTypes = [
    'text/plain', 'text/markdown', 'text/csv',
    'application/json', 'application/xml',
    'image/png', 'image/jpeg', 'image/gif', 'image/webp',
    'application/pdf',
    'application/zip', 'application/x-tar', 'application/gzip',
  ];

  for (const file of files) {
    // 检查文件大小
    if (file.size > maxSize) {
      showSimpleToast(`⚠️  文件 ${file.name} 超过 10MB 限制`, 'warning');
      continue;
    }

    // 检查文件类型（可选，根据需要启用）
    // if (!allowedTypes.includes(file.type)) {
    //     showSimpleToast(`不支持的文件类型: ${file.type}`, 'warning');
    //     continue;
    // }

    state.attachedFiles.push(file);
  }

  renderFileList();
  showSimpleToast(`✅  已添加 ${files.length} 个文件`, 'info');
}

function renderFileList() {
  let fileList = document.getElementById('file-list');

  if (state.attachedFiles.length === 0) {
    if (fileList) fileList.remove();
    return;
  }

  if (!fileList) {
    fileList = document.createElement('div');
    fileList.id = 'file-list';
    fileList.className = 'file-list';

    const inputRow = document.querySelector('.input-row');
    if (inputRow) {
      inputRow.parentElement.insertBefore(fileList, inputRow);
    }
  }

  fileList.innerHTML = state.attachedFiles.map((file, index) => `
        <div class="file-item">
            <span class="file-name" title="${escapeHtml(file.name)}">${escapeHtml(file.name)}</span>
            <span class="file-size">${formatFileSize(file.size)}</span>
            <span class="file-remove" onclick="removeFile(${index})">✕</span>
        </div>
    `).join('');
}

function removeFile(index) {
  state.attachedFiles.splice(index, 1);
  renderFileList();
}

function clearAttachedFiles() {
  state.attachedFiles = [];
  renderFileList();
}

function formatFileSize(bytes) {
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
  return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
}

async function uploadFiles() {
  const uploadedFiles = [];

  for (const file of state.attachedFiles) {
    try {
      const formData = new FormData();
      formData.append('file', file);

      const response = await fetch('/api/upload', {
        method: 'POST',
        body: formData,
      });

      if (!response.ok) {
        console.error(`文件 ${file.name} 上传失败`);
        continue;
      }

      const result = await response.json();
      uploadedFiles.push({
        name: file.name,
        path: result.path,
        size: file.size,
      });

    } catch (error) {
      console.error(`上传文件 ${file.name} 时出错:`, error);
    }
  }

  return uploadedFiles;
}

