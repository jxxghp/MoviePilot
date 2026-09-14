# ADR-0008：插件契约基类归属 SDK

- 状态：Accepted
- 日期：2026-09-14
- 对应任务：ARCH-262

## 决策

`_PluginBase` 与插件处理链的实现本体落在 `app/sdk/plugin/base.py`。`app/plugins/__init__.py`
只保留包文档，旧的包根符号 `app.plugins._PluginBase` 和 `app.plugins.PluginChian` 由
`app/runtime/compat` 的精确符号叠加承接。

`app/sdk/plugins.py` 拆为 `app/sdk/plugin/` 包：`base.py` 拥有契约基类，`manager.py` 拥有
`ModuleManager` 与 `PluginManager` 门面，包根只有文档。旧路径 `app.sdk.plugins` 登记为精确
模块别名，指向 `app.sdk.plugin.manager`。

处理链类在 SDK 中命名为 `PluginChain`。历史拼写 `PluginChian` 不进入 SDK，只作为 Compat
符号映射存在。

## 理由

`_PluginBase` 的构造依赖 `PluginDataOper`、`SystemConfigOper`、`MessageHelper` 和
`PluginDatabaseHandle`，横跨 `app.db`、`app.application` 两个方向。

`ChainBase` 的先例（canonical 在 `app.chain.base`，SDK 薄导出，包根交给 Compat）在这里走不通：
`app.runtime` 被 `FORBIDDEN_IMPORT_PREFIXES` 禁止导入 `app.adapters`、`app.application` 和
`app.sdk`，又被入口层门禁禁止导入 `app.db`。把契约基类放进 `app/runtime/extensions/plugin/`
必须先把四项依赖改成宿主注入的端口，等于改写插件构造语义和全部既装插件的 ABI。

`app/sdk` 是宿主中唯一没有 forbidden-import 约束的层，`app/sdk/_legacy/transferpending.py`
已有实现本体落在 SDK 的先例。契约基类是插件 ABI 本身而不是业务实现，由契约层拥有与
「sdk 为目标契约、compat 设版本期限」的既定方向一致。

## 影响

`docs/rules/05-architecture.md` 的落点决策第 10 条由「never move implementation there」改为
区分契约与实现：SDK 拥有插件契约类型本体，业务实现仍留 canonical 层。

`app/plugins/__init__.py` 此前直接导入 `app.core.config`、`app.core.event` 和
`app.helper.message` 三个幻影名，即宿主源码自己消费兼容层。迁移后依赖改为
`app.runtime.config`、`app.runtime.events`、`app.application.messaging.message` 和
`app.chain.base`，宿主对 Compat 的反向依赖清零。

包根不再在导入插件时拉起 Chain、数据库和消息栈，`app.plugins` 恢复为纯粹的插件安装命名空间。

既装插件的 `from app.plugins import _PluginBase` 保持可用，Compat 按既有规则在 DEBUG 下
每插件每符号发一次可执行的迁移提示。新插件使用 `from app.sdk.plugin import _PluginBase`。
