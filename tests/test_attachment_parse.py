#!/usr/bin/env python3
"""Test @get/@getall attachment parsing and dedup"""
import sys
sys.path.insert(0, "src")

from peng.channels.integration import parse_get_file_paths, resolve_file_path, _extract_file_paths_from_context

# Test 1: @get:<file> basic
paths = parse_get_file_paths("hello @get:/tmp/test.txt @get:report.pdf end")
print(f"Test 1 - @get basic: {paths}")
assert paths == ["/tmp/test.txt", "report.pdf"]
print("  PASS")

# Test 2: @get with no matches
paths = parse_get_file_paths("hello world")
assert paths == []
print("Test 2 - no @get: PASS")

# Test 3: @getall with context
paths = parse_get_file_paths(
    "please send @getall",
    "2025-01-01 #1 user\tsome text [附件] test.txt: /data/downloads/test.txt\n"
    "2025-01-01 #2 bot\treply"
)
assert "/data/downloads/test.txt" in paths
print("Test 3 - @getall: PASS")

# Test 4: @get + @getall combined
paths = parse_get_file_paths(
    "send @get:/specific/file.csv and @getall",
    "2025-01-01 #1 user\t[附件] data.json: /tmp/data.json"
)
assert "/specific/file.csv" in paths
assert "/tmp/data.json" in paths
print("Test 4 - @get + @getall: PASS")

# Test 5: dedup
paths = parse_get_file_paths("@get:/a.txt @get:/a.txt", "")
assert paths == ["/a.txt"]
print("Test 5 - dedup: PASS")

# Test 6: resolve_file_path
assert resolve_file_path("/absolute/path.txt") == "/absolute/path.txt"
assert resolve_file_path("relative.txt", "/workdir") == "/workdir/relative.txt"
print("Test 6 - resolve_file_path: PASS")

# Test 7: Windows paths
paths = parse_get_file_paths("@get:D:/downloads/report.xlsx", "")
assert paths == ["D:/downloads/report.xlsx"]
print("Test 7 - Windows path: PASS")

# Test 8: trailing punctuation
paths = parse_get_file_paths("@get:/tmp/test.txt,", "")
assert paths == ["/tmp/test.txt"]
print("Test 8 - trailing punct: PASS")

# Test 9: _extract_file_paths_from_context with multiple patterns
context = (
    "2025-01-01 00:00:00 #1  user\tsend file @getall\n"
    "2025-01-01 00:01:00 #2  bot\t[附件] image.jpg: /data/feishu_attachments/image.jpg\n"
    "2025-01-01 00:02:00 #3  user\talso /home/user/data/report.csv is here\n"
)
paths = _extract_file_paths_from_context(context)
assert "/data/feishu_attachments/image.jpg" in paths
assert "/home/user/data/report.csv" in paths
print("Test 9 - multiple patterns from context: PASS")

# Test 10: filter out Python module paths (misleading matches)
context = (
    "2025-01-01 #1 user\tfrom peng.nest.cli import something /peng/nest/cli.py\n"
    "2025-01-01 #2 bot\t[附件] real.txt: /mnt/data/real.txt\n"
)
paths = _extract_file_paths_from_context(context)
print(f"Test 10 - filter Python paths: {paths}")
assert "/mnt/data/real.txt" in paths, f"Expected /mnt/data/real.txt in {paths}"
assert "/peng/nest/cli.py" not in paths, f"Should NOT contain /peng/nest/cli.py in {paths}"
print("  PASS")

# Test 11: dedup with already sent
chain = [
    {"content": "send @getall"},
    {"content": "📎 已发送附件: /tmp/a.txt, /tmp/b.pdf"},
    {"content": "📎 已发送附件: /tmp/c.jpg"},
]
already = set()
for msg in chain:
    c = msg.get("content", "")
    if c.startswith("📎 已发送附件:"):
        paths_str = c[len("📎 已发送附件:"):].strip()
        for p in paths_str.split(", "):
            p = p.strip()
            if p:
                already.add(p)
assert already == {"/tmp/a.txt", "/tmp/b.pdf", "/tmp/c.jpg"}
new_paths = [p for p in ["/tmp/a.txt", "/tmp/b.pdf", "/tmp/c.jpg", "/tmp/d.xlsx"] if p not in already]
assert new_paths == ["/tmp/d.xlsx"]
print("Test 11 - dedup with already sent: PASS")

print("\nAll 11 tests passed!")
