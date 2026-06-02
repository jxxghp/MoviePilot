from __future__ import annotations

import unittest
from unittest.mock import patch

from app.helper.browser import PlaywrightHelper


class _FakePage:
    def __init__(self) -> None:
        self.headers = None
        self.loaded_url = None
        self.closed = False

    def set_extra_http_headers(self, headers: dict[str, str]) -> None:
        self.headers = headers

    def goto(self, url: str) -> None:
        self.loaded_url = url

    def wait_for_load_state(self, _state: str, timeout: int) -> None:
        self.timeout = timeout

    def content(self) -> str:
        return "<html>ok</html>"

    def close(self) -> None:
        self.closed = True


class _FakeContext:
    def __init__(self, page: _FakePage) -> None:
        self.page = page
        self.closed = False

    def new_page(self) -> _FakePage:
        return self.page

    def close(self) -> None:
        self.closed = True


class BrowserHelperTests(unittest.TestCase):
    def _assert_get_page_source_uses_cloakbrowser(self, emulation: str) -> None:
        page = _FakePage()
        context = _FakeContext(page)

        with patch("app.helper.browser.settings.BROWSER_EMULATION", emulation), \
                patch.object(
                    PlaywrightHelper,
                    "_PlaywrightHelper__launch_cloakbrowser_context",
                    return_value=context,
                ) as launch_context:
            source = PlaywrightHelper().get_page_source(
                url="https://example.com",
                cookies="uid=1",
                ua="UA",
                timeout=3,
            )

        self.assertEqual(source, "<html>ok</html>")
        launch_context.assert_called_once_with(
            headless=False,
            user_agent="UA",
            proxies=None,
        )
        self.assertEqual(page.headers, {"cookie": "uid=1"})
        self.assertEqual(page.loaded_url, "https://example.com")
        self.assertTrue(page.closed)
        self.assertTrue(context.closed)

    def test_default_emulation_uses_cloakbrowser_context(self):
        self._assert_get_page_source_uses_cloakbrowser("cloakbrowser")

    def test_legacy_playwright_emulation_uses_cloakbrowser_context(self):
        self._assert_get_page_source_uses_cloakbrowser("Playwright")

    def test_legacy_browser_type_constructor_is_accepted(self):
        page = _FakePage()
        context = _FakeContext(page)

        with patch.object(
            PlaywrightHelper,
            "_PlaywrightHelper__launch_cloakbrowser_context",
            return_value=context,
        ):
            source = PlaywrightHelper(browser_type="firefox").get_page_source(
                url="https://example.com"
            )

        self.assertEqual(source, "<html>ok</html>")
