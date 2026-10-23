# 单元测试规范

本文档定义 MoviePilot 后端（`app/`）单元测试的统一约定：运行入口、隔离模型、编写规范、`unittest → pytest` 演进路线，以及排查测试问题的常用手段。目标是让 `tests/` 在 **CI / 全新环境**下可**离线、可重复、零外部依赖**地跑完。

## 运行入口：统一 pytest

pytest 是唯一运行入口。`tests/conftest.py` 在收集前完成隔离引导，因此任何方式启动 pytest 都会自动隔离。

```bash
# 串行全量
uv run --locked --no-sync pytest tests
# 单文件
uv run --locked --no-sync pytest tests/test_xxx.py
# 单用例
uv run --locked --no-sync pytest tests/test_xxx.py::SomeTest::test_y
# 默认按文件预计耗时均衡为 4 片并行跑全量
uv run --locked --no-sync python tests/run.py
# 串行全量，便于调试或生成覆盖率
uv run --locked --no-sync python tests/run.py --serial
# 只跑指定分片，供 CI 复用
uv run --locked --no-sync python tests/run.py --shard 1/4
```

`tests/run.py` 的 runner 参数只有 `--serial`、`--shard N/TOTAL` 和 `--exclude-architecture-gate`；其余参数保持原顺序
透传给 pytest，例如 `python tests/run.py -q --maxfail=1`。分片使用受版本控制的
`tests/fixtures/durations.json` 慢文件耗时估算：按耗时降序，逐个分给预计总耗时最短的分片；
耗时相同时按路径、分片编号决定归属，片内仍按路径排序。新增或未记录文件使用 1 秒权重，
不依赖本机缓存，确保同一代码版本在本地与 CI 的文件集合和顺序一致。
耗时数据来自成功 Ubuntu/Python 3.14 Coverage 日志的逐文件进度时间戳（含续行），
只记录至少 3 秒的文件并向上取整；后续发现慢文件偏移时可依据成功 CI 日志更新。
`--exclude-architecture-gate` 跳过 `ARCHITECTURE_GATE_TESTS` 列出的架构测试文件，这些文件由 CI 的
`architecture` job 在同一进程内运行并共享全量扫描结果（`tests/architecture_cache.py`），覆盖率分片不再重复执行；
本地默认全量仍包含它们。
这些估算仅用于调度，不构成性能或覆盖率基线。

- 不再使用 `python -m unittest discover`：它不导入 `tests` 包、收不到纯函数用例，且绕过 `conftest.py` 的隔离。
- 不再依赖 `python tests/test_xxx.py` 直跑：所有 `if __name__ == "__main__": unittest.main()` 尾巴已移除。
- **复现 CI 用干净环境**：使用 `uv sync --locked` 从 `uv.lock` 创建环境，再以
  `uv run --locked --no-sync` 运行测试，避免本地额外包、未锁定解析结果或编译产物掩盖问题。

共享工作区的 `.venv-test` 与上述 `uv` 命令映射见 [开发环境设置](development-setup.md)；
`--no-sync` 不会验证已安装依赖是否匹配锁文件。

## 验证范围与维护者安排

Contributor 默认在提交前运行受影响测试与适用本地检查。依赖或锁文件、共享测试脚手架、数据库、
启动路径、跨模块生命周期、兼容层、大范围行为改动或维护者明确要求时，运行完整
`uv run --locked --no-sync python tests/run.py`。纯文档及其契约测试采用文本、结构、链接和
focused 测试验证，不要求启动产品全套测试。

已确认的维护者可按 `AGENTS.md` 明确调整验证范围、时机及交付顺序，复用仍有效的源码、锁文件、
脚手架和环境证据，也可先保存本地 anchor 再补检查。记录依据、未验证项和后续安排；HEAD 变化
本身不要求本地全量重跑，但实际变化使哪些证据失效，就重跑哪些检查。这不改变 GitHub Actions
按精确 merge tree/base 复用的实现合同，也不取消测试隔离或下述 TestCase 整文件迁移要求。

