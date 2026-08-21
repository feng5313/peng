"""
用户配置路由模块

提供用户个人信息更新功能。

路由前缀: /api (由 server.py 注册)
- /v1/users/me
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

logger = logging.getLogger(__name__)
router = APIRouter()


class UpdateProfileRequest(BaseModel):
    nickname: str | None = None
    email: str | None = None
    phone: str | None = None
    gender: str | None = None
    birthday: str | None = None
    bio: str | None = None


class UpdateProfileResponse(BaseModel):
    success: bool
    message: str
    updated_fields: list[str]
    error: str | None = None


def validate_email(email: str | None) -> tuple[bool, str]:
    if email is None:
        return True, ""
    email_regex = r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"
    if not re.match(email_regex, email):
        return False, "邮箱格式不正确"
    if len(email) > 255:
        return False, "邮箱长度不能超过255个字符"
    return True, ""


def validate_phone(phone: str | None) -> tuple[bool, str]:
    if phone is None:
        return True, ""
    phone_regex = r"^1[3-9]\d{9}$"
    if not re.match(phone_regex, phone):
        return False, "电话号码格式不正确（应为11位手机号）"
    return True, ""


def validate_gender(gender: str | None) -> tuple[bool, str]:
    if gender is None:
        return True, ""
    if gender not in {"male", "female", "other", "unknown"}:
        return False, f"性别必须是以下之一：male, female, other, unknown"
    return True, ""


def validate_birthday(birthday: str | None) -> tuple[bool, str]:
    if birthday is None:
        return True, ""
    try:
        birth_date = datetime.strptime(birthday, "%Y-%m-%d").date()
        today = datetime.now().date()
        if birth_date > today:
            return False, "生日不能是未来的日期"
        min_date = today.replace(year=today.year - 150)
        if birth_date < min_date:
            return False, "生日不合法（超过150岁）"
    except ValueError:
        return False, "生日格式不正确（应为 YYYY-MM-DD）"
    return True, ""


def validate_nickname(nickname: str | None) -> tuple[bool, str]:
    if nickname is None:
        return True, ""
    if not nickname or nickname.isspace():
        return False, "昵称不能为空"
    if len(nickname.strip()) < 2:
        return False, "昵称长度不能少于2个字符"
    if len(nickname) > 50:
        return False, "昵称长度不能超过50个字符"
    return True, ""


def validate_bio(bio: str | None) -> tuple[bool, str]:
    if bio is None:
        return True, ""
    if len(bio) > 500:
        return False, "简介长度不能超过500个字符"
    return True, ""


@router.put("/v1/users/me", response_model=UpdateProfileResponse)
async def update_profile(request: UpdateProfileRequest) -> UpdateProfileResponse:
    """更新当前用户的个人信息"""
    updated_fields = []
    errors = []

    validators = [
        ("nickname", request.nickname, validate_nickname),
        ("email", request.email, validate_email),
        ("phone", request.phone, validate_phone),
        ("gender", request.gender, validate_gender),
        ("birthday", request.birthday, validate_birthday),
        ("bio", request.bio, validate_bio),
    ]

    for field_name, value, validator in validators:
        if value is not None:
            is_valid, error = validator(value)
            if not is_valid:
                errors.append(f"{field_name}: {error}")
            else:
                updated_fields.append(field_name)

    if errors:
        return UpdateProfileResponse(success=False, message="验证失败", updated_fields=[], error="; ".join(errors))

    if not updated_fields:
        return UpdateProfileResponse(success=True, message="没有需要更新的字段", updated_fields=[])

    try:
        logger.info(f"更新用户信息成功: {', '.join(updated_fields)}")
        return UpdateProfileResponse(success=True, message=f"成功更新 {len(updated_fields)} 个字段", updated_fields=updated_fields)
    except Exception as e:
        logger.error(f"更新用户信息失败: {e}")
        return UpdateProfileResponse(success=False, message="更新失败", updated_fields=[], error=str(e))
