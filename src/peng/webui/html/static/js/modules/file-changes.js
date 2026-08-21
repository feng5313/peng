// ==================== 文件变化对话框 ====================

/**
 * 规范化文件路径（统一为绝对路径，去除尾部斜杠，统一分隔符）
 */
function normalizePath(path) {
  if (!path) return '';
  try {
    // 统一分隔符为正斜杠
    let p = path.replace(/\\/g, '/').replace(/\/+$/, '');
    // Windows 路径如 D:/work/... 不需要前缀 /，只有相对路径需要
    if (!p.startsWith('/') && !/^[A-Za-z]:/.test(p)) {
      p = '/' + p;
    }
    return p;
  } catch (e) {
    return path;
  }
}

/**
 * 处理文件变化消息
 */
function handleFileChanged(data) {
  try {
    const filePath = data.file_path || data.path;
    if (!filePath) {
      console.warn('[handleFileChanged] 缺少文件路径');
      return;
    }

    // 检查是否有活跃的编辑器
    if (!monacoEditor) {
      console.log('[handleFileChanged] 编辑器不存在，跳过');
      return;
    }

    // 获取当前标签页
    const currentTab = findTabById(getActiveEditorTabId());  // 🔧 Task#334
    if (!currentTab) {
      console.log('[handleFileChanged] 当前没有活跃的标签页');
      return;
    }

    // 检查是否是当前打开的文件
    // 🔧 Task#72：如果后端广播中包含 nest 字段且非null，需匹配当前 tab 的 nest
    // 注意：后端广播本地文件时nest=null，此时不应阻止匹配
    if (data.nest && currentTab.nest !== data.nest) {
      console.log(`[handleFileChanged] nest 不匹配: tab.nest=${currentTab.nest}, broadcast.nest=${data.nest}`);
      return;
    }
    const normalizedCurrentPath = normalizePath(currentTab.path);
    const normalizedIncomingPath = normalizePath(filePath);
    if (normalizedCurrentPath !== normalizedIncomingPath) {
      console.log(`[handleFileChanged] 文件不匹配: ${normalizedCurrentPath} ≠ ${normalizedIncomingPath}`);
      return;
    }

    // 判断当前tab是否已编辑
    const currentContent = monacoEditor.getValue();
    const isEdited = currentContent !== currentTab.original;

    if (!isEdited) {
      // 未编辑，直接重载
      monacoEditor.setValue(data.new_content);
      currentTab.content = data.new_content;
      currentTab.original = data.new_content;
      clearEditTrackerByPath(currentTab.path);  // 🔧 Task#421
      console.log(`[handleFileChanged] 文件已重载: ${filePath}`);
    } else {
      // 已编辑，弹出对话框
      showFileChangeDialog(filePath, data.old_content, data.new_content, currentTab);
    }
  } catch (error) {
    console.error('[handleFileChanged] 处理文件变化失败:', error);
  }
}

/**
 * 生成差异预览 HTML
 */
function generateDiffHtml(oldContent, newContent) {
  const oldLines = oldContent.split(/\r?\n/);
  const newLines = newContent.split(/\r?\n/);

  let html = '<div class="file-change-diff-content">';
  const maxLines = Math.max(oldLines.length, newLines.length);

  for (let i = 0; i < maxLines && i < 200; i++) {
    const oldLine = oldLines[i] || '';
    const newLine = newLines[i] || '';

    if (oldLine === newLine) {
      // 未变化
      html += `<div class="file-change-diff-line unchanged"><span class="file-change-diff-line-num">${i + 1}</span> ${escapeHtml(oldLine)}</div>`;
    } else if (oldLine && !newLine) {
      // 已删除
      html += `<div class="file-change-diff-line removed"><span class="file-change-diff-line-num">-${i + 1}</span> ${escapeHtml(oldLine)}</div>`;
    } else if (!oldLine && newLine) {
      // 已添加
      html += `<div class="file-change-diff-line added"><span class="file-change-diff-line-num">+${i + 1}</span> ${escapeHtml(newLine)}</div>`;
    } else {
      // 已修改
      html += `<div class="file-change-diff-line changed"><span class="file-change-diff-line-num">~${i + 1}</span> ${escapeHtml(newLine)}</div>`;
    }
  }

  if (maxLines >= 200) {
    html += '<div class="file-change-diff-line truncated">... 更多行数已截断 ...</div>';
  }

  html += '</div>';
  return html;
}

