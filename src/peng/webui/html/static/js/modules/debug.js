// ==================== WebUI Debug State ====================

/** 收集所有WebUI客户端状态（用于调试API） */
function collectWebUIDebugState() {
  // 检测是否为调试浏览器（headless chrome / playwright / agent-browser）
  const isDebugBrowser = /HeadlessChrome/i.test(navigator.userAgent) ||
                         window.__agent_browser ||
                         !window.matchMedia('(prefers-color-scheme: light)').matches && /Headless/i.test(navigator.userAgent);

  const result = {
    timestamp: new Date().toISOString(),
    client_id: window.__webui_client_id,
    _is_debug: !!isDebugBrowser,
    _user_agent: navigator.userAgent.substring(0, 120),
    layout: {
      current: state.currentLayout || 'default',
      panels: {},
    },
    editor: {
      tabs: [],
      currentTabId: getActiveEditorTabId(),  // 🔧 Task#334
      editorGroups: [],
      activeGroupId: state._lastActiveGroupId,
    },
    output: {
      title: '',
      currentViewTab: state.currentViewTab,
    },
    terminal: {
      sessions: [],
    },
    selection: {
      currentMessageId: state.currentMessageId,
      currentTaskId: state.currentTaskId,
      branchMessageId: state.branchMessageId,
    },
    theme: state.theme,
    localStorage: {},
  };

  // 布局面板可见性
  ['layout-editor-panel', 'layout-bottom-panel', 'layout-terminal-panel',
   'layout-split-handle-1', 'layout-split-handle-2', 'default-output-container',
   'layout-bottom-left', 'layout-bottom-right', 'layout-bottom-tabbed'].forEach(id => {
    const el = document.getElementById(id);
    result.layout.panels[id] = {
      display: el ? el.style.display : 'NOT_FOUND',
      offsetWidth: el ? el.offsetWidth : 0,
      offsetHeight: el ? el.offsetHeight : 0,
    };
  });

  // 编辑器标签页
  if (typeof getAllEditorTabs === 'function') {
    result.editor.tabs = getAllEditorTabs().map(t => ({
      id: t.id,
      name: t.name,
      path: t.path,
      language: t.language,
      isNewFile: t.isNewFile,
      isVirtual: t.isVirtual,
      groupId: t.groupId,
    }));
  }

  // 编辑器分组
  if (state.editorGroups) {
    result.editor.editorGroups = state.editorGroups.map(g => ({
      id: g.id,
      tabs: g.tabs.map(t => ({
        id: t.id,
        name: t.name,
        path: t.path,
        language: t.language,
        isNewFile: t.isNewFile,
      })),
      currentTabId: g.currentTabId,
      containerId: g.containerId,
      hasMonacoEditor: !!g.monacoEditor,
      domExists: !!document.getElementById(g.containerId),
    }));
  }

  // 输出标题
  const defaultOutputStatus = document.getElementById('output-msg-info-default');
  result.output.title = defaultOutputStatus ? defaultOutputStatus.textContent : '';
  ['output-msg-info-2', 'output-msg-info-3'].forEach(id => {
    const el = document.getElementById(id);
    if (el) result.output['title_' + id] = { text: el.textContent, display: el.style.display };
  });

  // localStorage相关数据
  try {
    ['peng-ed-layout', 'peng-ed-groups', 'peng-ed-tabs', 'peng-ed-content',
     'peng-ed-cursor', 'peng-ed-search-history', 'peng-theme'].forEach(key => {
      const val = localStorage.getItem(key);
      if (val) {
        try {
          result.localStorage[key] = JSON.parse(val);
        } catch {
          result.localStorage[key] = val;
        }
      }
    });
  } catch (e) {
    result.localStorage.error = e.message;
  }

  return result;
}

// 每个浏览器标签页的唯一ID（用于多浏览器区分）
if (!window.__webui_client_id) {
  window.__webui_client_id = 'c' + Date.now().toString(36) + Math.random().toString(36).substr(2, 4);
}

/** 上报客户端状态到服务器（供curl /api/webui/debug-webui查看） */
async function reportWebUIClientState() {
  try {
    const data = collectWebUIClientState();
    data.client_id = window.__webui_client_id;
    await fetch('/api/webui/client-state', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(data),
    });
  } catch (e) {
    console.warn('[debug] 上报客户端状态失败:', e);
  }
}

