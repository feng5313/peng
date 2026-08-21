"""
peng.tags - 为工作目录中的工程代码生成tags文件（不依赖大模型）

利用正则表达式，在远端的data目录生成<workdir>_tags文件，
包含工作目录中工程代码的变量、函数、类、宏定义、包名、namespace等tag。

用法:
  tags <workdir> [--output <output_dir>] [-l <lang> ...]

示例:
  tags /home/user/project
  tags /home/user/project --output /tmp/tags_output
  tags /home/user/project -l python -l go
  tags /home/user/project -l .py -l .rs
"""

import sys
import os
import re
import argparse
import pathspec
from pathlib import Path
from collections import defaultdict

# 代码文件扩展名到语言类型的映射
LANGUAGE_MAP = {
    '.py': 'python',
    '.js': 'javascript',
    '.ts': 'typescript',
    '.tsx': 'typescript',
    '.jsx': 'javascript',
    '.go': 'go',
    '.rs': 'rust',
    '.c': 'c',
    '.h': 'c',
    '.cpp': 'cpp',
    '.cc': 'cpp',
    '.cxx': 'cpp',
    '.hpp': 'cpp',
    '.hxx': 'cpp',
    '.java': 'java',
    '.kt': 'kotlin',
    '.lua': 'lua',
    '.zig': 'zig',
    '.rb': 'ruby',
    '.php': 'php',
    '.sh': 'shell',
    '.bash': 'shell',
}

# 忽略的目录
IGNORE_DIRS = {
    '.git', '__pycache__', 'node_modules', '.venv', 'venv',
    '.mypy_cache', '.ruff_cache', '.pytest_cache', 'dist',
    'build', '.tox', '.eggs', '*.egg-info', 'target',
    '.cargo', '.rustup', 'vendor',
}

# ============ 正则表达式模式 ============

# Python
PYTHON_PATTERNS = [
    (re.compile(r'^(\s*)(class\s+(\w+))'), 'class'),
    (re.compile(r'^(\s*)(async\s+)?def\s+(\w+)'), 'function'),
    (re.compile(r'^(\s*)(\w+)\s*=\s*'), 'variable'),  # 模块级变量
]

# C/C++
C_CPP_PATTERNS = [
    (re.compile(r'^\s*#\s*define\s+(\w+)'), 'macro'),
    (re.compile(r'^\s*typedef\s+.*\s+(\w+)\s*;'), 'typedef'),
    (re.compile(r'^\s*(?:template\s*<[^>]*>\s*)?(?:class|struct)\s+(\w+)'), 'class'),
    (re.compile(r'^\s*(?:inline\s+)?(?:static\s+)?(?:[\w:*&<>]+\s+)+(\w+)\s*\([^)]*\)\s*(?:const\s*)?(?:\{|$)'), 'function'),
    (re.compile(r'^\s*namespace\s+(\w+)'), 'namespace'),
]

# Go
GO_PATTERNS = [
    (re.compile(r'^\s*func\s+(?:\(\w+\s+\*?\w+\)\s+)?(\w+)'), 'function'),
    (re.compile(r'^\s*type\s+(\w+)\s+struct'), 'struct'),
    (re.compile(r'^\s*type\s+(\w+)\s+interface'), 'interface'),
    (re.compile(r'^\s*package\s+(\w+)'), 'package'),
    (re.compile(r'^\s*const\s+(\w+)\s*='), 'constant'),
    (re.compile(r'^\s*var\s+(\w+)\s*='), 'variable'),
]

# Rust
RUST_PATTERNS = [
    (re.compile(r'^\s*(?:pub\s+)?(?:async\s+)?fn\s+(\w+)'), 'function'),
    (re.compile(r'^\s*(?:pub\s+)?struct\s+(\w+)'), 'struct'),
    (re.compile(r'^\s*(?:pub\s+)?enum\s+(\w+)'), 'enum'),
    (re.compile(r'^\s*(?:pub\s+)?trait\s+(\w+)'), 'trait'),
    (re.compile(r'^\s*(?:pub\s+)?mod\s+(\w+)'), 'module'),
    (re.compile(r'^\s*(?:pub\s+)?const\s+(\w+)'), 'constant'),
    (re.compile(r'^\s*(?:pub\s+)?static\s+(\w+)'), 'static'),
    (re.compile(r'^\s*(?:pub\s+)?type\s+(\w+)'), 'type'),
]

