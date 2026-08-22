# Module / Integration 渐进质量清单

本清单对应 ARCH-242。机器可检查定义位于
`app/runtime/extensions/module/quality.py`；它不改变 Module ABI，也不把“已评估”误写成“所有规则满分”。

## 使用规则

- 当前 47 个宿主模块（含本 fork 自有的 7 个存储后端模块与 `filemanager` 改名后的
  `medialibrary`）都必须显式登记 `ModuleQualityProfile`；未知第三方扩展才解析为 `legacy`。
- 新模块必须在同一提交新增 profile，只可使用登记规则；宿主目录与 profile 集合不一致时测试失败。
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

## 当前 assessed 范围

全部 47 个宿主模块已经完成显式 assessed 登记。所有模块共同具备四项机器证据：全测试真实网络
守卫、覆盖 `app/modules` 的 async 阻塞扫描、宿主已观察能力的 Module Contract V2、明确的
`MoviePilot core` owner。鉴权、限流、并发、敏感日志和 reload/stop 等能力相关规则不做虚假
“全通过”声明，仍由对应模块专项测试证明，并在 profile 中保留豁免边界。

`bangumi` 与 `dingtalk` 已登记更细的专项证据；其他模块先完成“已审查、通用门禁已覆盖、专属规则
按能力适用”的收口。测试会阻止宿主模块退回无法区分是否审查过的 `legacy` 状态。

`alipan`/`alist`/`alistgo`/`localstorage`/`rclone`/`smb`/`u115` 是本 fork 自有的存储后端
模块，上游没有；它们与上游存储模块共用同一套 `StorageBase`/`_StorageModuleBase` 契约和
`list_files`/`download_file` 等 storage 方法族契约，按同一基线收口。`medialibrary` 是本
fork 对上游 `filemanager` 模块的重命名，旧路径经 `app/runtime/compat/manifest.py` 保留
兼容，沿用同一 profile。
