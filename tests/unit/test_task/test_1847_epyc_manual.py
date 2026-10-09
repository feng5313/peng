# -*- coding: utf-8 -*-
r"""事务#1847 远程架构 epyc 验收：vnas(Linux)进程内 + 远程epyc(Windows)nest

模拟真实生产架构：peng 主服务在 Linux，外部文件在远程 Windows nest。
验证 ExternalFileBackup 的远程分派：备份到 nest 端文件同盘符根 \.peng\backups。
"""
import sys

sys.path.insert(0, "/mnt/p/feng/work/peng/src")
from peng.service.tool_backup import ExternalFileBackup  # noqa: E402

TASK_ID = 9991847
NEST = "epyc"
FILES = [r"D:\peng_test_1847\ext_D.txt", r"E:\peng_test_1847\ext_E.txt"]

results = []


def check(name, cond, detail=""):
    ok = bool(cond)
    results.append((name, ok))
    print(("PASS " if ok else "FAIL ") + name + ((" | " + str(detail)) if detail else ""), flush=True)


def remote(cmd):
    """在 epyc 执行命令取输出"""
    import json
    import urllib.parse
    import urllib.request

    q = urllib.parse.quote(cmd)
    req = urllib.request.Request(
        f"http://localhost:6688/api/nest/exec?nest=epyc&command={q}&timeout=25",
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=35) as r:
        return json.loads(r.read().decode())


def main():
    # 1. 远程备份（vnas 进程内调 → nest 端盘符仓库）
    spec = ExternalFileBackup.backup(TASK_ID, FILES, "e2e-r4", nest_name=NEST)
    check("backup 返回 dict {D,E}", isinstance(spec, dict) and set(spec) == {"D", "E"}, spec)

    # 2. nest 端盘符根仓库与文件验证
    for d, content in (("D", "v1-D"), ("E", "v1-E")):
        out = remote(f"dir /b /a {d}:\\.peng\\backups\\task#{TASK_ID} && type {d}:\\.peng\\backups\\task#{TASK_ID}\\peng_test_1847\\ext_{d}.txt")
        stdout = out.get("stdout", "")
        check(f"{d}:\\.peng\\backups 仓库已生成", out.get("exit_code") == 0 and ".git" in stdout, stdout[:120])
        check(f"{d}: 备份文件内容正确", content in stdout, stdout[:150])

    # 3. 无变化检测
    changed = ExternalFileBackup.check_changes(TASK_ID, FILES, spec, nest_name=NEST)
    check("check_changes 无变化", changed == [], changed)

    # 4. 修改 + 检测 + 远程恢复
    remote(r"echo v2-modified> D:\peng_test_1847\ext_D.txt")
    changed = ExternalFileBackup.check_changes(TASK_ID, FILES[:1], spec, nest_name=NEST)
    check("check_changes 检测到D修改", changed == [FILES[0]], changed)
    n = ExternalFileBackup.restore(TASK_ID, FILES[:1], spec, nest_name=NEST)
    check("restore D 恢复1个", n == 1, n)
    out = remote(r"type D:\peng_test_1847\ext_D.txt")
    check("restore D 内容正确", "v1-D" in out.get("stdout", ""), out.get("stdout", "")[:80])

    # 5. get_all_heads 含远程盘符 + 默认
    heads = ExternalFileBackup.get_all_heads(TASK_ID, nest_name=NEST)
    check(
        "get_all_heads dict 含 D/E",
        isinstance(heads, dict) and {"D", "E"} <= set(heads),
        heads if isinstance(heads, dict) else str(heads),
    )

    # 6. list_baseline + 删除 + restore_all 全量恢复
    remote(r"del /q E:\peng_test_1847\ext_E.txt")
    remote(r"echo v3> D:\peng_test_1847\ext_D.txt")
    bl = ExternalFileBackup.list_baseline_files(TASK_ID, heads, nest_name=NEST)
    check(
        "list_baseline_files 含 D/E 文件",
        {f"D:/peng_test_1847/ext_D.txt", "E:/peng_test_1847/ext_E.txt"} <= {p.replace("\\", "/") for p in bl},
        bl,
    )
    n = ExternalFileBackup.restore_all(TASK_ID, heads, nest_name=NEST)
    check("restore_all 恢复全部", n >= 2, n)
    out = remote(r"type D:\peng_test_1847\ext_D.txt E:\peng_test_1847\ext_E.txt")
    check("restore_all 内容正确(删除重建)", "v1-D" in out.get("stdout", "") and "v1-E" in out.get("stdout", ""), out.get("stdout", "")[:100])

    fails = [r for r in results if not r[1]]
    print(f"\n=== SUMMARY: {len(results) - len(fails)}/{len(results)} passed ===", flush=True)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
