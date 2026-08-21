"""
Nest 安全控制

管理目录白名单/黑名单、命令白名单/黑名单，确保远程操作安全。

规则：
1. 如果 dir_whitelist 非空，只允许访问白名单中的目录
2. 如果 dir_blacklist 非空，禁止访问黑名单中的目录
3. 如果 cmd_whitelist 非空，只允许执行白名单中的命令
4. 如果 cmd_blacklist 非空，禁止执行黑名单中的命令
5. 白名单和黑名单同时存在时，先检查白名单再检查黑名单
"""

from __future__ import annotations

import fnmatch
import logging
import os
import re
import shutil
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class NestSecurity:
    """Nest 安全控制"""
    
    def __init__(
        self,
        dir_whitelist: list[str] | None = None,
        dir_blacklist: list[str] | None = None,
        cmd_whitelist: list[str] | None = None,
        cmd_blacklist: list[str] | None = None,
        enable_window_query: bool = True,
        enable_screenshot: bool = True,
        enable_input: bool = True,
    ):
        """
        初始化安全控制
        
        Args:
            dir_whitelist: 目录白名单（空列表或None表示允许所有）
            dir_blacklist: 目录黑名单
            cmd_whitelist: 命令白名单（空列表或None表示允许所有）
            cmd_blacklist: 命令黑名单
            enable_window_query: 是否允许窗口查询
            enable_screenshot: 是否允许截屏
            enable_input: 是否允许远程输入（鼠标/键盘）
        """
        self.dir_whitelist = [self._normalize_path(p) for p in (dir_whitelist or [])]
        self.dir_blacklist = [self._normalize_path(p) for p in (dir_blacklist or [])]
        self.cmd_whitelist = [p.lower() for p in (cmd_whitelist or [])]
        self.cmd_blacklist = [p.lower() for p in (cmd_blacklist or [])]
        self.enable_window_query = enable_window_query
        self.enable_screenshot = enable_screenshot
        self.enable_input = enable_input
    
    @staticmethod
    def _normalize_path(path: str) -> str:
        """标准化路径（展开用户目录、转为绝对路径）"""
        p = Path(path).expanduser()
        if not p.is_absolute():
            p = Path.cwd() / p
        try:
            return str(p.resolve())
        except Exception:
            return str(p)
    
    def check_path_access(self, path: str) -> tuple[bool, str]:
        """
        检查路径是否允许访问
        
        Args:
            path: 要检查的路径
            
        Returns:
            (是否允许, 原因)
        """
        try:
            normalized = self._normalize_path(path)
        except Exception:
            return False, f"无效的路径: {path}"
        
        # 检查黑名单
        for blocked in self.dir_blacklist:
            if normalized.startswith(blocked) or blocked.startswith(normalized):
                return False, f"路径被黑名单禁止: {path} (匹配规则: {blocked})"
        
        # 检查白名单（非空时）
        if self.dir_whitelist:
            allowed = False
            for whitelisted in self.dir_whitelist:
                if normalized.startswith(whitelisted) or whitelisted.startswith(normalized):
                    allowed = True
                    break
            if not allowed:
                return False, f"路径不在白名单中: {path}"
        
        return True, "允许访问"
    
    def check_command(self, command: str) -> tuple[bool, str]:
        """
        检查命令是否允许执行
        
        Args:
            command: 要检查的命令字符串
            
        Returns:
            (是否允许, 原因)
        """
        # 提取命令名（第一个词）
        cmd_name = command.strip().split()[0].lower() if command.strip() else ""
        
        # 去除路径前缀，只保留命令名
        cmd_basename = os.path.basename(cmd_name)
        
        # 检查黑名单
        for blocked in self.cmd_blacklist:
            if fnmatch.fnmatch(cmd_basename, blocked) or fnmatch.fnmatch(cmd_name, blocked):
                return False, f"命令被黑名单禁止: {cmd_basename} (匹配规则: {blocked})"
        
        # 检查白名单（非空时）
        if self.cmd_whitelist:
            allowed = False
            for whitelisted in self.cmd_whitelist:
                if fnmatch.fnmatch(cmd_basename, whitelisted) or fnmatch.fnmatch(cmd_name, whitelisted):
                    allowed = True
                    break
            if not allowed:
                return False, f"命令不在白名单中: {cmd_basename}"
        
        return True, "允许执行"
    
    def check_window_query(self) -> tuple[bool, str]:
        """检查窗口查询是否允许"""
        if not self.enable_window_query:
            return False, "窗口查询功能已禁用"
        return True, "允许"
    
    def check_screenshot(self) -> tuple[bool, str]:
        """检查截屏是否允许"""
        if not self.enable_screenshot:
            return False, "截屏功能已禁用"
        return True, "允许"
    
    def check_input(self) -> tuple[bool, str]:
        """检查远程输入是否允许"""
        if not self.enable_input:
            return False, "远程输入功能已禁用"
        return True, "允许"
    
    @classmethod
    def from_config(cls, config: dict[str, Any]) -> NestSecurity:
        """从配置字典创建"""
        return cls(
            dir_whitelist=config.get("dir_whitelist", []),
            dir_blacklist=config.get("dir_blacklist", []),
            cmd_whitelist=config.get("cmd_whitelist", []),
            cmd_blacklist=config.get("cmd_blacklist", []),
            enable_window_query=config.get("enable_window_query", True),
            enable_screenshot=config.get("enable_screenshot", True),
            enable_input=config.get("enable_input", True),
        )
