# 结构化通知与渠道能力

插件通过 `post_message(text=..., rich_content=...)` 提交一份与渠道无关的通知。
`text` 保留完整纯文本结果；`rich_content` 使用 `NotificationContent`，只包含标题和有序
`NotificationBlock` 列表，每段的 `title`、`text` 都是普通文本。插件不枚举渠道、不插入
Markdown 或 HTML 标签，也不判断渠道权限。

宿主通过 `ChannelCapabilityManager.get_notification_text_format()` 查询普通通知的投递能力，
按 `ChannelCapabilities.notification_text_format` 选择语法和转义方式。未声明该能力的渠道
继续使用 `text`。`notification_text_bytes` 提供正文的 UTF-8 字节预算；有原生分段协议的
渠道由自身实现控制。能力配置中的 `notification_mode_key` / `notification_mode_value`
区分企业微信应用与智能机器人模式。QQ 的实际服务端 Markdown 权限通过原有错误回退检测。

| 渠道 | 主程序投递方式 |
| --- | --- |
| Telegram | Rich Message，沿用自动分段并补充详情入口 |
| 飞书 | JSON 2.0 Markdown 卡片，按实际卡片 JSON 字节预算分段 |
| Slack | Block Kit，每条最多 50 个 block，保留标题和详情按钮 |
| 钉钉 | 自定义机器人 Markdown 消息 |
| Discord | 原生 Embed，按正文预算分段 |
| VoceChat | 原有 text/markdown 通道 |
| Synology Chat | 原有 Chat 格式化消息 |
| 企业微信智能机器人 | Markdown 消息及原生分段；应用模式仍使用纯文本 |
| QQ Bot | 有权限时分段发送，权限不足时回退原始纯文本一次 |

平台其他交互场景的 `RICH_TEXT` / `MARKDOWN` 声明与普通通知的投递协议相区别。
WebAgent 能渲染 Markdown，但通知中心仍记录普通文本；ClawBot 当前投递实现使用
`send_text`，不采用格式化通知。新渠道声明通知格式并接入适配器后，插件无需维护名称白名单。

新字段不改变通知开关、管理员或用户收件人路由。历史仍记录原始 `title`、`text`。
Telegram 原有的 `rich_message` 用于完整 Rich Markdown 正文，未提供 `rich_content` 时
沿用原来的行为。渠道 API 错误不因一次临时网络错误自动重发另一份通知。

站点自动签到只负责统计、结果分组和重试标记，结构化内容包括本次全部站点。
V3 插件版本为 3.0.0，显式声明 `system_version >=3.1.6`，提供默认开启的
“优化消息通知样式”开关；关闭后不传结构化内容，所有渠道恢复原有通知样式。
原 V2 版本 2.9.13 保持源码及版本不变，仅在市场索引声明 `v3: false`。
具体说明见插件仓 `plugins.v3/autosignin/README.md`。

平台格式参考：[Telegram Rich Messages](https://core.telegram.org/bots/features#rich-messages)、
[飞书 Markdown](https://open.feishu.cn/document/uAjLw4CM/ukzMukzMukzM/feishu-cards/card-json-v2-components/content-components/rich-text)、
[Slack section](https://docs.slack.dev/reference/block-kit/blocks/section-block/)、
[Discord Embed](https://docs.discord.com/developers/resources/message#embed-object)、
[Synology Chat](https://kb.synology.com/en-au/DSM/help/Chat/chat_enriching_your_chat?version=6)。
