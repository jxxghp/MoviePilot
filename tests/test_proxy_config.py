from app.runtime.config import Settings


PROXY_ENV_NAMES = (
    "PROXY_HOST",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "http_proxy",
    "https_proxy",
)


def clear_proxy_env(monkeypatch) -> None:
    """
    清理测试进程中的代理环境变量。
    """
    for name in PROXY_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def test_proxy_prefers_proxy_host_over_standard_env(monkeypatch) -> None:
    """
    PROXY_HOST 应优先于标准代理环境变量。
    """
    clear_proxy_env(monkeypatch)
    monkeypatch.setenv("HTTPS_PROXY", "http://env-proxy.example.com:7890")

    settings = Settings(PROXY_HOST=" http://custom-proxy.example.com:7890 ")

    assert settings.PROXY == {
        "http": "http://custom-proxy.example.com:7890",
        "https": "http://custom-proxy.example.com:7890",
    }


def test_proxy_falls_back_to_standard_proxy_env(monkeypatch) -> None:
    """
    未配置 PROXY_HOST 时应读取 HTTP_PROXY 和 HTTPS_PROXY。
    """
    clear_proxy_env(monkeypatch)
    monkeypatch.setenv("HTTP_PROXY", "http://http-proxy.example.com:7890")
    monkeypatch.setenv("HTTPS_PROXY", "http://https-proxy.example.com:7890")

    settings = Settings(PROXY_HOST=None)

    assert settings.PROXY == {
        "http": "http://http-proxy.example.com:7890",
        "https": "http://https-proxy.example.com:7890",
    }


def test_proxy_reuses_single_standard_env_for_both_schemes(monkeypatch) -> None:
    """
    只配置单个标准代理环境变量时应同时用于 http 和 https。
    """
    clear_proxy_env(monkeypatch)
    monkeypatch.setenv("HTTP_PROXY", "http://http-proxy.example.com:7890")

    settings = Settings(PROXY_HOST=None)

    assert settings.PROXY == {
        "http": "http://http-proxy.example.com:7890",
        "https": "http://http-proxy.example.com:7890",
    }


def test_proxy_returns_none_without_any_proxy(monkeypatch) -> None:
    """
    未配置任何代理时应返回 None。
    """
    clear_proxy_env(monkeypatch)

    settings = Settings(PROXY_HOST=None)

    assert settings.PROXY is None
