---
name: debug-peng
description: 调试peng/fly service/nest运行期内部状态和WebUI前端状态。触发词：webui、调试、debug、修复、peng、nest、fly、事务、编辑器、LSP。
---

# debug-peng 技能

调试peng服务端、WebUI前端和远端nest的统一调试接口。发现功能不足时立刻完善。

## 铁律

1. **agent-browser后台浏览器调试**，不要控制用户桌面的浏览器
2. **默认使用命名session**：所有agent-browser命令必须带 `--session task<task_id>` 参数，支持多个事务用不同session并行操作
3. **禁止vision工具识别文本**，一律用 `agent-browser eval` JS查询DOM
4. **所有JS/Python脚本用write tool写到 /tmp/ 再执行**，bash嵌套引号必崩
5. **修改前端JS/CSS文件不需要重启服务！** 直接刷新浏览器即可生效（`agent-browser --session task12 reload`）。只有修改Python服务端代码才需要重启服务。

## 铁律4做法

```
write tool → /tmp/x.js
agent-browser eval --stdin < /tmp/x.js

write tool → /tmp/x.py
python3 /tmp/x.py

write tool → /tmp/x.json
curl ... -d @/tmp/x.json
```

---

## 一、启动浏览器打开WebUI

```
agent-browser --session task12 close 2>/dev/null
agent-browser --session task12 open http://localhost:6688 --args "--no-sandbox"
sleep 5
```

服务端地址从 `config/webui.json` 的 host/port 获取。

> **多session并行**：不同事务可用不同session名并行操作浏览器（如 `--session task12`、`--session test1`），互不干扰。同一session内串行执行。

---

## 二、浏览器JS执行（所有JS写文件再eval）

```javascript
// /tmp/chk.js — 查询编辑器状态
JSON.stringify({
    nest: state.currentNest,
    workdir: state.workdir,
    groups: state.editorGroups ? state.editorGroups.map(function(g) {
        return {id: g.id, tabs: g.tabs.map(function(t){return {name:t.name, path:t.path, nest:t.nest}}), hasMonaco: !!g.monacoEditor};
    }) : []
})
```
```
agent-browser --session task12 eval --stdin < /tmp/chk.js
```

---

## 三、切换Nest（DOM结构是自定义div，不是select）

Nest下拉框DOM：`div.nest-dropdown-list > div.nest-dropdown-item`，每个item的textContent含nest名。

```javascript
// /tmp/select_nest.js — 切换到wsl
var items = document.querySelectorAll('.nest-dropdown-item');
for (var i = 0; i < items.length; i++) {
    if (items[i].textContent.indexOf('wsl') >= 0) { items[i].click(); break; }
}
'selected wsl'
```
```
agent-browser --session task12 eval --stdin < /tmp/select_nest.js
```

---

## 四、在编辑器中打开远程Nest上的文件

函数：`openEditorForFile(filePath, useLocal=false, foundNest=null)`
- foundNest='wsl' 表示在wsl nest上打开

```javascript
// /tmp/open_file.js — 切换nest + 打开文件一步到位
(async function() {
    var items = document.querySelectorAll('.nest-dropdown-item');
    for (var i = 0; i < items.length; i++) {
        if (items[i].textContent.indexOf('wsl') >= 0) { items[i].click(); break; }
    }
    await new Promise(function(r){ setTimeout(r, 1000); });
    await openEditorForFile('/mnt/wsl/work/huizi/src/console.cpp', false, 'wsl');
    return 'done';
})()
```
```
agent-browser --session task12 eval --stdin < /tmp/open_file.js && sleep 5
```

---

## 五、LSP API测试

端点：`/api/lsp/definition|references|hover|completion|symbols|implementation|diagnostics`

### 单次测试

```json
// /tmp/lsp_req.json
{"file_path":"/mnt/wsl/work/huizi/src/console.cpp","line":24,"column":6,"nest":"wsl"}
```
```
curl -s -X POST http://localhost:6688/api/lsp/definition -H "Content-Type: application/json" -d @/tmp/lsp_req.json
```

### 批量测试

```python
// /tmp/test_lsp.py
import json, subprocess
tests = [
    ('clrscr(line24)', {'file_path':'/mnt/wsl/work/huizi/src/console.cpp','line':24,'column':6,'nest':'wsl'}),
    ('gotoxy(line27)', {'file_path':'/mnt/wsl/work/huizi/src/console.cpp','line':27,'column':6,'nest':'wsl'}),
]
for name, body in tests:
    r = subprocess.run(['curl','-s','-X','POST','http://localhost:6688/api/lsp/definition',
        '-H','Content-Type: application/json','-d','@-'],
        input=json.dumps(body).encode(), capture_output=True, timeout=30)
    resp = json.loads(r.stdout)
    locs = resp.get('locations',[])
    loc_str = ', '.join([l['path'].split('/')[-1]+':'+str(l['startLine']) for l in locs]) if locs else 'EMPTY'
    print(f'{"OK  " if locs else "FAIL"} {name:35s} -> {loc_str}')
```
```
python3 /tmp/test_lsp.py
```

