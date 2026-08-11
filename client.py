from __future__ import annotations

from dataclasses import dataclass

import asyncio
from typing import Any
from urllib.parse import urljoin

import httpx
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class CbgClientError(RuntimeError):
    """Raised when CBG returns an unusable response."""


class MobileAuthRequired(CbgClientError):
    """Raised when CBG requires SMS (mobile) verification.

    Carries the auth_info needed to send/verify the SMS code.
    """

    def __init__(self, message: str, auth_info: dict | None = None):
        super().__init__(message)
        self.message = message
        self.auth_info = auth_info or {}
        self.op_type = self.auth_info.get("op_type") or "general_check"
        self.op_id = self.auth_info.get("op_id") or "general_check"


@dataclass(slots=True)
class CbgClientConfig:
    base_url: str = "https://yjwujian.cbg.163.com"
    api_prefix: str = "/cgi/api"
    cookie: str = ""
    timeout_seconds: int = 30
    request_interval: float = 0.5


class YjCbgClient:
    def __init__(self, config: CbgClientConfig):
        self.config = config
        self._client: httpx.AsyncClient | None = None
        self._session_ready = False
        # 风控退避重试次数与基础延迟（秒）
        self._captcha_retries = 2
        self._captcha_retry_delay = 2.0
        self._last_request_time = 0.0

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def search(self, keyword: str, page: int = 1, count: int = 5) -> dict[str, Any]:
        """Generic keyword search (all types)."""
        params = {
            "search_word": keyword,
            "key": keyword,
            "page": page,
            "count": count,
            "search_type": "role",
        }
        return await self._get_api("keyword_query", params)

    async def get_skin_types(self, keyword: str = "", page: int = 1, count: int = 30, kindid: int = 3) -> dict[str, Any]:
        """Get aggregated skin type list.

        kindid: 3 = hero skins, 4 = weapon skins.
        """
        params = {
            "client_type": "h5",
            "count": count,
            "page": page,
            "order_by": "selling_time DESC",
            "query_onsale": 1,
            "kindid": kindid,
        }
        return await self._get_api("get_aggregate_equip_type_list", params)

    async def get_equip_list(self, equip_type: str, page: int = 1, count: int = 15, order_by: str = "") -> dict[str, Any]:
        """Get specific equip items by equip_type using recommend.py.

        order_by: e.g. "price ASC", "price DESC", "collect_num DESC", "collect_num ASC".
        When empty, the API uses default recommendation order.
        """
        params = {
            "client_type": "h5",
            "act": "recommd_by_role",
            "equip_type": equip_type,
            "kindid": 3,
            "search_type": "role_skin",
            "page": page,
            "count": count,
        }
        if order_by.strip():
            params["order_by"] = order_by.strip()
        return await self._get_absolute("/cgi-bin/recommend.py", params)

    async def get_sms_code(self, op_type: str = "general_check", op_id: str = "general_check") -> dict[str, Any]:
        """Send an SMS verification code to the bound mobile phone.

        Called when the interface triggers MOBILE_AUTH. NetEase sends a
        one-time code to the account's bound phone; the user must input it.
        """
        params: dict[str, Any] = {
            "client_type": "h5",
            "op_type": op_type,
            "op_id": op_id,
            "_method": "POST",
        }
        return await self._get_absolute("/cgi/api/get_sms_code", params)

    async def verify_sms_code(self, sms_code: str, op_type: str = "general_check", op_id: str = "general_check") -> dict[str, Any]:
        """Submit the SMS code that the user entered to complete verification.

        On success the interface returns OK and the following requests are
        allowed through.
        """
        params: dict[str, Any] = {
            "client_type": "h5",
            "op_type": op_type,
            "op_id": op_id,
            "sms_code": sms_code,
            "_method": "POST",
        }
        return await self._get_absolute("/cgi/api/verify_sms_code", params)

    async def search_skins(self, keyword: str, page: int = 1, count: int = 15) -> dict[str, Any]:
        """Search role skins using keyword_query with kindid=3 (skins only)."""
        params = {
            "search_word": keyword,
            "key": keyword,
            "kindid": 3,
            "search_type": "role_skin",
            "page": page,
            "count": count,
        }
        return await self._get_api("keyword_query", params)

    async def recommend_by_role(self, role_name: str, page: int = 1, count: int = 15) -> dict[str, Any]:
        """Get recommendations by role name."""
        params = {
            "client_type": "h5",
            "act": "recommd_by_role",
            "kindid": 3,
            "search_type": "role_skin",
            "role_name": role_name,
            "page": page,
            "count": count,
        }
        return await self._get_absolute("/cgi-bin/recommend.py", params)

    async def detail(self, serverid: str, ordersn: str) -> dict[str, Any]:
        """Get item detail."""
        params = {"serverid": serverid, "ordersn": ordersn}
        try:
            return await self._get_absolute("/get_equip_desc", params)
        except CbgClientError:
            return await self._get_api("get_equip_desc", params)

    async def _get_api(self, endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
        prefix = "/" + self.config.api_prefix.strip("/")
        return await self._get_absolute(f"{prefix}/{endpoint.strip('/')}", params)

    async def _get_absolute(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        client = self._ensure_client()
        url = urljoin(self.config.base_url.rstrip("/") + "/", path.lstrip("/"))
        last_err: CbgClientError | None = None
        # 重试循环：处理会话建立、网络异常、风控（临时问题可自动恢复）
        method = (params or {}).pop("_method", "GET").upper()
        for attempt in range(self._captcha_retries + 1):
            if attempt > 0:
                await asyncio.sleep(self._captcha_retry_delay * attempt)
            # 全局限流：所有请求之间保持最小间隔，降低触发风控的概率
            interval = max(0.1, float(getattr(self.config, "request_interval", 0.5)))
            now = asyncio.get_event_loop().time()
            wait = interval - (now - self._last_request_time)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request_time = asyncio.get_event_loop().time()
            try:
                # Establish session first (visit homepage to get required cookies)
                if not self._session_ready:
                    await client.get(self.config.base_url.rstrip("/") + "/")
                    self._session_ready = True
                clean = _clean_params(params)
                if method == "POST":
                    response = await client.post(url, data=clean)
                else:
                    response = await client.get(url, params=clean)
            except httpx.HTTPError as exc:
                last_err = CbgClientError(f"网络连接失败：{type(exc).__name__}")
                continue
            if response.status_code >= 400:
                last_err = CbgClientError(f"HTTP {response.status_code}: {response.text[:200]}")
                continue
            try:
                data = response.json()
            except ValueError as exc:
                last_err = CbgClientError(f"接口没有返回 JSON：{response.text[:200]}")
                continue
            if not isinstance(data, dict):
                last_err = CbgClientError("接口返回格式不是对象")
                continue
            # Detect risk control / captcha / session timeout so the plugin can surface a clear message
            if data.get("status_code") == "CAPTCHA_AUTH" or data.get("status") == 3:
                last_err = CbgClientError(
                    "藏宝阁触发了安全验证（CAPTCHA_AUTH）。请稍后重试，或填写有效的登录 Cookie 后重试。"
                )
                continue
            if data.get("status_code") == "SESSION_TIMEOUT" or data.get("status") == 2:
                last_err = CbgClientError(
                    "藏宝阁会话已过期（SESSION_TIMEOUT）。请在插件配置中填写有效的登录 Cookie 后重试。"
                )
                continue
            if data.get("status_code") == "MOBILE_AUTH" or data.get("status") == 6:
                auth_info = data.get("auth_info") or {}
                op_type = auth_info.get("op_type") or "general_check"
                op_id = auth_info.get("op_id") or "general_check"
                raise MobileAuthRequired(
                    "藏宝阁接口要求手机验证（MOBILE_AUTH）。已准备发送短信验证码。",
                    {"op_type": op_type, "op_id": op_id},
                )
            if data.get("status_code") == "AUTO_LOGIN" or data.get("status") == 8:
                # 网易返回"更新登陆状态"：会话已失效，需重新登录。
                # 不处理的话插件会静默拿到空数据，表现为"搜索不到"却不报错。
                last_err = CbgClientError(
                    "藏宝阁会话已失效，需要重新登录（AUTO_LOGIN / 更新登陆状态）。"
                    "请在插件配置中更新为最新有效的登录 Cookie 后重试。"
                )
                continue
            if data.get("status_code") == "ERR" or data.get("status") == 0:
                # 接口繁忙/限流（"系统繁忙"）。退避后重试，仍繁忙则明确报错，
                # 避免静默拿到空结果。
                last_err = CbgClientError(
                    "藏宝阁接口繁忙/限流（系统繁忙），请稍后重试。"
                    "若频繁出现，请降低请求频率或稍等风控冷却。"
                )
                await asyncio.sleep(2 + attempt * 2)
                continue
            return data
        raise last_err if last_err is not None else CbgClientError("未知错误")

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            headers = {
                "Accept": "application/json, text/plain, */*",
                "Referer": self.config.base_url.rstrip("/") + "/",
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/126.0.0.0 Safari/537.36"
                ),
            }
            if self.config.cookie.strip():
                headers["Cookie"] = self.config.cookie.strip()
            self._client = httpx.AsyncClient(
                headers=headers,
                timeout=max(3, int(self.config.timeout_seconds)),
                follow_redirects=True,
                verify=False,
            )
        return self._client

    
def _clean_params(params: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in params.items() if v not in (None, "")}