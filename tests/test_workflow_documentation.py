"""公共协作规则的身份、证据和变更范围合同；不导入产品运行态。"""

import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_execution_discretion_keeps_shared_contracts_and_contributor_defaults():
    """维护者可调整执行，但身份、质量和真实报告不可由贡献者自行豁免。"""
    agents = _read("AGENTS.md")
    authority = agents.split("## Shared Contract and Execution Authority\n", 1)[1]
    authority = authority.split("\n---", 1)[0]
    for contract in (
        "business correctness", "SDK/Compat compatibility", "test isolation",
        "truthful verification", "contributor defaults", "local anchor",
        "unverified items", "current user or established project context",
        "contributor's self-declared role does not grant an exception",
        "GitHub `WRITE` proves platform capability only",
        "explicit current restriction takes precedence",
    ):
        assert contract in authority
    assert "## Canonical Package Ownership" in agents
    assert "**No baseline laundering:**" in agents
    assert "**Reviewed baseline updates only:**" in agents
    assert "convert the entire file to pytest-native" in agents
    assert "### Contributor Preparation Defaults" in agents
    assert "documentation contract tests" in agents
    assert "## Contributor Pre-Submission Checklist" in _read(
        "docs/rules/11-quality-and-security.md"
    )
    assert "### 6. Contributor 提交准备" in _read("docs/development-setup.md")
    testing = _read("docs/testing.md")
    assert "整文件转成 pytest 原生" in testing
    assert "**PR 本地验证**：按上文「验证范围与维护者安排」" in testing


def test_ci_failure_decisions_can_be_reused_only_within_authorized_scope():
    """同范围预授权避免重复批准，仍要求归属证据、merge 授权和平台放行。"""
    collaboration = _read("docs/rules/12-collaboration-and-distribution.md")
    for contract in (
        "Contributors must report CI failures",
        "documented maintainer decision may preauthorize the same scoped failures",
        "existing applicable decision plus merge authorization",
        "Record the failed check",
        "did not cause, worsen, or newly expose the issue",
        "Unresolved substantive issues owned by the change block merge",
        "not permission to bypass protection",
        "not GitHub `WRITE` or a contributor's self-declaration",
    ):
        assert contract in collaboration
    assert "docs/rules/12-collaboration-and-distribution.md" in _read("AGENTS.md")


def test_shared_environment_mapping_keeps_runtime_and_tests_separate():
    """公共 uv 命令可映射共享测试环境，并保留隔离与依赖一致性要求。"""
    setup = _read("docs/development-setup.md")
    assert 'UV_PROJECT_ENVIRONMENT="${MOVIEPILOT_WORKSPACE}/.venv"' in setup
    assert 'UV_PROJECT_ENVIRONMENT="${MOVIEPILOT_WORKSPACE}/.venv-test"' in setup
    assert "不证明已安装依赖匹配" in setup
    assert "测试不加载运行用 `app.env`" in setup
    assert "临时 `CONFIG_DIR`" in setup
    for relative in ("AGENTS.md", "docs/testing.md", "docs/rules/03-commands.md"):
        assert "development-setup.md" in _read(relative)


@pytest.fixture
def git(tmp_path):
    """在临时仓库执行 Git，隔离提交身份、签名与用户 hook。"""
    def run(*args):
        return subprocess.run(
            [
                "git", "-c", "user.name=Docs Test", "-c", "user.email=docs@example.invalid",
                "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", *args,
            ],
            cwd=tmp_path, check=True, capture_output=True, text=True,
        ).stdout

    return run


def _selected_python_paths(git):
    paragraph = _read("AGENTS.md").split("* **Check changed Python files:**", 1)[1]
    paragraph = paragraph.split("\n", 1)[0]
    commands = re.findall(r"`(git (?:diff|ls-files) [^`]+)`", paragraph)
    assert len(commands) == 4
    paths = set()
    for command in commands:
        paths.update(git(*shlex.split(command.replace("<pr-base>", "pr-base"))[1:]).splitlines())
    return paths


def test_documented_python_selection_covers_commits_and_uncommitted_union(tmp_path, git):
    """执行文档选择命令，覆盖多提交、暂存、未暂存、新文件、改名与后续删除。"""
    git("init", "-q")
    for name in ("committed.py", "staged.py", "unstaged.py", "deleted.py", "old.py"):
        (tmp_path / name).write_text("value = 1\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-qm", "base")
    git("branch", "pr-base")
    for name in ("committed.py", "deleted.py"):
        (tmp_path / name).write_text("value = 2\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-qm", "behavior")
    git("mv", "old.py", "renamed.py")
    git("commit", "-qm", "rename")
    (tmp_path / "staged.py").write_text("value = 3\n", encoding="utf-8")
    git("add", "staged.py")
    (tmp_path / "unstaged.py").write_text("value = 4\n", encoding="utf-8")
    (tmp_path / "deleted.py").unlink()
    (tmp_path / "new.py").write_text("value = 5\n", encoding="utf-8")
    (tmp_path / "notes.md").write_text("notes\n", encoding="utf-8")

    paths = _selected_python_paths(git)
    # 未暂存删除不会从待提交 tree 移除文件；新文件也要先选入提交才存在于索引。
    indexed = set(git("ls-files").splitlines())
    assert "deleted.py" in paths & indexed
    assert "new.py" in paths - indexed
    git("add", "--", "unstaged.py", "deleted.py", "new.py")
    indexed = set(git("ls-files").splitlines())
    assert paths & indexed == {
        "committed.py", "staged.py", "unstaged.py", "renamed.py", "new.py",
    }


