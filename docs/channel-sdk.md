# 渠道扩展 SDK v1

渠道扩展类型入口为 `app.sdk.channel`，当前 `CHANNEL_EXTENSION_API_VERSION == 1`。它重用现有规范模型和 `ChannelPlatform` Protocol，不提供新的 DTO、注册表或调度器。

```python
from app.sdk.channel import CHANNEL_EXTENSION_API_VERSION, ChannelActor, NotificationChannel

assert CHANNEL_EXTENSION_API_VERSION == 1
transport = NotificationChannel("platform:example-host")
actor = ChannelActor("person-id")
```

这段代码只创建数据。扩展标识解析不写进程级枚举缓存，不注册平台、建立连接或批准用户。完整导出集合以 [channel.py](../app/sdk/channel.py) 的 `__all__` 为准；方法形状由 [ChannelPlatform](../app/application/messaging/channel/platform.py) 和版本回归共同维护。

宿主扩展使用既有 `app/modules/<provider>/capability.toml`，声明 `kind = "host_module"`、本包 entrypoint、`metadata.channel_extension`、`metadata.channel_interaction` 和 `metadata.channel_api_version = 1`。未声明版本的旧宿主扩展按 v1 解释，未知或格式错误的版本拒绝加载。传输类型、provider、连接身份空间和解析 owner 的职责分开；同一传输可由不同 provider 使用，实例和业务权限仍隔离。

平台实现负责协议认证、规范个人/群组/线程、地址、凭据及特殊分段；宿主注册投影提供 module_id、provider、显示名称和 api_version。`ChannelConnection` 是受信任宿主扩展可接收的私有配置类型，导出类型并不提供读取已保存配置的接口。普通插件不能通过 SDK 注入接收器、提交私有证明或把自己注册为宿主 owner。

v1 保持当前类型对象、方法及参数语义；增加必需方法或改变既有语义需要兼容审查和新版本。可安装插件渠道的信任模型、认证委派与生命周期没有在本轮开放。已有平台传输 SDK（如 `app.sdk.feishu`）和插件模块方法继续按各自合同兼容，不等同于渠道信任接入。

宿主认证入站只调用装配时绑定的队列入口；缺身份、未就绪、停止、执行器拒绝或失败都会诊断，不隐式回环 HTTP。队列接受不代表审批或业务操作成功。旧插件的显式 HTTP helper 由 `app.sdk._legacy.ingress` 及精确惰性旧名适配保留，仍执行公开 HTTP 认证与当前授权，不能构造平台证明。没有具备移除条件的公开入口不会直接删除，也没有虚构弃用期限。

逐渠道清单、实际调用链、确认/重试语义、退出条件和验证索引见[SDK 与兼容台账](channel-refactor/sdk-boundaries.md)。
