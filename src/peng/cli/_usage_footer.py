"""
CLI provider 用量/费用页脚打印模块

bin/msg、bin/task 无参数执行时，在输出末尾打印激活的 provider 用量/费用信息，
格式与 WebUI 底部状态栏右侧用量/费用徽章一致
（webui: static/js/modules/coding-plan.js 与 deepseek-usage.js）。

数据来源：本地 peng service 的 HTTP API（与 WebUI 完全同源，共享同一份缓存）：
  GET /api/llm/providers            → 判断显示条件
  GET /api/llm/coding-plan-usage    → GLM 套餐用量（周额度使用率）
  GET /api/llm/deepseek-usage       → DeepSeek 余额/今日费用/累计费用/高峰标记

显示条件（以「激活的provider」即 current_provider 为准）：
  - GLM Coding Plan：当前激活的 provider 名称是 GLM/智谱系（z-* 开头 / 含 glm / 含 zhipu）
  - DeepSeek：当前激活的 provider 名称含 deepseek
  注意：与 WebUI 徽章的宽松条件（enable/存在即显示）不同，CLI 只打印当前激活的 provider。

容错：service 未运行或任何 API 异常时静默跳过，绝不影响主命令输出与退出码。
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor


def _service_base_url() -> str:
    """本地 peng service 基地址（端口与 peng.cli.remote.is_local_service_running 一致）"""
    port = int(os.environ.get("PENG_PORT", "6688"))
    return f"http://localhost:{port}"


def _fetch_json(path: str, timeout: float = 10.0) -> dict | None:
    """请求本地 peng service API，任何失败返回 None（静默容错）"""
    try:
        import requests

        resp = requests.get(f"{_service_base_url()}{path}", timeout=timeout)
        if resp.status_code != 200:
            return None
        return resp.json()
    except Exception:
        return None


def _load_colors() -> dict:
    """加载统一颜色配置（失败返回空 dict，输出退化为无色纯文本）"""
    try:
        from peng.common.colors import get_task_colors

        return get_task_colors() or {}
    except Exception:
        return {}


def _fmt_coding_plan(data: dict, colors: dict) -> str | None:
    """格式化 GLM 套餐用量行（等价 webui coding-plan.js _cpRender）"""
    if not data or not data.get("success"):
        return None
    wp = data.get("weekly_percentage")
    if wp is None:
        return None

    reset = colors.get("RESET", "")
    bold = colors.get("BOLD", "")

    # 用量百分比颜色：>=90 红，>=70 黄（webui 橙），否则绿
    if wp >= 90:
        pct_color = colors.get("ERROR", "")
    elif wp >= 70:
        pct_color = colors.get("WARNING", "")
    else:
        pct_color = colors.get("SUCCESS", "")

    parts: list[str] = []

    # 标题 + 等级（webui 等级为蓝色加粗）
    parts.append("GLM套餐用量")
    level = str(data.get("level") or "").upper()
    if level:
        parts.append(f"{bold}{colors.get('INFO', '')}{level}{reset}")

    # 百分比（webui 直接数值拼接：23 → 23%，23.45 → 23.45%）
    parts.append(f"{bold}{pct_color}{wp:g}%{reset}")

    # 文本进度条（webui cp-bar 的纯文本等价，10 格，封顶 100%）
    filled = int(round(max(0.0, min(float(wp), 100.0)) / 10.0))
    bar = "█" * filled + "░" * (10 - filled)
    parts.append(f"{pct_color}{bar}{reset}")

    # stale 标记 + 周额度重置时间（webui 顺序：⚠️ 在前，♻️ 在后）
    tail = ""
    if data.get("stale"):
        tail += "⚠️"
    weekly_reset = data.get("weekly_reset")
    if weekly_reset:
        if tail:
            tail += " "
        tail += f"♻️{weekly_reset}"
    if tail:
        parts.append(tail)

    return " ".join(parts)


def _fmt_deepseek(data: dict, colors: dict) -> str | None:
    """格式化 DeepSeek 用量/费用行（等价 webui deepseek-usage.js _dsRender）"""
    if not data or not data.get("success"):
        return None

    reset = colors.get("RESET", "")
    bold = colors.get("BOLD", "")
    dim = colors.get("DIM", "")

    parts: list[str] = []

    # 高峰/低谷标记（webui：高峰红、低谷绿，均加粗）
    if data.get("is_peak"):
        parts.append(f"{bold}{colors.get('ERROR', '')}🐋高峰{reset}")
    else:
        parts.append(f"{bold}{colors.get('SUCCESS', '')}🐋低谷{reset}")

    # 余额（<5 红、<20 黄（webui 橙）、否则绿；null 不显示）
    balance = data.get("balance_cny")
    if balance is not None:
        if balance < 5:
            bal_color = colors.get("ERROR", "")
        elif balance < 20:
            bal_color = colors.get("WARNING", "")
        else:
            bal_color = colors.get("SUCCESS", "")
        parts.append(f"{dim}余额{reset}{bold}{bal_color}¥{balance:.2f}{reset}")

    # 今日/累计费用（与 webui 一致：>0 才显示）
    today = data.get("today_cost") or 0
    if today > 0:
        parts.append(f"{dim}今日{reset}{bold}¥{today:.2f}{reset}")
    total = data.get("total_cost") or 0
    if total > 0:
        parts.append(f"{dim}累计{reset}{bold}¥{total:.2f}{reset}")

    # 数据延迟标记
    if data.get("stale"):
        parts.append("⚠️")

    return " ".join(parts)


def _is_glm_name(name: str) -> bool:
    """判断 provider 名称是否为 GLM/智谱系（z-* 开头 / 含 glm / 含 zhipu）"""
    return bool(name) and (
        name.startswith("z-") or "glm" in name.lower() or "zhipu" in name.lower()
    )


def _is_deepseek_name(name: str) -> bool:
    """判断 provider 名称是否为 DeepSeek 系"""
    return "deepseek" in (name or "").lower()


def print_provider_usage_footer() -> None:
    """在 CLI 输出末尾打印激活的 provider 用量/费用信息（静默容错，绝不抛异常）"""
    try:
        colors = _load_colors()

        # 显示条件：以当前激活的 provider（current_provider）为准
        providers_data = _fetch_json("/api/llm/providers", timeout=3.0)
        current = str((providers_data or {}).get("current_provider") or "")
        paths: dict[str, str] = {}
        if _is_glm_name(current):
            paths["coding_plan"] = "/api/llm/coding-plan-usage"
        if _is_deepseek_name(current):
            paths["deepseek"] = "/api/llm/deepseek-usage"
        if not paths:
            return

        # 并行获取两类用量数据（等价 WebUI 两个独立轮询）
        results: dict[str, dict | None] = {}
        with ThreadPoolExecutor(max_workers=len(paths)) as pool:
            futures = {key: pool.submit(_fetch_json, path, 15.0) for key, path in paths.items()}
            for key, fut in futures.items():
                try:
                    results[key] = fut.result()
                except Exception:
                    results[key] = None

        # 格式化输出（顺序与 WebUI 状态栏 DOM 一致：coding-plan 在前，deepseek 在后）
        lines: list[str] = []
        for key, fmt in (
            ("coding_plan", _fmt_coding_plan),
            ("deepseek", _fmt_deepseek),
        ):
            line = fmt(results.get(key) or {}, colors)
            if line:
                lines.append(line)

        if lines:
            print()
            for ln in lines:
                print(ln)
    except Exception:
        # 页脚属于附加信息，任何异常都静默忽略
        pass
