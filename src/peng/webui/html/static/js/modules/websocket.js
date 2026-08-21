// ==================== WebSocket ====================

// 🔧 Task#281：输出健康检测 —— 定时检查输出是否停滞，自动重新订阅
let _lastOutputReceivedAt = Date.now();
let _outputHealthCheckTimer = null;
const OUTPUT_HEALTH_CHECK_INTERVAL = 3000;  // 3秒检测一次
const OUTPUT_STALL_THRESHOLD = 5000;       // 5秒没收到输出视为停滞

function _startOutputHealthCheck() {
  if (_outputHealthCheckTimer) return;
  _outputHealthCheckTimer = setInterval(() => {
    // 只在当前事务running时检测
    if (state.currentTaskId == null || state.currentMessageId == null) return;
    if (!state.ws || state.ws.readyState !== WebSocket.OPEN) return;

    // 检查当前选中事务是否在running状态
    const task = findTaskById(state.tasks, state.currentTaskId);
    if (!task || task.status !== 'running') return;

    const elapsed = Date.now() - _lastOutputReceivedAt;
    if (elapsed > OUTPUT_STALL_THRESHOLD) {
      console.warn(`[Task#281] 输出停滞检测：${Math.round(elapsed/1000)}秒未收到输出，重新订阅 task=${state.currentTaskId}`);
      _lastOutputReceivedAt = Date.now();  // 重置防止连续触发
      // 用 sinceTime 增量获取：从最后已知时间戳之后的新输出，不重复历史
      const sinceTime = state.currentNewestTimestamp || null;
      if (sinceTime) {
        subscribeOutput(0, true, null, sinceTime);
      } else {
        subscribeOutput(0, true);
      }
    }
  }, OUTPUT_HEALTH_CHECK_INTERVAL);
}

function initWebSocket() {
  const wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  const wsUrl = `${wsProtocol}//${window.location.host}/ws/output`;

  // 🔧 优化(task#113)：跟踪是否首次连接，避免重复加载
  if (!state.ws) {
    state._wsFirstConnection = true;
  }

  try {
    state.ws = new WebSocket(wsUrl);

    state.ws.onopen = () => {
      renderThroughputStatusBar();  // WebSocket重连后刷新流量显示
      // 🔧 关键修复：移除这里的订阅调用！
      // 原因：selectMessage/selectTask 中的 subscribeOutput() 已经会等待WebSocket连接并发送订阅
      // 如果这里也调用，会导致重复订阅，后端发送两次历史输出，造成xterm被清空
      // 只有在断线重连时才需要在这里重新订阅（由 subscribeOutput 的后台重试机制处理）

      // 🔧 优化(task#113)：首次连接时跳过加载（初始化流程已加载），仅重连时刷新
      if (state._wsFirstConnection) {
        state._wsFirstConnection = false;
      } else {
        // WebSocket重连后立即刷新消息和事务列表
        loadMessages(false, true);  // WebSocket重连：获取完整数据
        loadTasks();

        // 🔧 Task#281关键修复：WS重连后必须重新订阅当前选中的输出
        // 因为WS断开时后端会注销客户端并清除订阅（websocket.py finally块），
        // WS重连是新客户端注册（空订阅），如果不重新订阅，xterm将不再收到任何输出
        if (state.currentMessageId != null && state.currentTaskId != null) {
          console.log(`[Task#281] WS重连后重新订阅: msg=${state.currentMessageId}, task=${state.currentTaskId}`);
          // skipHistory=true 因为已有缓存数据，只接收新输出
          subscribeOutput(0, true);
        }
      }
      // 🔧 Task #182：WebSocket连接后切换为慢速兜底轮询（30秒），避免不断无意义刷新
      startTasksRefresh();
      // 🔧 Task#281：启动输出健康检测
      _startOutputHealthCheck();
    };

    state.ws.onmessage = (event) => {
      const data = JSON.parse(event.data);
      handleWebSocketMessage(data);
    };

    state.ws.onclose = () => {
      const nestTrafficEl = document.getElementById('nest-traffic');
      if (nestTrafficEl) nestTrafficEl.textContent = '';

      // 🔧 Task #182：WebSocket断开后恢复原始快速轮询（作为唯一数据来源）
      startTasksRefresh();

      // 尝试重连
      setTimeout(() => {
        initWebSocket();
      }, 3000);
    };

    state.ws.onerror = (error) => {
      console.error('WebSocket 错误:', error);
    };
  } catch (error) {
    console.error('WebSocket 初始化失败:', error);
  }
}

