// ==================== 新建文件和另存为 ====================

// 创建新文件
async function createNewFile() {
  try {
    // 🔧 Task#255：统一走group路径，无group时先确保group结构存在
    if (!state.editorGroups || state.editorGroups.length === 0) {
      await loadMonacoEditor();
      ensureGroupStructure();
    }
    if (state.editorGroups && state.editorGroups.length > 0) {
      const activeGroupId = getActiveEditorGroupId();
      await createNewFileInGroup(activeGroupId);
      return;
    }

    await loadMonacoEditor();

    const timestamp = Date.now();
    const tabId = 'new-file-' + timestamp;

    // 从所有标签页（包括分拆列）中查找最大的"新文档"ID，防止重名
    let maxId = 0;
    const newDocRegex = /^新文档(\d+)$/;
    const allTabs = getAllEditorTabs();
    allTabs.forEach(tab => {
      if (tab.name) {
        const match = tab.name.match(newDocRegex);
        if (match) {
          const id = parseInt(match[1], 10);
          if (id > maxId) {
            maxId = id;
          }
        }
      }
    });

    // 生成新的ID（从1开始累加，位数保持最短）
    const newId = maxId + 1;
    const title = '新文档' + newId;

    const newTab = {
      id: tabId,
      path: 'data/' + title + '.md',  // 新文档默认data目录，无扩展名默认markdown
      name: title,
      language: 'markdown',  // 无扩展名默认markdown
      content: '',  // 🔧 修复：确保新文档内容为空字符串
      cursorPosition: null,
      scrollTop: null,
      isNewFile: true,
      isVirtual: false,
      nest: getSelectedNestName() || null,  // 🔧 Task#72：新文件也记住nest来源
    };

    // 🔧 Task#340：添加到group0（唯一数据源）
    ensureGroup0();
    state.editorGroups[0].tabs.push(newTab);
    setActiveEditorTabId(tabId);  // 🔧 Task#334

    // 保存标签页状态
    saveEditorTabs();

    updateEditorContent(newTab);

    document.getElementById('editor-file-path').textContent = 'data/' + title + '.md';
    if (typeof setLanguageBadge === 'function') { setLanguageBadge('markdown'); } else { document.getElementById('editor-language').textContent = 'Markdown'; }

    const editorView = document.getElementById('editor-view');
    if (editorView) {
      editorView.dataset.currentFilePath = 'data/' + title + '.md';
      editorView.dataset.isVirtual = 'false';
      editorView.dataset.isNewFile = 'true';
    }

    renderTabs();

    // 🔧 修复：验证编辑器内容是否为空
    // 🔧 Task#311：使用正确的编辑器实例验证（与updateEditorContent一致的逻辑）
    const validateGroupId = getActiveEditorGroupId();
    const validateGroup = (validateGroupId != null && state.editorGroups) ? findGroupById(validateGroupId) : null;
    const validateEditor = (validateGroup && validateGroup.monacoEditor) ? validateGroup.monacoEditor : monacoEditor;
    if (validateEditor) {
      const finalContent = validateEditor.getValue();
      if (finalContent.length > 0) {
        console.warn('[createNewFile] ⚠️ 警告：新文档内容不为空！内容预览:', finalContent.substring(0, 200));
        // 强制清空
        validateEditor.setValue('');
      }
    }
  } catch (error) {
    console.error('创建新文件失败:', error);
    showSimpleToast('创建新文件失败: ' + error.message, 'error');
  }
}

// 另存为文件
async function saveAsFile() {

  if (!monacoEditor) {
    showSimpleToast('编辑器不存在', 'warning');
    return;
  }

  // 🔧 修复：移除 isContentEditor 的限制，任何内容都应该允许另存为其他文件
  // 用户可能想把消息/事务内容保存到一个新文件中

  // 设置"另存为"模式标志
  state.isSaveAsMode = true;

  // 打开文件浏览器（从编辑器打开）
  if (typeof window.openFileBrowserFromEditor === 'function') {
    await window.openFileBrowserFromEditor();
  } else {
    showSimpleToast('文件浏览器功能不可用', 'error');
    state.isSaveAsMode = false;
  }
}

