"""受控外部网络探测应用服务。"""

from datetime import datetime
from typing import Any, Callable, Optional
from urllib.parse import urljoin, urlparse


class NetworkTestService:
    """执行服务端预定义目标的 HTTPS 连通性测试。"""

    def __init__(
        self,
        request_utils_cls: Callable[..., Any],
        settings_getter: Callable[[], Any],
        logger: Any,
        redirect_checker: Callable[[str, dict[str, Any]], bool],
        close_response: Callable[[Any], Any],
    ):
        """注入网络客户端、配置和安全边界，方便隔离测试。"""
        self.request_utils_cls = request_utils_cls
        self.settings_getter = settings_getter
        self.logger = logger
        self.redirect_checker = redirect_checker
        self.close_response = close_response

    async def execute(
        self,
        target: dict[str, Any],
        include: Optional[str] = None,
    ) -> tuple[bool, Optional[str], Optional[dict[str, int]]]:
        """请求目标并处理受控重定向，返回旧端点使用的结果三元组。"""
        start_time = datetime.now()
        url = target["url"]
        parsed = urlparse(url)
        if parsed.scheme.lower() != "https" or not parsed.netloc or parsed.username or parsed.password:
            return False, "测试地址无效", {"time": 0}
        if include:
            self.logger.debug("nettest include 参数已忽略，改为服务端固定校验")

        settings = self.settings_getter()
        request_utils = self.request_utils_cls(
            proxies=settings.get("PROXY") if target.get("proxy") else None,
            headers=target.get("headers"),
            timeout=10,
            ua=settings.get("NORMAL_USER_AGENT"),
            verify=True,
            follow_redirects=False,
        )
        result = None
        current_url = url
        redirect_count = 0
        while redirect_count <= 3:
            result = await request_utils.get_res(current_url, allow_redirects=False)
            if result is None or result.status_code not in {301, 302, 303, 307, 308}:
                break
            location = result.headers.get("location")
            if not location:
                break
            next_url = urljoin(current_url, location)
            if not self.redirect_checker(next_url, target):
                await self.close_response(result)
                self.logger.warning(f"拦截网络测试重定向: {current_url} -> {next_url}")
                return False, "测试目标发生了未授权跳转", None
            await self.close_response(result)
            current_url = next_url
            redirect_count += 1

        elapsed = round((datetime.now() - start_time).total_seconds() * 1000)
        timing = {"time": elapsed}
        if redirect_count > 3:
            return False, "测试目标重定向次数过多", None
        if result is None:
            return False, f"{target.get('proxy_name') or target.get('name')}无法连接", timing
        if result.status_code == 200:
            expected_text = target.get("expected_text")
            if expected_text and expected_text.lower() not in (result.text or "").lower():
                return False, target.get("invalid_message") or "无效响应", timing
            return True, None, timing
        if target.get("proxy_name"):
            message = f"{target['proxy_name']}已失效，错误码：{result.status_code}"
        else:
            message = f"错误码：{result.status_code}"
            if "github" in url:
                if result.status_code == 401:
                    message = "Github Token已失效，请检查配置"
                elif result.status_code in {403, 429}:
                    message = "触发限流，请配置Github Token"
        return False, message, timing
