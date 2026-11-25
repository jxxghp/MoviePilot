# 测试责任域导航

先按被验证行为的主要 owner 查找现有套件，再决定扩充或新增。目录与生命周期规则统一见
[测试规范](../docs/testing.md#测试归属与生命周期)，不按修复日期、审查批次或问题编号建立新套件。

| 目录 | 主要责任 |
| --- | --- |
| `agent/<能力>/` | Agent API、工具、模型、记忆、安全、评估和运行期 |
| `api/<能力>/` | HTTP 请求、响应、权限和分页的端点边界 |
| `application/<能力>/` | 单一应用用例与端口 |
| `domain/<能力>/`、`foundation/<能力>/` | 纯业务规则与无 I/O 基础能力 |
| `adapters/<能力>/`、`modules/<提供方>/` | 外部协议、具体 I/O 和提供方模块 |
| `runtime/<能力>/`、`startup/<能力>/` | 进程资源、配置、事件、并发及启停组合 |
| `db/<能力>/` | 事务、会话、查询适配及数据库迁移 |
| `chain/<能力>/`、`scheduler/<能力>/`、`workflow/<能力>/` | 相应源码 owner 的行为 |
| `integration/<业务域>/` | 下载、整理、订阅、音乐、插件等跨 owner 不变量 |
| `contracts/architecture/` | 依赖、事件、类型和质量门禁 |
| `contracts/compatibility/`、`contracts/documentation/` | SDK、旧导入及稳定架构文档契约 |
| `tooling/<工具域>/` | 测试工具、CLI、安装、发布和开发技能 |
| `support/<业务域>/` | 确有跨文件消费者的 fixture、工厂、数据与替身，不含测试入口 |
| `fixtures/` | 回放数据、已审核架构基线及耗时权重 |

根目录仅保留 `run.py`、`conftest.py`、`__init__.py` 和本导航。测试与 support 都不得导入
`test_*.py`。共享设施不依赖测试入口，也不通过旧路径壳模块转发。

```bash
uv run --locked --no-sync python scripts/testing/maintenance.py --base upstream/v3
uv run --locked --no-sync pytest tests/application/plugin -q
uv run --locked --no-sync python tests/run.py
```

迁移路径时同步 CI 命令、tests 相对耗时键和静态检查引用；验证参数化收集项及分片并集。
职责明确、覆盖不同状态的套件不必为了缩减文件数合并；重复候选需检查夹具和前置条件。
