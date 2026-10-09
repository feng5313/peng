import subprocess, sys, time
from pathlib import Path

# 先杀掉所有nest进程
try:
    import psutil
    for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
        try:
            cmdline = " ".join(proc.info.get('cmdline') or [])
            if 'nest' in cmdline.lower() and 'test_' not in cmdline:
                proc.kill()
                Path("D:/work/peng/logs/nest_start.log").write_text(
                    f"killed PID={proc.info['pid']}", encoding="utf-8"
                )
        except:
            pass
except:
    pass

time.sleep(2)

# 检查端口是否释放
r = subprocess.run(["netstat", "-an"], capture_output=True, text=True, encoding="utf-8", errors="replace")
port_busy = any("8899" in l and "LISTENING" in l for l in r.stdout.split("\n"))

if port_busy:
    Path("D:/work/peng/logs/nest_start.log").write_text("port 8899 still busy!", encoding="utf-8")
    sys.exit(1)

# 用pythonw启动nest（无窗口）
pythonw = str(Path("D:/work/peng/.venv/Scripts/pythonw.exe"))
if not Path(pythonw).exists():
    pythonw = str(Path("D:/work/peng/.venv/Scripts/python.exe"))

nest_script = str(Path("D:/work/peng/bin/nest"))
log_file = str(Path("D:/work/peng/logs/nest_service.log"))

Path(log_file).write_text("", encoding="utf-8")

proc = subprocess.Popen(
    [pythonw, nest_script, "serve"],
    stdout=open(log_file, "a"),
    stderr=subprocess.STDOUT,
    cwd="D:/work/peng",
    creationflags=subprocess.DETACHED_PROCESS | getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0),
    close_fds=True,
)

time.sleep(3)

if proc.poll() is None:
    import os
    Path("D:/work/peng/logs/nest_start.log").write_text(
        f"OK: PID={proc.pid} user={os.environ.get('USERNAME')} python={pythonw}", encoding="utf-8"
    )
else:
    Path("D:/work/peng/logs/nest_start.log").write_text(
        f"FAIL: exit={proc.returncode}\n{Path(log_file).read_text(encoding='utf-8', errors='replace')[:500]}", encoding="utf-8"
    )
