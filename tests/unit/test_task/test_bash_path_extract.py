"""
测试 bash/cmd 命令文件路径解析（事务#1847 第八轮）

生产事故：task#1886 用 bash `del D:\\work\\README.md` 删除文件无备份——
_extract_file_paths 旧实现"bash 无法确定具体文件"直接返回空。
新实现：动词（rm/del/mv/cp/move/copy/ren/...）参数 + 重定向目标 + 通配符
glob 展开 + 存在性过滤（仅备份真实存在的对象原版）。
"""
from pathlib import Path

from peng.service.tool_backup import ToolBackupManager


def test_rm_relative():
    assert ToolBackupManager._extract_paths_from_command("rm readme.md", "/wd") == []


def test_rm_existing_local(tmp_path: Path):
    f = tmp_path / "README.md"
    f.write_text("x", encoding="utf-8")
    out = ToolBackupManager._extract_paths_from_command(
        f"del {f}", str(tmp_path)
    )
    assert out == [str(f)]


def test_rm_with_options_and_abs(tmp_path: Path):
    f = tmp_path / "a.txt"
    f.write_text("1", encoding="utf-8")
    out = ToolBackupManager._extract_paths_from_command(
        f"del /q /f {f}", None
    )
    assert out == [str(f)]


def test_rm_relative_resolved_against_workdir(tmp_path: Path):
    (tmp_path / "sub").mkdir()
    f = tmp_path / "sub" / "b.txt"
    f.write_text("2", encoding="utf-8")
    out = ToolBackupManager._extract_paths_from_command(
        "rm -f sub/b.txt", str(tmp_path)
    )
    assert out == [str(f)]


def test_move_collects_src_and_dst(tmp_path: Path):
    src = tmp_path / "s.txt"
    dst = tmp_path / "d.txt"
    src.write_text("s", encoding="utf-8")
    dst.write_text("d-old", encoding="utf-8")
    out = ToolBackupManager._extract_paths_from_command(
        "move s.txt d.txt", str(tmp_path)
    )
    assert sorted(out) == sorted([str(src), str(dst)])


def test_redirect_target_collected(tmp_path: Path):
    f = tmp_path / "out.log"
    f.write_text("old", encoding="utf-8")
    out = ToolBackupManager._extract_paths_from_command(
        "echo new > out.log", str(tmp_path)
    )
    assert out == [str(f)]
    out2 = ToolBackupManager._extract_paths_from_command(
        "echo more >> out.log", str(tmp_path)
    )
    assert out2 == [str(f)]


def test_glob_pattern_expanded(tmp_path: Path):
    for name in ("x1.tmp", "x2.tmp"):
        (tmp_path / name).write_text(name, encoding="utf-8")
    (tmp_path / "keep.txt").write_text("k", encoding="utf-8")
    out = ToolBackupManager._extract_paths_from_command(
        "rm *.tmp", str(tmp_path)
    )
    assert sorted(Path(p).name for p in out) == ["x1.tmp", "x2.tmp"]


def test_quoted_path_with_spaces(tmp_path: Path):
    f = tmp_path / "my file.txt"
    f.write_text("sp", encoding="utf-8")
    out = ToolBackupManager._extract_paths_from_command(
        f'del "my file.txt"', str(tmp_path)
    )
    assert out == [str(f)]


def test_multiple_segments(tmp_path: Path):
    a = tmp_path / "a.log"
    b = tmp_path / "b.log"
    a.write_text("a", encoding="utf-8")
    b.write_text("b", encoding="utf-8")
    out = ToolBackupManager._extract_paths_from_command(
        "del a.log && rm b.log", str(tmp_path)
    )
    assert sorted(p.endswith("a.log") or p.endswith("b.log") for p in out) == [True, True]


def test_pipe_not_treated_as_verb_args(tmp_path: Path):
    """管道右侧命令的参数不误收（cat x | grep y 的 grep 非动词）"""
    f = tmp_path / "c.txt"
    f.write_text("c", encoding="utf-8")
    out = ToolBackupManager._extract_paths_from_command(
        "type c.txt | findstr hello", str(tmp_path)
    )
    assert out == []  # type/findstr 非修改动词


def test_nonexistent_target_skipped(tmp_path: Path):
    """不存在的文件（将被新建）无需备份原版"""
    out = ToolBackupManager._extract_paths_from_command(
        "rm newfile_not_exist.txt", str(tmp_path)
    )
    assert out == []


def test_extract_entrypoint_bash(tmp_path: Path):
    f = tmp_path / "z.md"
    f.write_text("z", encoding="utf-8")
    out = ToolBackupManager._extract_file_paths(
        "bash", {"command": f"del {f}"}, str(tmp_path)
    )
    assert out == [str(f)]


def test_script_execution_extracts_inner_paths(tmp_path: Path):
    """执行脚本的命令：读取脚本内容提取内部涉及路径"""
    data = tmp_path / "data.bin"
    data.write_text("D", encoding="utf-8")
    script = tmp_path / "clean.py"
    script.write_text(
        "import os\nos.remove('data.bin')\n", encoding="utf-8"
    )
    out = ToolBackupManager._extract_paths_from_command(
        f"python {script}", str(tmp_path)
    )
    assert str(data) in out


def test_script_relative_paths_both_bases(tmp_path: Path):
    """脚本内相对路径以 workdir 与脚本目录双基准解析"""
    sub = tmp_path / "pkg"
    sub.mkdir()
    target = sub / "cfg.ini"
    target.write_text("[x]", encoding="utf-8")
    script = tmp_path / "run.sh"
    script.write_text("rm pkg/cfg.ini\n", encoding="utf-8")
    out = ToolBackupManager._extract_paths_from_command(
        f"bash {script}", str(tmp_path)
    )
    assert str(target) in out


def test_script_module_names_not_misreported(tmp_path: Path):
    """脚本内 os.path 等模块样式词不会误报（存在性过滤兜底）"""
    script = tmp_path / "s.py"
    script.write_text("import os.path\nprint(os.path.join('a', 'b'))\n", encoding="utf-8")
    out = ToolBackupManager._extract_paths_from_command(
        f"python {script}", str(tmp_path)
    )
    assert all(p != "os.path" for p in out)
