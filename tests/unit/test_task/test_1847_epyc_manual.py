# -*- coding: utf-8 -*-
"""事务#1847 epyc(Windows) 真实环境验收测试

验证：非git控制的外部文件备份到该文件同盘符根目录 \\.peng\\backups\\task#N，
避免跨盘符复制；check_changes/restore/get_all_heads/restore_all 全链路正确。

用法： D:\\work\\peng\\.venv\\Scripts\\python.exe <本脚本>
"""
import os
import pathlib
import shutil
import sys

sys.path.insert(0, r"D:\work\peng\src")
from peng.service.tool_backup import ExternalFileBackup  # noqa: E402

TASK_ID = 9991847  # 测试专用事务ID，避免与真实事务冲突
DRIVES = ["C", "D", "E"]

results = []


def check(name, cond, detail=""):
    ok = bool(cond)
    results.append((name, ok))
    print(("PASS " if ok else "FAIL ") + name + ((" | " + str(detail)) if detail else ""), flush=True)


def main():
    print("os.name =", os.name, "python =", sys.version.split()[0], flush=True)
    if os.name != "nt":
        print("FATAL: 本测试必须在 Windows 上运行", flush=True)
        return 2

    drives = [d for d in DRIVES if os.path.exists(d + ":\\")]
    print("drives =", drives, flush=True)

    # ---------- 1. 每个盘符创建外部文件（workdir 外） ----------
    files = {}
    for d in drives:
        p = pathlib.Path(f"{d}:\\peng_test_1847\\ext_{d}.txt")
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(f"v1-on-{d}", encoding="utf-8")
        except PermissionError as e:
            print(f"WARN: {d}: 盘创建测试文件失败（权限）: {e}", flush=True)
            continue
        files[d] = str(p)
    if len(files) < 2:
        print("FATAL: 可用盘符不足2个，无法验证多盘符", flush=True)
        return 2

    # ---------- 2. backup：应返回 dict {盘符: commit} ----------
    file_list = list(files.values())
    spec = ExternalFileBackup.backup(TASK_ID, file_list, "e2e-test")
    check("backup returns dict", isinstance(spec, dict), spec)
    expect_keys = set(files.keys())
    check(
        "backup dict keys == 盘符集合",
        isinstance(spec, dict) and set(spec.keys()) == expect_keys,
        list(spec.keys()) if isinstance(spec, dict) else spec,
    )

    # ---------- 3. 每个盘符根目录生成 \.peng\backups\task#N（目录+git仓库+备份文件） ----------
    for d, fp in files.items():
        repo = pathlib.Path(f"{d}:\\.peng\\backups\\task#{TASK_ID}")
        check(f"{d}:\\.peng\\backups\\task#N 目录已生成", repo.is_dir(), str(repo))
        check(f"{d}: 备份仓库已git初始化", (repo / ".git").is_dir(), str(repo / ".git"))
        rel = ExternalFileBackup._repo_rel(fp, d)
        bf = pathlib.Path(os.path.join(str(repo), rel))
        # 🔧 防呆断言：备份文件绝不能等于源文件路径（Windows 盘符路径 join 陷阱）
        check(
            f"{d}: 备份路径≠源文件路径",
            str(bf).lower() != str(fp).lower(),
            f"bf={bf} fp={fp}",
        )
        check(f"{d}: 备份文件已生成", bf.is_file(), str(bf))
        check(
            f"{d}: 备份文件内容正确",
            bf.is_file() and bf.read_text(encoding="utf-8") == f"v1-on-{d}",
        )
        # 同盘符验证：备份路径与源文件同盘符（避免跨盘符复制的直接证据）
        check(
            f"{d}: 备份与源文件同盘符",
            str(bf)[:1].upper() == d,
        )

    # ---------- 4. check_changes 无变化 ----------
    changed = ExternalFileBackup.check_changes(TASK_ID, file_list, spec)
    check("check_changes 无变化", changed == [], changed)

    # ---------- 5. 修改→检测→恢复 ----------
    d0 = next(iter(files))  # 第一个盘符
    p0 = pathlib.Path(files[d0])
    p0.write_text("v2-modified", encoding="utf-8")
    changed = ExternalFileBackup.check_changes(TASK_ID, [files[d0]], spec)
    check(f"check_changes 检测到{d0}:修改", changed == [files[d0]], changed)
    n = ExternalFileBackup.restore(TASK_ID, [files[d0]], spec)
    check(f"restore {d0}: 恢复1个文件", n == 1, n)
    check(f"restore {d0}: 内容恢复", p0.read_text(encoding="utf-8") == f"v1-on-{d0}")

    # ---------- 6. get_all_heads 多仓库 ----------
    heads = ExternalFileBackup.get_all_heads(TASK_ID)
    check(
        "get_all_heads 返回 dict 含全部盘符 key",
        isinstance(heads, dict) and expect_keys <= set(heads.keys()),
        heads if isinstance(heads, dict) else str(heads),
    )

    # ---------- 7. list_baseline_files + 删除→restore_all 全量恢复 ----------
    d1 = list(files.keys())[-1]  # 最后一个盘符
    os.remove(files[d1])
    p0.write_text("v3", encoding="utf-8")
    bl = ExternalFileBackup.list_baseline_files(TASK_ID, heads)
    # list_baseline 返回正斜杠路径（POSIX 风格），与源路径规范分隔符后比较
    check(
        "list_baseline_files 列出全部备份文件",
        {os.path.normpath(p) for p in bl} == {os.path.normpath(f) for f in file_list},
        bl,
    )
    n = ExternalFileBackup.restore_all(TASK_ID, heads)
    check("restore_all 恢复全部文件", n == len(file_list), n)
    check(f"restore_all {d0}: 内容恢复", p0.read_text(encoding="utf-8") == f"v1-on-{d0}")
    check(
        f"restore_all {d1}: 已删除文件重建",
        pathlib.Path(files[d1]).read_text(encoding="utf-8") == f"v1-on-{d1}",
    )

    # ---------- 汇总 ----------
    fails = [r for r in results if not r[1]]
    total = len(results)
    print(f"\n=== SUMMARY: {total - len(fails)}/{total} passed ===", flush=True)
    if fails:
        for name, _ in fails:
            print(f"FAILED: {name}", flush=True)
        return 1
    return 0


def cleanup():
    """清理测试产物（-c cleanup 参数时执行）"""
    for d in DRIVES:
        for sub in (f"{d}:\\peng_test_1847", f"{d}:\\.peng\\backups\\task#{TASK_ID}"):
            try:
                shutil.rmtree(sub, ignore_errors=True)
                print("removed:", sub, flush=True)
            except Exception as e:
                print("remove failed:", sub, e, flush=True)
    default_repo = pathlib.Path(r"D:\work\peng\data\file_backups") / f"task#{TASK_ID}"
    try:
        shutil.rmtree(default_repo, ignore_errors=True)
        print("removed:", default_repo, flush=True)
    except Exception as e:
        print("remove failed:", default_repo, e, flush=True)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "cleanup":
        cleanup()
        sys.exit(0)
    sys.exit(main())
