#!/usr/bin/env python3
"""
用户认证模块 - 数据模型

提供用户认证相关的 Pydantic 模型和数据类。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional


@dataclass
class User:
    """
    用户数据模型

    Attributes:
        id: 用户ID（主键，自增）
        username: 用户名（唯一）
        email: 邮箱（唯一）
        hashed_password: 哈希后的密码
        nickname: 昵称
        is_active: 是否激活
        is_superuser: 是否是超级用户
        created_at: 创建时间
        updated_at: 更新时间
        last_login: 最后登录时间
    """
    username: str
    email: str
    hashed_password: str
    id: Optional[int] = None
    nickname: Optional[str] = None
    is_active: bool = True
    is_superuser: bool = False
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    last_login: Optional[datetime] = None

    def to_dict(self) -> dict[str, Any]:
        """转换为字典"""
        return {
            'id': self.id,
            'username': self.username,
            'email': self.email,
            'nickname': self.nickname,
            'is_active': self.is_active,
            'is_superuser': self.is_superuser,
            'created_at': str(self.created_at) if self.created_at else None,
            'updated_at': str(self.updated_at) if self.updated_at else None,
            'last_login': str(self.last_login) if self.last_login else None,
        }

    @classmethod
    def from_row(cls, row: dict) -> User:
        """从数据库行创建 User 对象"""
        return cls(
            id=row['id'],
            username=row['username'],
            email=row['email'],
            hashed_password=row['hashed_password'],
            nickname=row.get('nickname'),
            is_active=row.get('is_active', True),
            is_superuser=row.get('is_superuser', False),
            created_at=row.get('created_at'),
            updated_at=row.get('updated_at'),
            last_login=row.get('last_login'),
        )


@dataclass
class Session:
    """
    会话数据模型

    Attributes:
        id: 会话ID
        user_id: 用户ID
        access_token: 访问令牌
        refresh_token: 刷新令牌
        expires_at: 过期时间
        created_at: 创建时间
    """
    user_id: int
    access_token: str
    refresh_token: str
    expires_at: datetime
    id: Optional[int] = None
    created_at: Optional[datetime] = None

    def to_dict(self) -> dict[str, Any]:
        """转换为字典"""
        return {
            'id': self.id,
            'user_id': self.user_id,
            'access_token': self.access_token,
            'refresh_token': self.refresh_token,
            'expires_at': str(self.expires_at) if self.expires_at else None,
            'created_at': str(self.created_at) if self.created_at else None,
        }

    @classmethod
    def from_row(cls, row: dict) -> Session:
        """从数据库行创建 Session 对象"""
        return cls(
            id=row['id'],
            user_id=row['user_id'],
            access_token=row['access_token'],
            refresh_token=row['refresh_token'],
            expires_at=row['expires_at'],
            created_at=row.get('created_at'),
        )