/** 收集完整的客户端状态（含DOM验证和刷新测试结果） */
function collectWebUIClientState() {
  const data = collectWebUIDebugState();

  // DOM级别的验证：检查编辑器是否实际可见和可交互
  data._dom_verify = {
    editorViewVisible: !!document.getElementById('editor-view')?.offsetParent,
    groupsCount: document.querySelectorAll('[tag="editor-group"]').length,
    monacoEditors: document.querySelectorAll('.monaco-editor').length,
    activeMonacoVisible: (() => {
      const active = document.querySelector('[tag="editor-group"]:last-of-type .monaco-editor');
      return active ? active.offsetParent !== null : false;
    })(),
    pageFocus: document.hasFocus(),
  };

  // 刷新测试框架：读取刷新后的测试结果
  try {
    const testResult = localStorage.getItem('__peng_test_result');
    if (testResult) data._refresh_test = JSON.parse(testResult);
  } catch(e) {}

  // 方案B: 读取前端自测试结果
  try {
    const selfTestResult = localStorage.getItem('__peng_self_test_result');
    if (selfTestResult) data._self_test = JSON.parse(selfTestResult);
  } catch(e) {}

  return data;
}

// 每30秒自动上报一次状态
setInterval(reportWebUIClientState, 30000);
// 页面加载后2秒首次上报（缩短以便debug更快获取状态）
setTimeout(reportWebUIClientState, 2000);

// ==================== 刷新测试框架 ====================

/**
 * 刷新测试框架：保存参照值，刷新后自动对比验证
 *
 * 用法：
 *   1. debugSaveRefreshTest({ key1: val1, key2: val2, ... })
 *   2. 刷新页面
 *   3. 2秒后自动执行对比，结果写入 __peng_test_result
 *   4. 通过 curl /api/webui/debug-webui 读取结果
 *
 * 支持的key格式：
 *   - 普通key如 "currentLayout" -> state.currentLayout
 *   - "state:editorGroups[0].tabs.length" -> state.editorGroups[0].tabs.length
 *   - "ls:peng-ed-layout" -> JSON.parse(localStorage.getItem('peng-ed-layout'))
 *   - "dom:.editor-tab.active:textContent" -> document.querySelector('.editor-tab.active')?.textContent
 */
function debugSaveRefreshTest(checks) {
  const testData = {
    _saved_at: Date.now(),
    _url: location.href,
    checks: checks,
  };
  localStorage.setItem('__peng_test_expected', JSON.stringify(testData));
  localStorage.removeItem('__peng_test_result');
  console.log('[debug] 刷新测试参照值已保存:', JSON.stringify(checks));
}

/** 刷新后自动执行测试对比（在页面加载2秒后自动调用） */
function debugCheckRefreshTest() {
  try {
    const expected = localStorage.getItem('__peng_test_expected');
    if (!expected) return;
    const testData = JSON.parse(expected);
    const checks = testData.checks;

    const actual = {};
    const failedChecks = [];

    for (const [key, expectedVal] of Object.entries(checks)) {
      try {
        let actualVal;
        if (key.startsWith('dom:')) {
          const rest = key.substring(4);
          const lastColon = rest.lastIndexOf(':');
          const sel = rest.substring(0, lastColon);
          const prop = rest.substring(lastColon + 1) || 'textContent';
          const el = document.querySelector(sel);
          actualVal = el ? (prop === 'exists' ? true : el[prop]) : null;
        } else if (key.startsWith('ls:')) {
          const lsKey = key.substring(3);
          const raw = localStorage.getItem(lsKey);
          actualVal = raw ? JSON.parse(raw) : null;
        } else if (key.startsWith('state:')) {
          const path = key.substring(6).split('.');
          let obj = state;
          for (const p of path) { obj = obj?.[p]; }
          actualVal = obj;
        } else {
          const path = key.split('.');
          let obj = state;
          for (const p of path) { obj = obj?.[p]; }
          actualVal = obj;
        }

        actual[key] = actualVal;

        const match = actualVal === expectedVal ||
                      (actualVal == null && expectedVal == null) ||
                      (JSON.stringify(actualVal) === JSON.stringify(expectedVal));
        if (!match) {
          failedChecks.push({ key, expected: expectedVal, actual: actualVal });
        }
      } catch(e) {
        actual[key] = { _error: e.message };
        failedChecks.push({ key, expected: expectedVal, actual: actual[key] });
      }
    }

    const result = {
      _checked_at: Date.now(),
      _saved_at: testData._saved_at,
      _elapsed_ms: Date.now() - testData._saved_at,
      pass: failedChecks.length === 0,
      total: Object.keys(checks).length,
      failed: failedChecks.length,
      failedChecks: failedChecks,
      actual: actual,
    };
    localStorage.setItem('__peng_test_result', JSON.stringify(result));

    if (failedChecks.length > 0) {
      console.warn('[debug] 刷新测试失败:', JSON.stringify(failedChecks, null, 2));
    } else {
      console.log('[debug] ✅ 刷新测试全部通过! (' + Object.keys(checks).length + '项)');
    }

    // 清理expected（避免下次重复测试）
    localStorage.removeItem('__peng_test_expected');
    return result;
  } catch(e) {
    console.error('[debug] 刷新测试执行出错:', e);
  }
}

