# 单元测试规范

本文档定义 MoviePilot 后端（`app/`）单元测试的统一约定：运行入口、隔离模型、编写规范、`unittest → pytest` 演进路线，以及排查测试问题的常用手段。目标是让 `tests/` 在 **CI / 全新环境**下可**离线、可重复、零外部依赖**地跑完。

## 运行入口：统一 pytest

pytest 是唯一运行入口。`tests/conftest.py` 在收集前完成隔离引导，因此任何方式启动 pytest 都会自动隔离。

```bash
uv run --locked --no-sync pytest tests                              # 串行全量
uv run --locked --no-sync pytest tests/test_xxx.py                  # 单文件
uv run --locked --no-sync pytest tests/test_xxx.py::SomeTest::test_y   # 单用例
uv run --locked --no-sync python tests/run.py                       # 默认按文件连续切成 4 片并行跑全量
uv run --locked --no-sync python tests/run.py --serial              # 串行全量，便于调试或生成覆盖率
uv run --locked --no-sync python tests/run.py --shard 1/4           # 只跑指定分片，供 CI 复用
```

`tests/run.py` 的 runner 参数只有 `--serial` 和 `--shard N/TOTAL`；其余参数保持原顺序
透传给 pytest，例如 `python tests/run.py -q --maxfail=1`。文件先按字典序排序，再以
`ceil(文件数 / 分片数)` 的大小连续切片，确保本地与 CI 执行相同的文件集合和顺序。

- 不再使用 `python -m unittest discover`：它不导入 `tests` 包、收不到纯函数用例，且绕过 `conftest.py` 的隔离。
- 不再依赖 `python tests/test_xxx.py` 直跑：所有 `if __name__ == "__main__": unittest.main()` 尾巴已移除。
- **复现 CI 用干净环境**：使用 `uv sync --locked` 从 `uv.lock` 创建环境，再以
  `uv run --locked --no-sync` 运行测试，避免本地额外包、未锁定解析结果或编译产物掩盖问题。

## 隔离模型（`tests/conftest.py`）

收集任何测试模块、`import app.*` **之前**，conftest 完成两件事：

1. **临时库**：把 `CONFIG_DIR` 指向临时目录并 `init_db()` 建表。引擎本身已惰性创建（`import app.db` 不再连库），但 `settings` 在 `import app.runtime.config` 那一刻就把 `CONFIG_DIR` 读进字段并建好配置子目录，之后再改环境变量对 `settings.CONFIG_PATH` 毫无影响——引擎晚点才建，连的仍是真实 `user.db`。所以隔离必须早于首个牵入 `app.runtime.config` 的 import（`app.db` / `app.application.orchestration.*` 都会牵入）；空库会让运行期查表报 `no such table`，故必须建表。
2. **`app.application.site.sites` 垫片**：该模块由独立仓库动态拉取、CI 无此文件，conftest 统一补最小垫片（本地存在真实模块时优先用真实模块）。兼容层会把旧插件的 `app.helper.sites` 导入路由到同一模块。

由此推出两条**硬规范**：

- 用例**不得**连接或写入真实数据库、不得读写真实 `config/`。需要的库状态在用例内构造。
- 用例**不得**依赖某个本地才有的动态模块副本；缺失的外部模块由 conftest 兜底或用例自行 mock。

## 外部依赖：一律 mock，零真实网络

测试**禁止**发起任何真实外部请求，包括但不限于 TMDB（`api.themoviedb.org`）、LLM 目录（`models.dev`）、下载器、媒体服务器、MP 服务器（`movie-pilot.org` 的共享识别 API）、以及任意外链图片/资源。**验收标准是全量跑测零真实出站**。

两种标准做法：

**1. 在调用边界打桩**（外部客户端、helper、SDK 入口）：

```python
from unittest.mock import patch, AsyncMock

with patch.object(SomeModule, "fetch", new=AsyncMock(return_value=FAKE)):
    ...
```

