import subprocess, sys
from pathlib import Path

results = []

r = subprocess.run(["netstat", "-an"], capture_output=True, text=True, encoding="utf-8", errors="replace")
for l in r.stdout.split("\n"):
    if "8899" in l:
        results.append(l.strip())
        
# 检查nest进程
try:
    import psutil
    for proc in psutil.process_iter(['pid', 'name', 'cmdline', 'username']):
        try:
            cmdline = " ".join(proc.info.get('cmdline') or [])
            if 'nest' in cmdline.lower() and 'test_check' not in cmdline:
                results.append(f"PID={proc.info['pid']} user={proc.info.get('username')} cmd={cmdline[:120]}")
        except:
            pass
except:
    pass

Path("D:/work/peng/logs/check_nest.log").write_text("\n".join(results), encoding="utf-8")
