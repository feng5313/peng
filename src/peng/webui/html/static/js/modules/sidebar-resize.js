// ==================== 边栏拖拽调整 ====================

const MIN_SIDEBAR_WIDTH = 180;
const MAX_SIDEBAR_WIDTH = 500;

function initResizeHandles() {
  const leftHandle = document.getElementById('resize-left');
  const rightHandle = document.getElementById('resize-right');
  const leftSidebar = document.getElementById('sidebar-left');
  const rightSidebar = document.getElementById('sidebar-right');

  if (leftHandle && leftSidebar) {
    initResize(leftHandle, leftSidebar, 'left');
  }

  if (rightHandle && rightSidebar) {
    initResize(rightHandle, rightSidebar, 'right');
  }
}

function initResize(handle, sidebar, direction) {
  let startX = 0;
  let startWidth = 0;

  handle.addEventListener('mousedown', (e) => {
    e.preventDefault();
    startX = e.clientX;
    startWidth = sidebar.offsetWidth;
    handle.classList.add('dragging');
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';

    const onMouseMove = (e) => {
      const delta = e.clientX - startX;
      let newWidth;

      if (direction === 'left') {
        newWidth = startWidth + delta;
      } else {
        newWidth = startWidth - delta;
      }

      // 动态计算最大宽度：只受最小宽度限制，可以拖拽到非常宽
      // 确保至少保留200px给主内容区
      const viewportWidth = window.innerWidth;
      const otherSidebarWidth = direction === 'left'
        ? (document.getElementById('sidebar-right')?.offsetWidth || 0)
        : (document.getElementById('sidebar-left')?.offsetWidth || 0);
      const maxWidth = viewportWidth - otherSidebarWidth - 200; // 200px是主内容区最小宽度

      // 限制宽度范围
      newWidth = Math.max(MIN_SIDEBAR_WIDTH, Math.min(maxWidth, newWidth));
      sidebar.style.width = newWidth + 'px';
    };

    const onMouseUp = () => {
      handle.classList.remove('dragging');
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
      document.removeEventListener('mousemove', onMouseMove);
      document.removeEventListener('mouseup', onMouseUp);

      // 🔧 修复性能：拖拽结束后才保存到 localStorage（避免每次 mousemove 都写）
      localStorage.setItem(`sidebar-${direction}-width`, sidebar.offsetWidth);

      // 侧边栏大小变化后重新适配终端
      // 🔧 修复：使用双 requestAnimationFrame 延迟 fit，确保嵌套 flex 布局完成
      if (state.outputFit) {
        requestAnimationFrame(() => {
          requestAnimationFrame(() => {
            try {
              if (state.outputFit) {
                state.outputFit.fit();
              }
            } catch (e) { }
          });
        });
      }

      // 同步侧边栏宽度到后端 config
      _syncSidebarWidthToBackend();
    };

    document.addEventListener('mousemove', onMouseMove);
    document.addEventListener('mouseup', onMouseUp);
  });
}

function restoreSidebarWidths() {
  // 优先从 webuiConfig 加载（来自 config/webui.json），localStorage 可覆盖
  const leftConfigWidth = state.webuiConfig.sidebar_left_width;
  const rightConfigWidth = state.webuiConfig.sidebar_right_width;
  const leftWidth = localStorage.getItem('sidebar-left-width') || leftConfigWidth;
  const rightWidth = localStorage.getItem('sidebar-right-width') || rightConfigWidth;

  const leftSidebar = document.getElementById('sidebar-left');
  const rightSidebar = document.getElementById('sidebar-right');

  if (leftWidth && leftSidebar) {
    leftSidebar.style.width = leftWidth + 'px';
  }

  if (rightWidth && rightSidebar) {
    rightSidebar.style.width = rightWidth + 'px';
  }
}