# Java/Kotlin
JAVA_PATTERNS = [
    (re.compile(r'^\s*(?:public|private|protected)?\s*(?:static\s+)?(?:final\s+)?(?:abstract\s+)?class\s+(\w+)'), 'class'),
    (re.compile(r'^\s*(?:public|private|protected)?\s*(?:static\s+)?(?:final\s+)?(?:abstract\s+)?interface\s+(\w+)'), 'interface'),
    (re.compile(r'^\s*(?:public|private|protected)?\s*(?:static\s+)?(?:final\s+)?(?:abstract\s+)?(?:synchronized\s+)?(?:[\w<>\[\],\s]+\s+)(\w+)\s*\('), 'method'),
    (re.compile(r'^\s*package\s+([\w.]+)'), 'package'),
]

# JavaScript/TypeScript
JS_TS_PATTERNS = [
    (re.compile(r'^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+(\w+)'), 'function'),
    (re.compile(r'^\s*(?:export\s+)?(?:default\s+)?class\s+(\w+)'), 'class'),
    (re.compile(r'^\s*(?:export\s+)?(?:const|let|var)\s+(\w+)'), 'variable'),
    (re.compile(r'^\s*(?:export\s+)?(?:async\s+)?(\w+)\s*[=:]\s*(?:async\s+)?(?:function|[(])'), 'function'),  # 箭头函数/函数表达式
    (re.compile(r'^\s*(?:export\s+)?interface\s+(\w+)'), 'interface'),
    (re.compile(r'^\s*(?:export\s+)?type\s+(\w+)'), 'type'),
    (re.compile(r'^\s*(?:export\s+)?enum\s+(\w+)'), 'enum'),
]

# Lua
LUA_PATTERNS = [
    (re.compile(r'^\s*(?:local\s+)?function\s+(\w+)'), 'function'),
    (re.compile(r'^\s*(?:local\s+)?(\w+)\s*=\s*(?:function|\{)'), 'variable'),
    (re.compile(r'^\s*module\s*=\s*"([\w.]+)"'), 'module'),
]

# Zig
ZIG_PATTERNS = [
    (re.compile(r'^\s*pub\s+fn\s+(\w+)'), 'function'),
    (re.compile(r'^\s*pub\s+const\s+(\w+)'), 'constant'),
    (re.compile(r'^\s*pub\s+var\s+(\w+)'), 'variable'),
]

# 按语言类型分组的模式
PATTERNS_BY_LANG = {
    'python': PYTHON_PATTERNS,
    'c': C_CPP_PATTERNS,
    'cpp': C_CPP_PATTERNS,
    'go': GO_PATTERNS,
    'rust': RUST_PATTERNS,
    'java': JAVA_PATTERNS,
    'kotlin': JAVA_PATTERNS,
    'javascript': JS_TS_PATTERNS,
    'typescript': JS_TS_PATTERNS,
    'lua': LUA_PATTERNS,
    'zig': ZIG_PATTERNS,
}


def resolve_language(lang_spec: str) -> str | None:
    """将语言名称或文件后缀解析为标准语言名称
    
    支持的输入格式:
      - 语言名称: 'python', 'go', 'rust', 'javascript', 'typescript', 'c', 'cpp', 
                  'java', 'kotlin', 'lua', 'zig', 'ruby', 'php', 'shell'
      - 文件后缀: '.py', '.go', '.rs', '.js', '.ts', '.c', '.cpp', '.java', '.kt',
                  '.lua', '.zig', '.rb', '.php', '.sh'
    """
    # 尝试作为后缀匹配
    if lang_spec.startswith('.') or lang_spec in {s for s in LANGUAGE_MAP}:
        return LANGUAGE_MAP.get(lang_spec)
    # 尝试作为语言名称匹配（在 PATTERNS_BY_LANG 中查找）
    lang_lower = lang_spec.lower()
    if lang_lower in PATTERNS_BY_LANG:
        return lang_lower
    # 兼容别名
    aliases = {
        'js': 'javascript', 'ts': 'typescript', 'py': 'python',
        'rb': 'ruby', 'sh': 'shell', 'bash': 'shell',
    }
    if lang_lower in aliases:
        return aliases[lang_lower]
    return None