**2. 外部 HTTP API 用「录制—回放」(cassette)**：一次性录制真实响应存入 `tests/fixtures/`，测试时按请求键回放，使识别/解析等逻辑仍由真实结构数据驱动，但全程离线。参考实现：`tests/test_tmdb_recognize.py` + `tests/fixtures/tmdb_recognize_cassette.json`（在 `setUpModule` 中替换 TMDB 客户端的 HTTP 出入口；重新录制时临时包裹该出入口、跑一遍真实请求并落盘）。

> 注意：识别这类端到端流程往往不止一个外部出口。例如 TMDB 识别除了目录请求，链路层还会向 MP 服务器上报/查询「共享识别 API」——这类旁路出口必须一并打桩。用下文的 socket 探针确认确实零出站。

## 自隔离：用了什么，就还原什么

用例若修改了**进程级状态**——`sys.modules` 桩、单例（`Singleton._instances`）、`lru_cache`、环境变量、`settings` 字段——必须在用例或模块结束时还原。pytest 一次性导入全部测试模块，未还原的污染会扩散到后续用例，产生“单独跑过、一起跑挂”的测不准现象。

正确姿势：

- 上下文管理器（`with patch(...)`）、`setUp` + `addCleanup`、或方法内 `patch`，退出即还原。
- 模块级需要的桩用上下文包住 import 段，import 完即还原。

反模式（**评审应拒绝**）：

- 模块顶层 `sys.modules["x"] = stub` 且不还原。
- 桩掉 `requirements` 里**真实可用**的第三方包（如把 `cn2an.an2cn` 换成 `str`），导致被测行为漂移；真包能用就用真包。
- 依赖测试执行顺序。

## 编写新测试：强制 pytest 原生

新增测试**一律** pytest 原生风格，评审不接受新写的 `unittest.TestCase`：

- 文件名 `test_*.py`，置于 `tests/`。
- 函数式用例 `def test_xxx():` + 普通 `assert` + pytest fixture，不用 `self.assertXxx`。
- 涉及外部服务一律 mock（见上）。
- 异常断言用 `pytest.raises`，参数化用 `@pytest.mark.parametrize`。

```python
import pytest

from app.schemas.types import MediaSource

@pytest.fixture
def sample_meta():
    """构造一条可复用的识别元数据。"""
    return MetaInfo(title="示例 (2020)")

def test_recognize_prefers_explicit_identity(sample_meta, monkeypatch):
    """显式媒体来源与原生 ID 时应优先精确识别，而非回退标题搜索。"""
    monkeypatch.setattr(SomeClient, "fetch", lambda *a, **k: FAKE_MOVIE)
    result = recognize(
        sample_meta,
        media_source=MediaSource.TMDB,
        media_id="123",
    )
    assert result.media_source == MediaSource.TMDB
    assert result.media_id == "123"
```

## `unittest → pytest` 演进路线：改到即转

存量有大量 `unittest.TestCase`。pytest 原生支持运行 `TestCase`，所以它们能正常跑——**不做大爆炸式重写**，避免无谓的回归风险。路线是：

- **新测试**：直接 pytest 原生（见上）。
- **存量**：当你因别的原因改到某个 `TestCase` 文件时，**顺手**把它整文件转成 pytest 原生，并跑一遍该文件确认行为不变。
- 不为转换而转换：没有改动需求的文件可暂时保留 `TestCase`。

常见转换对照：

| unittest | pytest 原生 |
| --- | --- |
| `class T(unittest.TestCase):` + 方法 | 模块级 `def test_xxx():` |
| `self.assertEqual(a, b)` | `assert a == b` |
| `self.assertTrue(x)` / `assertFalse(x)` | `assert x` / `assert not x` |
| `self.assertIn(a, b)` / `assertNotIn` | `assert a in b` / `assert a not in b` |
| `self.assertIsNone(x)` / `assertIsNotNone` | `assert x is None` / `assert x is not None` |
| `self.assertRaises(E)` | `with pytest.raises(E):` |
| `setUp` / `tearDown` | fixture（`yield` 前为准备、后为清理）|
| `setUpClass` / `tearDownClass` | `@pytest.fixture(scope="class")` 或模块级 fixture |
| `@unittest.skipIf(c, r)` | `@pytest.mark.skipif(c, reason=r)` |