---

## 六、服务端调试（不依赖浏览器）

### Python eval — 查询peng服务端状态

```json
// /tmp/q.json
{"code": "len(MessageManager().db.get_all_messages())"}
```
```
curl -s -X POST http://localhost:6688/api/webui/debug-eval -H "Content-Type: application/json" -d @/tmp/q.json
```

### Python exec — 修改peng服务端状态（通过result返回）

```json
// /tmp/ex.json
{"code": "import sys; result[\"version\"] = sys.version"}
```
```
curl -s -X POST http://localhost:6688/api/webui/debug-exec -H "Content-Type: application/json" -d @/tmp/ex.json
```

### 在远端nest执行bash命令

```
curl -s -X POST "http://localhost:6688/api/nest/exec?nest=wsl&command=hostname&timeout=10" -H "Content-Type: application/json"
```
返回：`{"success": true, "exit_code": 0, "stdout": "...", "stderr": "..."}`

### 获取完整状态快照

```
curl -s http://localhost:6688/api/webui/debug-webui | python3 -m json.tool
```

### 可用变量

- **peng服务端**: `app`, `state`, `config`, `result`(exec), `MessageManager`, `TaskManager`, `terminal_session_manager`, `Path`, `json`, `os`, `sys`, `time`, `datetime`, `asyncio`, `traceback`
- **Nest端**: `result`(exec), `Path`, `json`, `os`, `sys`, `time`, `datetime`, `asyncio`, `traceback`, `logging`
- **浏览器JS**: `state`, `monacoEditor`, `window`, `document`, `localStorage`

注意：debug-exec不能用asyncio.run()（已在asyncio事件循环中）。需要nest操作用 `/api/nest/exec`。

---

## 七、服务重启

> ⚠️ **重要：修改前端JS/CSS文件（`src/peng/webui/html/static/` 下的文件）不需要重启服务！** 只需刷新浏览器页面即可生效。只有修改Python服务端代码（`.py`文件）才需要重启。

```
nohup ssh root@localhost fly restart &>/dev/null &
tail -20 logs/peng.log   # 必须！看到"日志系统已初始化"才成功
# 重启后等3-5秒再操作
```

---

## 八、刷新测试框架

验证"刷新后状态恢复"。debug eval刷新后断连，必须用此框架。

### 操作步骤

```javascript
// /tmp/save_test.js — 保存参照值
debugSaveRefreshTest({
  "currentLayout": "default",
  "state:editorTabs.length": 3,
  "state:editorGroups.length": 1
});
'saved'
```
```
agent-browser --session task12 eval --stdin < /tmp/save_test.js
agent-browser --session task12 reload
sleep 8
```
然后读结果：
```python
// /tmp/read_result.py
import json
data = json.load(open('/tmp/snap.json'))
for cid, cs in data.get('client_state', {}).items():
    t = cs.get('_refresh_test')
    if t:
        print(f"{'PASS' if t['pass'] else 'FAIL'} ({t['total']})")
        for f in t.get('failedChecks', []):
            print(f"  FAIL {f['key']}: expected={f['expected']}, actual={f['actual']}")
```
```
curl -s http://localhost:6688/api/webui/debug-webui > /tmp/snap.json
python3 /tmp/read_result.py
```

key格式：`普通key`=state属性, `state:路径`=深层属性, `ls:key`=localStorage, `dom:选择器:属性`=DOM

前端自测试：页面加载4秒后自动 `debugRunSelfTest()`，结果在 `client_state._self_test`。

---

## 九、相关文件路径

| 文件 | 路径 |
|------|------|
| 前端主JS | `src/peng/webui/html/static/js/app.js` |
| 前端模块 | `src/peng/webui/html/static/js/modules/` |
| CSS | `src/peng/webui/html/static/css/style.css` |
| HTML | `src/peng/webui/html/index.html` |
| 服务端 | `src/peng/webui/server.py` |
| WebSocket | `src/peng/webui/websocket.py` |
| Debug API | `src/peng/webui/routes/api.py` |
| Nest路由 | `src/peng/webui/routes/nest_routes.py` |
| 文件操作 | `src/peng/webui/routes/file_operations.py` |
| LSP路由 | `src/peng/webui/routes/lsp_routes.py` |
| LSP前端 | `src/peng/webui/html/static/js/modules/lsp-provider.js` |
| 配置 | `config/webui.json` |
| Nest配置 | `config/nest.json` |