改动涉及的行为必须有可信验证。失败须判断是否由本次改动造成、加重或重新触达；无关失败用当前
目标 base 复现或其他充分证据说明，并按 `docs/rules/12-collaboration-and-distribution.md`
的已记录维护者决定处置。已预授权的同一范围无需逐次重问，缺少证据时保留未知，不伪报通过。

持久通知恢复由 `tests/test_outbox_recovery.py` 使用临时 SQLite、真实线程及屏障验证：
整理事件阻塞时下载通知仍能完成、同类事件不会并行重复认领、在途租约续期、崩溃租约接管、
提交失败与取消后的重试，以及关闭超时保留资源。`tests/test_telegram.py` 覆盖海报失败后的文本
兜底、会话/话题/按钮保留、429 不追加请求，以及发送失败不能被 outbox 当作成功确认。

`tests/test_db_error_diagnostics.py` 在临时 SQLite 的同步及异步连接池中注入
`SQLITE_IOERR_SHORT_READ`：失败事务保留原始异常、不自动重放写入，后续独立事务
使用新连接恢复；锁争用和约束错误不触发额外的连接失效。
`tests/test_event_runtime_components.py` 验证 `SystemError` 处理器失败时只记日志，
不再调用通知回调或派生错误广播。此回归不模拟真实磁盘故障，也不保证持续 I/O 故障时数据库可用。

## 隔离模型（`tests/conftest.py`）

收集任何测试模块、`import app.*` **之前**，conftest 完成两件事：

1. **临时库**：把 `CONFIG_DIR` 指向临时目录并 `init_db()` 建表。引擎本身已惰性创建（`import app.db` 不再连库），但 `settings` 在 `import app.runtime.config` 那一刻就把 `CONFIG_DIR` 读进字段并建好配置子目录，之后再改环境变量对 `settings.CONFIG_PATH` 毫无影响——引擎晚点才建，连的仍是真实 `user.db`。所以隔离必须早于首个牵入 `app.runtime.config` 的 import（`app.db` / `app.chain.*` 都会牵入）；空库会让运行期查表报 `no such table`，故必须建表。
2. **`app.application.site.sites` 垫片**：该模块由独立资源仓按平台下发，conftest 统一安装最小垫片，普通单测不会加载源码目录中的 `.so` / `.pyd`。兼容层会把旧插件的 `app.helper.sites` 导入路由到同一模块；真实制品由资源与 ABI 专项验收覆盖。

由此推出两条**硬规范**：

- 用例**不得**连接或写入真实数据库、不得读写真实 `config/`。需要的库状态在用例内构造。
- 用例**不得**依赖某个本地才有的动态模块副本；缺失的外部模块由 conftest 兜底或用例自行 mock。

## 外部依赖：一律 mock，零真实网络

高清杜比换域回归由 `tests/test_hddolby_spider.py`、`tests/test_hddolby_parser.py` 和
`tests/test_site_connectivity.py` 验证：网页使用 `www.uhddolby.com` 时，搜索、用户资料和
做种统计仍可按资源的 `api_url` 访问 `api.hddolby.com`，并保留站点代理和认证配置。
API 连接失败或返回 HTTP 错误时，连通性提示指出实际接口主机；网页解析成功不能代替
API 验证。错误提示和搜索告警不得输出地址中的账号、密码或查询令牌。测试不使用真实站点凭据。

社区勋章使用 `tests/test_community_medal_background.py` 验证启动主动预热、无人访问时的六小时
更新、失败后的自动重试及 TaskRegistry 关闭。外部下载或远端开关被屏障阻塞时，查询必须在
屏障释放前返回当前快照；冷启动通过 `refreshing` 表示后台尚未完成，不能把刷新锁或网络等待
转移到页面请求。`tests/test_community_medals.py` 保留凭据隔离、有效开关和名单协议覆盖。

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

### 飞书长会话流式卡片回归

