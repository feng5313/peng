"""
Web工具

提供websearch和webfetch功能。
"""

import hashlib
import logging
import re
import time
import traceback
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlparse

import httpx

from peng.common import jsonp as json
from peng.common.colors import get_web_colors
from peng.tools.base import Tool, ToolError

# 从统一颜色配置获取颜色常量

logger = logging.getLogger(__name__)
_COLORS = get_web_colors()
BLUE = _COLORS.get("INFO", "")
RESET = _COLORS.get("RESET", "")

# 通用预编译正则表达式（被多个Provider共享）
_HTML_TAG_PATTERN = re.compile(r"<[^>]+>")
_WHITESPACE_PATTERN = re.compile(r"\s+")
_HREF_PATTERN = re.compile(r'href="([^"]+)"')

# 通用搜索结果解析预编译正则
_TITLE_H2_PATTERN = re.compile(r"<h2[^>]*>.*?<a[^>]*>(.*?)</a>", re.DOTALL)
_CONTENT_P_PATTERN = re.compile(r'<p class="content[^"]*"[^>]*>(.*?)</p>', re.DOTALL)
_PROMO_PRICE_PATTERN = re.compile(r'<i class="promo-price">.*?<span>([^<]+)</span>', re.DOTALL)
_J_PRICE_PATTERN = re.compile(r'<strong class="J_price"[^>]*>([^<]+)</strong>')
_COMMENT_COUNT_PATTERN = re.compile(r'<a[^>]*class="comment[^"]*"[^>]*>(\d+)')
_TITLE_EM_PATTERN = re.compile(r'<div class="p-name[^"]*"[^>]*>.*?<em>(.*?)</em>', re.DOTALL)
_HREF_SLASH_PATTERN = re.compile(r'href="//([^"]+)"')
_SINA_ITEM_PATTERN = re.compile(r'<div class="box-result[^"]*"[^>]*>(.*?)</div>', re.DOTALL)
_JD_ITEM_PATTERN = re.compile(r'<li class="gl-item"[^>]*>(.*?)</li>', re.DOTALL)

# 淘宝相关预编译正则
_TAOBAO_PATTERNS = [
    re.compile(r'<div class="Card--doubleCard[^"]*"[^>]*>(.*?)</div>\s*</div>', re.DOTALL),
    re.compile(r'<div[^>]*class="[^"]*Item--[^"]*"[^>]*>(.*?)</div>\s*</div>', re.DOTALL),
    re.compile(r'<div[^>]*class="[^"]*J_item[^"]*"[^>]*>(.*?)</div>', re.DOTALL),
]
_TAOBAO_TITLE_PATTERN = re.compile(
    r'<span[^>]*class="[^"]*Title[^"]*"[^>]*>(.*?)</span>', re.DOTALL
)
_TAOBAO_PRICE_PATTERN = re.compile(r'<span[^>]*class="[^"]*[Pp]rice[^"]*"[^>]*>([^<]+)</span>')
_TAOBAO_SHOP_PATTERN = re.compile(r'<span[^>]*class="[^"]*[Ss]hop[^"]*"[^>]*>([^<]+)</span>')
_TAOBAO_ITEM_LINKS_PATTERN = re.compile(
    r'<a[^>]*href="(//(?:item\.taobao\.com|detail\.tmall\.com)[^"]*)"[^>]*>([^<]{10,100})</a>'
)

# 生意社相关预编译正则
_SHENGYISHE_ITEM_PATTERN = re.compile(r'<div class="list-item[^"]*"[^>]*>(.*?)</div>', re.DOTALL)
_SHENGYISHE_TITLE_PATTERN = re.compile(r'<a[^>]*class="title[^"]*"[^>]*>(.*?)</a>', re.DOTALL)
_SHENGYISHE_PRICE_PATTERN = re.compile(r'<span class="price[^"]*"[^>]*>([^<]+)</span>')
_SHENGYISHE_CHANGE_PATTERN = re.compile(r'<span class="change[^"]*"[^>]*>([^<]+)</span>')

# Google搜索相关预编译正则
_GOOGLE_RESULT_PATTERN = re.compile(
    r'<div[^>]*data-ved[^>]*>.*?<a[^>]*href="([^"]+)"[^>]*>.*?<h3[^>]*>(.*?)</h3>.*?</div>',
    re.DOTALL,
)

# HTML内容格式化预编译正则
_SCRIPT_TAG_PATTERN = re.compile(r"<script[^>]*>.*?</script>", re.DOTALL | re.IGNORECASE)
_STYLE_TAG_PATTERN = re.compile(r"<style[^>]*>.*?</style>", re.DOTALL | re.IGNORECASE)
_HTML_COMMENT_PATTERN = re.compile(r"<!--.*?-->", re.DOTALL)
_HTML_TITLE_PATTERN = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)

# SinaStock Provider预编译正则
_SIX_DIGIT_PATTERN = re.compile(r"^\d{6}$")
_SINA_STOCK_DATA_PATTERN = re.compile(r'var hq_str_[^=]+="(.*)"')

# 全局已显示域名集合（每个进程独立）
_displayed_domains: set[str] = set()


def _get_domain(url: str) -> str:
    """从URL提取域名"""
    try:
        parsed = urlparse(url)
        return parsed.netloc or ""
    except Exception as e:
        logger.error(f"❌ _get_domain() - web.py:91: 异常 - {e}\n{traceback.format_exc()}")
        return ""


def _format_url_display(url: str) -> str:
    """
    格式化URL显示（蓝色）

    相同域名的网络访问1个进程中只显示1次

    Args:
        url: 完整的URL

    Returns:
        格式化的URL字符串（如果是新域名）或空字符串（如果域名已显示过）
    """
    if not url:
        return ""

    domain = _get_domain(url)
    if not domain:
        return ""

    # 检查域名是否已显示过
    if domain in _displayed_domains:
        return ""

    # 记录已显示的域名
    _displayed_domains.add(domain)

    return f"{BLUE}🌐 网络访问:{RESET} {url}"


# ==================== 搜索源健康状态管理 ====================


@dataclass
class ProviderHealth:
    """搜索源健康状态"""

    # 连续失败次数
    consecutive_failures: int = 0
    # 最后一次失败时间
    last_failure_time: float = 0
    # 最后一次成功时间
    last_success_time: float = 0
    # 总调用次数
    total_calls: int = 0
    # 总成功次数
    total_success: int = 0
    # 是否被禁用
    disabled: bool = False
    # 禁用原因
    disable_reason: str = ""


