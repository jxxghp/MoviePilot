"""
S4b / 剩余 import 环回归:打断 chain↔helper import-time 循环。

环根是 helper→chain 的反向边:`app/helper/image.py` 曾在模块顶层
`from app.chain.mediaserver import MediaServerChain` / `from app.chain.tmdb import TmdbChain`,
与 32 条合法的 chain→helper 正向边闭合成环
(`chain/recommend.py:11 → helper/image.py → app.chain`)。

修复 = 把这两个 import 降为 `WallpaperHelper` 四个方法内的函数级惰性导入
(沿用 event.py:706 / search.py:381 / plugin_source.py:40 / helper/skill.py:42 的同款技术),
导入 `app.helper.image` 不再在 import-time 拉起 `app.chain`，循环打断；行为字节级不变。

本地 venv 存在预存的 jieba_next/zhconv 缺口，但 `app.helper.image` 不依赖它们，
故子进程导入探针可稳定运行；另以源码结构契约做环境无关的双保险。
"""
import subprocess
import sys
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
IMAGE_PY = REPO_DIR / "app" / "helper" / "image.py"


# ---- (a) 源码结构契约：helper/image 顶层零 app.chain 引用 ----

def test_image_has_no_top_level_chain_import():
    """顶层 import 必然顶格；函数内惰性 import 有缩进。顶层不得出现 app.chain。"""
    src = IMAGE_PY.read_text(encoding="utf-8")
    for line in src.splitlines():
        assert not line.startswith("from app.chain"), f"image.py 顶层不应 import chain: {line}"
        assert not line.startswith("import app.chain"), f"image.py 顶层不应 import chain: {line}"


def test_chain_classes_lazily_imported_in_methods():
    """两个 Chain 仍在方法体内惰性导入（缩进），确保只是降级而非误删。"""
    lines = IMAGE_PY.read_text(encoding="utf-8").splitlines()
    tmdb = [ln for ln in lines if "from app.chain.tmdb import TmdbChain" in ln]
    mediaserver = [ln for ln in lines if "from app.chain.mediaserver import MediaServerChain" in ln]
    # 2 个 tmdb 方法 + 2 个 mediaserver 方法
    assert len(tmdb) == 2 and all(ln.startswith(" ") for ln in tmdb), tmdb
    assert len(mediaserver) == 2 and all(ln.startswith(" ") for ln in mediaserver), mediaserver


# ---- (b) 确定性证明：全新解释器 import image 不拉起任何 app.chain ----

def test_importing_image_pulls_no_chain():
    code = (
        "import sys, app.helper.image as im;"
        "leaked=[m for m in sys.modules if m.startswith('app.chain')];"
        "assert not leaked, ('app.chain leaked at import: %r' % leaked);"
        "assert hasattr(im,'ImageHelper') and hasattr(im,'WallpaperHelper'),'classes missing';"
        "print('OK')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(REPO_DIR), capture_output=True, text=True,
    )
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert "OK" in result.stdout
