# -*- coding: utf-8 -*-
"""事务#1847 第九轮 epyc 验收：历史版本还原命令 + git管理文件过滤"""
import base64
import json
import subprocess
import sys
import urllib.parse
import urllib.request

sys.path.insert(0, "/mnt/p/feng/work/peng/src")
from peng.service.tool_backup import ToolBackupManager  # noqa: E402

TASK = 998043
WD = r"D:\peng_r9"
ROOT = r"D:\.peng\backups"
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
    # 场景1：历史版本还原（核心）：v1备份 → 改v2 → 再备份 → 用第一次的还原命令 → 恢复v1
    remote_py(
        f"import os\nos.makedirs(r'{WD}', exist_ok=True)\n"
        f"open(r'{WD}\\h.txt','w').write('VERSION-1')\nprint('OK')\n"
    )
    meta1 = ToolBackupManager.create_snapshot(
        TASK, "edit", {"filePath": f"{WD}\\h.txt"}, workdir=WD, nest_name="epyc",
    )
    check("第一次备份成功", meta1 is not None and meta1.get("type") == "external", meta1)
    # 取第一次 commit 消息中的还原命令
    out = remote_py(
        "import subprocess, json\n"
        f"r = subprocess.run(['git','log','-1','--pretty=%B'], cwd=r'{ROOT}', capture_output=True)\n"
        "print(json.dumps(r.stdout.decode('utf-8', 'replace')))\n"
    )
    msg = json.loads(out.get("stdout", '"x"').strip().split("\n")[-1])
    check("消息含 :/ 历史还原命令", "checkout \":/peng backup task#" in msg and " && copy /y" in msg, msg[:150])
    restore_cmd = next(
        (ln.strip() for ln in msg.splitlines() if ln.strip().startswith("git -C")), ""
    )
    check("提取到还原命令", bool(restore_cmd), restore_cmd[:120])

    remote_py(f"open(r'{WD}\\h.txt','w').write('VERSION-2')\nprint('V2')\n")
    meta2 = ToolBackupManager.create_snapshot(
        TASK, "edit", {"filePath": f"{WD}\\h.txt"}, workdir=WD, nest_name="epyc",
    )
    check("第二次备份成功（工作区现为VERSION-2）", meta2 is not None)
    # 执行第一次备份的还原命令（此刻工作区/最新备份都是VERSION-2，只有历史commit里有VERSION-1）
    out = remote_py(
        "import subprocess, json\n"
        f"r = subprocess.run({restore_cmd!r}, shell=True, capture_output=True)\n"
        "print(json.dumps({'rc': r.returncode, 'err': r.stderr.decode('utf-8','replace')[:150]}))\n"
    )
    exec_res = json.loads(out.get("stdout", "{}").strip().split("\n")[-1]) if out.get("stdout") else {}
    check("还原命令执行成功", exec_res.get("rc") == 0, exec_res)
    out = remote_py(f"print(open(r'{WD}\\h.txt').read())\n")
    check("★恢复的是历史版本VERSION-1（非最新VERSION-2）", "VERSION-1" in out.get("stdout", ""), out.get("stdout", ""))

    # 场景2：git管理文件过滤（嵌套git仓库）
    remote_py(
        f"import os, subprocess\n"
        f"os.makedirs(r'{WD}\\repo', exist_ok=True)\n"
        f"subprocess.run(['git','init'], cwd=r'{WD}\\repo', capture_output=True)\n"
        f"open(r'{WD}\\repo\\managed.txt','w').write('in-git-repo')\n"
        f"open(r'{WD}\\plain.txt','w').write('non-git')\nprint('OK')\n"
    )
    meta3 = ToolBackupManager.create_snapshot(
        TASK, "edit", {"filePath": f"{WD}\\repo\\managed.txt"}, workdir=WD, nest_name="epyc",
    )
    check("嵌套git仓库内文件被跳过（git管理）", meta3 is None, (meta3 or {}).get("external_files"))
    meta4 = ToolBackupManager.create_snapshot(
        TASK, "edit", {"filePath": f"{WD}\\plain.txt"}, workdir=WD, nest_name="epyc",
    )
    check("非git文件正常备份", meta4 is not None and meta4.get("external_files") == [f"{WD}\\plain.txt"],
          (meta4 or {}).get("external_files"))

    n = sum(results)
    print(f"\n=== SUMMARY: {n}/{len(results)} passed ===", flush=True)
    return 0 if n == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