class ProviderHealthManager:
    """
    搜索源健康状态管理器

    功能：
    1. 冷却时间：失败后一段时间内不再调用
    2. 失败计数：记录连续失败次数
    3. 自动禁用：连续失败多次后自动禁用
    4. 自动恢复：一段时间后自动尝试恢复
    """

    # 连续失败多少次后禁用
    MAX_CONSECUTIVE_FAILURES = 5
    # 冷却时间（秒）
    COOLDOWN_SECONDS = 300  # 5分钟
    # 禁用后多久尝试恢复（秒）
    RECOVERY_SECONDS = 3600  # 1小时

    def __init__(self):
        self._health: dict[str, ProviderHealth] = {}

    def get_health(self, provider_name: str) -> ProviderHealth:
        """获取搜索源健康状态"""
        if provider_name not in self._health:
            self._health[provider_name] = ProviderHealth()
        return self._health[provider_name]

    def is_available(self, provider_name: str) -> tuple[bool, str]:
        """
        检查搜索源是否可用

        Returns:
            (是否可用, 不可用原因)
        """
        health = self.get_health(provider_name)

        # 已被禁用
        if health.disabled:
            # 检查是否可以尝试恢复
            if time.time() - health.last_failure_time > self.RECOVERY_SECONDS:
                # 允许尝试恢复
                return True, ""
            return False, f"已禁用: {health.disable_reason}"

        # 冷却中
        if health.consecutive_failures > 0:
            elapsed = time.time() - health.last_failure_time
            if elapsed < self.COOLDOWN_SECONDS:
                remaining = int(self.COOLDOWN_SECONDS - elapsed)
                return False, f"冷却中（剩余{remaining}秒）"

        return True, ""

    def record_success(self, provider_name: str) -> None:
        """记录成功"""
        health = self.get_health(provider_name)
        health.consecutive_failures = 0
        health.last_success_time = time.time()
        health.total_calls += 1
        health.total_success += 1

        # 成功后解除禁用
        if health.disabled:
            health.disabled = False
            health.disable_reason = ""

    def record_failure(self, provider_name: str, error: str) -> None:
        """记录失败"""
        health = self.get_health(provider_name)
        health.consecutive_failures += 1
        health.last_failure_time = time.time()
        health.total_calls += 1

        # 连续失败过多则禁用
        if health.consecutive_failures >= self.MAX_CONSECUTIVE_FAILURES:
            health.disabled = True
            health.disable_reason = f"连续失败{health.consecutive_failures}次"

    def get_status_report(self) -> str:
        """获取状态报告"""
        lines = ["搜索源健康状态:"]
        for name, health in sorted(self._health.items()):
            if health.disabled:
                status = "❌ 禁用"
            elif health.consecutive_failures > 0:
                status = f"⚠️ 冷却中（失败{health.consecutive_failures}次）"
            else:
                success_rate = (
                    health.total_success / health.total_calls * 100 if health.total_calls > 0 else 0
                )
                status = f"✅ 正常（成功率{success_rate:.0f}%）"
            lines.append(f"  {name}: {status}")
        return "\n".join(lines)

    def reset(self, provider_name: str | None = None) -> None:
        """重置状态"""
        if provider_name:
            if provider_name in self._health:
                del self._health[provider_name]
        else:
            self._health.clear()


# 全局健康状态管理器
_health_manager = ProviderHealthManager()


# ==================== 搜索源定义 ====================


class SearchProvider:
    """搜索源基类"""

    name: str = "unknown"
    url: str = ""
    category: str = "general"  # 类别: general, news, video, finance, wiki, code, qa
    description: str = ""  # 描述，用于大模型选择

    async def search(
        self, query: str, num_results: int, client: httpx.AsyncClient
    ) -> list[dict[str, str]]:
        """执行搜索，返回结果列表"""
        raise NotImplementedError


class BingSearchProvider(SearchProvider):
    """必应搜索 - 通用搜索"""

    name = "bing"
    url = "https://cn.bing.com/search"
    category = "general"
    description = "通用搜索，适合大多数查询场景，中文效果好"

    # 预编译正则表达式
    _RESULT_PATTERN = re.compile(r'<li class="b_algo"[^>]*>(.*?)</li>', re.DOTALL)
    _TITLE_PATTERN = re.compile(r"<h2[^>]*>(.*?)</h2>", re.DOTALL)
    _TAG_PATTERN = re.compile(r"<[^>]+>")
    _LINK_PATTERN = re.compile(r'href="(https?://[^"]+)"')
    _SNIPPET_PATTERN = re.compile(r'<div class="b_caption"[^>]*>.*?<p>(.*?)</p>', re.DOTALL)
    _WHITESPACE_PATTERN = re.compile(r"\s+")

    async def search(
        self, query: str, num_results: int, client: httpx.AsyncClient
    ) -> list[dict[str, str]]:
        response = await client.get(
            self.url,
            params={"q": query, "count": str(num_results)},
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            },
        )
        response.raise_for_status()
        html = response.text

        results = []
        matches = self._RESULT_PATTERN.findall(html)[:num_results]

        for match in matches:
            title_match = self._TITLE_PATTERN.search(match)
            title = ""
            if title_match:
                title = self._TAG_PATTERN.sub("", title_match.group(1)).strip()

            link = ""
            h2_content = title_match.group(1) if title_match else ""
            link_match = self._LINK_PATTERN.search(h2_content)
            if link_match:
                link = link_match.group(1)

            if not link:
                all_links = self._LINK_PATTERN.findall(match)
                for found_link in all_links:
                    if not any(
                        x in found_link.lower()
                        for x in [".css", ".js", ".png", ".jpg", "r.bing.com"]
                    ):
                        link = found_link
                        break

            snippet_match = self._SNIPPET_PATTERN.search(match)
            snippet = ""
            if snippet_match:
                snippet = self._TAG_PATTERN.sub("", snippet_match.group(1)).strip()
                snippet = self._WHITESPACE_PATTERN.sub(" ", snippet)

            if title:
                results.append({"title": title[:150], "snippet": snippet[:300], "url": link})

        return results


