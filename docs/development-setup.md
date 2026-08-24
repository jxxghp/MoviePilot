## 开发环境设置指南

本文档旨在帮助开发者快速设置开发环境，并说明主程序、开发测试、构建工具和插件依赖的管理边界。

### 环境准备

在开始之前，请确保您的系统已安装以下软件：

- **Python 3.14+**
- **uv 0.12.5**（Python 版本、虚拟环境和依赖锁定工具）
- **Git** (用于版本控制)
- **RAR 解压工具**：本地开发如需测试或使用 `.rar` 字幕包解压，请安装 `unar`、`unrar`、`7z` 或 `bsdtar` 之一；Docker 镜像会内置 `unar`。

Rust 加速扩展通过 `moviepilot-rust` PyPI 包安装，主项目本地开发不再需要 Rust toolchain。需要修改或发布 Rust 扩展时，请在 `MoviePilot-Rust` 仓库中构建。

### 1. 创建锁定环境

仓库通过 `pyproject.toml` 声明直接依赖，并提交统一的 `uv.lock`。在项目根目录执行：

```bash
uv sync --locked
```

`uv` 会创建或更新 `.venv`，并安装运行时与默认 `dev` 依赖组。命令中的
`--locked` 会在 `pyproject.toml` 与 `uv.lock` 不一致时直接失败，避免开发环境静默解析出一套
未提交的依赖结果。只需要生产运行依赖时使用：

```bash
uv sync --locked --no-dev --no-install-project
```

### 2. 依赖分层与事实源

主程序只维护以下依赖事实源：

| 位置 | 用途 | 维护方式 |
| --- | --- | --- |
| `pyproject.toml` 的 `[project].dependencies` | 两套 Python 运行时共享的主程序生产依赖。 | 开发者按直接依赖的兼容范围维护。 |
| `pyproject.toml` 的 `[dependency-groups].dev` | pytest、覆盖率、Pylint 和源码构建等开发工具。 | 不进入 Docker 生产运行环境。 |
| `pyproject.toml` 的 `[dependency-groups].runtime-*` | 标准与 free-threaded 解释器互斥的 ABI 敏感运行依赖。 | 只放两套运行时确实不同的直接依赖。 |
| `uv.lock` | Python 3.14+、两套运行时 profile 和受支持平台共享的完整解析结果。 | 修改 `pyproject.toml` 后由 `uv lock` 更新并提交。 |

主程序不再维护 `requirements.in`、`requirements-dev.in` 或 `requirements.txt`，也不生成
平台专属的 requirements 锁文件。Docker、CLI 和 CI 都以提交的 `uv.lock` 为安装输入。

### 2.1 本地启动脚本

不需要打开 IDE 时，可以直接使用仓库内的启动脚本。脚本会自动定位项目根目录和虚拟环境，并以模块方式启动后端，避免 `ModuleNotFoundError: No module named 'app'`。

```bash
# 默认启动后端开发服务，前台运行，按 Ctrl+C 停止
./scripts/start-local.sh
./scripts/start-local.sh backend

# 如果已经安装前端发布包，可启动完整的前后端服务
./scripts/start-local.sh service start

# 管理完整服务
./scripts/start-local.sh stop
./scripts/start-local.sh restart
./scripts/start-local.sh status
./scripts/start-local.sh logs --follow
```

默认会使用 `DEBUG=true` 和 `DEV=true`，与 IDE 开发启动保持一致。开发热重载通过
`app.factory:create_app` 的 import string/factory 入口运行，文件变化后由 Uvicorn 重新创建
应用结构；不会尝试在 reload 进程间传递已经实例化的 FastAPI 对象。如果不需要热重载，
可以这样启动以降低资源占用：

```bash
DEV=false ./scripts/start-local.sh
```

脚本会优先使用 `CONFIG_DIR`，其次使用 `MOVIEPILOT_CONFIG_DIR`，再检测 `~/Documents/moviepilot`，最后回退到仓库内的 `config` 目录。需要使用其他配置目录时，可以这样运行：

```bash
MOVIEPILOT_CONFIG_DIR=/path/to/moviepilot-config ./scripts/start-local.sh
```

首次使用前如果脚本没有执行权限，运行：

```bash
chmod +x scripts/start-local.sh
```