/**
 * 显示文件变化对话框
 */
// 文件变化对话框存储的内容（用于合并操作）
let _fileChangeDialogData = null;

function showFileChangeDialog(filePath, oldContent, newContent, tab) {
  // 存储对话框数据供合并操作使用
  _fileChangeDialogData = { filePath, oldContent, newContent };

  // 移除已存在的对话框
  const existingDialog = document.getElementById('file-change-dialog');
  if (existingDialog) {
    existingDialog.remove();
  }

  // 生成差异预览
  const diffHtml = generateDiffHtml(oldContent, newContent);

  // 创建对话框
  const dialog = document.createElement('div');
  dialog.id = 'file-change-dialog';
  dialog.className = 'file-change-dialog';
  dialog.innerHTML = `
    <div class="file-change-dialog-content">
      <div class="file-change-dialog-header">
        <h3>📄 ${escapeHtml(filePath)}</h3>
        <button class="file-change-dialog-close" onclick="closeFileChangeDialog()">✕</button>
      </div>
      <div class="file-change-dialog-body">
        <p>文件在外部被修改了，是否更新？</p>
        <div class="file-change-diff-preview">${diffHtml}</div>
      </div>
      <div class="file-change-dialog-footer">
        <button class="btn-file-change-load" onclick="handleFileChangeReload()">
          🔄 重载（放弃前端编辑）
        </button>
        <button class="btn-file-change-merge" onclick="handleFileChangeMerge()">
          🔀 自动合并（保留git格式的冲突）
        </button>
        <button class="btn-file-change-cancel" onclick="closeFileChangeDialog()">
          ❌ 取消
        </button>
      </div>
    </div>
  `;

  // 添加到 body
  document.body.appendChild(dialog);
  console.log('[handleFileChanged] 文件变化对话框已显示:', filePath);
}

/**
 * 关闭文件变化对话框
 */
function closeFileChangeDialog() {
  _fileChangeDialogData = null;  // 清除存储的数据
  const dialog = document.getElementById('file-change-dialog');
  if (dialog) {
    dialog.remove();
  }
}

/**
 * 处理重载按钮点击
 * 从 _fileChangeDialogData 获取文件路径，避免 Windows 路径中的反斜杠在 onclick 属性中被转义的问题
 */
async function handleFileChangeReload() {
  try {
    const filePath = _fileChangeDialogData ? _fileChangeDialogData.filePath : null;
    if (!filePath) {
      console.error('[handleFileChangeReload] 缺少文件路径');
      showSimpleToast('重载失败：缺少文件路径', 'error');
      closeFileChangeDialog();
      return;
    }
    // 重新从服务器获取最新内容
    // 🔧 Task#72：使用当前标签记住的nest而非全局nest-select
    const currentTab = findTabById(getActiveEditorTabId());  // 🔧 Task#334
    const reloadNestParam = (currentTab && currentTab.useLocal) ? '' : (currentTab && currentTab.nest ? `&nest=${encodeURIComponent(currentTab.nest)}` : '');
    const response = await fetch(`/api/files/read?path=${encodeURIComponent(filePath)}${reloadNestParam}`);
    if (response.ok) {
      const data = await response.json();
      const content = data.content;
      monacoEditor.setValue(content);
      const currentTab = findTabById(getActiveEditorTabId());  // 🔧 Task#334
      if (currentTab) {
        currentTab.content = content;
        currentTab.original = content;
        clearEditTrackerByPath(currentTab.path);  // 🔧 Task#421
      }
      console.log(`[handleFileChanged] 文件已重载: ${filePath}`);
      showSimpleToast('文件已重载', 'success');
    } else {
      console.error(`[handleFileChanged] 重载失败: HTTP ${response.status}`);
      showSimpleToast('重载失败：服务器错误', 'error');
    }
  } catch (error) {
    console.error('[handleFileChanged] 重载失败:', error);
    showSimpleToast('重载失败', 'error');
  } finally {
    closeFileChangeDialog();
  }
}

