import subprocess, sys
from pathlib import Path
r = subprocess.run(
    [sys.executable, "D:/work/peng/bin/msg", "list"],
    capture_output=True, encoding="utf-8", errors="replace", timeout=10,
    cwd="D:/work/peng",
)
Path("D:/work/peng/logs/msg_list.log").write_text(
    f"stdout:\n{r.stdout}\nstderr:\n{r.stderr}", encoding="utf-8"
)