class BingNewsProvider(SearchProvider):
    """必应新闻搜索"""

    name = "bing_news"
    url = "https://cn.bing.com/news/search"
    category = "news"
    description = "新闻搜索，适合查询最新资讯、热点事件"

    # 预编译正则表达式
    _NEWS_PATTERN = re.compile(
        r'<div class="[^"]*newsitem[^"]*"[^>]*>(.*?)</div>\s*</div>', re.DOTALL
    )
    _TITLE_PATTERN = re.compile(r'<a[^>]*class="title"[^>]*>(.*?)</a>', re.DOTALL)
    _TAG_PATTERN = re.compile(r"<[^>]+>")
    _LINK_PATTERN = re.compile(r'href="(https?://[^"]+)"')
    _SNIPPET_PATTERN = re.compile(r'<div class="snippet"[^>]*>(.*?)</div>', re.DOTALL)

    async def search(
        self, query: str, num_results: int, client: httpx.AsyncClient
    ) -> list[dict[str, str]]:
        response = await client.get(
            self.url,
            params={"q": query, "count": str(num_results)},
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "text/html",
                "Accept-Language": "zh-CN,zh;q=0.9",
            },
        )
        response.raise_for_status()
        html = response.text

        results = []
        matches = self._NEWS_PATTERN.findall(html)[:num_results]

        for match in matches:
            title_match = self._TITLE_PATTERN.search(match)
            title = ""
            if title_match:
                title = self._TAG_PATTERN.sub("", title_match.group(1)).strip()

            link_match = self._LINK_PATTERN.search(match)
            link = link_match.group(1) if link_match else ""

            snippet_match = self._SNIPPET_PATTERN.search(match)
            snippet = ""
            if snippet_match:
                snippet = self._TAG_PATTERN.sub("", snippet_match.group(1)).strip()

            if title:
                results.append({"title": title[:150], "snippet": snippet[:200], "url": link})

        return results


class ZhihuProvider(SearchProvider):
    """知乎搜索"""

    name = "zhihu"
    url = "https://www.zhihu.com/search"
    category = "qa"
    description = "知乎问答搜索，适合查询专业见解、经验分享"

    # 预编译正则表达式
    _CARD_PATTERN = re.compile(r'<div class="[^"]*Card[^"]*"[^>]*>(.*?)</div>\s*</div>', re.DOTALL)
    _CONTENT_TITLE_PATTERN = re.compile(
        r'<h2[^>]*class="[^"]*ContentItem-title[^"]*"[^>]*>(.*?)</h2>', re.DOTALL
    )
    _RICH_CONTENT_PATTERN = re.compile(
        r'<div class="RichContent-inner"[^>]*>(.*?)</div>', re.DOTALL
    )

    async def search(
        self, query: str, num_results: int, client: httpx.AsyncClient
    ) -> list[dict[str, str]]:
        response = await client.get(
            self.url,
            params={"type": "content", "q": query},
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "text/html",
                "Accept-Language": "zh-CN,zh;q=0.9",
            },
        )
        response.raise_for_status()
        html = response.text

        results = []
        # 知乎结果格式（使用预编译正则）
        matches = self._CARD_PATTERN.findall(html)[:num_results]

        for match in matches:
            title_match = self._CONTENT_TITLE_PATTERN.search(match)
            title = ""
            if title_match:
                title = _HTML_TAG_PATTERN.sub("", title_match.group(1)).strip()

            link_match = _HREF_PATTERN.search(match)
            link = ""
            if link_match:
                href = link_match.group(1)
                if href.startswith("//"):
                    link = "https:" + href
                else:
                    link = href

            snippet_match = self._RICH_CONTENT_PATTERN.search(match)
            snippet = ""
            if snippet_match:
                snippet = _HTML_TAG_PATTERN.sub("", snippet_match.group(1)).strip()[:200]

            if title:
                results.append({"title": title[:150], "snippet": snippet, "url": link})

        return results


class BaiduBaikeProvider(SearchProvider):
    """百度百科搜索"""

    name = "baike"
    url = "https://baike.baidu.com/search"
    category = "wiki"
    description = "百科搜索，适合查询概念定义、知识解释"

    # 预编译正则表达式
    _RESULT_PATTERNS = [
        re.compile(r'<dd class="result"[^>]*>(.*?)</dd>', re.DOTALL),
        re.compile(r'<div[^>]*class="[^"]*searchResult[^"]*"[^>]*>(.*?)</div>\s*</div>', re.DOTALL),
        re.compile(r'<div[^>]*class="[^"]*result[^"]*item[^"]*"[^>]*>(.*?)</div>', re.DOTALL),
    ]
    _ITEM_LINK_PATTERN = re.compile(r'<a[^>]*href="([^"]*item[^"]*)"[^>]*>(.*?)</a>', re.DOTALL)
    _SUMMARY_PATTERN = re.compile(r'<p[^>]*class="[^"]*summary[^"]*"[^>]*>(.*?)</p>', re.DOTALL)
    _DIRECT_ITEM_LINKS = re.compile(r'<a[^>]*href="(/item/[^"]+)"[^>]*>([^<]{5,100})</a>')

    def _parse_html(self, html: str, num_results: int) -> list[dict[str, str]]:
        """解析百度百科搜索结果 HTML

        百度百科搜索页面使用 React 渲染，搜索结果可能是动态加载的。
        尝试匹配多种可能的 HTML 格式。
        """
        results = []

        # 尝试多种搜索结果格式（使用预编译正则）
        for pattern in self._RESULT_PATTERNS:
            matches = pattern.findall(html)[:num_results]
            if matches:
                for match in matches:
                    # 提取标题和链接
                    title_match = self._ITEM_LINK_PATTERN.search(match)
                    title = ""
                    link = ""
                    if title_match:
                        link = title_match.group(1)
                        if link and not link.startswith("http"):
                            link = "https://baike.baidu.com" + link
                        title = _HTML_TAG_PATTERN.sub("", title_match.group(2)).strip()

                    # 提取摘要
                    snippet_match = self._SUMMARY_PATTERN.search(match)
                    snippet = ""
                    if snippet_match:
                        snippet = _HTML_TAG_PATTERN.sub("", snippet_match.group(1)).strip()

                    if title:
                        results.append(
                            {"title": title[:150], "snippet": snippet[:200], "url": link}
                        )

                if results:
                    return results[:num_results]

        # 如果上述模式都不匹配，尝试直接提取 /item/ 链接
        item_links = self._DIRECT_ITEM_LINKS.findall(html)[:num_results]
        for link, title in item_links:
            full_link = "https://baike.baidu.com" + link
            results.append({"title": title[:150], "snippet": "", "url": full_link})

        return results[:num_results]

    async def search(
        self, query: str, num_results: int, client: httpx.AsyncClient
    ) -> list[dict[str, str]]:
        # 构建完整 URL
        params = {"word": query, "pn": "0", "rn": str(num_results)}
        full_url = f"{self.url}?word={quote(query)}&pn=0&rn={num_results}"

        # 先尝试 HTTP 请求
        try:
            response = await client.get(
                self.url,
                params=params,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Accept": "text/html",
                    "Accept-Language": "zh-CN",
                },
            )
            response.raise_for_status()
            results = self._parse_html(response.text, num_results)
            if results:
                return results
        except Exception as e:
            logger.error(f"❌ _parse_html() - web.py:723: 异常 - {e}\n{traceback.format_exc()}")

        return []


