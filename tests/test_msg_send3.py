import subprocess, sys, os

msg = "用shot工具截取全屏并识别有哪些在运行的程序窗口"

r = subprocess.run(
    [sys.executable, "D:/work/peng/bin/msg", "send", msg],
    capture_output=True, encoding="utf-8", errors="replace", timeout=10,
    cwd="D:/work/peng",
)
with open("D:/work/peng/logs/msg_send3.log", "w", encoding="utf-8") as f:
    f.write(f"rc={r.returncode}\nstdout:\n{r.stdout}\nstderr:\n{r.stderr}")
