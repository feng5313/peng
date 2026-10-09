# -*- coding: utf-8 -*-
"""事务#1847 第七轮 epyc 真实验收：backups 根 git 环境 + 绝对路径映射（初始设计）

D:\\works\\t.txt → D:\\.peng\\backups\\d\\works\\t.txt；无 task#id 子目录；
commit 消息含事务id+原路径+一键还原命令；非git workdir 绝不全量扫描。
"""
import base64
import json
import sys
import urllib.parse
import urllib.request

sys.path.insert(0, "/mnt/p/feng/work/peng/src")
from peng.service.tool_backup import ToolBackupManager  # noqa: E402

TASK = 998040
WD = r"D:\peng_r7"
SRC = r"D:\peng_r7\src.txt"
ROOT = r"D:\.peng\backups"
BAK = r"D:\.peng\backups\d\peng_r7\src.txt"

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
    # 0. 清场
    remote_py(
        "import os, shutil, json\n"
        f"shutil.rmtree(r'{WD}', ignore_errors=True)\n"
        f"os.makedirs(r'{WD}', exist_ok=True)\n"
        f"open(r'{SRC}','w').write('v1-r7')\n"
        # 清掉根仓库里本 task 的历史（保留其他）+ 阻断文件
        "import subprocess\n"
        f"if os.path.isdir(r'{ROOT}\\\\.git'):\n"
        f"    subprocess.run(['git','rm','-r','-f','--quiet','d/peng_r7'], cwd=r'{ROOT}', capture_output=True)\n"
        f"    subprocess.run(['git','commit','-m','clean r7 test','--allow-empty','--quiet'], cwd=r'{ROOT}', capture_output=True)\n"
        f"if os.path.isfile(r'{ROOT}\\\\task%%231886'): pass\n"
        "print('READY')\n"
    )

    # 1. create_snapshot（非git workdir，edit 工具）
    meta = ToolBackupManager.create_snapshot(TASK, "edit", {"filePath": SRC}, workdir=WD, nest_name="epyc")
    check("快照 type=external", meta is not None and meta.get("type") == "external", meta)

    # 2. backups 根 git 环境 + 路径映射 + 无 task 子目录
    out = remote_py(
        "import os, subprocess\n"
        f"r1 = os.path.isdir(r'{ROOT}\\\\.git')\n"
        f"r2 = os.path.isfile(r'{BAK}')\n"
        f"r3 = open(r'{BAK}').read() if r2 else ''\n"
        f"r4 = os.path.isdir(r'{ROOT}\\\\task%%23{TASK}')\n"
        "print('%d|%d|%s|%d' % (r1, r2, r3, r4))\n"
    )
    stdout = out.get("stdout", "").strip()
    parts = stdout.split("|")
    check("backups 根=.git 环境", len(parts) > 3 and parts[0] == "1", stdout)
    check("备份映射 d/peng_r7/src.txt 内容正确", len(parts) > 3 and parts[1] == "1" and parts[2] == "v1-r7", stdout)
    check("无 task#id 子目录", len(parts) > 3 and parts[3] == "0", stdout)

    # 3. commit 消息规范：事务id+原路径+一键还原命令
    out = remote_py(
        "import subprocess\n"
        f"r = subprocess.run(['git','log','-3','--pretty=%B'], cwd=r'{ROOT}', capture_output=True, text=True)\n"
        "print(r.stdout)\n"
    )
    msg = out.get("stdout", "")
    check("commit消息含事务id+原路径+copy还原", f"task#{TASK}" in msg and SRC in msg and "copy /y" in msg and "one-key restore:" in msg, msg[:200])

    # 4. check_post_tool_changes：无变化 None
    det = ToolBackupManager.check_post_tool_changes(dict(meta), WD, "epyc")
    check("无变化 → None", det is None, det)

    # 5. 修改 → 检测 → 回滚恢复
    remote_py(f"open(r'{SRC}','w').write('v2-r7')\nprint('MOD')\n")
    det = ToolBackupManager.check_post_tool_changes(dict(meta), WD, "epyc")
    check("检测到修改", det is not None and SRC in det.get("changed_files", []), (det or {}).get("changed_files"))
    ok, msg, undo, changed = ToolBackupManager.rollback(det)
    check("回滚成功且实际变化", ok and changed, msg)
    out = remote_py(f"print(open(r'{SRC}').read())\n")
    check("回滚内容=v1-r7", "v1-r7" in out.get("stdout", ""), out.get("stdout", ""))

    # 6. 巨型 workdir 防护：空 args（iteration_start）→ None 零动作
    meta2 = ToolBackupManager.create_snapshot(TASK, "iteration_start", {}, workdir=r"D:\work", nest_name="epyc")
    check("空涉及文件 → None（绝不全量扫描 D:\\work）", meta2 is None, meta2)

    n = sum(results)
    print(f"\n=== SUMMARY: {n}/{len(results)} passed ===", flush=True)
    return 0 if n == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