function handleWebSocketMessage(data) {
  switch (data.type) {
    case 'connected':
      // 检测服务重启：server_start_time变化则自动刷新页面
      if (data.server_start_time !== undefined) {
        if (state._lastServerStartTime && state._lastServerStartTime !== data.server_start_time) {
          console.log('[WS] 检测到服务重启，自动刷新页面');
          location.reload();
          return;
        }
        state._lastServerStartTime = data.server_start_time;
      }
      break;

    case 'subscribed':
      break;

    case 'output_header':
      // 显示历史输出头部功能链接
      showOutputHeaderLinks(data.messageId, data.taskIds, data.scrollback);
      break;

    case 'output_batch':
      // 后端发送：
      // - 首次订阅：batch 1 是最新 scrollback 行，hasMore 表示是否还有更老历史
      // - "上一页"请求：isOlderHistory=true，返回比 beforeTimestamp 更老的 scrollback 行
      // - 🔧 Task#219 增量获取：isIncremental=true，返回 sinceTime 之后的新输出
      // 每批内部顺序：[旧→新]（文件本身就是按时间写入的）
      // 缓存需要：[老→新]
      const isFirstBatch = data.batchNum === 1;
      const isOlderHistory = data.isOlderHistory === true;
      const isIncremental = data.isIncremental === true;
      const hasMore = data.hasMore === true;
      // 🔧 Fix Task#840：后端提供的确定性行号信息
      const totalLines = data.totalLines || 0;
      const fileLineStart = data.fileLineStart || 0;
      const fileLineEnd = data.fileLineEnd || 0;

      if (data.outputs && Array.isArray(data.outputs) && data.outputs.length > 0) {
        // 解析每行并分配全局行号（fileLineNo = 文件中的实际行号）
        const parsedOutputs = data.outputs.map((line, i) => {
          const cleanLine = line.replace(/\n$/, '').replace(/\r$/, '');
          const parts = cleanLine.split('\t');
          const item = parts.length >= 4
            ? { timestamp: parts[0], source: parts[1], taskId: parseInt(parts[2]) || 0, content: parts.slice(3).join('\t') }
            : { timestamp: '', source: '', taskId: 0, content: cleanLine };
          // 🔧 Fix Task#840：fileLineNo = 文件实际行号（后端提供）
          item.fileLineNo = fileLineStart + i;
          return item;
        });

        if (parsedOutputs.length) {
          const taskId = parsedOutputs[0].taskId;  // 取第一行的 taskId 作为该批次的 taskId（理论上同一批次应该是同一个 taskId）
          const history = getOutputHistory(taskId);

          if (isIncremental) {
            // 🔧 Fix Task#840：增量数据 - fileLineNo 已由后端提供
            state._totalLines = Math.max(state._totalLines, totalLines);
            // 🔧 Fix Task#1008：防重复 —— 使用 fileLineNo 去重（比 timestamp 更精确）
            // history 按 fileLineNo 单调递增，最后一条即最大行号
            const existingMaxLineNo = history.length > 0
              ? (history[history.length - 1].fileLineNo || 0)
              : 0;
            const existingNewestTs = history.length > 0 ? history[history.length - 1].timestamp : null;
            const newOutputs = parsedOutputs.filter(o => {
              // 主去重：fileLineNo 大于已有最大行号
              if (o.fileLineNo && existingMaxLineNo > 0) {
                return o.fileLineNo > existingMaxLineNo;
              }
              // 备用去重：timestamp 大于已有最新时间戳
              if (existingNewestTs) {
                return o.timestamp > existingNewestTs;
              }
              return true;
            });
            // 🔧 Fix Task#1435：快速切换事务时，旧的增量批次可能延迟到达，
            // 数据仍缓存到正确 taskId 的 history（无害），但不写入 xterm 或更新全局视图状态
            const isStaleBatch = taskId !== state.currentTaskId;
            if (isStaleBatch) {
              console.warn(`[Task#1435] 延迟到达的增量批次不写入xterm: batchTaskId=${taskId}, currentTaskId=${state.currentTaskId}, ${newOutputs.length}行`);
            }
            // 始终缓存到正确 taskId 的 history（切换回该事务时可用）
            if (newOutputs.length > 0) {
              history.push(...newOutputs);
              // 只有当前事务的增量数据才写入 xterm 和更新全局状态
              if (!isStaleBatch) {
              _lastOutputReceivedAt = Date.now();

              // 🔧 Fix Task#1108: 安全检查 —— 打破 userScrolledUp 死锁（同 appendOutput）
              if (state.userScrolledUp && !state._browsingHistory && state.output && isExactlyAtBottom()) {
                state.userScrolledUp = false;
              }

              // 🔧 Fix Task#1096：与 appendOutput 保持一致 —— 用户滚动上移或翻页浏览时跳过 xterm 写入
              const shouldSkipWrite = state._browsingHistory || state.userScrolledUp;

              if (!shouldSkipWrite) {
                const chunks = [];
                for (const item of newOutputs) {
                  if (passFilters(item.source)) {
                    // 🔧 Fix Task#1008：补上 formatLineNoPrefix，与 appendOutput 保持一致
                    chunks.push(formatLineNoPrefix(item.fileLineNo) + convertFilePathsToLinks(item.content) + '\r\n');
                  }
                }
                if (chunks.length > 0 && state.output) {
                  // 🔧 Fix #1064: 不使用 _pagingRecoveryUntil（同 appendOutput 修复）
                  // onScroll 中的 buffer长度追踪会自动处理 write 引起的 scroll 事件
                  state.output.write(chunks.join(''));
                  requestAnimationFrame(() => {
                    // 🔧 Fix #1428: rAF 可能在用户 wheel 事件之后才执行，
                    // 需要重新检查 userScrolledUp 和用户滚动意图窗口
                    if (state.output && !state.userScrolledUp
                        && !(state._userScrollIntentUntil && Date.now() < state._userScrollIntentUntil)) {
                      state.output.scrollToBottom();
                    }
                  });
                }
              }
              state.currentNewestTimestamp = newOutputs[newOutputs.length - 1].timestamp;
              if (state.currentOldestTimestamp === null) {
                state.currentOldestTimestamp = newOutputs[0].timestamp;
              }
              } // end if (!isStaleBatch)
            }
            updatePaginationButtons();
          } else if (isOlderHistory) {
            // 🔧 Fix Task#840：老历史 - unshift 到缓存前面，行号已由后端分配
            state._totalLines = Math.max(state._totalLines, totalLines);
            history.unshift(...parsedOutputs);
          } else if (isFirstBatch) {
            // 🔧 Fix Task#840：首批 - fileLineNo 已由后端提供，totalLines 是文件实际行数
            state._totalLines = totalLines;
            state._appendCount = 0;
            if (history.length > 0) {
              // 🔧 Fix Task#1008：防重复 —— 使用 fileLineNo 去重
              const existingMaxLineNo = history.length > 0
                ? (history[history.length - 1].fileLineNo || 0)
                : 0;
              const existingNewestTs = history[history.length - 1].timestamp;
              const newOutputs = parsedOutputs.filter(o => {
                if (o.fileLineNo && existingMaxLineNo > 0) {
                  return o.fileLineNo > existingMaxLineNo;
                }
                return o.timestamp > existingNewestTs;
              });
              if (newOutputs.length > 0) {
                history.push(...newOutputs);
              }
            } else {
              history.push(...parsedOutputs);
            }
          } else {
            // 旧逻辑的后续批次（更旧）：插入缓存前面
            history.unshift(...parsedOutputs);
          }

          // 更新 hasMoreHistory 状态
          state.hasMoreHistory.set(taskId, hasMore);

          // 设置时间戳指针并渲染（只在首批或老历史请求时，增量数据已在上面处理）
          if (!isIncremental) {
            if (isFirstBatch && !isOlderHistory) {
              // 🔧 Fix Task#1435：首批输出也可能因快速切换而延迟到达，必须检查 taskId
              if (taskId !== state.currentTaskId) {
                console.warn(`[Task#1435] 延迟到达的首批不写入xterm: batchTaskId=${taskId}, currentTaskId=${state.currentTaskId}`);
              } else {
              // 🔧 Fix Task#1433：首批输出到达时，强制全量渲染（不依赖 scrollToNewest 的增量判断）
              // 根因：在 output_batch 到达之前，_watch_loop 可能已通过 appendOutput 写入了
              // 几行实时输出并设置了 currentNewestTimestamp，导致 scrollToNewest 误判
              // "已在最新位置"跳过全量渲染，只显示那几行。
              if (!state._browsingHistory && taskId === state.currentTaskId) {
                renderFromCache(taskId);
              } else if (!state._browsingHistory) {
                scrollToNewest();
              }
              // 🔧 修复：首批输出加载后确保xterm充满容器（刷新页面后容器尺寸可能还没确定）
              setTimeout(() => fitAllTerminals(), 200);
              } // end if (taskId === currentTaskId)
            } else if (isOlderHistory) {
              // "上一页"加载的老历史：重新渲染显示
              renderOlderHistory(taskId, parsedOutputs);
            }
            updatePaginationButtons();
          }
        }
      } else if (isOlderHistory) {
        // 空响应但标记为老历史请求：更新 hasMore 状态
        const taskId = state.currentTaskId;
        if (taskId !== null) {
          state.hasMoreHistory.set(taskId, hasMore);
          updatePaginationButtons();
        }
        // 🔧 Task#1102：如果正在显示"加载中"，恢复之前的页面内容
        if (state._loadingPrevPage) {
          state._loadingPrevPage = false;
          restoreCurrentPage();
          showSimpleToast('⏹️ 没有更旧的输出了', 'info');
        }
      } else if (!isOlderHistory && data.outputs !== undefined) {
        // 首次订阅但无输出：更新 hasMoreHistory 状态
        const taskId = state.currentTaskId;
        if (taskId !== null) {
          state.hasMoreHistory.set(taskId, hasMore);
          updatePaginationButtons();
        }
      }
      break;

    case 'output':
      // 存储并显示进程输出
      _lastOutputReceivedAt = Date.now();  // 🔧 Task#281：更新最后收到输出时间
      appendOutput(data.content, data.source, data.taskId, data.messageId, data.timestamp, data.fileLineNo);
      break;

    case 'status':
      handleStatusUpdate(data);
      break;

    case 'message_update':
      handleMessageUpdate(data);
      break;

    case 'task_update':
      handleTaskUpdate(data);
      break;

    case 'throughput_update':
      handleThroughputUpdate(data);
      break;

    case 'toast':
      showToast(data.toast_type, data.title, data.message, data.duration);
      break;

    case 'file_changed':
      handleFileChanged(data);
      break;

    case 'debug_eval':
      // 调试API：执行JS代码并返回结果
      handleDebugEval(data);
      break;

    case 'debug_eval_retry':
      // 方案A: 刷新重连后，服务端重发的pending eval请求
      console.log('[debug] 收到缓存的eval重试请求:', data.eval_id);
      handleDebugEval(data);
      break;

    case 'terminal_event':
      // 🔧 Task#185：后端驱动的终端事件（创建/关闭终端）
      handleTerminalEvent(data);
      break;

    default:
      break;
  }
}