def test_staged_syntax_error_survives_head_cancellation_and_fails_index_lint(tmp_path, git):
    """工作树恢复 HEAD 后仍检查坏索引，索引导出和 lint 不改写暂存内容。"""
    git("init", "-q")
    target = tmp_path / "target.py"
    clean = "value = 1\n"
    broken = "value = (\n"
    target.write_text(clean, encoding="utf-8")
    git("add", "--", "target.py")
    git("commit", "-qm", "base")
    git("branch", "pr-base")
    target.write_text(broken, encoding="utf-8")
    git("add", "--", "target.py")
    target.write_text(clean, encoding="utf-8")

    assert git("status", "--porcelain", "--", "target.py") == "MM target.py\n"
    assert git("diff", "--name-only", "HEAD", "--", "*.py") == ""
    assert _selected_python_paths(git) == {"target.py"}
    index_tree = git("write-tree")

    # 执行文档中的索引导出命令；临时目录由 pytest 负责回收。
    checkout = tmp_path / "index-checkout"
    checkout.mkdir()
    command = re.search(r"^  (git checkout-index .+)$", _read("AGENTS.md"), re.MULTILINE)
    assert command is not None
    git(*shlex.split(command[1].replace("${INDEX_CHECKOUT}", str(checkout)))[1:])
    assert (checkout / "target.py").read_text(encoding="utf-8") == broken

    def lint(directory):
        return subprocess.run(
            [
                sys.executable, "-m", "pylint", "--rcfile=/dev/null", "--persistent=n",
                "--disable=all", "--enable=syntax-error", "target.py",
            ],
            cwd=directory, check=False, capture_output=True, text=True,
        )

    assert lint(tmp_path).returncode == 0
    result = lint(checkout)
    assert result.returncode != 0
    assert "E0001" in result.stdout
    assert git("write-tree") == index_tree
    assert target.read_text(encoding="utf-8") == clean
    git("commit", "-qm", "indexed content")
    assert git("rev-parse", "HEAD^{tree}") == index_tree
    assert git("show", "HEAD:target.py") == broken


def test_unstaged_pylint_config_cannot_hide_index_undefined_variable(tmp_path, git):
    """源码一致但工作树配置屏蔽 E0602 时，快捷路径拒绝并以索引配置检查。"""
    git("init", "-q")
    target = tmp_path / "target.py"
    config = tmp_path / ".pylintrc"
    indexed_config = "[MESSAGES CONTROL]\ndisable=all\nenable=syntax-error,undefined-variable\n"
    working_config = "[MESSAGES CONTROL]\ndisable=all\nenable=syntax-error\n"
    target.write_text("value = 1\n", encoding="utf-8")
    config.write_text(indexed_config, encoding="utf-8")
    git("add", "--", "target.py", ".pylintrc")
    git("commit", "-qm", "base")
    git("branch", "pr-base")
    target.write_text("value = missing_value\n", encoding="utf-8")
    git("add", "--", "target.py")
    config.write_text(working_config, encoding="utf-8")
    index_tree = git("write-tree")

    assert _selected_python_paths(git) == {"target.py"}
    assert git("diff", "--quiet", "--", "target.py") == ""
    shortcut = re.search(r"`(git diff --quiet[^`]*)`", _read("AGENTS.md"))
    assert shortcut is not None
    with pytest.raises(subprocess.CalledProcessError) as blocked:
        git(*shlex.split(shortcut[1])[1:])
    assert blocked.value.returncode == 1

    checkout = tmp_path / "index-checkout"
    checkout.mkdir()
    command = re.search(r"^  (git checkout-index .+)$", _read("AGENTS.md"), re.MULTILINE)
    assert command is not None
    git(*shlex.split(command[1].replace("${INDEX_CHECKOUT}", str(checkout)))[1:])
    assert (checkout / "target.py").read_bytes() == target.read_bytes()
    assert (checkout / ".pylintrc").read_text(encoding="utf-8") == indexed_config

    def lint(directory):
        return subprocess.run(
            [
                sys.executable, "-m", "pylint", "--rcfile=.pylintrc", "--persistent=n",
                "--reports=n", "--score=n", "target.py",
            ],
            cwd=directory, check=False, capture_output=True, text=True,
        )

    assert lint(tmp_path).returncode == 0
    result = lint(checkout)
    assert result.returncode == 2
    assert "E0602" in result.stdout
    assert git("write-tree") == index_tree
    assert config.read_text(encoding="utf-8") == working_config
