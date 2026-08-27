from unittest.mock import patch

from lxml import etree

from app.application.security.cookie import CookieHelper


class _CookieContext:
    """提供登录测试所需的最小浏览器上下文。"""

    @staticmethod
    def cookies() -> list[dict[str, str]]:
        """返回登录后的会话 Cookie。"""
        return [{"name": "session", "value": "authenticated"}]


class _CookiePage:
    """模拟首页跳转登录页并提交表单的浏览器页面。"""

    def __init__(self) -> None:
        self.url = "https://dicmusic.example/"
        self.context = _CookieContext()
        self.fills: list[tuple[str, str]] = []
        self.goto_calls: list[tuple[str, dict]] = []
        self.submitted = False

    def content(self) -> str:
        """按当前页面阶段返回对应 HTML。"""
        if self.submitted:
            return '<html><body><a href="logout.php">退出</a></body></html>'
        if self.url.endswith("/login.php"):
            return (
                '<html><body><form action="login.php">'
                '<input name="username">'
                '<input name="password" type="password">'
                '<input type="submit" value="登录">'
                "</form></body></html>"
            )
        return '<html><body><a href="login.php">登录</a></body></html>'

    def goto(self, url: str, **kwargs) -> None:
        """记录并切换页面地址。"""
        self.url = url
        self.goto_calls.append((url, kwargs))

    @staticmethod
    def wait_for_load_state(_state: str, timeout: int) -> None:
        """模拟页面已完成所需加载。"""

    @staticmethod
    def wait_for_selector(_selector: str, *args, **kwargs) -> None:
        """模拟表单元素已经可用。"""

    @staticmethod
    def query_selector(_selector: str):
        """当前页面没有保持登录复选框或验证码。"""
        return None

    def fill(self, selector: str, value: str) -> None:
        """记录表单填充值。"""
        self.fills.append((selector, value))

    def click(self, _selector: str) -> None:
        """模拟提交登录表单。"""
        self.submitted = True

    @staticmethod
    def evaluate(_expression: str) -> str:
        """返回浏览器 User-Agent。"""
        return "Browser UA"


def test_cookie_login_follows_same_origin_login_link():
    """首页仅提供登录链接时应进入同源登录页后完成 Cookie 获取。"""
    page = _CookiePage()

    def run_action(**kwargs):
        return kwargs["callback"](page)

    with patch(
        "app.application.security.cookie.PlaywrightHelper.action",
        side_effect=run_action,
    ):
        cookie, ua, message = CookieHelper().get_site_cookie_ua(
            url="https://dicmusic.example/",
            username="moviepilot",
            password="secret-password",
            timeout=30,
        )

    assert cookie == "session=authenticated; "
    assert ua == "Browser UA"
    assert message == ""
    assert page.goto_calls == [
        (
            "https://dicmusic.example/login.php",
            {"wait_until": "domcontentloaded", "timeout": 30000},
        )
    ]
    assert page.fills == [
        ('//input[@name="username"]', "moviepilot"),
        ('//input[@name="password"]', "secret-password"),
    ]


def test_cookie_login_rejects_cross_origin_login_link():
    """跨域登录链接不得成为账号密码填充目标。"""
    login_url = CookieHelper._find_login_page_url(
        etree.HTML(
            '<html><body><a href="https://other.example/login.php">登录</a></body></html>'
        ),
        "https://dicmusic.example/",
    )

    assert login_url is None