class SinaFinanceProvider(SearchProvider):
    """新浪财经搜索 - 股票、基金、外汇、加密货币"""

    name = "sina_finance"
    url = "https://search.sina.com.cn/"
    category = "finance"
    description = "新浪财经搜索，适合查询股票行情、基金、外汇、比特币等"

    async def search(
        self, query: str, num_results: int, client: httpx.AsyncClient
    ) -> list[dict[str, str]]:
        response = await client.get(
            self.url,
            params={"q": query, "c": "news", "from": "channel", "ie": "utf-8"},
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "text/html",
                "Accept-Language": "zh-CN,zh;q=0.9",
            },
        )
        response.raise_for_status()
        html = response.text

        results = []
        matches = _SINA_ITEM_PATTERN.findall(html)[:num_results]

        for match in matches:
            title_match = _TITLE_H2_PATTERN.search(match)
            title = ""
            link = ""
            if title_match:
                title = _HTML_TAG_PATTERN.sub("", title_match.group(1)).strip()
                link_match = _HREF_PATTERN.search(match)
                link = link_match.group(1) if link_match else ""

            snippet_match = _CONTENT_P_PATTERN.search(match)
            snippet = ""
            if snippet_match:
                snippet = _HTML_TAG_PATTERN.sub("", snippet_match.group(1)).strip()

            if title:
                results.append({"title": title[:100], "snippet": snippet[:200], "url": link})

        return results


class SinaNewsProvider(SearchProvider):
    """新浪新闻搜索"""

    name = "sina_news"
    url = "https://search.sina.com.cn/"
    category = "news"
    description = "新浪新闻搜索，适合查询热点新闻、时事资讯"

    async def search(
        self, query: str, num_results: int, client: httpx.AsyncClient
    ) -> list[dict[str, str]]:
        response = await client.get(
            self.url,
            params={"q": query, "c": "news", "from": "channel", "ie": "utf-8"},
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "text/html",
                "Accept-Language": "zh-CN,zh;q=0.9",
            },
        )
        response.raise_for_status()
        html = response.text

        results = []
        matches = _SINA_ITEM_PATTERN.findall(html)[:num_results]

        for match in matches:
            title_match = _TITLE_H2_PATTERN.search(match)
            title = ""
            link = ""
            if title_match:
                title = _HTML_TAG_PATTERN.sub("", title_match.group(1)).strip()
                link_match = _HREF_PATTERN.search(match)
                link = link_match.group(1) if link_match else ""

            snippet_match = _CONTENT_P_PATTERN.search(match)
            snippet = ""
            if snippet_match:
                snippet = _HTML_TAG_PATTERN.sub("", snippet_match.group(1)).strip()

            if title:
                results.append({"title": title[:100], "snippet": snippet[:200], "url": link})

        return results


class SinaStockLookupProvider(SearchProvider):
    """新浪财经股票查询 - 实时行情"""

    name = "sina_stock"
    url = "https://hq.sinajs.cn/list"
    category = "finance"
    description = "新浪财经股票实时行情查询，输入6位股票代码（如600519）"

    async def search(
        self, query: str, num_results: int, client: httpx.AsyncClient
    ) -> list[dict[str, str]]:
        # 清理输入
        code = query.strip()

        # 如果是6位纯数字，自动添加前缀
        if _SIX_DIGIT_PATTERN.match(code):
            # 6开头是上海，0/3开头是深圳
            if code.startswith("6"):
                code = f"sh{code}"
            elif code.startswith(("0", "3")):
                code = f"sz{code}"
            # 其他情况尝试上海
            else:
                code = f"sh{code}"

        response = await client.get(
            f"{self.url}={code}",
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "*/*",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Referer": "https://finance.sina.com.cn/",
            },
        )
        response.raise_for_status()
        text = response.text

        results = []
        # 解析返回格式: var hq_str_sh600519="贵州茅台,1800.00,1790.00,..."
        # 字段含义: 名称,今开,昨收,当前价,最高,最低,买一,卖一,成交量,成交额, ...
        match = _SINA_STOCK_DATA_PATTERN.search(text)
        if match:
            data_str = match.group(1)
            if data_str:  # 非空数据
                fields = data_str.split(",")
                if len(fields) >= 6:
                    name = fields[0]
                    open_price = fields[1]
                    prev_close = fields[2]
                    current_price = fields[3]
                    high = fields[4]
                    low = fields[5]

                    # 计算涨跌幅
                    try:
                        change_pct = (
                            (float(current_price) - float(prev_close)) / float(prev_close) * 100
                        )
                        change_str = (
                            f"+{change_pct:.2f}%" if change_pct >= 0 else f"{change_pct:.2f}%"
                        )
                    except (ValueError, ZeroDivisionError):
                        change_str = "N/A"

                    results.append(
                        {
                            "title": f"{name} ({code}) ¥{current_price} {change_str}",
                            "snippet": (
                                f"今开: {open_price} | 昨收: {prev_close} | "
                                f"最高: {high} | 最低: {low}"
                            ),
                            "url": f"https://finance.sina.com.cn/realstock/company/{code}/nc.shtml",
                        }
                    )

        return results[:num_results]


