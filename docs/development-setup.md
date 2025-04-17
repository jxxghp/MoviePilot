## 开发环境设置指南

本文档旨在帮助开发者快速设置开发环境，并介绍如何使用 `pip-tools` 管理依赖项和使用 `safety` 进行安全检查。

### 环境准备

在开始之前，请确保您的系统已安装以下软件：

- **Python 3.12 或更高版本** (暂时兼容 3.11 ，推荐使用 3.12+)
- **pip** (Python 包管理器)
- **Git** (用于版本控制)

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

### 2. 使用 pip-tools 管理依赖项

我们使用 `pip-tools` 来管理项目的 Python 依赖项，这有助于保持 `requirements.txt` 文件的一致性和更新性。

#### 安装 pip-tools

首先，您需要安装 `pip-tools` 以便管理依赖项：

```bash
pip install pip-tools
```

#### 管理依赖项

1. **修改 `requirements.in` 文件**：

   `requirements.in` 文件是项目依赖项的源文件。要添加或更新依赖项，请直接编辑该文件。

2. **更新特定的依赖项**：

   如果你只想更新 `requirements.in` 中的某个特定依赖包，而不影响其他依赖项，可以使用 `--upgrade-package` 选项，指定要升级的包：

   ```bash
   pip-compile --upgrade-package <package-name> requirements.in
   ```

   例如，要只升级 `requests` 这个包，你可以运行以下命令：

   ```bash
   pip-compile --upgrade-package requests requirements.in
   ```
   
3. **全量更新依赖项**：

   如果你想更新 `requirements.in` 中的所有依赖包，运行以下命令生成或更新 `requirements.txt` 文件：

   ```bash
   pip-compile requirements.in
   ```

   这将根据 `requirements.in` 中指定的依赖项生成一个锁定的 `requirements.txt` 文件。

4. **安装依赖项**：

   使用以下命令安装 `requirements.txt` 文件中列出的依赖项：

   ```bash
   pip install -r requirements.txt
   ```

### 3. 运行安全检查

我们使用 `safety` 工具来检查依赖项中是否存在已知的安全漏洞。请确保在每次更新依赖项后都运行安全检查，以确保项目的安全性。

#### 安装 safety

您可以使用以下命令安装 `safety`：

```bash
pip install safety
```

#### 执行安全检查

运行以下命令以检查 `requirements.txt` 文件中列出的依赖项是否存在安全漏洞：

```bash
safety check -r requirements.txt --policy-file=safety.policy.yml > safety_report.txt
```

这将生成一个名为 `safety_report.txt` 的报告文件，您可以查看其中的漏洞报告并进行相应处理。

### 4. 提交代码前的检查

在提交代码之前，请确保完成以下步骤：

1. **确保依赖项已更新**：如果您对 `requirements.in` 进行了更改，请重新生成 `requirements.txt` 并安装依赖项。

2. **运行安全检查**：确保 `safety` 检查通过，没有新的安全漏洞。

3. **运行测试**：如果项目中包含测试，请确保所有测试都通过。运行以下命令以执行测试：

   ```bash
   pytest
   ```

### 5. 参考资源

- [pip-tools 官方文档](https://github.com/jazzband/pip-tools)
- [safety 官方文档](https://pyup.io/safety/)