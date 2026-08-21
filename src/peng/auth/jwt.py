#!/usr/bin/env python3
"""
JWT Token 工具

提供 JWT token 的生成、验证和刷新功能。
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta
from typing import Any, Optional

from jose import JWTError, jwt

logger = logging.getLogger(__name__)

# JWT 配置
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24  # 24小时
REFRESH_TOKEN_EXPIRE_DAYS = 7  # 7天

# 缓存的 SECRET_KEY
_cached_secret_key: str | None = None


def get_secret_key() -> str:
    """
    获取 JWT 密钥

    优先级：
    1. 环境变量 JWT_SECRET_KEY
    2. 配置文件 webui.json 中的 jwt_secret_key
    3. 默认开发密钥（仅用于开发环境）

    Returns:
        JWT 密钥字符串
    """
    global _cached_secret_key

    if _cached_secret_key is not None:
        return _cached_secret_key

    # 1. 优先使用环境变量
    env_key = os.environ.get("JWT_SECRET_KEY")
    if env_key:
        _cached_secret_key = env_key
        logger.debug("JWT SECRET_KEY 从环境变量加载")
        return _cached_secret_key

    # 2. 从配置文件读取
    try:
        from peng.config import load_config

        config = load_config()
        if hasattr(config, "webui") and hasattr(config.webui, "jwt_secret_key"):
            config_key = config.webui.jwt_secret_key
            # 处理环境变量引用语法 ${VAR:-default}
            if config_key and config_key.startswith("${"):
                # 配置中是环境变量引用，但环境变量未设置，使用默认值
                # 解析 ${JWT_SECRET_KEY:-default} 格式
                import re

                match = re.match(r"\$\{[^:]+:-([^}]*)\}", config_key)
                if match:
                    _cached_secret_key = match.group(1)
                else:
                    _cached_secret_key = config_key
            else:
                _cached_secret_key = config_key
            logger.debug("JWT SECRET_KEY 从配置文件加载")
            return _cached_secret_key
    except Exception as e:
        logger.warning(f"无法从配置文件加载 JWT SECRET_KEY: {e}")

    # 3. 使用默认开发密钥
    default_key = "peng-dev-secret-key-CHANGE-IN-PRODUCTION"
    _cached_secret_key = default_key
    logger.warning("⚠️ 使用默认 JWT SECRET_KEY，生产环境请配置 JWT_SECRET_KEY 环境变量！")
    return _cached_secret_key


def create_access_token(data: dict[str, Any], expires_delta: Optional[timedelta] = None) -> str:
    """
    创建访问令牌

    Args:
        data: 要编码的数据（通常是 {"sub": user_id}）
        expires_delta: 过期时间增量（可选，默认使用 ACCESS_TOKEN_EXPIRE_MINUTES）

    Returns:
        JWT 访问令牌
    """
    to_encode = data.copy()

    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)

    to_encode.update({"exp": expire, "type": "access"})

    encoded_jwt = jwt.encode(to_encode, get_secret_key(), algorithm=ALGORITHM)
    return encoded_jwt


def create_refresh_token(data: dict[str, Any]) -> str:
    """
    创建刷新令牌

    Args:
        data: 要编码的数据（通常是 {"sub": user_id}）

    Returns:
        JWT 刷新令牌
    """
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)
    to_encode.update({"exp": expire, "type": "refresh"})

    encoded_jwt = jwt.encode(to_encode, get_secret_key(), algorithm=ALGORITHM)
    return encoded_jwt


def verify_token(token: str, token_type: str = "access") -> Optional[dict[str, Any]]:
    """
    验证令牌

    Args:
        token: JWT 令牌
        token_type: 令牌类型（access 或 refresh）

    Returns:
        解码后的数据，如果验证失败则返回 None
    """
    try:
        payload = jwt.decode(token, get_secret_key(), algorithms=[ALGORITHM])

        # 验证令牌类型
        if payload.get("type") != token_type:
            logger.warning(f"令牌类型不匹配: 期望 {token_type}, 实际 {payload.get('type')}")
            return None

        return payload

    except JWTError as e:
        logger.warning(f"JWT 验证失败: {e}")
        return None


def decode_token(token: str) -> Optional[dict[str, Any]]:
    """
    解码令牌（不验证签名）

    Args:
        token: JWT 令牌

    Returns:
        解码后的数据，如果解码失败则返回 None
    """
    try:
        payload = jwt.decode(token, get_secret_key(), algorithms=[ALGORITHM], options={"verify_signature": False})
        return payload
    except JWTError as e:
        logger.warning(f"JWT 解码失败: {e}")
        return None


def get_user_id_from_token(token: str) -> Optional[int]:
    """
    从令牌中获取用户ID

    Args:
        token: JWT 令牌

    Returns:
        用户ID，如果获取失败则返回 None
    """
    payload = verify_token(token, token_type="access")
    if payload:
        try:
            return int(payload.get("sub"))
        except (ValueError, TypeError):
            pass
    return None