def load_gitignore_specs(workdir: Path) -> dict[Path, pathspec.PathSpec]:
    """递归加载工作目录及子目录中的 .gitignore 规则
    
    Returns:
        dict: {目录Path: pathspec.PathSpec} 映射，每个 .gitignore 文件对应的目录和规则
    """
    specs = {}
    for gitignore in workdir.rglob('.gitignore'):
        if not gitignore.is_file():
            continue
        gitignore_dir = gitignore.parent
        try:
            with open(gitignore, encoding='utf-8', errors='ignore') as f:
                patterns = f.read().splitlines()
            # 过滤空行和注释
            patterns = [p.rstrip() for p in patterns if p and not p.startswith('#')]
            if patterns:
                specs[gitignore_dir] = pathspec.PathSpec.from_lines('gitwildmatch', patterns)
        except Exception as e:
            print(f"Warning: Failed to parse {gitignore}: {e}", file=sys.stderr)
    return specs


def is_gitignored(filepath: Path, workdir: Path, gitignore_specs: dict[Path, pathspec.PathSpec]) -> bool:
    """检查文件是否被任何 .gitignore 规则匹配
    
    遍历所有 gitignore 规则，检查文件相对于对应 .gitignore 所在目录的相对路径是否匹配
    """
    for gitignore_dir, spec in gitignore_specs.items():
        try:
            rel_path = filepath.relative_to(gitignore_dir)
        except ValueError:
            continue
        rel_str = str(rel_path)
        if spec.match_file(rel_str):
            return True
    return False


def should_ignore(path: Path) -> bool:
    """检查路径是否应该被忽略"""
    for part in path.parts:
        for pattern in IGNORE_DIRS:
            if '*' in pattern:
                import fnmatch
                if fnmatch.fnmatch(part, pattern):
                    return True
            elif part == pattern:
                return True
    return False


def collect_files(workdir: Path, languages: list[str] | None = None) -> dict[str, list[Path]]:
    """收集工作目录中的代码文件，按语言分组
    
    Args:
        workdir: 工作目录路径
        languages: 指定要扫描的语言列表（标准语言名称），None表示扫描所有语言
    """
    # 将指定语言转换为小写集合以便快速查找
    lang_filter = set(l.lower() for l in languages) if languages else None
    
    # 加载 .gitignore 规则
    gitignore_specs = load_gitignore_specs(workdir)
    if gitignore_specs:
        print(f"  加载了 {len(gitignore_specs)} 个 .gitignore 规则文件")
    
    files_by_lang = defaultdict(list)
    for filepath in workdir.rglob('*'):
        if should_ignore(filepath):
            continue
        if not filepath.is_file():
            continue
        # 检查 .gitignore 规则
        if gitignore_specs and is_gitignored(filepath, workdir, gitignore_specs):
            continue
        ext = filepath.suffix
        lang = LANGUAGE_MAP.get(ext)
        if lang and (lang_filter is None or lang in lang_filter):
            files_by_lang[lang].append(filepath)
    return files_by_lang


def extract_tags(filepath: Path, lang: str) -> list[dict]:
    """从文件中提取tags"""
    patterns = PATTERNS_BY_LANG.get(lang, [])
    if not patterns:
        return []

    tags = []
    try:
        with open(filepath, encoding='utf-8', errors='ignore') as f:
            for line_num, line in enumerate(f, 1):
                stripped = line.rstrip('\n\r')
                for pattern, tag_type in patterns:
                    m = pattern.match(stripped)
                    if m:
                        # 取最后一个捕获组（名称）
                        name = m.group(m.lastindex)
                        if name and not name.startswith('__'):
                            rel_path = filepath
                            try:
                                rel_path = filepath.relative_to(filepath.anchor)
                            except ValueError:
                                pass
                            tags.append({
                                'name': name,
                                'type': tag_type,
                                'file': str(filepath),
                                'line': line_num,
                                'lang': lang,
                            })
    except Exception as e:
        print(f"Warning: Failed to read {filepath}: {e}", file=sys.stderr)
    return tags


