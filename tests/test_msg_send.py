import subprocess, sys, os

r = subprocess.run(
    [sys.executable, "D:/work/peng/bin/msg", "send", "test screen"],
    capture_output=True, encoding="utf-8", errors="replace", timeout=10,
    cwd="D:/work/peng",
)
# 用utf-8写文件，避免gbk编码问题
with open("D:/work/peng/logs/msg_send.log", "w", encoding="utf-8") as f:
    f.write(f"rc={r.returncode}\nstdout:\n{r.stdout}\nstderr:\n{r.stderr}")
