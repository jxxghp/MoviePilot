## 开发环境设置指南

本文档旨在帮助开发者快速设置开发环境，并说明主程序、开发测试、构建工具和插件依赖的管理边界。

### 环境准备

在开始之前，请确保您的系统已安装以下软件：

- **Python 3.11 或更高版本**
- **pip** (Python 包管理器)
- **Git** (用于版本控制)
- **RAR 解压工具**：本地开发如需测试或使用 `.rar` 字幕包解压，请安装 `unar`、`unrar`、`7z` 或 `bsdtar` 之一；Docker 镜像会内置 `unar`。

Rust 加速扩展通过 `moviepilot-rust` PyPI 包安装，主项目本地开发不再需要 Rust toolchain。需要修改或发布 Rust 扩展时，请在 `MoviePilot-Rust` 仓库中构建。

### 1. 创建虚拟环境

在项目根目录下创建并激活虚拟环境：

- 在 Windows 上：

  ```bash
  python -m venv venv
  .\venv\Scripts\activate
  ```

- 在 macOS/Linux 上：

  ```bash
  python3 -m venv venv
  source venv/bin/activate
  ```

虚拟环境确保项目的依赖项与系统全局环境隔离，防止冲突。

### 2. 依赖分层与安装

主程序依赖按使用场景分层，避免运行时镜像携带只在开发、测试或构建时需要的工具：

| 文件 | 用途 | 典型安装场景 |
| --- | --- | --- |
| `requirements.in` | 主程序运行时依赖。只放启动、后台任务、插件运行框架和内置功能在生产环境需要导入的包。 | Docker 镜像、CLI 本地运行、运行时依赖自愈。 |
| `requirements-dev.in` | 开发、测试、静态检查和源码构建辅助依赖。 | CI 单测、本地跑测、Pylint、显式源码构建。 |
| `requirements.txt` | 兼容入口，默认只委托到 `requirements.in`。它不是跨平台完整锁文件，不应在本地开发机上直接维护一份平台相关锁定结果。 | 旧脚本、Docker 运行时恢复、CLI 安装入口。 |

运行主程序只需要安装运行时依赖：

```bash
pip install -r requirements.txt
```

开发、测试、静态检查或执行源码编译时安装开发依赖入口：

```bash
pip install -r requirements-dev.in
```

### 3. 修改主程序依赖

新增或升级依赖时，先确认依赖属于哪个层级：

1. **运行时依赖**：被 `app/` 生产代码直接导入，或是生产功能、后台任务、插件框架启动必需，写入 `requirements.in`。
2. **开发 / 测试 / 静态检查 / 构建依赖**：只用于单测、覆盖率、lint 辅助、源码构建等，不应进入生产运行时，写入 `requirements-dev.in`。
3. **工具依赖**：`pip-tools`、`uv`、`safety` 这类安装或审计工具不属于主程序运行依赖，按脚本或 CI 场景显式安装。
4. **插件依赖**：由插件声明并在插件安装阶段处理，不直接并入主程序 `requirements.in`。

### 4. 准备资源与插件目录

本地源码开发时，主程序需要读取资源文件和插件源码。相关文件需要放到主程序实际加载的目录下：

- **资源文件**：将 [MoviePilot-Resources](https://github.com/jxxghp/MoviePilot-Resources) 仓库中 `resources.v2/` 下的文件同步到本仓库的 `app/helper/` 目录下。CLI 安装和 Docker 构建流程也会按这个位置准备资源。
- **插件源码**：需要开发或调试的插件放到本仓库的 `app/plugins/` 目录下，例如 `app/plugins/<插件目录>/`。主程序运行时从该目录加载插件，独立插件仓库只是源码来源。

如果资源文件没有放到 `app/helper/`，站点索引、规则和内置资源相关能力可能无法按本地开发预期工作；如果插件没有放到 `app/plugins/`，主程序也不会在本地运行时发现该插件。

### 5. 运行安全检查

我们使用 `safety` 工具检查依赖项中是否存在已知安全漏洞。更新运行时依赖后，应至少检查运行时入口；更新开发测试依赖时，也应覆盖开发入口。

#### 安装 safety

您可以使用以下命令安装 `safety`：

```bash
pip install safety
```

#### 执行安全检查

运行以下命令检查运行时入口：

```bash
safety check -r requirements.txt --policy-file=safety.policy.yml > safety_report.txt
```

这将生成一个名为 `safety_report.txt` 的报告文件，您可以查看其中的漏洞报告并进行相应处理。

### 6. 提交代码前的检查

在提交代码之前，请确保完成以下步骤：

1. **确认依赖分层正确**：运行时包进入 `requirements.in`；测试、覆盖率、静态检查和构建辅助进入 `requirements-dev.in`；插件依赖不并入主程序运行时依赖。

2. **运行安全检查**：确保 `safety` 检查通过，没有新的安全漏洞。

3. **运行测试**：如果项目中包含测试，请确保所有测试都通过。运行以下命令以执行测试：

   ```bash
   pytest
   ```

### 7. 参考资源

- [pip-tools 官方文档](https://github.com/jazzband/pip-tools)
- [uv 官方文档](https://docs.astral.sh/uv/)
- [safety 官方文档](https://pyup.io/safety/)
- [MoviePilot-Resources](https://github.com/jxxghp/MoviePilot-Resources)
- [MoviePilot-Plugins](https://github.com/jxxghp/MoviePilot-Plugins)
