"""
音乐规则引擎的受控装配；不注册测试用例。
"""

import pytest

from app.adapters.system import rust
from app.domain.meta import runtime
from app.domain.meta.metamusic import MetaMusic


@pytest.fixture(params=["python", "rust"])
def music_engine(request, monkeypatch):
    """
    分别验证纯 Python 和实际 Rust 解析器，恢复全局加速器配置。
    """
    if request.param == "rust":
        assert rust.is_available(), "锁定环境应安装 moviepilot-rust"
        monkeypatch.setattr(rust, "is_enabled", lambda: True)

        def reject_python_fallback(*_args, **_kwargs):
            """
            阻止 Rust 用例静默退回 Python 解析器。
            """
            raise AssertionError("Rust 资源名回归不能用 Python 回退冒充通过")

        monkeypatch.setattr(MetaMusic, "_prepare_name_context", reject_python_fallback)
    monkeypatch.setattr(runtime, "_metainfo_accelerator", rust if request.param == "rust" else None)