// 页面加载完成后自动执行刷新测试
setTimeout(debugCheckRefreshTest, 2000);

// 暴露到全局方便控制台调试
window.collectWebUIDebugState = collectWebUIDebugState;
window.reportWebUIClientState = reportWebUIClientState;
window.debugSaveRefreshTest = debugSaveRefreshTest;
window.debugCheckRefreshTest = debugCheckRefreshTest;

// ==================== 调试API：远程JS执行 ====================

/** 处理服务端发来的debug_eval请求：执行JS代码并通过POST返回结果 */
async function handleDebugEval(data) {
  const evalId = data.eval_id;
  const code = data.code;
  let result;
  let error = null;

  try {
    // 执行JS代码，捕获返回值
    const fn = new Function('state', 'monacoEditor', 'window', 'document', 'localStorage', code);
    result = fn(state, typeof monacoEditor !== 'undefined' ? monacoEditor : null, window, document, localStorage);
    // 如果结果是Promise，等待resolve
    if (result && typeof result.then === 'function') {
      result = await result;
    }
  } catch (e) {
    error = e.message + (e.stack ? '\n' + e.stack.split('\n').slice(0, 3).join('\n') : '');
  }

  // 通过POST返回结果给服务端
  try {
    await fetch('/api/webui/eval-response', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        eval_id: evalId,
        client_id: window.__webui_client_id || 'unknown',
        is_debug: !!window.__is_debug_browser,
        result: error ? { _error: error } : result,
      }),
    });
  } catch (e) {
    console.warn('[debug_eval] 返回结果失败:', e);
  }
}

// 标记是否为调试浏览器
window.__is_debug_browser = /HeadlessChrome/i.test(navigator.userAgent);

// ==================== 方案A: 刷新重连 - 客户端ready信号 ====================

/**
 * 页面加载完成后发送 ready 信号给服务端
 * 服务端收到后会把缓存的pending eval请求重新发送给此客户端
 * 这解决了debug eval在页面刷新后断连、无法获取恢复后状态的问题
 */
async function notifyClientReady() {
  try {
    const resp = await fetch('/api/webui/client-ready', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        client_id: window.__webui_client_id || 'unknown',
        is_debug: !!window.__is_debug_browser,
        user_agent: navigator.userAgent.substring(0, 120),
        timestamp: Date.now(),
      }),
    });
    const data = await resp.json();
    if (data.success) {
      console.log('[debug] client-ready 信号已发送, pending_evals:', (data.pending_evals || 0));
    }
  } catch (e) {
    console.warn('[debug] 发送client-ready信号失败:', e);
  }
}

// 页面加载后3秒发送ready信号（等待WebSocket连接建立和初始状态恢复）
setTimeout(notifyClientReady, 3000);

// ==================== 方案B: 前端自测试能力 ====================

/**
 * 运行前端自测试：验证DOM渲染、编辑器状态一致性、localStorage恢复等
 * 
 * 测试项：
 *   1. 编辑器标签页 vs DOM标签页元素 一一对应
 *   2. 编辑器分组 vs DOM分组容器 一一对应
 *   3. 当前活跃标签页在DOM中有对应的Monaco编辑器实例
 *   4. localStorage中的数据与内存state一致
 *   5. 面板布局合理性（分拆模式下两侧面板都可见）
 * 
 * @returns {object} 测试结果 {pass, total, failed, details}
 */