class BilibiliProvider(SearchProvider):
    """B站搜索 - 视频（使用 Web API + wbi 签名）"""

    name = "bilibili"
    url = "https://api.bilibili.com/x/web-interface/search/type"
    category = "video"
    description = "B站视频搜索，适合查询教程、技术讲解、娱乐视频"

    # wbi 签名所需的混淆表
    _WBI_MIXIN_TABLE = [
        46,
        47,
        18,
        2,
        53,
        8,
        23,
        32,
        15,
        50,
        10,
        31,
        58,
        3,
        45,
        35,
        27,
        43,
        5,
        49,
        33,
        9,
        42,
        19,
        29,
        28,
        14,
        39,
        12,
        38,
        41,
        13,
        37,
        48,
        7,
        16,
        24,
        55,
        40,
        61,
        26,
        17,
        0,
        1,
        60,
        51,
        30,
        4,
        22,
        25,
        54,
        21,
        56,
        59,
        6,
        63,
        57,
        62,
        11,
        36,
        20,
        34,
        44,
        52,
    ]

    # 缓存的 wbi key
    _wbi_key: str = ""
    _wbi_key_time: float = 0

    def _get_mixin_key(self, orig: str) -> str:
        """生成混淆 key"""
        return "".join(orig[i] for i in self._WBI_MIXIN_TABLE[:32])

    async def _get_wbi_key(self, client: httpx.AsyncClient) -> str:
        """获取 wbi 签名 key（带缓存）"""
        # 缓存 10 分钟
        if self._wbi_key and time.time() - self._wbi_key_time < 600:
            return self._wbi_key

        try:
            resp = await client.get(
                "https://api.bilibili.com/x/web-interface/nav",
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Referer": "https://www.bilibili.com",
                },
            )
            data = resp.json()
            img_url = data.get("data", {}).get("wbi_img", {}).get("img_url", "")
            sub_url = data.get("data", {}).get("wbi_img", {}).get("sub_url", "")

            # 提取 key
            img_key = img_url.split("/")[-1].split(".")[0] if img_url else ""
            sub_key = sub_url.split("/")[-1].split(".")[0] if sub_url else ""

            # 生成混淆后的 key
            self._wbi_key = self._get_mixin_key(img_key + sub_key)
            self._wbi_key_time = time.time()
            return self._wbi_key
        except Exception as e:
            logger.error(f"❌ _get_mixin_key() - web.py:1018: 异常 - {e}\n{traceback.format_exc()}")
            return ""

    def _sign_params(self, params: dict, wbi_key: str) -> dict:
        """对参数进行 wbi 签名"""
        # 添加时间戳
        params["wts"] = int(time.time())

        # 按键排序并拼接
        sorted_params = sorted(params.items())
        # 过滤特殊字符并编码
        query = "&".join(f"{k}={quote(str(v), safe='')}" for k, v in sorted_params)

        # 计算签名
        sign = hashlib.md5((query + wbi_key).encode()).hexdigest()
        params["w_rid"] = sign
        return params

    async def search(
        self, query: str, num_results: int, client: httpx.AsyncClient
    ) -> list[dict[str, str]]:
        # 获取 wbi key
        wbi_key = await self._get_wbi_key(client)

        # 构建参数
        params = {
            "keyword": query,
            "search_type": "video",
            "page_size": str(num_results),
        }

        # 签名
        if wbi_key:
            params = self._sign_params(params, wbi_key)

        response = await client.get(
            self.url,
            params=params,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                "Referer": "https://www.bilibili.com",
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Origin": "https://www.bilibili.com",
            },
        )
        response.raise_for_status()

        # 解析 JSON 响应
        data = response.json()

        # 检查 API 响应状态
        if data.get("code", 0) != 0:
            error_msg = data.get("message", "Unknown API error")
            raise Exception(f"Bilibili API error: {error_msg}")

        results = []
        result_list = data.get("data", {}).get("result", [])

        for item in result_list[:num_results]:
            # 清理标题中的 HTML 标签
            title = item.get("title", "")
            clean_title = _HTML_TAG_PATTERN.sub("", title).strip()

            # 提取其他信息
            bvid = item.get("bvid", "")
            author = item.get("author", "")
            description = item.get("description", "")
            play = item.get("play", 0)

            # 格式化播放数
            if play >= 10000:
                play_str = f"{play / 10000:.1f}万"
            else:
                play_str = str(play)

            # 截取描述
            desc_short = description[:100] if description else ""

            results.append(
                {
                    "title": f"{clean_title} (👁{play_str}播放)",
                    "snippet": f"UP主: {author} | {desc_short}",
                    "url": f"https://www.bilibili.com/video/{bvid}",
                }
            )

        return results


class JDProvider(SearchProvider):
    """京东搜索 - 购物"""

    name = "jd"
    url = "https://search.jd.com/Search"
    category = "shopping"
    description = "京东商品搜索，适合查询商品价格、评价、购买建议"

    async def search(
        self, query: str, num_results: int, client: httpx.AsyncClient
    ) -> list[dict[str, str]]:
        response = await client.get(
            self.url,
            params={"keyword": query, "enc": "utf-8"},
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "text/html",
                "Accept-Language": "zh-CN,zh;q=0.9",
            },
        )
        response.raise_for_status()
        html = response.text

        results = []
        matches = _JD_ITEM_PATTERN.findall(html)[:num_results]

        for match in matches:
            # 提取商品名称
            title_match = _TITLE_EM_PATTERN.search(match)
            title = ""
            if title_match:
                title = _HTML_TAG_PATTERN.sub("", title_match.group(1)).strip()

            # 提取链接
            link_match = _HREF_SLASH_PATTERN.search(match)
            link = "https://" + link_match.group(1) if link_match else ""

            # 提取价格
            price_match = _PROMO_PRICE_PATTERN.search(match)
            price = ""
            if not price_match:
                price_match = _J_PRICE_PATTERN.search(match)
            if price_match:
                price = price_match.group(1).strip()

            # 提取评价数
            comment_match = _COMMENT_COUNT_PATTERN.search(match)
            comments = comment_match.group(1) if comment_match else ""

            snippet = f"价格: {price}" if price else ""
            if comments:
                snippet += f" | 评价: {comments}+" if snippet else f"评价: {comments}+"

            if title:
                results.append({"title": title[:100], "snippet": snippet, "url": link})

        return results