`tests/test_feishu.py` 在 CardKit API 边界模拟 `300309` / `200850`：重开同一卡片的
流式模式后补发完整累积文本，设置、文本和结束关闭共用严格递增的 sequence，
每次请求使用独立 UUID。恢复操作每次刷新最多尝试一次；恢复失败或普通更新失败时，
按 2、4、8、16、32、60 秒退避，之后保持 60 秒间隔。冷却期间不请求远端、不重复告警，
也不消耗序号；返回失败以保留 Agent 未发送文本，成功后清除退避状态。
用例通过虚拟时钟验证反复到期、恢复失败、异常、最新文本重试和卡片间隔离，不连接飞书。

### 存储与监控的边界回归

`tests/test_transfer_safe_overwrite.py` 使用真实临时文件验证覆盖失败保留旧文件，
并在持久步骤边界模拟中断；copy、move、硬链接和软链接均须先完成新文件再替换旧目标。
恢复回归还需验证源文件被替换、新目标变成半文件、备份被替换以及历史清理的符号链接路径，
不能仅凭路径存在就删除源文件或旧备份。
远端状态不明保持人工复核，不能通过删除旧文件后盲目重试证明成功。

存储快照用例优先在 SDK 的目录列表、stat、类型查询和连接边界打桩，保留真实的路径映射、
文件项构造、快照遍历及轮询逻辑。仅替换 `snapshot_storage` 的返回值可以验证轮询器，
但不能证明存储适配器正确区分了空目录与查询失败。

`tests/test_smb_storage.py` 提供以下离线场景：

- 单共享和多共享下，祖先目录时间不变时仍发现后代新增、修改、保留旧 mtime 的文件移入和删除。
- 根目录、子目录、单个文件或类型查询失败时，不提交空快照或部分快照。
- 连接失效保留旧基线；恢复后发现新文件，再次轮询不重复派发。
- 真正清空的目录和确认不存在的根路径可以提交空快照。
- 完整遍历仍遵守递归深度上限，不因本次修复增加扫描频率。

这些场景只承诺已验证的 SMB 查询语义；为其他存储接入严格快照时，应在其 SDK/API
边界复现同样的状态转换，并评估目录时间语义、分页和请求成本。

## 种子下载重试回归

下载选集回归使用真实 qBittorrent/Transmission Module 与离线下载器端口，验证自定义识别词
从订阅计划一直传到下载器文件选择；目标集数不匹配或优先级未确认时不启动任务，不能写成功历史。
启动结果未知时尝试确认暂停；无法确认则明确要求检查下载器，不能宣称任务已停止。
已有下载任务只按原始种子 info 字节或磁力中的 v1/v2 哈希确认，名称与大小相同不能接管任务。

`tests/test_download_category.py` 从真实下载链经过 qBittorrent 模块和客户端，在 SDK 边界检查
音乐缺分类时使用“音乐”、已有分类优先，以及自动管理开关和指定路径的传递。
覆盖豆瓣音乐、MusicBrainz、无显式身份、专辑、单曲与艺术家合集，且下载器分类兜底不回写媒体信息。

`tests/test_music_bilingual_matching.py` 在 Python/Rust 解析、同步/异步/流式搜索和订阅复核中，
验证双语作品名及艺人署名只有在括号两侧均属于同一实体的可信名称时才匹配；
未知别名、副标题、不同录音版本及歌曲所属专辑继续保留各自的身份边界。

普通电视剧首次下载的整包优先回归见 `tests/test_subscribe_paged_search.py`：订阅没有下载记录，
且最新媒体库缺失全部目标集时，先收集所选搜索范围内的候选再尝试完整包；单页策略仍只查每个
来源的第一页。整包继续通过规则过滤、优先级门槛和种子文件完整覆盖校验，无可用整包时按集
回退。已有下载记录或媒体库已有目标集时沿用补集行为，提交前重新检查当前订阅；不会开启洗版
或写入洗版优先级。测试覆盖显式缺集列表、后页整包、不完整包和搜索期间进度变化，均隔离外部服务。

整包优先也约束候选分批：已收集的整包即使排序低于分集、检查点恢复后缺少临时下载地址，
也须先恢复地址并共同进入下载选择；多个整包不完整、下载失败或恢复后已不可用时才回退分集。
`tests/test_search_scan.py` 与 `tests/test_subscribe_paged_search.py` 在离线边界验证恢复、过滤和
实际下载提交的先后顺序，同时保留普通补集按可用前缀提交的行为。

