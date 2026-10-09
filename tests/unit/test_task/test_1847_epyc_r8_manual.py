# -*- coding: utf-8 -*-
"""事务#1847 第八轮 epyc 验收：bash 命令解析备份（task#1886 del README.md 事故场景）"""
import base64
import json
import sys
import urllib.parse
import urllib.request

sys.path.insert(0, "/mnt/p/feng/work/peng/src")
from peng.service.tool_backup import ToolBackupManager  # noqa: E402

TASK = 998041
WD = r"D:\peng_r8"
results = []


def check(name, cond, detail=""):
    ok = bool(cond)
    results.append(ok)
    print(("PASS " if ok else "FAIL ") + name + ((" | " + str(detail)) if detail else ""), flush=True)


def remote_py(py, timeout=40):
    b64 = base64.b64encode(py.encode()).decode()
    cmd = f'python -c "import base64;exec(base64.b64decode(\'{b64}\').decode(\'utf-8\'))"'
    q = urllib.parse.quote(cmd)
    req = urllib.request.Request(
        f"http://localhost:6688/api/nest/exec?nest=epyc&command={q}&timeout={timeout}", method="POST"
    )
    with urllib.request.urlopen(req, timeout=timeout + 15) as r:
        return json.loads(r.read().decode())


def main():
    # 场景1：del 删除 workdir 内文件（task#1886 事故复现）
    remote_py(
        f"import os\nos.makedirs(r'{WD}', exist_ok=True)\n"
        f"open(r'{WD}\\README.md','w').write('v1-r8-readme')\nprint('OK')\n"
    )
    meta = ToolBackupManager.create_snapshot(
        TASK, "bash", {"command": f"del {WD}\\README.md"}, workdir=WD, nest_name="epyc",
    )
    check("del 命令快照 type=external", meta is not None and meta.get("type") == "external", meta)
    check("del 目标已备份", meta and meta.get("external_files") == [f"{WD}\\README.md"], (meta or {}).get("external_files"))
    out = remote_py(f"print(open(r'D:\\.peng\\backups\\d\\peng_r8\\README.md').read())\n")
    check("备份内容正确（backups/d 路径映射）", "v1-r8-readme" in out.get("stdout", ""), out.get("stdout", ""))

    # 模拟删除 + 检测 + 回滚
    remote_py(f"import os\nos.remove(r'{WD}\\README.md')\nprint('DELETED')\n")
    det = ToolBackupManager.check_post_tool_changes(dict(meta), WD, "epyc")
    check("删除被检测", det is not None and f"{WD}\\README.md" in det.get("changed_files", []),
          (det or {}).get("changed_files"))
    ok, msg, _u, changed = ToolBackupManager.rollback(det)
    check("回滚执行", ok and changed, msg)
    out = remote_py(f"print(open(r'{WD}\\README.md').read())\n")
    check("删除的文件已恢复", "v1-r8-readme" in out.get("stdout", ""), out.get("stdout", ""))

    # 场景2：rm 相对路径
    remote_py(f"open(r'{WD}\\tmp.log','w').write('log-v1')\nprint('OK')\n")
    meta2 = ToolBackupManager.create_snapshot(
        TASK, "bash", {"command": "rm tmp.log"}, workdir=WD, nest_name="epyc",
    )
    check("rm 相对路径已备份", meta2 is not None and meta2.get("external_files") == [f"{WD}\\tmp.log"],
          (meta2 or {}).get("external_files"))

    # 场景3：执行脚本，脚本内部删文件
    remote_py(
        f"import os\n"
        f"open(r'{WD}\\inner.bin','w').write('INNER-DATA')\n"
        f"open(r'{WD}\\clean.py','w').write(\"import os\\nos.remove('inner.bin')\\n\")\nprint('OK')\n"
    )
    meta3 = ToolBackupManager.create_snapshot(
        TASK, "bash", {"command": f"python {WD}\\clean.py"}, workdir=WD, nest_name="epyc",
    )
    check("脚本内部路径已备份", meta3 is not None and f"{WD}\\inner.bin" in (meta3.get("external_files") or []),
          (meta3 or {}).get("external_files"))

    # 场景4：无涉及文件的命令 → None（零动作）
    meta4 = ToolBackupManager.create_snapshot(
        TASK, "bash", {"command": "dir"}, workdir=WD, nest_name="epyc",
    )
    check("无涉及文件命令 → None", meta4 is None, meta4)

    n = sum(results)
    print(f"\n=== SUMMARY: {n}/{len(results)} passed ===", flush=True)
    return 0 if n == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