function debugRunSelfTest() {
  const results = {
    _run_at: Date.now(),
    pass: true,
    total: 0,
    failed: 0,
    details: [],
  };

  function check(name, passed, detail) {
    results.total++;
    if (!passed) {
      results.pass = false;
      results.failed++;
      results.details.push({ check: name, status: 'FAIL', detail: detail });
      console.warn(`[debug-self-test] ❌ ${name}:`, detail);
    } else {
      results.details.push({ check: name, status: 'PASS' });
    }
  }

  // --- Test 1: 标签页 vs DOM一致性 ---
  const allTabs = typeof getAllEditorTabs === 'function' ? getAllEditorTabs() : [];
  if (allTabs.length > 0) {
    const tabElements = document.querySelectorAll('.editor-tab');
    check('tabs_count_match',
      tabElements.length >= allTabs.length,
      `state有${allTabs.length}个tab, DOM有${tabElements.length}个tab元素`);

    // 检查每个state tab在DOM中有对应的标签页元素
    for (const tab of allTabs) {
      const tabEl = document.querySelector(`[data-tab-id="${tab.id}"]`);
      check(`tab_dom_exists_${tab.name}`,
        !!tabEl,
        `tab "${tab.name}" (id=${tab.id}) 在DOM中未找到 [data-tab-id="${tab.id}"]`);
    }
  }

  // --- Test 2: 编辑器分组 vs DOM一致性 ---
  if (state.editorGroups && state.editorGroups.length > 0) {
    const groupContainers = document.querySelectorAll('.editor-group');
    check('groups_count_match',
      groupContainers.length === state.editorGroups.length,
      `state有${state.editorGroups.length}个group, DOM有${groupContainers.length}个group容器`);

    for (const group of state.editorGroups) {
      const container = document.getElementById(group.containerId);
      check(`group_dom_exists_${group.id}`,
        !!container,
        `group ${group.id} (containerId=${group.containerId}) 在DOM中未找到`);

      // 检查当前活跃tab对应的Monaco编辑器实例
      if (group.currentTabId && group.monacoEditor) {
        const editorDom = container ? container.querySelector('.monaco-editor') : null;
        check(`group_monaco_visible_${group.id}`,
          !!editorDom,
          `group ${group.id} 有monacoEditor实例但DOM中没有.monaco-editor`);
      }

      // 检查group的tabs数量
      check(`group_tabs_count_${group.id}`,
        group.tabs && group.tabs.length > 0,
        `group ${group.id} 没有任何tabs`);
    }
  }

  // --- Test 3: localStorage恢复一致性 ---
  try {
    const layoutStr = localStorage.getItem('peng-ed-layout');
    if (layoutStr) {
      const lsLayout = JSON.parse(layoutStr);
      check('ls_layout_match_state',
        lsLayout.layout === state.currentLayout,
        `localStorage.layout="${lsLayout.layout}", state.currentLayout="${state.currentLayout}"`);
      check('ls_viewTab_match_state',
        lsLayout.viewTab === state.currentViewTab,
        `localStorage.viewTab="${lsLayout.viewTab}", state.currentViewTab="${state.currentViewTab}"`);
    }

    const groupsStr = localStorage.getItem('peng-ed-groups');
    if (groupsStr && state.editorGroups) {
      const lsGroups = JSON.parse(groupsStr);
      check('ls_groups_count_match',
        lsGroups.length === state.editorGroups.length,
        `localStorage有${lsGroups.length}个group, state有${state.editorGroups.length}个`);
    }

    const tabsStr = localStorage.getItem('peng-ed-tabs');
    if (tabsStr) {
      const lsTabs = JSON.parse(tabsStr);
      check('ls_tabs_count_match',
        lsTabs.length === allTabs.length,
        `localStorage有${lsTabs.length}个tab, state有${allTabs.length}个`);
    }
  } catch (e) {
    check('ls_parse_ok', false, `localStorage解析失败: ${e.message}`);
  }

  // --- Test 4: 布局面板合理性 ---
  if (state.currentLayout === 'split' && state.editorGroups) {
    // 分拆模式下检查两个面板都可见
    const leftPanel = document.getElementById('layout-bottom-left');
    const rightPanel = document.getElementById('layout-bottom-right');
    check('split_both_panels_exist',
      !!leftPanel && !!rightPanel,
      `分拆模式下缺少面板: left=${!!leftPanel}, right=${!!rightPanel}`);
    if (leftPanel && rightPanel) {
      check('split_both_panels_visible',
        leftPanel.style.display !== 'none' && rightPanel.style.display !== 'none',
        `分拆模式下面板不可见: left.display="${leftPanel.style.display}", right.display="${rightPanel.style.display}"`);
    }
  }

  // --- Test 5: 活跃tab内容一致性 ---
  if (state.editorGroups) {
    for (const group of state.editorGroups) {
      if (group.currentTabId && group.monacoEditor) {
        const currentTab = group.tabs.find(t => t.id === group.currentTabId);
        if (currentTab && !currentTab.isNewFile && currentTab.path) {
          const editorContent = group.monacoEditor.getValue();
          check(`active_tab_has_content_${group.id}`,
            editorContent.length > 0 || currentTab.isNewFile,
            `活跃tab "${currentTab.name}" Monaco编辑器内容为空 (path=${currentTab.path})`);
        }
      }
    }
  }

  // 写入localStorage供debug API读取
  try {
    localStorage.setItem('__peng_self_test_result', JSON.stringify(results));
  } catch (e) {}

  if (results.pass) {
    console.log(`[debug-self-test] ✅ 全部${results.total}项自测试通过!`);
  } else {
    console.warn(`[debug-self-test] ❌ ${results.failed}/${results.total}项自测试失败!`);
  }

  return results;
}

// 暴露自测试函数
window.debugRunSelfTest = debugRunSelfTest;

// 页面加载后4秒自动运行自测试（在刷新测试之后，状态已恢复）
setTimeout(debugRunSelfTest, 4000);