### 3. 修改主程序依赖

新增或升级依赖时，先确认依赖属于哪个层级：

1. **共享运行时依赖**：被 `app/` 生产代码直接导入，或是生产功能、后台任务、插件框架启动必需，写入 `[project].dependencies`。
2. **ABI 敏感运行依赖**：标准与 free-threaded 解释器必须选择不同制品或版本时，分别写入 `runtime-standard` 和 `runtime-free-threaded`；两组保持互斥并由运行时统一选择。
3. **开发 / 测试 / 静态检查 / 构建依赖**：只用于单测、覆盖率、lint 辅助、源码构建等，写入 `[dependency-groups].dev`。
4. **工具依赖**：仓库要求使用 `uv 0.12.5`；不应为了安装工具而把它加入主程序运行依赖。
5. **插件依赖**：由插件清单声明并在插件安装阶段处理，不直接并入主程序依赖。

修改后更新并校验锁文件：

```bash
uv lock
uv lock --check
uv sync --locked
uv sync --locked --offline --inexact --no-dev --check
```

`uv pip check` 可用于查看第三方包元数据诊断，但不作为项目依赖合同：`oss2` 已停止维护，其元数据仍
声明旧 `crcmod`，而主程序统一使用保持相同导入接口的 `crcmod-plus`。项目一致性以锁文件和上述
`uv sync --check` 结果为准。

`uv.lock` 同时覆盖 Linux x86_64/arm64、macOS x86_64/arm64 和 Windows x64。统一锁文件只
固定解析结果，不能替代这些平台的真实安装门禁；平台条件依赖变更必须通过对应 CI 环境验证。

### 3.1 插件依赖清单

新插件可以在插件根目录使用 `pyproject.toml`，宿主只读取 `[project].dependencies` 作为运行依赖：

```toml
[project]
name = "example-plugin"
version = "1.0.0"
dependencies = ["example-package>=1,<2"]
```

插件依赖遵循以下合同：

- `pyproject.toml` 优先于历史 `requirements.txt`；两者同时存在时只读取前者；
- `[dependency-groups]` 属于插件自身的开发、测试或构建环境，宿主不安装其中内容；
- `pyproject.toml` 存在但格式或依赖声明无效时直接报错，不回退到 `requirements.txt`；
- 仅有 `requirements.txt` 的历史插件继续按原方式安装；
- 宿主不消费插件自己的 `uv.lock`，因为多个插件共享同一主程序环境，不能分别同步独立锁文件。

### 3.2 异步 HTTP 客户端边界

主程序自建的 `AsyncRequestUtils` 使用 HTTPX2，`app.sdk.network.AsyncRequestUtils` 与旧插件
入口 `app.utils.http.AsyncRequestUtils` 共享同一实现。未显式传入客户端时，返回的响应与抛出的
请求异常均来自 `httpx2`；直接依赖响应类型或异常类型的 V3 代码应导入 `httpx2`。

OpenAI、Anthropic、Google GenAI、LangChain、CloakBrowser 等第三方 SDK 继续使用它们声明的
HTTPX 版本。不得调用 `httpx2.alias_httpx()` 在进程内替换 `httpx`，否则会同时改变第三方 SDK、
测试工具和插件的导入结果。确需复用调用方自管客户端时，向 `AsyncRequestUtils` 传入
`httpx2.AsyncClient`。

### 4. 准备资源与插件目录

本地源码开发时，主程序需要读取资源文件和插件源码。相关文件需要放到主程序实际加载的目录下：

