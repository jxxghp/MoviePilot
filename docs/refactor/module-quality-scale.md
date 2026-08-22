# Module / Integration 渐进质量清单

本清单对应 ARCH-242。机器可检查定义位于
`app/runtime/extensions/module/quality.py`；它不改变 Module ABI，也不要求一次修完全部历史模块。

## 使用规则

- 未在本阶段修改的模块解析为 `legacy`，必须携带统一豁免原因和 owner。
- 新模块或本阶段修改的模块必须新增显式 `ModuleQualityProfile`，只可使用登记规则。
- `assessed` 表示已明确检查的规则集合，不等于所有规则满分；未覆盖项必须写精确原因。
- 测试不得访问真实网络。外部错误、限流和超时通过 fake client、fixture 或 adapter stub 验证。
- profile 不能替代 Module Contract V2；对外能力仍须在 contract registry 单独登记。

## 规则说明

| 规则 | 验收证据 |
| --- | --- |
| `fake-client-or-fixture` | provider 测试使用 fake client 或稳定录制 fixture |
| `zero-real-network-tests` | 网络守卫下专项测试通过 |
| `sync-async-boundary` | 同步 I/O 与 async 入口的调度策略明确 |
| `no-blocking-io-in-event-loop` | async 专项测试或受控线程池证据 |
| `auth-rate-timeout-offline-semantics` | 鉴权过期、限流、超时、离线结果分别测试 |
| `bounded-concurrency-or-polling` | 并发上限、轮询周期或不适用理由明确 |
| `reload-stop-idempotent` | init/reload/stop 可重复且资源最终释放 |
| `module-contract-v2` | 公开能力进入 Module Contract V2 |
| `sensitive-log-redaction` | token/cookie/password 不进入日志 |
| `owner-declared` | profile 有维护 owner |

## 当前 assessed 切片

`bangumi`：本轮配置快照改造已验证 fake client、零真实网络、同步/异步边界、reload/stop、
Contract V2、敏感日志和 owner。限流/并发仍复用通用 HTTP adapter，未在本切片重复实现。

