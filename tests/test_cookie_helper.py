from lxml import etree

from app.application.security.cookie import (
    CookieHelper,
    configure_cookie_ports,
    reset_cookie_ports,
)


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


class _CaptchaElement:
    """模拟可见验证码输入框。"""

    def __init__(self, page: "_CaptchaPage") -> None:
        """绑定所属页面，以便记录验证码填写。"""
        self._page = page

    def is_visible(self) -> bool:
        """返回验证码输入框可见。"""
        return True

    def fill(self, value: str) -> None:
        """记录验证码内容。"""
        self._page.captcha_values.append(value)


class _CaptchaPage:
    """模拟使用非固定字段名并需要多次 OCR 尝试的登录页。"""

    def __init__(self) -> None:
        """初始化登录页状态。"""
        self.url = "https://pt.example/login"
        self.context = _CookieContext()
        self.fills: list[tuple[str, str]] = []
        self.captcha_values: list[str] = []
        self.captcha_refreshes = 0
        self.submitted = False

    def content(self) -> str:
        """按提交状态返回登录表单或已登录页面。"""
        if self.submitted:
            return '<html><body><a href="logout.php">退出</a></body></html>'
        return (
            '<html><body><form action="login">'
            '<input name="txt_email">'
            '<input type="password">'
            '<input name="verification_code">'
            '<img alt="captcha" src="/captcha.png">'
            '<button type="button">登录</button>'
            "</form></body></html>"
        )

    @staticmethod
    def wait_for_load_state(_state: str, timeout: int) -> None:
        """模拟页面加载完成。"""

    @staticmethod
    def wait_for_selector(_selector: str, *args, **kwargs) -> None:
        """模拟输入框或按钮已经出现。"""

    def query_selector(self, selector: str):
        """仅返回验证码输入框，其他可选元素视为不存在。"""
        if "verification_code" in selector:
            return _CaptchaElement(self)
        return None

    def fill(self, selector: str, value: str) -> None:
        """记录普通字段的填写。"""
        self.fills.append((selector, value))

    def click(self, selector: str, **kwargs) -> None:
        """区分验证码刷新和登录提交。"""
        if "img" in selector:
            self.captcha_refreshes += 1
            return
        self.submitted = True

    @staticmethod
    def evaluate(_expression: str) -> str:
        """返回浏览器 User-Agent。"""
        return "Captcha Browser UA"


def test_cookie_login_infers_common_fields_and_retries_captcha():
    """通用字段推断、验证码刷新重试和登录按钮点击应协同工作。"""
    page = _CaptchaPage()
    ocr_results = iter(("", "", "ABC123"))

    class FakeBrowserPort:
        """把 Application 登录回调运行在验证码页面上。"""

        @staticmethod
        def action(**kwargs):
            """执行登录回调。"""
            return kwargs["callback"](page)

    class HttpPort:
        """提供验证码图片字节。"""

        @staticmethod
        def fetch(**_kwargs):
            """返回固定验证码图片。"""
            return b"captcha-image"

    class OcrPort:
        """按预设顺序返回 OCR 结果。"""

        @staticmethod
        def recognize(_image_data: bytes) -> str:
            """返回下一次识别结果。"""
            return next(ocr_results)

    configure_cookie_ports(browser=FakeBrowserPort(), http=HttpPort(), ocr=OcrPort())
    try:
        cookie, ua, message = CookieHelper().get_site_cookie_ua(
            url=page.url,
            username="moviepilot@example.com",
            password="dummy-password",
            timeout=30,
        )
    finally:
        reset_cookie_ports()

    assert cookie == "session=authenticated; "
    assert ua == "Captcha Browser UA"
    assert message == ""
    assert page.fills[:2] == [
        ('//input[@name="txt_email"]', "moviepilot@example.com"),
        ('//input[@type="password"]', "dummy-password"),
    ]
    assert page.fills[2] == ('//input[@name="verification_code"]', "ABC123")
    assert page.captcha_refreshes == 2
    assert page.submitted is True


def test_cookie_click_falls_back_to_evaluate_when_overlay_blocks_pointer():
    """提交按钮被遮挡时应继续尝试 JavaScript 点击。"""

    class FallbackPage:
        """让普通、强制和元素点击都失败的页面。"""

        def click(self, _selector: str, **_kwargs) -> None:
            """模拟指针点击被遮挡。"""
            raise RuntimeError("pointer-events overlay")

        def query_selector(self, _selector: str):
            """返回一个同样无法指针点击的元素。"""
            return self

        def evaluate(self, _script: str, _selector: str) -> bool:
            """模拟 XPath JavaScript 点击成功。"""
            return True

        def is_visible(self) -> bool:
            """满足元素接口。"""
            return True

    page = FallbackPage()
    CookieHelper._click_with_fallback(page, "//button[@type='submit']")


def test_cookie_login_follows_same_origin_login_link():
    """首页仅提供登录链接时应进入同源登录页后完成 Cookie 获取。"""
    page = _CookiePage()

    class FakeBrowserPort:
        """把 Application 登录回调运行在测试页面上。"""

        @staticmethod
        def action(**kwargs):
            """执行登录回调。"""
            return kwargs["callback"](page)

    class UnusedPort:
        """标记当前无验证码流程不得触达的端口。"""

        def __getattr__(self, _name):
            """拒绝任何意外调用。"""
            raise AssertionError("无验证码登录不应使用该端口")

    configure_cookie_ports(
        browser=FakeBrowserPort(), http=UnusedPort(), ocr=UnusedPort()
    )
    try:
        cookie, ua, message = CookieHelper().get_site_cookie_ua(
            url="https://dicmusic.example/",
            username="moviepilot",
            password="secret-password",
            timeout=30,
        )
    finally:
        reset_cookie_ports()

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


def test_cookie_login_prefers_password_form_over_unrelated_username_input():
    """页面有搜索框时，用户名候选应来自同一个密码表单。"""
    html = etree.HTML(
        '<html><body><input name="username">'
        '<form><input type="text" name="member_id">'
        '<input type="password" name="secret"></form></body></html>'
    )

    username_xpath, password_xpath = CookieHelper._find_login_fields(html)

    assert username_xpath is not None
    assert password_xpath is not None
    assert html.xpath(username_xpath)[0].get("name") == "member_id"
    assert html.xpath(password_xpath)[0].get("name") == "secret"
