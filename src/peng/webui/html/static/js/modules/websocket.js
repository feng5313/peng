// ==================== WebSocket ====================

// 🔧 Task#281：输出健康检测 —— 定时检查输出是否停滞，自动重新订阅
let _lastOutputReceivedAt = Date.now();
let _outputHealthCheckTimer = null;
const OUTPUT_HEALTH_CHECK_INTERVAL = 3000;  // 3秒检测一次
const OUTPUT_STALL_THRESHOLD = 12000;       // 🔧 Task#1725：12秒没收到输出才视为停滞
//                                            （LLM思考间隔普遍5~15秒，旧值5秒对断续输出事务是误报机器）
let _stallRetryCount = 0;                   // 🔧 Task#1725：连续停滞重订阅计数（退避防风暴）
let _nextStallRetryAt = 0;                  // 下次允许停滞重订阅的时间戳

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
      // 🔧 Task#1725：退避重订阅——连续停滞时逐次拉长间隔(12s→24s→48s→96s封顶)，
      // 防止"断续输出事务+重订阅风暴"打爆前后端（曾致服务端每5秒被重订阅刷屏）
      if (Date.now() < _nextStallRetryAt) return;
      _stallRetryCount++;
      const backoff = Math.min(12000 * Math.pow(2, _stallRetryCount - 1), 96000);
      _nextStallRetryAt = Date.now() + backoff;
      console.warn(`[Task#281] 输出停滞检测：${Math.round(elapsed/1000)}秒未收到输出，重新订阅 task=${state.currentTaskId} (第${_stallRetryCount}次, 退避${Math.round(backoff/1000)}s)`);
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
      // 🔧 Task#1725：启动输出健康检测
      _startOutputHealthCheck();
      // 🔧 Task#1725：连接成功即清除connecting标记
      state._wsConnectingSince = null;
    };

    // 🔧 Task#1725：WS自愈守护——根治"断线后页面永远没数据"
    // 此前重连仅靠 onclose→setTimeout(3s)→initWebSocket 单链，任何一环断裂
    // （后台标签页setTimeout被浏览器节流至1次/分钟、initWebSocket偶发异常、
    //  onclose未触发）页面就永久断供：xterm不动、事务树不刷，用户只见死页面。
    // 守护三保险：
    //   1) 页面切回前台立即检查重连（后台节流的setTimeout在切回瞬间补偿）
    //   2) 每10秒健康巡检（前台不节流）：非OPEN且无在途重连 → 立即重连
    //   3) CONNECTING卡死>15秒（握手挂起）→ 强制重建
    let _wsReconnectScheduled = false;
    function _wsSelfHealCheck(reason) {
      try {
        const rs = state.ws ? state.ws.readyState : WebSocket.CLOSED;
        if (rs === WebSocket.OPEN) {
          state._wsConnectingSince = null;
          _wsReconnectScheduled = false;
          return;  // 健康
        }
        if (rs === WebSocket.CONNECTING) {
          if (state._wsConnectingSince && Date.now() - state._wsConnectingSince > 15000) {
            console.warn(`[Task#1725] WS握手卡死>15s(${reason})，强制重建连接`);
            try { state.ws.onclose = null; state.ws.close(); } catch (e) {}
            state.ws = null;
          } else {
            return;  // 正常握手中
          }
        }
        // CLOSED/CLOSING/被强杀：调度重连（防重复调度）
        if (!_wsReconnectScheduled) {
          _wsReconnectScheduled = true;
          console.warn(`[Task#1725] WS自愈守护触发重连(${reason}, readyState=${rs})`);
          setTimeout(() => {
            _wsReconnectScheduled = false;
            initWebSocket();
          }, 500);
        }
      } catch (e) {
        console.error('[Task#1725] WS自愈巡检异常:', e);
      }
    }
    let _wsSelfHealTimer = null;
    function _startWsSelfHealWatchdog() {
      if (_wsSelfHealTimer) return;
      _wsSelfHealTimer = setInterval(() => {
        // 浏览器对后台标签会把interval节流到1次/分钟——切回前台由visibilitychange立即补偿
        _wsSelfHealCheck('interval');
      }, 10000);
      document.addEventListener('visibilitychange', () => {
        if (document.visibilityState === 'visible') {
          _wsSelfHealCheck('visibility');
        }
      });
      window.addEventListener('online', () => _wsSelfHealCheck('online'));
    }
    _startWsSelfHealWatchdog();

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
      // 🔧 Task#1725：重连期间标记 connecting 时间戳，供 _wsSelfHealWatchdog 判断
      state._wsConnectingSince = Date.now();
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
      // - 🔧 Task#1725 行号翻页：isPageRange=true，返回 [pageStart, pageEnd] 行号范围的行（O(1)）
      // 每批内部顺序：[旧→新]（文件本身就是按时间写入的）
      // 缓存需要：[老→新]
      const isFirstBatch = data.batchNum === 1;
      const isOlderHistory = data.isOlderHistory === true;
      const isIncremental = data.isIncremental === true;
      const isPageRange = data.isPageRange === true;
      const hasMore = data.hasMore === true;
      // 🔧 Fix Task#840：后端提供的确定性行号信息
      const totalLines = data.totalLines || 0;
      const fileLineStart = data.fileLineStart || 0;
      const fileLineEnd = data.fileLineEnd || 0;

      // 🔧 Task#1725：行号翻页响应 —— 解析行内行号（新格式）、分组缓存、原子渲染目标页
      if (isPageRange) {
        handlePageRangeBatch(data, totalLines, fileLineStart, fileLineEnd, hasMore);
        break;
      }

      if (data.outputs && Array.isArray(data.outputs) && data.outputs.length > 0) {
        // 🔧 Task#1725：统一行解析 —— 支持新格式（第3字段=行号）与旧格式（第3字段=taskId）
        // 新格式下行内没有 taskId，全部归属当前订阅事务；行号优先用行内值（权威）
        const lineNoInField = data.lineNoInField === true;
        const parsedOutputs = data.outputs.map((line, i) => {
          const cleanLine = line.replace(/\n$/, '').replace(/\r$/, '');
          const parts = cleanLine.split('\t');
          let item;
          if (lineNoInField && parts.length >= 4) {
            // 新格式: {ts}\t{source}\t{lineNo}\t{content}
            // 🔧 Task#1725 v2：行号统一用后端物理行号(fileLineStart+i)，
            // 行内行号与物理行号可能有恒定偏移（多次runner重启的历史文件）
            item = {
              timestamp: parts[0],
              source: parts[1],
              taskId: state.currentTaskId || 0,
              content: parts.slice(3).join('\t'),
            };
            item.fileLineNo = fileLineStart + i;
          } else if (parts.length >= 4) {
            // 旧格式: {ts}\t{source}\t{taskId}\t{content}
            // 🔧 Fix #1751：防御 —— 混合格式文件交界批次（旧行占多数）中，
            // 新格式行号会被误判为旧格式taskId；行号(≥100000)不可能是事务ID，兜底当前事务
            var tid = parseInt(parts[2]) || 0;
            if (tid > 100000) tid = state.currentTaskId || 0;
            item = {
              timestamp: parts[0],
              source: parts[1],
              taskId: tid,
              content: parts.slice(3).join('\t'),
            };
            item.fileLineNo = fileLineStart + i;
          } else {
            item = { timestamp: '', source: '', taskId: state.currentTaskId || 0, content: cleanLine };
            item.fileLineNo = fileLineStart + i;
          }
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
              _stallRetryCount = 0;  // 🔧 Task#1725：收到增量数据，重置停滞退避

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
              // 🔧 Task#1725 致命修复：首批必须与已到的实时行【按行号合并】，绝不能丢弃！
              // 旧逻辑"只保留fileLineNo>已有最大行号"会把整个首批丢掉——
              // 实时行先到(如115586~115624)时，首批[115125~115624]全被过滤 →
              // 缓存稀疏只剩实时行 → renderPageRange按页范围渲染大面积缺行
              // → 半黑（部分缺行）/全黑（范围全缺）。这正是"正在输出时翻页变黑"的根因！
              const existingByLn = new Map();
              for (const h of history) {
                if (h.fileLineNo != null) existingByLn.set(h.fileLineNo, h);
              }
              const merged = [];
              for (const o of parsedOutputs) {
                // 首批为权威数据：重复行号以首批为准，跳过已有
                if (o.fileLineNo != null && existingByLn.has(o.fileLineNo)) continue;
                merged.push(o);
              }
              // 保留实时行中超出首批范围的新行（首批读取期间/之后新产生的）
              const batchMaxLn = parsedOutputs.length > 0
                ? (parsedOutputs[parsedOutputs.length - 1].fileLineNo || 0) : 0;
              const tailRealtime = history.filter(h =>
                h.fileLineNo != null && h.fileLineNo > batchMaxLn);
              history.length = 0;
              history.push(...merged, ...tailRealtime);
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
        // 🔧 Task#1725：恢复当前页（不清屏式加载，屏幕内容一直保持完整）
        restoreCurrentPage();
        showSimpleToast('⏹️ 没有更旧的输出了', 'info');
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
      _stallRetryCount = 0;  // 🔧 Task#1725：收到实时输出，重置停滞退避
      appendOutput(data.content, data.source, data.taskId, data.messageId, data.timestamp, data.fileLineNo);
      break;

    case 'output_reset':
      // 🔧 Task#1725：输出文件被重写（行号格式迁移）→ 清缓存重新拉取
      handleOutputReset(data.taskId);
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

/**
 * 🔧 Task#1725：行号翻页响应处理（isPageRange=true 的 output_batch）
 *
 * - 解析行内行号（新格式 {ts}\t{source}\t{lineNo}\t{content}，lineNoInField=true）
 *   或使用物理行号 fallback（旧格式，兼容未迁移文件）
 * - 按行 taskId 分组 unshift 到各自缓存（比旧行为"整批塞进第一行taskId缓存"正确）
 * - 仅当响应匹配当前翻页请求（_loadingPage 存在且批次覆盖请求范围）时才渲染目标页，
 *   过期/超时后的迟到响应只缓存不渲染（防止页面突跳）
 * - 全程不清屏（黑屏根治）：加载中提示为非破坏性 toast
 */
function handlePageRangeBatch(data, totalLines, fileLineStart, fileLineEnd, hasMore) {
  const outputs = data.outputs;
  const lineNoInField = data.lineNoInField === true;
  const curTaskId = state.currentTaskId;

  // 请求匹配信息（请求发起时记录的目标页范围）
  const pending = state._loadingPage;
  state._totalLines = Math.max(state._totalLines, totalLines);

  if (!outputs || !Array.isArray(outputs) || outputs.length === 0) {
    // 空响应：请求范围超出文件（通常是请求了不存在的历史）→ 修正 totalLines 上界
    if (pending) {
      state._totalLines = Math.min(state._totalLines, Math.max(0, pending.start - 1));
      state._loadingPage = null;
    }
    showSimpleToast('⏹️ 没有更旧的输出了', 'info');
    updatePaginationButtons();
    return;
  }

  // 解析行并按 taskId 分组
  const byTask = new Map();
  for (let i = 0; i < outputs.length; i++) {
    const cleanLine = outputs[i].replace(/\n$/, '').replace(/\r$/, '');
    const parts = cleanLine.split('\t');
    let item;
    if (lineNoInField && parts.length >= 4) {
      // 🔧 新格式: {ts}\t{source}\t{lineNo}\t{content} —— 第3字段是行号，taskId归属订阅事务
      // 🔧 Task#1725 v2：行号统一用后端物理行号(fileLineStart+i)！
      // 行内行号在经历过多次runner重启的文件里与物理行号有恒定偏移（如差4），
      // 用它会让缓存行号与索引/翻页体系错位→缺行黑屏
      item = {
        timestamp: parts[0],
        source: parts[1],
        taskId: curTaskId || 0,
        fileLineNo: fileLineStart + i,
        content: parts.slice(3).join('\t'),
      };
    } else if (parts.length >= 4) {
      // 旧格式: {ts}\t{source}\t{taskId}\t{content} —— 行号用后端物理行号
      // 🔧 Fix #1751：防御 —— 行号(≥100000)不可能是事务ID，兜底当前事务
      var tid = parseInt(parts[2]) || 0;
      if (tid > 100000) tid = curTaskId || 0;
      item = {
        timestamp: parts[0],
        source: parts[1],
        taskId: tid,
        fileLineNo: fileLineStart + i,
        content: parts.slice(3).join('\t'),
      };
    } else {
      item = { timestamp: '', source: '', taskId: curTaskId || 0, fileLineNo: fileLineStart + i, content: cleanLine };
    }
    if (!byTask.has(item.taskId)) byTask.set(item.taskId, []);
    byTask.get(item.taskId).push(item);
  }

  // 分组缓存：批次按行号升序，unshift 到各缓存头部（翻页向更旧行进行，缓存现有行都更新）
  // 🔧 Task#1725：按行号去重 —— 同一范围被重复请求（翻页往返/多次跳页）时不产生重复行
  for (const [tid, items] of byTask) {
    if (items.length > 0) {
      const history = getOutputHistory(tid);
      const existing = new Set();
      for (const h of history) {
        if (h.fileLineNo != null) existing.add(h.fileLineNo);
      }
      const fresh = items.filter(it => it.fileLineNo == null || !existing.has(it.fileLineNo));
      if (fresh.length > 0) {
        history.unshift(...fresh);
      }
    }
  }

  // 🔧 Task#1725 消费式渲染：page_range 防重入保证同一时刻只有一个在途请求，
  // 响应到达即视为该请求的结果，直接渲染响应的实际范围。
  // （旧判定 batchCoversRequest 过严：请求范围被文件头夹紧时 fileLineEnd < pending.end
  //   会永不渲染，_loadingPage 挂到超时 → "经常加载超时"）
  if (pending) {
    state._loadingPage = null;
    // 🔧 Task#1725：渲染后端实际返回的行范围（权威数据完整）。
    // 旧逻辑 renderEnd=max(pending.end, fileLineEnd)：文件头夹紧时
    // fileLineEnd<pending.end，渲染[pending.start, pending.end]里
    // [fileLineEnd+1, pending.end]不存在 → 尾部缺行=半黑。
    const renderStart = Math.min(pending.start, fileLineStart || pending.start);
    const renderEnd = Math.min(pending.end, fileLineEnd || pending.end);
    renderPageRange(renderStart, renderEnd);
    var pi = getPageInfo(renderStart);
    if (pi) showSimpleToast('📄 第' + pi.page + '/' + pi.totalPages + '页', 'info');
  }
  updatePaginationButtons();
}

/**
 * 🔧 Task#1725：输出文件重写重置处理（行号格式迁移后触发）
 * 清空该任务缓存与显示状态，重新订阅拉取首批（新格式）输出
 */
function handleOutputReset(taskId) {
  if (taskId == null) return;
  // 非当前事务：只清缓存（切换回该事务时会重新拉取）
  if (taskId !== state.currentTaskId) {
    state.outputHistory.delete(taskId);
    state.hasMoreHistory.delete(taskId);
    return;
  }
  // 当前事务：清缓存 + 清屏 + 重新订阅
  state.outputHistory.delete(taskId);
  state.hasMoreHistory.delete(taskId);
  state.currentOldestTimestamp = null;
  state.currentNewestTimestamp = null;
  state._totalLines = 0;
  state._pagingStartLine = -1;
  state._pagingEndLine = -1;
  state._loadingPage = null;
  state._browsingHistory = false;
  state.userScrolledUp = false;
  state._xtermNewestLineNo = 0;
  if (state.output) {
    state.output.clear();
    state.currentColor = '';
  }
  showSimpleToast('🔄 输出格式迁移完成，重新加载...', 'info');
  subscribeOutput(0, false);
}

