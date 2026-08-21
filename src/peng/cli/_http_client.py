#!/usr/bin/env python3
"""
peng 后端 HTTP 客户端

轻量 HTTP 客户端，使用 Python 标准库 urllib（零额外依赖）。
供 bin/task 和 bin/msg 通过 HTTP API 与 peng 后端交互。

设计原则：
- 零额外依赖（只用标准库 urllib）
- 简单的 CRUD 操作封装
- 自动从配置文件读取后端地址
- 友好的错误处理（后端未运行时给出提示）
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

# 默认后端地址
DEFAULT_BASE_URL = "http://localhost:6688"


class PengHttpError(Exception):
    """HTTP API 错误"""

    def __init__(self, status_code: int, message: str, detail: str = ""):
        self.status_code = status_code
        self.message = message
        self.detail = detail
        super().__init__(f"[{status_code}] {message}: {detail}")


class PengHttpClient:
    """
    peng 后端 HTTP 客户端

    使用方法：
        client = PengHttpClient()
        result = client.get("/api/cli/task/123")
        result = client.post("/api/cli/task/123/pause")
    """

    def __init__(self, base_url: str | None = None):
        if base_url is None:
            base_url = self._get_base_url()
        # 去除末尾的 /
        self.base_url = base_url.rstrip("/")

    @staticmethod
    def _get_base_url() -> str:
        """从环境变量或配置文件获取后端地址"""
        # 优先使用环境变量
        env_url = os.environ.get("PENG_API_URL")
        if env_url:
            return env_url

        # 尝试从配置文件读取端口
        try:
            from peng.config.loader import load_config

            config = load_config()
            port = config.webui.port if hasattr(config, "webui") else 6688
            host = "localhost"
            return f"http://{host}:{port}"
        except Exception:
            return DEFAULT_BASE_URL

    def _request(
        self,
        method: str,
        path: str,
        data: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """
        发送 HTTP 请求

        Args:
            method: HTTP 方法 (GET/POST/PUT/DELETE)
            path: API 路径（如 /api/cli/task/123）
            data: 请求体数据（自动 JSON 编码）
            params: URL 查询参数

        Returns:
            响应 JSON 数据

        Raises:
            PengHttpError: HTTP 错误
            ConnectionError: 后端未运行
        """
        # 构建 URL
        url = f"{self.base_url}{path}"
        if params:
            query_string = urllib.parse.urlencode(params)
            url = f"{url}?{query_string}"

        # 构建请求体
        body = None
        if data is not None:
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")

        # 创建请求
        req = urllib.request.Request(
            url,
            data=body,
            method=method,
            headers={"Content-Type": "application/json; charset=utf-8"}
            if body
            else {},
        )

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                resp_data = resp.read().decode("utf-8")
                return json.loads(resp_data)
        except urllib.error.HTTPError as e:
            # HTTP 错误（4xx, 5xx）
            try:
                detail = e.read().decode("utf-8")
                # 尝试解析 JSON 错误响应
                try:
                    error_json = json.loads(detail)
                    detail = error_json.get("detail", detail)
                except (json.JSONDecodeError, AttributeError):
                    pass
            except Exception:
                detail = ""
            raise PengHttpError(e.code, e.reason, detail)
        except urllib.error.URLError as e:
            # 连接错误（后端未运行）
            raise ConnectionError(
                f"无法连接到 peng 后端 ({self.base_url})，请确认 fly 已启动。\n"
                f"错误详情: {e.reason}"
            )
        except json.JSONDecodeError as e:
            raise PengHttpError(500, "响应解析失败", str(e))

    def get(
        self, path: str, params: dict[str, str] | None = None
    ) -> dict[str, Any]:
        """GET 请求"""
        return self._request("GET", path, params=params)

    def post(
        self, path: str, data: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """POST 请求"""
        return self._request("POST", path, data=data)

    def put(
        self, path: str, data: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """PUT 请求"""
        return self._request("PUT", path, data=data)

    def delete(self, path: str) -> dict[str, Any]:
        """DELETE 请求"""
        return self._request("DELETE", path)

    def ping(self) -> bool:
        """检查后端是否可用"""
        try:
            self.get("/api/status")
            return True
        except Exception:
            return False


# 全局单例
_client: PengHttpClient | None = None


def get_client() -> PengHttpClient:
    """获取全局 HTTP 客户端实例"""
    global _client
    if _client is None:
        _client = PengHttpClient()
    return _client