class TaobaoProvider(SearchProvider):
    """淘宝搜索 - 购物"""

    name = "taobao"
    url = "https://s.taobao.com/search"
    category = "shopping"
    description = "淘宝商品搜索，适合查询商品价格、店铺、款式"

    def _parse_html(self, html: str, num_results: int) -> list[dict[str, str]]:
        """解析淘宝搜索结果 HTML

        淘宝搜索页面使用 React 渲染，商品数据可能是动态加载的。
        尝试匹配多种可能的 HTML 格式。
        """
        results = []

        for item_pattern in _TAOBAO_PATTERNS:
            matches = item_pattern.findall(html)[:num_results]
            if matches:
                for match in matches:
                    # 提取商品名称
                    title_match = _TAOBAO_TITLE_PATTERN.search(match)
                    title = ""
                    if title_match:
                        title = _HTML_TAG_PATTERN.sub("", title_match.group(1)).strip()

                    # 提取链接
                    link_match = _HREF_PATTERN.search(match)
                    link = link_match.group(1) if link_match else ""
                    if link and not link.startswith("http"):
                        link = "https:" + link

                    # 提取价格
                    price_match = _TAOBAO_PRICE_PATTERN.search(match)
                    price = price_match.group(1) if price_match else ""

                    # 提取店铺
                    shop_match = _TAOBAO_SHOP_PATTERN.search(match)
                    shop = shop_match.group(1) if shop_match else ""

                    snippet = f"价格: ¥{price}" if price else ""
                    if shop:
                        snippet += f" | 店铺: {shop}" if snippet else f"店铺: {shop}"

                    if title:
                        results.append({"title": title[:100], "snippet": snippet, "url": link})

                if results:
                    return results[:num_results]

        # 如果上述模式都不匹配，尝试直接提取商品链接
        item_links = _TAOBAO_ITEM_LINKS_PATTERN.findall(html)[:num_results]
        for link, title in item_links:
            full_link = "https:" + link
            results.append({"title": title[:100], "snippet": "", "url": full_link})

        return results[:num_results]

    async def search(
        self, query: str, num_results: int, client: httpx.AsyncClient
    ) -> list[dict[str, str]]:
        # 构建完整 URL
        params = {"q": query, "ie": "utf8"}
        full_url = f"{self.url}?q={quote(query)}&ie=utf8"

        # 先尝试 HTTP 请求
        try:
            response = await client.get(
                self.url,
                params=params,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Accept": "text/html",
                    "Accept-Language": "zh-CN,zh;q=0.9",
                },
            )
            response.raise_for_status()
            results = self._parse_html(response.text, num_results)
            if results:
                return results
        except Exception as e:
            logger.error(f"❌ web.py:1332: 异常 - {e}\n{traceback.format_exc()}")

        return []


class DuckDuckGoSearchProvider(SearchProvider):
    """DuckDuckGo 搜索"""

    name = "duckduckgo"
    url = "https://api.duckduckgo.com/"
    category = "general"
    description = "隐私友好的搜索，适合快速获取摘要信息"

    async def search(
        self, query: str, num_results: int, client: httpx.AsyncClient
    ) -> list[dict[str, str]]:
        response = await client.get(
            self.url,
            params={"q": query, "format": "json", "no_html": 1, "skip_disambig": 1},
        )
        response.raise_for_status()
        data = response.json()

        results = []

        # 抽象结果
        abstract = data.get("Abstract")
        if abstract:
            results.append(
                {
                    "title": data.get("Heading", "摘要"),
                    "snippet": abstract,
                    "url": data.get("AbstractURL", ""),
                }
            )

        # 相关主题
        for topic in data.get("RelatedTopics", [])[:num_results]:
            if isinstance(topic, dict) and "Text" in topic:
                results.append(
                    {
                        "title": topic.get("FirstURL", "").split("/")[-1].replace("_", " "),
                        "snippet": topic.get("Text", ""),
                        "url": topic.get("FirstURL", ""),
                    }
                )

        return results[:num_results]


class GoogleSearchProvider(SearchProvider):
    """Google 搜索"""

    name = "google"
    url = "https://www.google.com/search"
    category = "general"
    description = "Google通用搜索，全球最大搜索引擎，英文效果极佳"

    def _parse_html(self, html: str, num_results: int) -> list[dict[str, str]]:
        """解析 Google 搜索结果 HTML"""
        results = []

        matches = _GOOGLE_RESULT_PATTERN.findall(html)[:num_results]

        for link, title_html in matches:
            title = _HTML_TAG_PATTERN.sub("", title_html).strip()
            if not title:
                continue

            # 处理链接
            real_url = link
            if link.startswith("/url?q="):
                # 旧格式：提取真实 URL
                from urllib.parse import unquote

                real_url = unquote(link.replace("/url?q=", "").split("&")[0])
            elif link.startswith("/search") or link.startswith("/webhp"):
                # 跳过 Google 内部链接
                continue
            elif not link.startswith("http"):
                continue

            results.append({"title": title[:150], "snippet": "", "url": real_url})

        return results

    async def search(
        self, query: str, num_results: int, client: httpx.AsyncClient
    ) -> list[dict[str, str]]:
        # Google 直接使用 Playwright（HTTP 必然失败）
        full_url = f"{self.url}?q={quote(query)}&num={num_results}"

        return []


# 搜索源列表（按类别组织）
# 注：已移除反爬虫严重的搜索源（bing_news, zhihu, jd, github）
SEARCH_PROVIDERS: list[SearchProvider] = [
    # 通用搜索
    BingSearchProvider(),
    DuckDuckGoSearchProvider(),
    GoogleSearchProvider(),
    # 新闻搜索
    SinaNewsProvider(),
    # 财经搜索
    SinaFinanceProvider(),
    SinaStockLookupProvider(),
]

# 搜索源名称到实例的映射
PROVIDER_MAP: dict[str, SearchProvider] = {p.name: p for p in SEARCH_PROVIDERS}


def _get_provider_descriptions() -> str:
    """生成搜索源描述文本（用于工具说明）"""
    lines = ["可用搜索源："]
    lines.extend(f"  - {p.name}: {p.description}" for p in SEARCH_PROVIDERS)
    return "\n".join(lines)


