"""
.gitignore 解析模块

解析 .gitignore 文件并提供匹配检查功能。
支持查找从搜索目录向上遍历到 git 根目录的所有 .gitignore 文件，
并支持嵌套目录中的 .gitignore 规则。

参考 git 的 .gitignore 规则：
- 空行或 # 开头的行被忽略
- ! 开头的行表示取反（取消忽略）
- / 结尾的模式只匹配目录
- 包含 / 的模式相对于 .gitignore 文件所在目录
- 不包含 / 的模式匹配任意位置
- ** 匹配任意多级目录
- * 匹配任意字符但不匹配 /
- ? 匹配单个字符但不匹配 /
"""

import os
from pathlib import Path, PurePosixPath
from typing import NamedTuple


class GitIgnoreRule(NamedTuple):
    """单条 .gitignore 规则"""
    pattern: str       # 处理后的匹配模式
    base_dir: str      # .gitignore 文件所在目录的相对路径（相对于 git 根，空字符串表示根目录）
    negated: bool      # 是否取反（! 前缀）
    dir_only: bool     # 是否只匹配目录（/ 后缀）
    anchored: bool     # 是否锚定（模式中包含 / 或以 / 开头）


def _find_git_root(path: Path) -> Path | None:
    """从给定路径向上查找 git 根目录（包含 .git 的目录）"""
    current = path.resolve()
    while True:
        if (current / ".git").exists():
            return current
        parent = current.parent
        if parent == current:
            return None
        current = parent