## 排查测试问题

- **收集报错（collection error）**：多为 import 期副作用或顶层桩污染。优先改成真实 import（conftest 已隔离临时库，真实 `settings`/helper 可加载）+ 方法内 patch，而不是靠事后还原（收集期污染发生在 import 那一刻，事后还原太晚）。
- **检测真实网络泄漏**：进程级挂一个 `socket.getaddrinfo` 探针记录非本地出站主机，跑目标用例即可定位是谁在联网：

  ```python
  import socket
  _orig = socket.getaddrinfo
  hits = []
  def _spy(host, *a, **k):
      if host not in ("127.0.0.1", "localhost", "::1"):
          hits.append(str(host))
      return _orig(host, *a, **k)
  socket.getaddrinfo = _spy
  # 跑用例后断言 hits 为空
  ```

- **测试间污染（测不准）**：定位被改而未还原的进程级状态（单例 / `lru_cache` / `sys.modules` / 环境变量 / `settings`），按「自隔离」补还原。
- **怀疑用例空过**：用变异验证——临时打断对应生产逻辑（让它返回错误值），跑该用例应**失败**；若仍通过，说明断言没真正覆盖该逻辑。

## CI 与 PR

- **门禁**：`.github/workflows/test.yml` 在指向 `v3` 的 `pull_request` / `push` 及手动触发时，从 `uv.lock` 同步环境。独立 `architecture` job 先运行宿主依赖、运行契约和基线 CLI 快速门禁；全量测试再通过 `tests/run.py --shard N/TOTAL` 稳定分到 4 个 pytest job。每个分片都有独立进程和临时 `CONFIG_DIR`，不共用 SQLite 或进程级状态。
- **跨仓观察**：`.github/workflows/architecture-observe.yml` 每周或手工检出官方插件仓最新 `main`，使用 `--check-plugins` 比较公开导入、Hook 和动态 API 契约。它只上传 `official-plugin-architecture-report.json`，不会自动刷新 fixture；语义变化必须人工审查后显式执行 `--write-plugins`。
- **静态检查**：`.github/workflows/pylint.yml` 对指向 `v3` 的 PR、推送和手工触发运行 Pylint。PR/推送改动到的 Python 文件是硬门禁；`app/` 全量扫描保留为建议性 JSON 构建工件，存量告警不会掩盖或阻塞本次增量治理。
- **PR 本地验证**：提交前运行受影响测试和适用的静态检查。涉及依赖或锁文件、共享测试基建、数据库、启动链、跨模块生命周期、兼容层或大范围行为变化时，运行 `uv run --locked --no-sync python tests/run.py` 完成本地全量；需要断点、输出顺序或测试污染诊断时使用 `--serial`。所有测试都应确认受影响路径通过且 socket 探针无真实出站，验证说明准确标注执行范围。若存在无关失败，必须在当前 `upstream/v3` 基线上独立复现并在 PR 中如实说明；不得静默扩大当前 PR 去修复基线问题。纯文档变更执行适用的文本、结构和 diff 检查，CI 继续运行全量门禁。
- 覆盖率不参与常规 PR / push 的合并门禁；需要覆盖率制品时手动触发 `Unit Tests` workflow，独立的 `Coverage Report` job 会通过 `tests/run.py --serial` 跑串行全量并上传 JSON / XML 报告。
- 复现 CI 使用 `uv sync --locked`；主程序运行依赖位于 `[project].dependencies`，pytest 与覆盖率工具位于默认 `dev` 依赖组。