全集洗版（`best_version=1`、`best_version_full=1`）的主动搜索始终使用洗版规则组，
包括尚无下载记录或仍有缺集的订阅；未通过洗版规则的整包不会回落到普通订阅规则组。
订阅显式指定的规则组仍优先。分集洗版中从未下载的剧集继续使用普通订阅规则。
`tests/test_subscribe_paged_search.py` 覆盖无记录、部分及全部已下载、整季标题与显式集数包，
验证只有符合洗版规则的完整包能够提交；没有合格候选时，不写下载事实或提升洗版优先级。

种子下载重试的离线边界见 `tests/test_torrent_download_retry.py`：普通 GET 在网络失败、
403/5xx、空响应或无法解析的响应后最多重试两次，退避为 1、2 秒。签名链接刷新、换票地址、
首次下载确认按 HTML 表单语义读取控件，兼容单双引号、属性顺序、空 action 和提交按钮；
仅向同来源的下载端点提交一次 POST，使用确认页地址作为 Referer。HTTP 429 或 JSON `message` / `msg` 明确提示限流时，
停止短退避重试并在通知中保留原因，不把限流响应交给种子解析器或写入无效地址缓存。
测试替换 HTTP 端口与等待，不访问真实站点。
瞬时状态不会写入 24 小时无效链接缓存；订阅记录的泛化“下载种子内容为空”使用 1 小时冷却，
明确资源失效仍保留 24 小时冷却。每日下载配额耗尽冷却 24 小时，覆盖站点时区差异；
短期限流至少冷却 1 小时，提示等待更长时按提示延长。准备流程通过单次失败回调把限流原因
交给持久化冷却，避免统一的空内容错误覆盖它；签名链接遇到明确限流也不刷新重试。
此策略不追溯修改已经写入的失败记录。
种子内容耗尽重试仍无效或首次下载确认仍返回无效内容时，加入现有 24 小时下载失败黑名单；
下载链再次遇到该地址直接跳过请求和通知。黑名单保存原始资源地址，不使用重定向目标；
短期签名或换票资源在下载链中屏蔽原始 enclosure，不将临时请求地址写入无效缓存。
订阅持久化保留具体失败原因，无效种子使用 24 小时资源冷却，避免重启后每小时重复通知。
网络异常、空响应、403/5xx 和限流仍沿用瞬时重试与流控规则，不因本次修改加入内容无效黑名单。
下载器报告同一任务已存在且整包全部媒体文件明细已记录时，按资源指纹登记 7 天提交退避，
使订阅不再重投同一个已持有的整包；退避只压制该资源，候选仍继续尝试同媒体的其他种子，
订阅不会被判定完成或失败，且仅订阅来源写台账（手动补投不留冷却记录）。冷却跳过日志转述台账
中的原因，不再把它断言成"添加下载失败"。见 `tests/test_torrent_download_retry.py` 与
`tests/test_downloader_submission.py`。
整包退避指纹不包含解析种子后补齐的集数，下一轮搜索仍能命中；普通失败仍按原有集数指纹隔离。
只重复选择了已记录的部分集数、但包内仍有未记录文件时，不登记整包退避，也不把未选文件写入历史。

换票资源的跨批次复用见 `tests/test_download_resource_cache.py`：种子成功解析后，
本地 `torrents` 缓存以站点资源身份、原始换票请求和 Cookie 的摘要保存内容，命中后
不再换票。缓存沿用种子目录的清理周期；损坏或清理后重新获取，缺少稳定身份时不共享。
测试覆盖十二轮无需要集或无法识别集数、缺集变化后的即时选中、站点与凭据隔离，
以及完整包和拆包保留每日限流原因并采用 24 小时冷却。缓存不保存缺集判断，
当前识别词和允许集数仍在每批重新应用。

## 自隔离：用了什么，就还原什么