- **资源文件**：将 [MoviePilot-Resources](https://github.com/jxxghp/MoviePilot-Resources) 仓库中 `resources.v3/` 下的文件同步到本仓库的 `app/application/site/` 目录下。CLI 安装和 Docker 构建流程只读取 V3 资源。
- **插件源码**：需要开发或调试的插件放到本仓库的 `app/plugins/` 目录下，例如 `app/plugins/<插件目录>/`。主程序运行时从该目录加载插件，独立插件仓库只是源码来源。

如果资源文件没有放到 `app/application/site/`，站点索引、规则和内置资源相关能力可能无法按本地开发预期工作；如果插件没有放到 `app/plugins/`，主程序也不会在本地运行时发现该插件。

### 4.1 GitHub 发版时生成插件市场默认值

源码分支中的 `ConfigModel.PLUGIN_MARKET` 只保留官方插件仓库作为离线兜底。GitHub 的 V3 正式版与 Beta 镜像构建会检出 `MoviePilot-Wiki` 的 `main` 分支，并由 `scripts/generate_plugin_market_default.py` 读取 `plugin.md` 中 `plugin-market-repos:start/end` 标记区域，将规范化、去重后的公开仓库清单写入构建工作区。

生成过程遵循以下约束：

- 标记必须唯一、顺序正确，清单不能为空且必须包含 `jxxghp/MoviePilot-Plugins`；不满足时直接终止构建。
- 生成脚本只替换 `ConfigModel` 中的 `PLUGIN_MARKET` 默认值，不写入运行时环境变量，因此用户仍可通过系统环境变量或 `/config/app.env` 覆盖。
- 正式版工作流会创建仅由 Release Tag 引用的本地快照提交，Docker 镜像和 Tag 源码归档均来自该快照；Actions 不会将生成结果回写到 `v3` 分支。
- Release Tag 快照提交信息和镜像标签会记录本次使用的 MoviePilot Wiki Commit，便于追溯清单来源。

本地验证生成结果时，先激活项目虚拟环境，再执行：

```bash
python -m scripts.generate_plugin_market_default \
  --wiki-file /path/to/MoviePilot-Wiki/plugin.md \
  --config-file app/runtime/config.py
```

### 5. 运行依赖漏洞检查

正式发布会使用固定版本的 `pip-audit` 检查 `uv.lock` 锁定的运行时依赖。依赖变更后也可以在
本地执行同一检查：

```bash
uv export --quiet --locked --no-dev --no-emit-project \
  --output-file /tmp/moviepilot-audit-requirements.txt
uvx --from pip-audit==2.10.1 pip-audit \
  --require-hashes --disable-pip --strict --progress-spinner off \
  --requirement /tmp/moviepilot-audit-requirements.txt
```

导出文件由 `uv.lock` 生成且保留哈希，不作为项目依赖清单提交。

### 6. 提交代码前的检查

在提交代码之前，请确保完成以下步骤：

1. **确认依赖分层正确**：运行时包进入 `[project].dependencies`；测试、覆盖率、静态检查和构建辅助进入 `[dependency-groups].dev`；插件依赖不并入主程序运行时依赖。

2. **运行依赖漏洞检查**：确保锁定的运行时依赖通过 `pip-audit`。

3. **运行测试**：如果项目中包含测试，请确保所有测试都通过。运行以下命令以执行测试：

   ```bash
   uv run --locked --no-sync pytest
   ```

   `python tests/run.py` 在本地默认把排序后的测试文件按向上取整的连续区间切成 4 片，
   并启动 4 个独立 pytest 进程；GitHub Actions 使用同一入口的 `--shard N/TOTAL`
   参数启动对应分片。需要单进程调试时使用 `python tests/run.py --serial`。覆盖率报告
   按需通过 `Unit Tests` workflow 的手动触发串行生成，不阻塞常规 PR / push 门禁。

4. **运行架构与静态门禁**：主仓架构检查不依赖独立插件仓；官方插件兼容观察单独运行，
   任何检查命令都不会写入 fixture。

   ```bash
   uv run --locked --no-sync python scripts/architecture/baseline.py --check-host
   uv run --locked --no-sync python scripts/architecture/baseline.py \
     --check-plugins --plugin-repo ../MoviePilot-Plugins \
     --report official-plugin-architecture-report.json
   uv run --locked --no-sync pylint app/
   ```

   GitHub Actions 会在 `v3` 的 PR/push 中独立执行宿主架构门禁，并对本次改动的 Python
   文件执行 Pylint 硬门禁；`app/` 全量结果作为建议性报告上传。最新官方插件仓通过每周
   或手工观察工作流检查，只上传语义差异报告，不会自动更新已提交基线。

### 7. 参考资源

- [uv 官方文档](https://docs.astral.sh/uv/)
- [pip-audit](https://github.com/pypa/pip-audit)
- [MoviePilot-Resources](https://github.com/jxxghp/MoviePilot-Resources)
- [MoviePilot-Plugins](https://github.com/jxxghp/MoviePilot-Plugins)
