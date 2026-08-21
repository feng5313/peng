// ==================== 全局禁用浏览器自动补全 ====================
// Task#1322: WebUI 复杂层叠上下文导致浏览器原生补全弹出框无法操作（z-index/事件冲突）
// 方案：全局拦截，对 input/textarea/form 强制设置 autocomplete="off"
// 覆盖范围：静态HTML元素 + 动态JS创建的元素（通过MutationObserver）

(function suppressBrowserAutocomplete() {
  'use strict';

  // 对单个元素强制设置 autocomplete="off"
  function suppress(el) {
    if (el.nodeType !== Node.ELEMENT_NODE) return;
    var tag = el.tagName;
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'FORM') {
      if (el.getAttribute('autocomplete') !== 'off') {
        el.setAttribute('autocomplete', 'off');
      }
    }
  }

  // 遍历处理一批元素
  function processAll(root) {
    (root || document).querySelectorAll('input, textarea, form').forEach(suppress);
  }

  // 1. 立即处理当前已有元素（脚本同步执行时部分DOM可能已存在）
  processAll();

  // 2. DOMContentLoaded 后再处理一次（确保静态HTML全部就绪）
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function() { processAll(); });
  } else {
    processAll();
  }

  // 3. MutationObserver：拦截所有后续动态添加到DOM的 input/textarea/form
  //    覆盖 createElement、innerHTML 等所有动态创建方式
  var observer = new MutationObserver(function(mutations) {
    for (var i = 0; i < mutations.length; i++) {
      var added = mutations[i].addedNodes;
      for (var j = 0; j < added.length; j++) {
        var node = added[j];
        if (node.nodeType !== Node.ELEMENT_NODE) continue;
        suppress(node);       // 检查节点自身
        if (node.querySelectorAll) {
          node.querySelectorAll('input, textarea, form').forEach(suppress);
        }
      }
    }
  });

  observer.observe(document.documentElement, { childList: true, subtree: true });
})();
