"""验证采集器独立版本及发布边界，所有 Git 操作仅使用临时仓库。"""

import os
import subprocess
from pathlib import Path

import pytest
from ruamel.yaml import YAML

from scripts.collector import release

PROJECT_ROOT = Path(__file__).parents[1]


def _git(repository: Path, *arguments: str) -> str:
    """在临时仓库运行 Git，隔离真实仓库和用户提交身份。"""
    return subprocess.check_output(
        ["git", "-c", "user.name=Collector Test", "-c", "user.email=collector@example.invalid",
         *arguments], cwd=repository, text=True,
    ).strip()


@pytest.fixture
def collector_repository(tmp_path: Path) -> Path:
    """创建已发布 1.0.1 的临时采集器仓库。"""
    _git(tmp_path, "init", "-b", "v3")
    for relative in release.RELEASE_PATHS:
        path = tmp_path / relative
        if relative == "scripts/collector":
            path = path / "release.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("initial\n", encoding="utf-8")
    (tmp_path / "scripts/site_adapter_collector.py").write_text(
        'COLLECTOR_VERSION = "1.0.1"\n', encoding="utf-8",
    )
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "initial collector")
    _git(tmp_path, "tag", "site-adapter-collector-v1.0.1")
    return tmp_path


def test_latest_release_ignores_backend_and_orders_versions_numerically():
    """后端版本和非法标签不得参与采集器版本排序。"""
    assert release.latest_release_tag([
        "v3.1.4", "site-adapter-collector-v1.0.9", "site-adapter-collector-v1.0.10",
        "site-adapter-collector-v1.0.11-rc1", "site-adapter-collector-v1.01.1",
    ]) == "site-adapter-collector-v1.0.10"
    assert release.latest_release_tag(["v3.1.4"]) is None


def test_first_release_uses_existing_collector_version(collector_repository: Path):
    """首次独立发布沿用采集器版本，不能使用后端版本号。"""
    plan = release.release_plan(collector_repository, None)
    assert plan["tag"] == "site-adapter-collector-v1.0.1"
    assert plan["changed"] == "true"
    assert plan["source_sha"] == _git(collector_repository, "rev-parse", "HEAD")


def test_backend_and_document_changes_do_not_release(collector_repository: Path):
    """后端和文档提交、手动重复执行都不能消耗采集器版本。"""
    for name in ("version.py", "README.md", "tests/test_collector.py"):
        path = collector_repository / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("unrelated\n", encoding="utf-8")
    _git(collector_repository, "add", ".")
    _git(collector_repository, "commit", "-m", "backend and docs")
    for _ in range(2):
        plan = release.release_plan(collector_repository, "site-adapter-collector-v1.0.1")
        assert plan["changed"] == "false"
        assert plan["version"] == "1.0.1"


@pytest.mark.parametrize("relative", release.RELEASE_PATHS)
def test_collector_input_change_increments_patch(collector_repository: Path, relative: str):
    """采集器任意发布输入改变都必须生成下一个补丁版本。"""
    path = collector_repository / relative
    if relative == "scripts/collector":
        path = path / "release.py"
    with path.open("a", encoding="utf-8") as output:
        output.write("# collector change\n")
    _git(collector_repository, "add", ".")
    _git(collector_repository, "commit", "-m", "change collector input")
    plan = release.release_plan(collector_repository, "site-adapter-collector-v1.0.1")
    assert plan["changed"] == "true"
    assert plan["version"] == "1.0.2"


def test_subsequent_release_uses_published_version(collector_repository: Path):
    """源码版本无需回写，下次补丁必须以正式 Release 版本为准。"""
    _git(collector_repository, "tag", "site-adapter-collector-v1.0.9")
    (collector_repository / "scripts/site_adapter_collector.spec").unlink()
    _git(collector_repository, "add", ".")
    _git(collector_repository, "commit", "-m", "remove collector build input")
    assert release.release_plan(
        collector_repository, "site-adapter-collector-v1.0.9",
    )["version"] == "1.0.10"


def test_missing_previous_tag_fails_instead_of_publishing(collector_repository: Path):
    """比较失败不能误判为采集器改变并发版。"""
    with pytest.raises(RuntimeError, match="无法比较"):
        release.release_plan(collector_repository, "site-adapter-collector-v1.0.8")