// 执行文件保存操作（另存为）
async function performSaveAs(filePath) {
  if (!monacoEditor) {
    showSimpleToast('编辑器不存在', 'warning');
    return false;
  }

  let content = monacoEditor.getValue();

  const saveBtn = document.getElementById('editor-save');
  if (saveBtn) {
    saveBtn.disabled = true;
    setSaveBtnContent(saveBtn, 'saving');
  }

  try {

    // 🔧 Task#72：使用当前标签记住的nest而非全局nest-select
    const currentTabForSave = findTabById(getActiveEditorTabId());  // 🔧 Task#334
    const nestParam = currentTabForSave && currentTabForSave.nest ? `&nest=${encodeURIComponent(currentTabForSave.nest)}` : getNestParam();
    const response = await fetch(`/api/files/write${nestParam ? '?' + nestParam.slice(1) : ''}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: filePath, content: content })
    });

    if (!response.ok) {
      const errorData = await response.json().catch(() => ({}));
      throw new Error(errorData.detail || '保存失败');
    }

    const result = await response.json();

    const currentTab = findTabById(getActiveEditorTabId());  // 🔧 Task#334
    if (currentTab) {
      currentTab.path = filePath;
      currentTab.name = filePath.split('/').pop();
      currentTab.isNewFile = false;
      currentTab.isVirtual = false;
      // 🔧 Task#72：另存为成功后，更新tab的nest来源（继承当前标签的nest）
      if (!currentTab.nest && currentTabForSave && currentTabForSave.nest) {
        currentTab.nest = currentTabForSave.nest;
      }

      const extension = filePath.split('.').pop().toLowerCase();
      currentTab.language = getMonacoLanguage(extension);

      const editorView = document.getElementById('editor-view');
      if (editorView) {
        editorView.dataset.currentFilePath = filePath;
        editorView.dataset.isVirtual = 'false';
        editorView.dataset.isNewFile = 'false';
      }
    }

    document.getElementById('editor-file-path').textContent = filePath;
    if (typeof setLanguageBadge === 'function') { setLanguageBadge(currentTab ? currentTab.language : 'Unknown'); } else { document.getElementById('editor-language').textContent = currentTab ? currentTab.language : 'Unknown'; }
    renderTabs();
    showSimpleToast('文件已保存: ' + filePath, 'success');
    showInOutput('文件已保存: ' + filePath, 'success');
    // 🔧 Task#248：保存后使用和"最近的文件"一致的格式显示路径
    if (currentTab && typeof _setEditorFilePathHTML === 'function') {
      _setEditorFilePathHTML(filePath, currentTab.nest);
    }
    return true;

  } catch (error) {
    console.error('保存失败:', error);
    showSimpleToast('保存失败: ' + error.message, 'error');
    showInOutput('保存失败: ' + error.message, 'error');
    return false;
  } finally {
    if (saveBtn) {
      saveBtn.disabled = false;
      setSaveBtnContent(saveBtn, 'save');
    }
  }
}

// 根据文件扩展名获取Monaco语言
function getMonacoLanguage(extension) {
  const languageMap = {
    'js': 'javascript', 'mjs': 'javascript', 'cjs': 'javascript',
    'ts': 'typescript',
    'py': 'python',
    'kn': 'python',
    'java': 'java',
    'c': 'c',
    'cpp': 'cpp',
    'h': 'c',
    'hpp': 'cpp',
    'cs': 'csharp',
    'php': 'php',
    'rb': 'ruby',
    'go': 'go',
    'rs': 'rust',
    'zig': 'zig',
    'lua': 'lua',
    'swift': 'swift',
    'kt': 'kotlin',
    'scala': 'scala',
    'html': 'html', 'htm': 'html',
    'xml': 'xml',
    'css': 'css',
    'scss': 'scss',
    'less': 'less',
    'json': 'json',
    'yaml': 'yaml', 'yml': 'yaml',
    'toml': 'toml',
    'ini': 'ini',
    'md': 'markdown', 'markdown': 'markdown',
    'txt': 'plaintext',
    'sql': 'sql',
    'sh': 'shell', 'bash': 'shell', 'zsh': 'shell', 'fish': 'shell',
    'ps1': 'powershell',
    'dockerfile': 'dockerfile',
    'makefile': 'makefile',
    'cmake': 'cmake',
    'gradle': 'gradle',
    'vue': 'vue',
    'svelte': 'svelte',
    'tsx': 'typescript',
    'jsx': 'javascript'
  };
  return languageMap[extension] || 'markdown';  // 🔧 Task#866：未匹配扩展名默认markdown高亮
}