class WebSearchTool(Tool):
    """网络搜索工具"""

    # 预编译关键词正则表达式（用于_select_providers）
    _STOCK_CODE_PATTERN = re.compile(r"^[0-9]{6}$|^[a-zA-Z]{1,2}\d+$")
    _FINANCE_PATTERN = re.compile(
        r"股票|股价|A股|港股|美股|涨停|跌停|期货|基金|理财|投资|"
        r"比特币|btc|eth|以太坊|加密货币|虚拟币|行情|K线|证券|金融|财经"
    )
    _COMMODITY_PATTERN = re.compile(
        r"大宗商品|原材料|化工|塑料|橡胶|有色金属|黑色金属|钢材|"
        r"铜价|铝价|石油|原油|天然气|煤炭|焦炭|农产品|粮食|大豆|"
        r"玉米|小麦|棉花|价格走势|现货价格|批发价格|commodity|原材料价格"
    )
    _NEWS_PATTERN = re.compile(r"新闻|最新|今天|热点|资讯|消息|news|latest")
    _SHOPPING_PATTERN = re.compile(
        r"购买|买|价格|多少钱|便宜|优惠|折扣|京东|淘宝|天猫|拼多多|"
        r"商城|商品|产品|型号|款式|shop|buy|price|cheap"
    )
    _VIDEO_PATTERN = re.compile(r"视频|教程|演示|讲解|video|tutorial|b站|哔哩哔哩")
    _CODE_PATTERN = re.compile(r"代码|github|开源|库|api|code|python|javascript|java|golang")
    _QA_PATTERN = re.compile(r"如何|怎么|为什么|什么|怎样|经验|推荐|评价")
    _WIKI_PATTERN = re.compile(r"是什么|定义|介绍|概念|含义|意思")

    @property
    def name(self) -> str:
        return "websearch"

    @property
    def description(self) -> str:
        return f"""使用搜索引擎搜索网络内容。返回搜索结果摘要。

{_get_provider_descriptions()}

不指定 provider 时自动选择合适的搜索源。"""

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索查询",
                },
                "provider": {
                    "type": "string",
                    "description": (
                        f"搜索源选择: {', '.join(p.name for p in SEARCH_PROVIDERS)}。"
                        "不指定则自动选择。"
                    ),
                    "default": "",
                },
                "num_results": {
                    "type": "integer",
                    "description": "返回结果数量（默认10）",
                    "default": 10,
                },
                "show_status": {
                    "type": "boolean",
                    "description": "显示搜索源健康状态报告",
                    "default": False,
                },
                "reset_provider": {
                    "type": "string",
                    "description": "重置指定搜索源的健康状态（设为'all'重置全部）",
                    "default": "",
                },
            },
            "required": ["query"],
        }

    async def execute(self, **kwargs: Any) -> str:
        """执行网络搜索（支持指定搜索源或多源并发搜索）"""
        import asyncio

        query: str = kwargs.get("query", "")
        provider_name: str = kwargs.get("provider", "")
        num_results: int = kwargs.get("num_results", 10)
        show_status: bool = kwargs.get("show_status", False)
        reset_provider: str = kwargs.get("reset_provider", "")

        # 显示状态报告
        if show_status:
            return _health_manager.get_status_report()

        # 重置搜索源状态
        if reset_provider:
            if reset_provider == "all":
                _health_manager.reset()
                return "已重置所有搜索源的健康状态"
            _health_manager.reset(reset_provider)
            return f"已重置搜索源 '{reset_provider}' 的健康状态"

        # 如果指定了搜索源，只使用该源
        if provider_name and provider_name in PROVIDER_MAP:
            provider = PROVIDER_MAP[provider_name]

            # 检查可用性
            available, reason = _health_manager.is_available(provider.name)
            if not available:
                return f"搜索源 {provider.name} 不可用: {reason}"

            async with httpx.AsyncClient(timeout=30.0) as client:
                try:
                    results = await provider.search(query, num_results, client)
                    _health_manager.record_success(provider.name)
                    if results:
                        url_display = _format_url_display(f"{provider.url}?q={query}")
                        return self._format_results(
                            query, results[:num_results], url_display, provider.name
                        )
                    return f"未找到关于 '{query}' 的结果（来源: {provider.name}）"
                except Exception as e:
                    import traceback

                    logger.error(f"❌ web.py:1540: Exception - {e}\n{traceback.format_exc()}")
                    _health_manager.record_failure(provider.name, str(e))
                    return f"搜索失败 ({provider.name}): {e}"

        # 未指定搜索源，并发请求多个合适的源（过滤不可用的）
        all_providers = self._select_providers(query)

        # 过滤不可用的搜索源
        providers_to_try = []
        skipped_providers = []
        for p in all_providers:
            available, reason = _health_manager.is_available(p.name)
            if available:
                providers_to_try.append(p)
            else:
                skipped_providers.append((p.name, reason))

        if not providers_to_try:
            skipped_info = ", ".join(f"{n}({r})" for n, r in skipped_providers[:3])
            return f"所有搜索源均不可用: {skipped_info}"

        async def search_with_provider(
            provider: SearchProvider, client: httpx.AsyncClient
        ) -> tuple[str, list[dict[str, str]], str]:
            """带错误处理和健康状态记录的搜索"""
            try:
                results = await provider.search(query, num_results, client)
                _health_manager.record_success(provider.name)
                return (provider.name, results, "")
            except Exception as e:
                import traceback

                logger.error(f"❌ web.py:1570: Exception - {e}\n{traceback.format_exc()}")
                _health_manager.record_failure(provider.name, str(e))
                return (provider.name, [], str(e))

        async with httpx.AsyncClient(timeout=30.0) as client:
            # 创建所有事务
            tasks = [asyncio.create_task(search_with_provider(p, client)) for p in providers_to_try]

            # 收集结果
            all_results: list[tuple[str, list[dict[str, str]]]] = []
            errors: list[str] = []
            first_provider = None
            first_url_display = ""

            # 等待事务完成，收到第一个有效结果就停止
            remaining_tasks = set(tasks)
            max_wait_time = 10.0  # 最多等待10秒
            start_time = asyncio.get_event_loop().time()

            while remaining_tasks:
                elapsed = asyncio.get_event_loop().time() - start_time
                if elapsed >= max_wait_time:
                    # 超时，取消剩余事务
                    for t in remaining_tasks:
                        t.cancel()
                    break

                try:
                    # 等待任意一个事务完成
                    done, remaining_tasks = await asyncio.wait(
                        remaining_tasks,
                        timeout=min(3.0, max_wait_time - elapsed),
                        return_when=asyncio.FIRST_COMPLETED,
                    )

                    for task in done:
                        try:
                            provider_name_result, results, error = task.result()
                            if results:
                                # 收到有效结果
                                all_results.append((provider_name_result, results))
                                if not first_provider:
                                    first_provider = PROVIDER_MAP.get(provider_name_result)
                                    first_url_display = (
                                        _format_url_display(f"{first_provider.url}?q={query}")
                                        if first_provider
                                        else ""
                                    )
                                # 立即取消其他事务并返回
                                for t in remaining_tasks:
                                    t.cancel()
                                remaining_tasks = set()
                                break
                            elif error:
                                errors.append(f"{provider_name_result}: {error}")
                        except asyncio.CancelledError:
                            pass
                        except Exception as e:
                            import traceback

                            logger.error(
                                f"❌ web.py:1628: Exception - {e}\n{traceback.format_exc()}"
                            )
                            errors.append(f"事务异常: {e}")

                except TimeoutError:
                    # 等待超时，继续下一轮
                    pass

        if all_results:
            # 合并结果
            seen_titles: set[str] = set()
            merged_results: list[dict[str, str]] = []
            used_providers: list[str] = []

            for pname, results in all_results:
                used_providers.append(pname)
                for r in results:
                    title_key = r.get("title", "")[:50]
                    if title_key and title_key not in seen_titles:
                        seen_titles.add(title_key)
                        merged_results.append(r)

            return self._format_results(
                query,
                merged_results[:num_results],
                first_url_display,
                "+".join(used_providers[:3]),
            )

        if errors:
            return f"搜索失败: {'; '.join(errors[:3])}"

        return f"未找到关于 '{query}' 的结果（尝试了所有搜索源）"

    def _select_providers(self, query: str) -> list[SearchProvider]:
        """根据查询内容选择合适的搜索源"""
        query_lower = query.lower()

        # 判断是否是股票代码查询（6位数字或字母数字组合）
        is_stock_code = bool(self._STOCK_CODE_PATTERN.match(query.strip()))

        if is_stock_code:
            # 股票代码直接查询
            return [p for p in SEARCH_PROVIDERS if p.name in ("sina_stock", "sina_finance", "bing")]
        if self._FINANCE_PATTERN.search(query_lower):
            return [
                p
                for p in SEARCH_PROVIDERS
                if p.name in ("sina_finance", "sina_stock", "bing", "duckduckgo")
            ]

        # 大宗商品类查询（使用通用搜索）
        if self._COMMODITY_PATTERN.search(query_lower):
            return [p for p in SEARCH_PROVIDERS if p.name in ("bing", "duckduckgo")]

        # 新闻类查询
        if self._NEWS_PATTERN.search(query_lower):
            return [p for p in SEARCH_PROVIDERS if p.name in ("sina_news", "bing", "duckduckgo")]

        # 购物类查询（使用通用搜索）
        if self._SHOPPING_PATTERN.search(query_lower):
            return [p for p in SEARCH_PROVIDERS if p.name in ("bing", "duckduckgo")]

        # 视频类查询
        if self._VIDEO_PATTERN.search(query_lower):
            return [p for p in SEARCH_PROVIDERS if p.name in ("bing", "duckduckgo")]

        # 编程类查询
        if self._CODE_PATTERN.search(query_lower):
            return [p for p in SEARCH_PROVIDERS if p.name in ("github", "bing", "duckduckgo")]

        # 问答类查询（知乎已移除，使用通用搜索）
        if self._QA_PATTERN.search(query_lower):
            return [p for p in SEARCH_PROVIDERS if p.name in ("bing", "duckduckgo")]

        # 百科类查询（百度百科已移除，使用通用搜索）
        if self._WIKI_PATTERN.search(query_lower):
            return [p for p in SEARCH_PROVIDERS if p.name in ("bing", "duckduckgo")]

        # 默认使用通用搜索
        return [p for p in SEARCH_PROVIDERS if p.category == "general"]

    def _format_results(
        self, query: str, results: list[dict[str, str]], url_display: str, provider_name: str
    ) -> str:
        """格式化搜索结果"""
        output: list[str] = []

        if url_display:
            output.append(url_display)

        output.append(f"搜索结果: {query} (来源: {provider_name})\n")

        for i, r in enumerate(results, 1):
            output.append(f"{i}. {r['title']}")
            if r.get("snippet"):
                output.append(f"   {r['snippet']}")
            if r.get("url"):
                output.append(f"   URL: {r['url']}")
            output.append("")

        return "\n".join(output)