def test_stamp_sets_runtime_and_manifest_version(tmp_path: Path):
    """构建版本写入现有声明，其他源码和采集包协议版本保持一致。"""
    source = tmp_path / "collector.py"
    source.write_text('COLLECTOR_VERSION = "1.0.1"\nFORMAT_VERSION = 1\n', encoding="utf-8")
    release.stamp_version(source, "1.0.10")
    assert source.read_text(encoding="utf-8") == 'COLLECTOR_VERSION = "1.0.10"\nFORMAT_VERSION = 1\n'
    with pytest.raises(ValueError, match="无效采集器版本"):
        release.stamp_version(source, "1.0.10\ninvalid")


def test_workflow_separates_releases_and_pins_all_build_sources():
    """工作流只跟随采集器输入，三平台成功才发布且保留主程序 latest。"""
    workflow = YAML(typ="safe").load(
        (PROJECT_ROOT / ".github/workflows/site-adapter-collector.yml").read_text(encoding="utf-8"),
    )
    assert "release" not in workflow["on"]
    assert workflow["on"]["push"]["branches"] == ["v3"]
    paths = workflow["on"]["push"]["paths"]
    assert set(paths) == set(release.RELEASE_PATHS) - {"scripts/collector"} | {"scripts/collector/**"}
    assert workflow["concurrency"]["cancel-in-progress"] is False
    jobs = workflow["jobs"]
    assert "needs.prepare.outputs.changed == 'true'" in jobs["build"]["if"]
    assert "!inputs.publish" in jobs["build"]["if"]
    assert jobs["publish"]["needs"] == ["prepare", "build"]
    for job in ("build", "publish"):
        checkout = jobs[job]["steps"][0]
        assert checkout["with"]["ref"] == "${{ needs.prepare.outputs.source_sha }}"
    build_step = next(step for step in jobs["build"]["steps"] if step["name"] == "Build single-file collector")
    assert build_step["shell"] == "bash"
    assert "release.py stamp" in build_step["run"]
    publish = jobs["publish"]["steps"][-1]["run"]
    assert "--target \"$SOURCE_SHA\" --draft" in publish
    assert "--draft=false --latest=false" in publish
    assert "--json isDraft,targetCommitish" in publish
    assert '"$draft_source" != "$SOURCE_SHA"' in publish
    assert '"$RELEASE_TAG^{commit}"' not in publish


@pytest.mark.parametrize(
    ("draft_flag", "draft_source", "accepted"),
    [("true", "source-sha", True), ("true", "other-sha", False), ("false", "source-sha", False)],
)
def test_draft_retry_checks_source_without_requiring_git_tag(
    tmp_path: Path, draft_flag: str, draft_source: str, accepted: bool,
):
    """实际运行发布 Shell，确认无标签草稿可重试且拒绝错误源码或正式版本。"""
    workflow = YAML(typ="safe").load(
        (PROJECT_ROOT / ".github/workflows/site-adapter-collector.yml").read_text(encoding="utf-8"),
    )
    commands = workflow["jobs"]["publish"]["steps"][-1]["run"]
    binaries = tmp_path / "bin"
    binaries.mkdir()
    gh_stub = binaries / "gh"
    gh_stub.write_text(
        '#!/bin/bash\n'
        'if [[ "$1 $2" == "release view" ]]; then\n'
        '  printf "%s\\t%s\\n" "$DRAFT_FLAG" "$DRAFT_SOURCE"\n'
        'else\n'
        '  printf "%s\\n" "$*" >> "$GH_LOG"\n'
        'fi\n', encoding="utf-8",
    )
    git_stub = binaries / "git"
    git_stub.write_text("#!/bin/bash\nexit 79\n", encoding="utf-8")
    for path in (gh_stub, git_stub):
        path.chmod(0o755)
    log = tmp_path / "gh.log"
    environment = {
        **os.environ,
        "PATH": f"{binaries}{os.pathsep}{os.environ['PATH']}",
        "DRAFT_FLAG": draft_flag,
        "DRAFT_SOURCE": draft_source,
        "GH_LOG": str(log),
        "SOURCE_SHA": "source-sha",
        "RELEASE_TAG": "site-adapter-collector-v1.0.2",
        "COLLECTOR_VERSION": "1.0.2",
        "GITHUB_REPOSITORY": "test/collector",
    }
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", commands],
        cwd=tmp_path, env=environment, capture_output=True, text=True, check=False,
    )
    if accepted:
        assert result.returncode == 0, result.stderr
        calls = log.read_text(encoding="utf-8")
        assert "release upload site-adapter-collector-v1.0.2" in calls
        assert "--draft=false --latest=false" in calls
    else:
        assert result.returncode != 0
        assert not log.exists()