`tests/test_workflow_safety.py` 使用真实工作流 manager、执行器和节点线程模拟同 ID 重跑、
终态事务阻塞与失败，确保准入之前没有 reset/start，真实结束和最终落盘之前不释放 owner。
虚拟时钟验证普通同步动作超时、循环等待截止与协作取消；单节点无边图可执行，坏 DAG 保存和运行均拒绝。
`tests/test_security_user_boundaries.py` 覆盖管理权限、已有/未落库会话隔离、用户名回收、
旧记录恢复、缓存授权以及管理员续聊保留原归属和实际执行人。
图片代理测试通过真实组合根 HTTP Adapter 和 MockTransport 验证跳转在请求前拒绝，
事件契约/SDK 测试同时验证诊断无原始输入和嵌套快照隔离，均无需外部网络。

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
    """
    构造一条可复用的识别元数据。

    :return: 包含示例标题和年份的媒体识别输入
    """
    return MetaInfo(title="示例 (2020)")

def test_recognize_prefers_explicit_identity(sample_meta, monkeypatch):
    """
    验证指定媒体来源与原生标识时优先精确识别。

    :param sample_meta: 待识别的示例媒体输入
    :param monkeypatch: 隔离外部查询并在用例结束后恢复的测试夹具
    """
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

## 存量测试清理

删除测试需要明确指出保留的等价覆盖，不能只依据文件年代、执行耗时、名称相似或覆盖率百分比。

- **可删除的重复**：被测入口、输入、fixture、参数化和执行前状态一致，且保留用例包含全部断言。完全相同的执行体可保留一份；同输入的断言子集可并入更完整用例的说明。
- **不能自动删除的相似用例**：同步与异步入口、冷启动与重复重置、过期清理前后、迁移前后、不同平台或可选依赖条件，均可能拥有独立行为。
- **没有直接 `assert` 不等于空测试**：`pytest.raises`、辅助断言、迁移 round-trip、导入兼容和“不抛异常”都是有效契约；只有名称或注释声称存在的覆盖则需要进一步核实。
- **删除后验证**：运行受影响文件，核对保留用例及生产代码行/分支覆盖；跨领域清理再运行 `python tests/run.py`。不降低覆盖率基线、不新增 skip、不削弱架构或兼容门禁。

## 排查测试问题

- **收集报错（collection error）**：多为 import 期副作用或顶层桩污染。优先改成真实 import（conftest 已隔离临时库，真实 `settings`/helper 可加载）+ 方法内 patch，而不是靠事后还原（收集期污染发生在 import 那一刻，事后还原太晚）。
- **检测真实网络泄漏**：进程级挂一个 `socket.getaddrinfo` 探针记录非本地出站主机，跑目标用例即可定位是谁在联网：

  ```python
  import socket
  _orig = socket.getaddrinfo
  hits = []
  def _spy(host, *a, **k):
      """
      记录非本地主机的地址解析请求。

      此探针只记录请求，仍调用真实解析函数；运行用例前须完成外部依赖隔离。

      :param host: 待解析的主机名或地址
      :param a: 原样传给地址解析函数的位置参数
      :param k: 原样传给地址解析函数的关键字参数
      :return: 原始地址解析函数的返回值
      """
      if host not in ("127.0.0.1", "localhost", "::1"):
          hits.append(str(host))
      return _orig(host, *a, **k)
  socket.getaddrinfo = _spy
  # 跑用例后断言 hits 为空
  ```

- **测试间污染（测不准）**：定位被改而未还原的进程级状态（单例 / `lru_cache` / `sys.modules` / 环境变量 / `settings`），按「自隔离」补还原。
- **怀疑用例空过**：用变异验证——临时打断对应生产逻辑（让它返回错误值），跑该用例应**失败**；若仍通过，说明断言没真正覆盖该逻辑。

## CI 与 PR