/**
 * 三方合并算法（git格式冲突标记）
 * 注意：此函数保证永远不会失败，任何错误都会返回安全的结果
 * 
 * @param {string} baseContent - 原始内容（文件打开时的内容）
 * @param {string} localContent - 前端编辑的内容
 * @param {string} remoteContent - 后端变化后的内容
 * @returns {{content: string, hasConflict: boolean, conflictCount: number, firstConflictLine: number}} - 合并结果、是否有冲突、冲突数量、第一个冲突行号
 */
function threeWayMerge(baseContent, localContent, remoteContent) {
  try {
    // 处理空内容的情况
    const base = baseContent || '';
    const local = localContent || '';
    const remote = remoteContent || '';

    const baseLines = base.split(/\r?\n/);
    const localLines = local.split(/\r?\n/);
    const remoteLines = remote.split(/\r?\n/);

    const result = [];
    let conflictCount = 0;
    let firstConflictLine = -1;
    const maxLen = Math.max(baseLines.length, localLines.length, remoteLines.length);

    for (let i = 0; i < maxLen; i++) {
      const baseLine = baseLines[i] || '';
      const localLine = localLines[i] || '';
      const remoteLine = remoteLines[i] || '';

      if (baseLine === localLine && baseLine === remoteLine) {
        result.push(baseLine);
      } else if (baseLine !== localLine && baseLine === remoteLine) {
        result.push(localLine);
      } else if (baseLine === localLine && baseLine !== remoteLine) {
        result.push(remoteLine);
      } else if (localLine === remoteLine && baseLine !== localLine) {
        result.push(localLine);
      } else {
        conflictCount++;
        if (firstConflictLine === -1) {
          firstConflictLine = result.length;
        }
        result.push('<<<<<<< 前端编辑');
        result.push(localLine);
        result.push('=======');
        result.push(remoteLine);
        result.push('>>>>>>> 后端变化');
      }
    }

    return { content: result.join('\n'), hasConflict: conflictCount > 0, conflictCount, firstConflictLine };
  } catch (error) {
    // 任何错误都返回安全的结果：使用本地内容
    console.error('[threeWayMerge] 合并算法异常，使用本地内容:', error);
    return { content: localContent || '', hasConflict: false, conflictCount: 0, firstConflictLine: -1 };
  }
}

/**
 * 处理合并按钮点击（三方合并，保留git格式的冲突）
 * 注意：直接使用对话框存储的 newContent，不需要再去 fetch
 */
async function handleFileChangeMerge() {
  // 检查是否有存储的对话框数据
  if (!_fileChangeDialogData) {
    showSimpleToast('合并失败: 缺少文件变化数据', 'error');
    closeFileChangeDialog();
    return;
  }

  const { filePath, oldContent, newContent } = _fileChangeDialogData;

  // 关闭对话框
  closeFileChangeDialog();

  // 获取当前编辑器内容（local）
  const currentContent = monacoEditor ? monacoEditor.getValue() : '';
  const currentTab = findTabById(getActiveEditorTabId());  // 🔧 Task#334
  const baseContent = currentTab ? (currentTab.original || '') : '';

  // 使用对话框存储的 newContent 作为远程内容（remote）
  const remoteContent = newContent || '';

  // 执行三方合并
  const { content: mergedContent, hasConflict, conflictCount, firstConflictLine } = threeWayMerge(baseContent, currentContent, remoteContent);

  // 更新编辑器
  if (currentTab) {
    currentTab.content = mergedContent;
    currentTab.original = remoteContent;
    clearEditTrackerByPath(currentTab.path);  // 🔧 Task#421
  }

  if (monacoEditor) {
    monacoEditor.setValue(mergedContent);

    // 如果有冲突，自动滚动到第一个冲突处
    if (hasConflict && firstConflictLine >= 0) {
      try {
        monacoEditor.revealLineInCenter(firstConflictLine + 1);
        monacoEditor.setPosition({ lineNumber: firstConflictLine + 1, column: 1 });
      } catch (scrollError) {
        console.warn('[handleFileChanged] 滚动失败:', scrollError);
      }
    }
  }

  console.log(`[handleFileChanged] 文件已合并: ${filePath}`);

  // 显示结果提示
  if (hasConflict) {
    showSimpleToast(`⚠️ 自动合并发现 ${conflictCount} 处冲突，需手动合并`, 'warning');
  } else {
    showSimpleToast('✅ 合并成功！', 'success');
  }
}

