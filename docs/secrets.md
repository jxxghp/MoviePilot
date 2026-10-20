# 使用 Docker / Kubernetes Secrets

MoviePilot V3 支持把部署配置作为 UTF-8 文件挂载到 `/run/secrets/`，例如
`/run/secrets/API_TOKEN`、`/run/secrets/GITHUB_TOKEN` 或
`/run/secrets/DB_POSTGRESQL_PASSWORD`。文件内容直接进入配置解析，不导出为进程环境变量，
不复制到 `app.env`。此能力针对主程序的部署配置，不读取插件数据库配置。

配置优先级为：**显式构造参数 > 进程环境变量 > `<CONFIG_DIR>/app.env` > secret 文件 > 字段默认值**。
Web 设置通常写入 `app.env`；环境变量管理的项仍拒绝界面写回。
只要目录中存在同名 secret 项（包括无效文件、目录或被高优先级来源遮蔽的项），
界面与自动修正都不会为它写入 `app.env`，并提示修改文件后重启。原值不变的提交无需更新。

## 目录与格式

- 默认读取 `/run/secrets`，目录不存在时静默使用其他来源与默认值。
- 在**进程环境变量**中设置 `MOVIEPILOT_SECRETS_DIR` 可更换目录，兼容 `SECRETS_DIR`；前者优先。
  显式设为空字符串、`false`、`off` 或 `0` 可关闭 secrets。目录开关在模块导入时确定，
  不从 `app.env` 或 secret 文件读取。`CONFIG_DIR` 仍应在进程环境中设置。
- 文件名必须与配置项大小写完全一致，例如 `API_TOKEN`，不能写成 `api_token`。
- 文件写**裸值**，不要照搬 `app.env` 的外层引号；引号属于文件内容。
- 内容使用 UTF-8，允许 UTF-8 BOM；首尾空白和末尾换行会去除，内部换行保留。
- list / dict 字段使用 JSON，例如 `ALLOWED_HOSTS` 的内容为 `["https://mp.example.com"]`，
  `DB_CONNECT_ARGS` 的内容为 `{"connect_timeout": 10}`。
- 空文件、非法 UTF-8、非法 JSON、类型或字段约束错误、过短的 `API_TOKEN` 会按项跳过，
  日志只提示字段名。`API_TOKEN` 需至少 16 个字符；无其他有效来源时保持未配置，
  不为无效的外部令牌生成另一份密钥。错误目录或读取权限也会告警，请修正挂载和运行用户权限。
- 支持 K8s 投影文件的符号链接。轮换文件内容后必须重启 MoviePilot；现有进程不会热加载。
- 当前不支持 `XXX_FILE` 约定。单项任意路径可通过挂载到 secrets 目录中的同名文件实现。

## Compose 示例

```yaml
services:
  moviepilot:
    image: jxxghp/moviepilot-v3:latest
    volumes:
      - ./config:/config
    secrets:
      - source: moviepilot_api_token
        target: API_TOKEN
      - source: moviepilot_github_token
        target: GITHUB_TOKEN

secrets:
  moviepilot_api_token:
    file: ./private/api_token
  moviepilot_github_token:
    file: ./private/github_token
```

把实际密钥放到 `./private/` 对应文件并从版本管理中排除；Compose 文件只引用路径。
Docker Swarm 可将顶层声明改为已有的外部 secret；Kubernetes 可把 Secret 的 key 按配置名投影到
`/run/secrets`。按部署平台提供合适的只读权限，确保 MoviePilot 运行用户能够读取。

## 从旧配置迁移

先准备 secret 文件，再从编排环境变量和 `app.env` 中移除同名值，最后重启。
高优先级值仍存在时会继续生效，轮换 secret 无法覆盖它。已经写在 `app.env` 里的历史值
需要手工删除整行；界面“清空”可能写入字段默认值，不代表删除覆盖项。
外部管理的配置不能通过 Web 或 CLI 改为本地配置；需要先移除 secret 挂载并重启。