- **合并检查复用**：单测/架构与 Pylint 工作流各自保留 PR、push 和手动入口。PR 完整通过全部门禁后，末尾 `CI proof (<github.sha>)` job 记录实际验证的模拟合并提交。合并 push 只在同一工作流的最新 PR 运行完整成功、证明成功、代码树完全相同、模拟合并父提交分别等于 push 前的目标分支和 PR head 时跳过重复门禁。直接 push、强制 push、基线变化、旧工作流缺少证明、失败/未完成运行或 API 异常都执行全量；不依赖提交消息，也不把 PR 的 head SHA 当作测试的合并 SHA。标准 merge/squash/rebase 仅在上述证据一致时复用。GitHub 构建与发布工作流保持独立。
- **去重脚本验证**：`node --test .github/scripts/reuse.test.mjs` 离线覆盖成功复用及保守回退，两个检查工作流均在判定前执行。复用 job 仅持有 contents/actions/pull-requests 读取权限；没有修改分支保护设置。

- **门禁**：`.github/workflows/test.yml` 在指向 `v3` 的 `pull_request` / `push` 及手动触发时，从 `uv.lock` 同步环境。宿主架构门禁分为两个并行 job，`architecture` 运行宿主依赖、运行契约、事件政策测试和基线快照，`architecture-ratchets` 运行类型、复杂度、并发、Ruff/mypy ratchet 与启动性能检查；其余测试通过 `coverage run --parallel-mode tests/run.py --exclude-architecture-gate --shard N/6` 分到 6 个 job，一次执行同时验证单测并采集覆盖率；门禁已运行的架构测试不在分片中重复执行。每个分片都有独立进程和临时 `CONFIG_DIR`，不共用 SQLite 或进程级状态，由单一报告 job 合并后检查除 `app/plugins` 外全部 `app` Python 文件的逐文件低水位。
- **启动性能**：`scripts/startup/performance.py --check --repeat 3` 对 `app.startup.lifecycle`、`app.factory`、`app.main` 分别执行三次冷导入采样。主启动各入口的耗时上限固定为 3000ms，本次采样中位数不超过 3 秒即通过，超过则失败；历史基线的中位数与最大值仅用于诊断，不改变该上限。宿主模块数量、生命周期组件集合与顺序、线程/任务释放和隔离采样无数据库连接的契约仍按原规则检查。
- **跨仓观察**：`.github/workflows/architecture-observe.yml` 每周或手工检出官方插件仓最新 `main`，使用 `--check-plugins` 比较公开导入、Hook 和动态 API 契约。它只上传 `official-plugin-architecture-report.json`，不会自动刷新 fixture；语义变化必须人工审查后显式执行 `--write-plugins`。
- **静态检查**：`.github/workflows/pylint.yml` 对指向 `v3` 的 PR、推送和手工触发运行 Pylint。PR/推送改动到的 Python 文件是硬门禁；`app/` 全量扫描保留为建议性 JSON 构建工件，存量告警不会掩盖或阻塞本次增量治理。
- **PR 本地验证**：按上文「验证范围与维护者安排」选择 contributor 默认检查或已记录的维护者安排，统一处理证据复用与失败归属；需要断点、输出顺序或测试污染诊断时使用 `--serial`。确认受影响路径与零真实出站，准确标注验证范围；本地执行安排不改变 CI 的全量验证与复用合同。
- **覆盖率门禁**：`Unit Tests with Coverage` jobs 会在 `v3` 的 PR、push 和手工触发中通过 `tests/run.py --exclude-architecture-gate --shard N/6` 并行采集覆盖率数据，`Coverage Report` 再合并全部分片并只读检查除 `app/plugins` 外全部 `app` Python 文件的逐文件低水位；已有文件覆盖率只能保持或提升，若可执行语句数减少且未覆盖行数不增加，也允许由此产生的比例下降；未覆盖行数增加仍判为回退。单独一行的 `...` 与 `pass` 一样属于无操作占位，不计入覆盖率，其他包含 `...` 的真实语句仍需覆盖。新文件必须达到至少 80%，同时上传 JSON / XML 工件。macOS 本地报告只用于诊断，不直接作为可提交基线。
- 复现 CI 使用 `uv sync --locked`；主程序运行依赖位于 `[project].dependencies`，pytest 与覆盖率工具位于默认 `dev` 依赖组。
