"""
S4c / 剩余 import 环回归：打断 core↔utils import-time 循环，令 utils 成为 core 的真叶子。

原 4 条 utils→core 顶层反向边：
  - app/utils/mixins.py     → app.core.event（eventmanager/Event）
  - app/utils/security.py   → app.core.config.settings
  - app/utils/rust_accel.py → app.core.config.settings
  - app/utils/http.py       → app.core.config.settings
与 core→utils 正向边（core/config.py:21-22、core/event.py:19-20）闭合成 import-time 环。

修复 = 把这 4 条反向边降为函数级惰性导入（mixins 在 __init_subclass__ 的 CONFIG_WATCH
分支内；其余在使用处），导入任一 utils 模块不再在 import-time 拉起 app.core；行为字节级不变。
沿用 event.py:706 / search.py:381 / plugin_source.py:40 / helper/skill.py:42 的同款技术。
"""
import ast
import subprocess
import sys
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
UTILS_DIR = REPO_DIR / "app" / "utils"


def _top_level_import_modules(path: Path):
    """仅返回模块顶层（非函数/类体内）的 import 目标模块名。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    mods = []
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module:
            mods.append(node.module)
        elif isinstance(node, ast.Import):
            mods.extend(alias.name for alias in node.names)
    return mods


# ---- (a) 源码结构契约：整个 utils 层顶层零 app.core（utils=叶子，milestone 守护） ----

def test_no_utils_file_imports_core_at_top_level():
    offenders = {}
    for py in UTILS_DIR.rglob("*.py"):
        bad = [m for m in _top_level_import_modules(py) if m.startswith("app.core")]
        if bad:
            offenders[py.name] = bad
    assert not offenders, f"utils 顶层仍反向依赖 core: {offenders}"


def test_lazy_core_imports_preserved():
    """惰性导入仍在（确保只是降级而非误删）。"""
    assert "from app.core.event import eventmanager" in (UTILS_DIR / "mixins.py").read_text(encoding="utf-8")
    for name in ("rust_accel.py", "security.py", "http.py"):
        assert "from app.core.config import settings" in (UTILS_DIR / name).read_text(encoding="utf-8")


# ---- (b) 确定性证明：全新解释器 import 各 utils 模块不拉起 app.core ----

def test_importing_utils_pulls_no_core():
    mods = ["app.utils.rust_accel", "app.utils.security", "app.utils.http", "app.utils.mixins"]
    code = (
        "import sys\n"
        f"for m in {mods!r}:\n"
        "    __import__(m)\n"
        "bad=[m for m in ('app.core.config','app.core.event') if m in sys.modules]\n"
        "assert not bad, ('app.core leaked at import: %r' % bad)\n"
        "print('OK')\n"
    )
    result = subprocess.run([sys.executable, "-c", code], cwd=str(REPO_DIR), capture_output=True, text=True)
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert "OK" in result.stdout


# ---- (c) 行为未变：惰性 settings / mixin 子类注册仍正常 ----

def test_lazy_settings_resolves():
    from app.utils import rust_accel
    assert isinstance(rust_accel.is_config_enabled(), bool)
    from app.utils.http import RequestUtils
    r = RequestUtils(ua="probe-ua")
    assert r._headers.get("User-Agent") == "probe-ua"
    from app.utils.security import SecurityUtils
    assert len(SecurityUtils._sign_url_payload("http://x/y", "image")) == 64


def test_configreload_subclass_still_registers():
    """带 CONFIG_WATCH 的子类在定义期仍能构建 handler（__init_subclass__ 惰性路径 + 闭包注解）。"""
    from app.utils.mixins import ConfigReloadMixin

    class _Probe(ConfigReloadMixin):
        CONFIG_WATCH = {"PROXY"}

        def on_config_changed(self):
            return None

    assert hasattr(_Probe, "handle_config_changed")