def _parse_gitignore_file(gitignore_path: Path, git_root: Path) -> list[GitIgnoreRule]:
    """解析单个 .gitignore 文件，返回规则列表"""
    rules = []
    try:
        base_dir = ""
        if gitignore_path.parent != git_root:
            base_dir = str(PurePosixPath(gitignore_path.parent.relative_to(git_root)))
        
        with open(gitignore_path, encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.rstrip("\n").rstrip("\r")
                # 去掉行尾空格
                line = line.rstrip()
                # 跳过空行和注释
                if not line or line.startswith("#"):
                    continue
                
                negated = line.startswith("!")
                if negated:
                    line = line[1:]
                
                dir_only = line.endswith("/")
                if dir_only:
                    line = line.rstrip("/")
                
                # 去掉行首的 /（锚定标记，但 / 已被记录）
                anchored = "/" in line
                if line.startswith("/"):
                    line = line[1:]
                    anchored = True
                
                if not line:
                    continue
                
                rules.append(GitIgnoreRule(
                    pattern=line,
                    base_dir=base_dir,
                    negated=negated,
                    dir_only=dir_only,
                    anchored=anchored,
                ))
    except (OSError, IOError):
        pass
    return rules


def _collect_all_gitignore_rules(git_root: Path) -> list[GitIgnoreRule]:
    """收集 git 根目录下所有 .gitignore 文件的规则"""
    rules = []
    root_gitignore = git_root / ".gitignore"
    if root_gitignore.exists():
        rules.extend(_parse_gitignore_file(root_gitignore, git_root))
    
    try:
        for dirpath, dirnames, filenames in os.walk(git_root):
            # 跳过 .git 目录
            if ".git" in dirnames:
                dirnames.remove(".git")
            
            if ".gitignore" in filenames:
                gitignore_path = Path(dirpath) / ".gitignore"
                if gitignore_path != root_gitignore:
                    rules.extend(_parse_gitignore_file(gitignore_path, git_root))
    except OSError:
        pass
    
    return rules


def _fnmatch_path(pattern: str, path: str) -> bool:
    """
    使用 fnmatch 进行路径匹配，支持 ** 通配符。
    
    Args:
        pattern: gitignore 模式
        path: 相对路径字符串
    
    gitignore 中 ** 的语义：
    - **/foo 匹配任意目录下的 foo
    - foo/** 匹配 foo 下的所有内容
    - foo/**/bar 匹配 foo 下面任意层级的 bar
    - a/**/b/**/c 匹配 a 下面任意层级有 b，b 下面任意层级有 c
    """
    import fnmatch
    
    if "**" not in pattern:
        return fnmatch.fnmatch(path, pattern)
    
    # 将 pattern 和 path 按 / 分割
    pat_parts = pattern.split("/")
    path_parts = path.split("/")
    
    return _match_parts(pat_parts, 0, path_parts, 0)


def _match_parts(pat_parts: list[str], pi: int, path_parts: list[str], si: int) -> bool:
    """递归匹配 ** 模式"""
    import fnmatch
    
    # 跳过连续的空字符串（如 "a//b"）
    while pi < len(pat_parts) and pat_parts[pi] == "":
        pi += 1
    while si < len(path_parts) and path_parts[si] == "":
        si += 1
    
    # 模式用完
    if pi >= len(pat_parts):
        return si >= len(path_parts)
    
    # 路径用完但模式还有
    if si >= len(path_parts):
        # 剩余模式都是 ** 才算匹配
        return all(p == "**" for p in pat_parts[pi:])
    
    pat = pat_parts[pi]
    
    if pat == "**":
        # ** 可以匹配零个或多个路径段
        # 尝试匹配 0, 1, 2, ... 个路径段
        # 先跳过连续的 **
        next_pi = pi + 1
        while next_pi < len(pat_parts) and pat_parts[next_pi] == "**":
            next_pi += 1
        
        # 如果 ** 是最后一个模式段，匹配所有剩余路径
        if next_pi >= len(pat_parts):
            return True
        
        # 尝试从不同位置匹配下一个模式段
        for skip in range(si, len(path_parts) + 1):
            if _match_parts(pat_parts, next_pi, path_parts, skip):
                return True
        return False
    
    # 普通模式段：用 fnmatch 匹配一个路径段
    if fnmatch.fnmatch(path_parts[si], pat):
        return _match_parts(pat_parts, pi + 1, path_parts, si + 1)
    
    return False


def _match_rule(rule: GitIgnoreRule, rel_to_base: str, is_dir: bool) -> bool:
    """
    检查路径是否匹配规则。
    
    Args:
        rule: gitignore 规则
        rel_to_base: 相对于 .gitignore 文件所在目录的路径
        is_dir: 是否为目录
    """
    if rule.anchored:
        # 锚定模式：只匹配相对于 .gitignore 所在目录的路径
        if rule.dir_only:
            if is_dir:
                return _fnmatch_path(rule.pattern, rel_to_base)
            else:
                return False
        else:
            return _fnmatch_path(rule.pattern, rel_to_base)
    else:
        # 非锚定模式：匹配路径的任意部分
        base_parts = rel_to_base.split("/")
        for i in range(len(base_parts)):
            subpath = "/".join(base_parts[i:])
            if rule.dir_only:
                # dir_only 模式只匹配目录
                # subpath 是一个目录当且仅当它不是最后一个部分，
                # 或者路径本身是目录且 i == 0
                is_subpath_dir = (i < len(base_parts) - 1) or (is_dir and i == len(base_parts) - 1)
                if is_subpath_dir and _fnmatch_path(rule.pattern, subpath):
                    return True
            else:
                if _fnmatch_path(rule.pattern, subpath):
                    return True
        return False


def _check_path_against_rules(
    rel_to_base: str,
    is_dir: bool,
    rules: list[GitIgnoreRule],
) -> bool | None:
    """
    对单条相对路径检查所有规则。
    
    Returns:
        True=被忽略, False=被取反, None=无匹配
    """
    result = None
    for rule in rules:
        if _match_rule(rule, rel_to_base, is_dir):
            result = not rule.negated
    return result


def is_ignored(
    path: Path,
    git_root: Path,
    rules: list[GitIgnoreRule],
) -> bool:
    """
    检查给定路径是否被 gitignore 规则忽略。
    
    检查逻辑：不仅检查路径本身，还检查其每个父目录。
    如果某个父目录被 dir_only 规则忽略，则其下所有内容也应被忽略。
    
    Args:
        path: 要检查的绝对路径
        git_root: git 根目录
        rules: gitignore 规则列表
    
    Returns:
        是否被忽略
    """
    try:
        rel_to_root = str(PurePosixPath(path.relative_to(git_root)))
    except ValueError:
        return False
    
    is_dir = path.is_dir()
    parts = rel_to_root.split("/")
    
    # 遍历路径的每个层级（从根到叶），检查是否有父目录被忽略
    for depth in range(1, len(parts) + 1):
        sub_rel = "/".join(parts[:depth])
        sub_is_dir = depth < len(parts) or is_dir
        
        # 计算相对于每条规则的 base_dir 的路径
        result = None
        for rule in rules:
            if rule.base_dir:
                rule_dir_prefix = rule.base_dir + "/"
                if not sub_rel.startswith(rule_dir_prefix) and sub_rel != rule.base_dir:
                    continue
                rel_to_base = sub_rel[len(rule.base_dir):].lstrip("/")
                if not rel_to_base:
                    continue
            else:
                rel_to_base = sub_rel
            
            if _match_rule(rule, rel_to_base, sub_is_dir):
                result = not rule.negated
        
        if result is True:
            return True
        elif result is False:
            # 取反规则，继续检查更深层级
            continue
    
    return False


class GitIgnoreFilter:
    """
    .gitignore 过滤器
    
    解析搜索路径下所有 .gitignore 文件，提供路径过滤功能。
    如果搜索路径不在 git 仓库内，则不做任何过滤。
    
    用法:
        gitignore_filter = GitIgnoreFilter(search_path)
        if gitignore_filter.should_ignore(file_path):
            continue
    """
    
    def __init__(self, search_path: Path):
        self._search_path = search_path.resolve()
        self._rules: list[GitIgnoreRule] = []
        self._git_root: Path | None = None
        self._initialized = False
        self._has_gitignore = False
        self._init()
    
    def _init(self):
        """初始化：查找 git 根目录并收集所有 gitignore 规则"""
        if self._initialized:
            return
        self._initialized = True
        
        git_root = _find_git_root(self._search_path)
        if git_root is None:
            return
        
        self._git_root = git_root
        rules = _collect_all_gitignore_rules(git_root)
        if rules:
            self._rules = rules
            self._has_gitignore = True
    
    @property
    def has_gitignore(self) -> bool:
        """是否存在 gitignore 规则"""
        return self._has_gitignore
    
    @property
    def git_root(self) -> Path | None:
        """git 根目录"""
        return self._git_root
    
    def should_ignore(self, path: Path) -> bool:
        """
        检查路径是否应被忽略
        
        Args:
            path: 要检查的路径
        
        Returns:
            True 表示应忽略
        """
        if not self._rules or not self._git_root:
            return False
        
        if not path.is_absolute():
            path = self._search_path / path
        
        return is_ignored(path, self._git_root, self._rules)
    
    def filter_paths(self, paths: list[Path]) -> list[Path]:
        """过滤路径列表，返回不被忽略的路径"""
        if not self._rules:
            return paths
        return [p for p in paths if not self.should_ignore(p)]
    
    def make_walk_filter(self):
        """
        返回用于 os.walk 的目录过滤函数。
        """
        git_root = self._git_root
        rules = self._rules
        
        def filter_dirs(dirpath: str, dirnames: list[str]) -> list[str]:
            if not rules or not git_root:
                return dirnames
            parent = Path(dirpath)
            result = []
            for d in dirnames:
                full_path = parent / d
                if not is_ignored(full_path, git_root, rules):
                    result.append(d)
            return result
        
        return filter_dirs