def generate_tags_file(workdir: str, output_dir: str = None, languages: list[str] | None = None) -> str:
    """生成tags文件
    
    Args:
        workdir: 工作目录路径
        output_dir: 输出目录
        languages: 指定要扫描的语言列表（标准语言名称），None表示扫描所有语言
    """
    workdir = Path(workdir).resolve()
    if not workdir.exists():
        print(f"Error: 工作目录不存在: {workdir}", file=sys.stderr)
        sys.exit(1)

    if languages:
        print(f"正在扫描 {workdir} (语言: {', '.join(languages)}) ...")
    else:
        print(f"正在扫描 {workdir} ...")

    files_by_lang = collect_files(workdir, languages=languages)
    total_files = sum(len(f) for f in files_by_lang.values())
    print(f"找到 {total_files} 个代码文件 ({len(files_by_lang)} 种语言)")

    all_tags = []
    for lang, files in sorted(files_by_lang.items()):
        print(f"  处理 {lang}: {len(files)} 个文件...")
        for filepath in sorted(files):
            tags = extract_tags(filepath, lang)
            all_tags.extend(tags)

    # 提取去重的tag名称
    seen = set()
    unique_names = []
    for tag in all_tags:
        name = tag['name']
        if name not in seen:
            seen.add(name)
            unique_names.append(name)
    unique_names.sort(key=str.lower)

    # 生成输出文件路径
    if output_dir:
        out_dir = Path(output_dir)
    else:
        out_dir = workdir / 'data'
    out_dir.mkdir(parents=True, exist_ok=True)

    # 生成文件名: <workdir_basename>_tags
    workdir_name = workdir.name.replace('/', '_').replace('\\', '_')
    out_file = out_dir / f"{workdir_name}_tags"

    # 写入tags文件（仅tag名称，空格分隔）
    with open(out_file, 'w', encoding='utf-8') as f:
        f.write(' '.join(unique_names) + '\n')

    print(f"\n生成完成: {out_file}")
    print(f"共 {len(all_tags)} 个tags")
    for lang in sorted(files_by_lang.keys()):
        count = sum(1 for t in all_tags if t['lang'] == lang)
        type_counts = defaultdict(int)
        for t in all_tags:
            if t['lang'] == lang:
                type_counts[t['type']] += 1
        type_str = ', '.join(f"{k}:{v}" for k, v in sorted(type_counts.items()))
        print(f"  {lang}: {count} tags ({type_str})")

    return str(out_file)


def main():
    parser = argparse.ArgumentParser(description='为工作目录生成代码tags文件')
    parser.add_argument('workdir', help='工作目录路径')
    parser.add_argument('--output', '-o', help='输出目录（默认为<workdir>/data）')
    parser.add_argument('-l', '--language', action='append', default=None,
                        help='指定编程语言（语言名称如python/go，或后缀如.py/.rs），可重复指定多个')
    args = parser.parse_args()

    # 解析语言参数
    resolved_languages = None
    if args.language:
        resolved_languages = []
        for lang_spec in args.language:
            resolved = resolve_language(lang_spec)
            if resolved is None:
                print(f"Error: 不支持的语言或后缀: '{lang_spec}'", file=sys.stderr)
                supported = sorted(set(list(PATTERNS_BY_LANG.keys()) + list(LANGUAGE_MAP.keys())))
                print(f"支持的语言/后缀: {', '.join(supported)}", file=sys.stderr)
                sys.exit(1)
            if resolved not in resolved_languages:
                resolved_languages.append(resolved)

    generate_tags_file(args.workdir, args.output, languages=resolved_languages)