class WebFetchTool(Tool):
    """网页获取工具"""

    @property
    def name(self) -> str:
        return "webfetch"

    @property
    def description(self) -> str:
        return "获取指定URL的网页内容。支持Markdown和纯文本格式。"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "要获取的URL",
                },
                "format": {
                    "type": "string",
                    "enum": ["markdown", "text", "html"],
                    "description": "返回格式（默认markdown）",
                    "default": "markdown",
                },
                "timeout": {
                    "type": "integer",
                    "description": "超时时间（毫秒，默认30000）",
                    "default": 30000,
                },
            },
            "required": ["url"],
        }

    async def execute(self, **kwargs: Any) -> str:
        """获取网页内容"""
        url: str = kwargs.get("url", "")
        format_type: str = kwargs.get("format", "markdown")
        timeout: int = kwargs.get("timeout", 30000)

        # 显示网络访问信息
        url_display = _format_url_display(url)

        try:
            async with httpx.AsyncClient(timeout=timeout / 1000) as client:
                response = await client.get(
                    url,
                    follow_redirects=True,
                    headers={
                        "User-Agent": "Mozilla/5.0 (compatible; PengBot/1.0)",
                        "Accept": "text/html,application/xhtml+xml",
                    },
                )
                response.raise_for_status()

            content_type = response.headers.get("content-type", "")

            # 如果是JSON，直接返回
            if "application/json" in content_type:
                try:
                    data = response.json()
                    result = json.dumps(data, ensure_ascii=False, indent=2)
                    if url_display:
                        return f"{url_display}\n\n{result}"
                    return result
                except json.JSONDecodeError:
                    pass

            # 获取文本内容
            text = response.text

            if format_type == "html":
                if url_display:
                    return f"{url_display}\n\n{text}"
                return text

            # 简单的HTML到文本转换
            if format_type == "text" or format_type == "markdown":
                # 移除脚本和样式
                text = _SCRIPT_TAG_PATTERN.sub("", text)
                text = _STYLE_TAG_PATTERN.sub("", text)
                text = _HTML_COMMENT_PATTERN.sub("", text)

                # 提取title
                title_match = _HTML_TITLE_PATTERN.search(text)
                title = title_match.group(1).strip() if title_match else ""

                # 移除HTML标签
                text = _HTML_TAG_PATTERN.sub(" ", text)

                # 清理空白
                text = _WHITESPACE_PATTERN.sub(" ", text)
                text = text.strip()

                # 限制长度
                if len(text) > 10000:
                    text = text[:10000] + "\n\n... [内容已截断]"

                # 构建输出
                output_parts = []
                if url_display:
                    output_parts.append(url_display)
                    output_parts.append("")

                if title:
                    output_parts.append(f"# {title}\n\n{text}")
                else:
                    output_parts.append(text)

                return "\n".join(output_parts)

            if url_display:
                return f"{url_display}\n\n{text}"
            return text

        except httpx.HTTPError as e:
            import traceback

            logger.error(f"❌ 获取网页失败: {e}\n{traceback.format_exc()}")
            raise ToolError(f"获取网页失败: {e}") from e
        except Exception as e:
            import traceback

            logger.error(f"❌ 获取网页失败: {e}\n{traceback.format_exc()}")
            raise ToolError(f"获取网页失败: {e}") from e
