# 小组件只读接口

MoviePilot App 的系统小组件直接读取用户连接的 MoviePilot 实例。中央 MoviePilot Server
不接收业务快照或小组件凭据。这些接口不改变普通 Web/PWA 的请求和登录行为。

## 启用与移除

前台 App 先使用既有主登录读取 `GET /api/v1/widget/context`。标准响应的 `data` 只包含
`version=1`、不可变 `owner` 和当前 `availableContents`，不会创建、轮换或列出任何登记。
没有可用内容时返回空集合。App 用不可变身份核对组件资料与当前网页账号，不能仅比较可复用的
数字用户 ID 或显示名；组件专用凭据不能调用该接口。此响应禁止缓存，不能用旧上下文替代当前校验。

前台 App 在用户明确启用后，使用既有登录调用
`POST /api/v1/widget/registrations`，请求体只包含随机 UUID `installationId`。
标准响应的 `data` 包含 `version=1`、`registrationId`、`installationId`、不可变 `owner`、
`secret`、`expiresAt` 和 `availableContents`。`secret` 只返回这一次，数据库只保存 SHA-256 摘要。

凭据有效期为 30 天，每个账号最多 10 个有效安装。同账号同安装再次登记会在一个事务中
撤销旧凭据并保存新凭据，安装名额不变。登记必须串行执行；响应丢失后可以重新登记，
但会再次轮换，不能恢复旧密钥，也不应由 HTTP 客户端自动重放写请求。并发登记按提交顺序覆盖。
扩展不能自行续期；主 App 通过现有登录更新授权。

主登录可调用 `DELETE /api/v1/widget/registrations/{registrationId}` 移除自己的单个登记。
专用凭据可调用 `DELETE /api/v1/widget/registration` 撤销自身，不能指定其他安装。
没有公开的凭据列表，不返回其他用户的登记存在性。

## 刷新与权限

`GET /api/v1/widget/snapshot` **只接受** `Authorization: Bearer <secret>`，不接受 URL 查询、
Cookie、主登录 JWT、资源令牌或 `API_TOKEN`。小组件凭据也不能用于普通 REST/MCP 业务 API。
快照直接返回下述模型，不额外包裹标准响应的 `data`。成功和鉴权错误均禁止共享缓存。

每次请求都重新检查登记、到期、激活账号、不可变身份及最新权限；来源读取完成后再次检查，
等待中的撤销、改名或权限变化会阻止旧快照返回。账号删除时级联清理；即使主键复用或旧库
存在残留登记，新建账号也不能继承旧授权。

| 内容 | 授权和数据范围 |
| --- | --- |
| `overview` | 仅超级管理员；复用仪表板媒体统计和存储汇总 |
| `downloads` | `manage`，且 `manage.downloading` 未禁用；普通用户按网页相同的 `userid` / `username` 精确匹配本人 |
| `imports` | `manage`，且 `manage.history` 未禁用；原历史查询的最近成功入库记录 |
| `movieSubscriptions` | `subscribe`，且 `subscribe.movie` 未禁用；本人电影订阅 |
| `tvSubscriptions` | `subscribe`，且 `subscribe.tv` 未禁用；本人剧集订阅 |
| `musicSubscriptions` | `subscribe`，且 `subscribe.music` 未禁用；本人音乐订阅 |

管理员沿用原页面的全局数据范围。普通用户的分类权限必须明确为 `true`，缺省功能权限继承
分类；设置中的 `admin` 标记不能授予管理员权限。订阅按数据库 owner/type 分页并返回真实总数，
不把展示的六项摘要当作全部数量。

## 快照协议 v1

根字段为 `version`、`owner`、`generatedAt`、`expiresAt`、`availableContents`、`sections`。
时间均为 UTC Unix 秒；展示有效期最长 24 小时，且不晚于凭据到期。
可用内容与 section 一一对应，权限不足的内容不返回。

每个 section 包含 `content`、`title`、`state`、可选 `updatedAt`、`metrics`、`items` 和可选 `message`。
`ready` 表示成功，`empty` 表示成功但没有记录，`unavailable` 表示来源不可用。
失败来源没有数据或伪造的采样时间，不会被转换成成功的零值；其他成功来源仍可展示。
旧记录的时间没有明确时区时，不猜测为 UTC。

每类最多六个指标和六条摘要，完整 JSON 不超过 32 KiB。标题、辅助文字和值按 UTF-8 字节限制，
移除控制字符。下载摘要展示当前采样的速度和自然进度；进度为 0…1。
不返回下载 hash、完整路径、tracker、图片 URL、插件配置、主登录令牌或业务命令。

客户端须验证版本、账号、内容枚举、体积和期限，使用系统调度刷新并显示实际更新时间。
401/403 时立即清理本地授权和快照，引导在 App 中重新启用；网络错误可按客户端明确的
过期策略暂用旧快照，不能把失败时刻改成“刚刚更新”。退出时先尝试撤销，断网不能阻止本地清理。
反向代理应保留 Authorization、使用 HTTPS，并避免记录凭据；不应把密钥放入 URL。
