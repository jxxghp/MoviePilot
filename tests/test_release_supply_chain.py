"""正式镜像发布的供应链门禁合同。"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from ruamel.yaml import YAML

from scripts.normalize_audit_requirements import normalize_requirements

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "docker" / "Dockerfile"
RELEASE_WORKFLOW = ROOT / ".github" / "workflows" / "build-v3.yml"
BETA_WORKFLOW = ROOT / ".github" / "workflows" / "beta.yml"
PR_AGENT_WORKFLOW = ROOT / ".github" / "workflows" / "pr-agent.yml"
CODEX_EVENT_WORKFLOW = ROOT / ".github" / "workflows" / "moviepilot-codex-events.yml"
WORKFLOW_ROOT = ROOT / ".github" / "workflows"

ALLOWED_ACTION_REFS = {
    "actions/checkout@v7",
    "actions/setup-python@v7",
    "actions/github-script@v9",
    "actions/stale@v11",
    "astral-sh/setup-uv@v10.0.1",
    "docker/metadata-action@v6",
    "docker/setup-qemu-action@v4",
    "docker/setup-buildx-action@v4",
    "docker/build-push-action@v7",
    "docker/login-action@v4",
    "actions/upload-artifact@v7",
    "actions/download-artifact@v8",
    "docker://ghcr.io/infinitypacer/pr-review-runner:latest",
}


def _load_workflow(path: Path = RELEASE_WORKFLOW) -> dict:
    """以 YAML 1.2 解析镜像发布工作流。"""
    yaml = YAML(typ="safe")
    return yaml.load(path.read_text(encoding="utf-8"))


def _steps_by_name(workflow: dict) -> dict[str, dict]:
    """按名称索引发布步骤，顺序仍由原列表校验。"""
    return {
        step["name"]: step
        for step in workflow["jobs"]["Docker-build"]["steps"]
        if "name" in step
    }


def _write_fake_gh(tmp_path: Path) -> Path:
    """创建可控制响应和退出状态的 gh 测试替身。"""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(
        """#!/usr/bin/env bash
printf '%s\n' "$*" >> "$GH_LOG"
cat "$GH_RESPONSE_FILE"
cat "$GH_ERROR_FILE" >&2
exit "$GH_EXIT_CODE"
""",
        encoding="utf-8",
    )
    gh.chmod(0o755)
    return bin_dir


def _run_release_script(
    script: str,
    tmp_path: Path,
    *,
    response: str = "",
    error: str = "",
    exit_code: int = 0,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """在隔离的 gh 替身环境中执行发布 workflow 脚本。"""
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("release workflow contract requires Bash")
    response_file = tmp_path / "response.txt"
    error_file = tmp_path / "error.txt"
    response_file.write_text(response, encoding="utf-8")
    error_file.write_text(error, encoding="utf-8")
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{_write_fake_gh(tmp_path)}:{env['PATH']}",
            "GH_RESPONSE_FILE": str(response_file),
            "GH_ERROR_FILE": str(error_file),
            "GH_EXIT_CODE": str(exit_code),
            "GH_LOG": str(tmp_path / "gh.log"),
            "GITHUB_REPOSITORY": "jxxghp/MoviePilot",
            "GITHUB_ENV": str(tmp_path / "github.env"),
            "GITHUB_OUTPUT": str(tmp_path / "github.output"),
        }
    )
    env.update(extra_env or {})
    return subprocess.run(
        [bash, "-euo", "pipefail", "-c", script],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_base_image_uses_refreshable_tag_and_apt_does_not_upgrade_in_place() -> None:
    """使用官方基础镜像及所需运行依赖，不追加系统包扫描补丁。"""
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    assert 'ARG MOVIEPILOT_PYTHON_VERSION="3.14.7"' in dockerfile
    assert "FROM python:${MOVIEPILOT_PYTHON_VERSION}-slim-trixie AS base" in dockerfile
    assert 'uv python install --no-bin "${MOVIEPILOT_PYTHON_VERSION}t"' in dockerfile
    assert "apt-get upgrade" not in dockerfile
    assert "pip uninstall" not in dockerfile
    assert "break-system-packages" not in dockerfile
    browser_install = dockerfile.split("RUN playwright install-deps chromium", maxsplit=1)[1]
    assert "apt-get install" not in browser_install


def test_rclone_image_uses_official_stable_channel() -> None:
    """rclone 使用官方稳定镜像，不固定用于应对扫描的临时 Beta。"""
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    assert "FROM rclone/rclone:latest AS rclone" in dockerfile


def test_release_audits_locked_runtime_dependencies_before_building() -> None:
    """正式版和 Beta 构建前必须分别审计两套锁定运行依赖。"""
    for workflow_path in (RELEASE_WORKFLOW, BETA_WORKFLOW):
        workflow = _load_workflow(workflow_path)
        steps = workflow["jobs"]["Docker-build"]["steps"]
        names = [step.get("name") for step in steps]
        audit = _steps_by_name(workflow)["Audit locked Python dependencies"]["run"]

        first_publish = next(name for name in names if name and name.startswith("Publish "))
        assert names.index("Audit locked Python dependencies") < names.index(first_publish)
        assert "--group runtime-standard" in audit
        assert "--group runtime-free-threaded" in audit
        assert "scripts/normalize_audit_requirements.py" in audit
        assert "uvx --from pip-audit pip-audit" in audit
        assert "pip-audit==" not in audit
        for option in ("--require-hashes", "--no-deps", "--disable-pip", "--strict"):
            assert option in audit


def test_direct_url_audit_requirement_uses_version_from_matching_lock_source(tmp_path: Path) -> None:
    """URL 依赖的漏洞审计版本必须来自同名且同来源的锁文件条目。"""
    lock_file = tmp_path / "uv.lock"
    lock_file.write_text(
        """
version = 1

[[package]]
name = "Brotli"
version = "1.2.0"
source = { url = "https://example.com/brotli.tar.gz" }
""",
        encoding="utf-8",
    )
    exported = (
        "brotli @ https://example.com/brotli.tar.gz ; python_version >= '3.14' \\\n"
        "    # via httpx\n"
    )

    normalized = normalize_requirements(exported, lock_file)

    assert "brotli==1.2.0 ; python_version >= '3.14' \\" in normalized
    assert "@ https://example.com/brotli.tar.gz" not in normalized


def test_direct_url_audit_requirement_rejects_unlocked_source(tmp_path: Path) -> None:
    """不能把未匹配锁文件来源的 URL 依赖伪装为已审计版本。"""
    lock_file = tmp_path / "uv.lock"
    lock_file.write_text("version = 1\npackage = []\n", encoding="utf-8")

    with pytest.raises(ValueError, match="无法在锁文件中定位精确版本"):
        normalize_requirements("demo @ https://example.com/demo.tar.gz\n", lock_file)


@pytest.mark.parametrize("path", (RELEASE_WORKFLOW, BETA_WORKFLOW))
def test_release_builds_publish_both_architectures_directly(path: Path) -> None:
    """正式版和 Beta 直接构建发布双架构产物，删除扫描专用候选流程。"""
    workflow = _load_workflow(path)
    steps = workflow["jobs"]["Docker-build"]["steps"]
    builds = [step for step in steps if step.get("uses") == "docker/build-push-action@v7"]
    assert len(builds) == 2
    for step in builds:
        config = step["with"]
        assert config["push"] is True
        assert config["pull"] is True
        assert set(config["platforms"].split()) == {"linux/amd64", "linux/arm64/v8"}
        assert "cache-to" in config
    assert all("trivy" not in step.get("uses", "") for step in steps)


def test_workflows_follow_maintained_action_channels() -> None:
    """官方工具使用批准的稳定引用，不引入未知来源或手工 commit SHA。"""
    for workflow_path in sorted(WORKFLOW_ROOT.glob("*.yml")):
        workflow = _load_workflow(workflow_path)
        for job_name, job in workflow.get("jobs", {}).items():
            for step in job.get("steps", []):
                uses = step.get("uses")
                if uses:
                    assert uses in ALLOWED_ACTION_REFS, (
                        f"{workflow_path}:{job_name}:{step.get('name', '<unnamed>')}: {uses}"
                    )
                    if uses == "astral-sh/setup-uv@v10.0.1":
                        assert "version" not in step.get("with", {})


def test_all_workflows_are_valid_yaml() -> None:
    """所有 GitHub Actions 工作流都必须能被 YAML 1.2 解析。"""
    for workflow_path in sorted(WORKFLOW_ROOT.glob("*.yml")):
        workflow = _load_workflow(workflow_path)
        assert isinstance(workflow, dict), workflow_path
        assert isinstance(workflow.get("jobs"), dict), workflow_path


def test_codex_event_intake_replaces_legacy_pr_agent_trigger() -> None:
    """旧 PR-Agent 仅保留手工入口，Issue/PR 自动分析统一交给 Codex 事件桥。"""
    legacy_workflow = _load_workflow(PR_AGENT_WORKFLOW)
    assert set(legacy_workflow["on"]) == {"workflow_dispatch"}

    workflow = _load_workflow(CODEX_EVENT_WORKFLOW)
    assert "issues" in workflow["on"]
    assert "pull_request_target" in workflow["on"]
    assert workflow["permissions"] == {"actions": "write", "contents": "read"}
    steps = workflow["jobs"]["dispatch"]["steps"]
    assert len(steps) == 1
    dispatch_step = steps[0]
    assert "gh workflow run" in dispatch_step["run"]
    assert "OPENAI_API_KEY" not in dispatch_step["run"]
    assert "TELEGRAM_BOT_TOKEN" not in dispatch_step["run"]


def test_release_uses_github_cli_for_tag_and_release_lifecycle() -> None:
    """正式发布复用 GitHub CLI，并只把明确不存在识别为新 Release。"""
    workflow = _load_workflow()
    indexed = _steps_by_name(workflow)
    serialized = RELEASE_WORKFLOW.read_text(encoding="utf-8")

    assert "dev-drprasad/delete-tag-and-release" not in serialized
    assert "softprops/action-gh-release" not in serialized
    release_query = indexed["Get existing release body"]["run"]
    assert "gh api --include" in release_query
    assert 'if [ "$status_code" = "404" ]' in release_query
    assert "cat \"$error_file\" >&2\n    exit 1" in release_query
    assert "gh release delete" not in serialized
    assert 'git tag -f "$tag_name" "$RELEASE_COMMIT"' in indexed["Publish Release Tag"]["run"]
    assert 'git push --force origin "refs/tags/${tag_name}"' in indexed["Publish Release Tag"]["run"]
    publish_release = indexed["Publish Release"]["run"]
    assert 'if [ "$RELEASE_EXISTS" = "true" ]' in publish_release
    assert "gh release edit" in publish_release
    assert "gh release create" in publish_release
    assert '--notes-file "$notes_file"' in publish_release
    assert "--draft=false" in publish_release
    assert "--prerelease=false" in publish_release
    assert "--latest" in publish_release
    names = [step.get("name") for step in workflow["jobs"]["Docker-build"]["steps"]]
    assert names.index("Get existing release body") < names.index("Publish Release Tag")
    assert names.index("Publish Release Tag") < names.index("Publish Release")


@pytest.mark.parametrize(
    ("response", "exit_code", "expected_exists", "expected_body"),
    [
        ("HTTP/2.0 200 OK\nHeader: value\n\nmanual body\n", 0, "true", "manual body"),
        ("HTTP/2.0 200 OK\n\nmanual\nEOF\nMORE=value\n", 0, "true", "manual\nEOF\nMORE=value"),
        ("HTTP/2.0 404 Not Found\n\n", 1, "false", ""),
    ],
)
def test_release_query_preserves_existing_body_or_handles_explicit_404(
    tmp_path: Path,
    response: str,
    exit_code: int,
    expected_exists: str,
    expected_body: str,
) -> None:
    """已有 Release 保留正文，只有明确 404 才将正文初始化为空。"""
    script = _steps_by_name(_load_workflow())["Get existing release body"]["run"]
    script = script.replace("v${{ env.app_version }}", "v3.0.0")

    result = _run_release_script(script, tmp_path, response=response, exit_code=exit_code)

    assert result.returncode == 0, result.stderr
    output = (tmp_path / "github.output").read_text(encoding="utf-8")
    environment = (tmp_path / "github.env").read_text(encoding="utf-8")
    assert f"exists={expected_exists}" in output
    assert expected_body in environment
    delimiter = environment.splitlines()[0].split("<<")[1]
    assert delimiter.startswith("RELEASE_NOTES_")
    assert environment.splitlines()[-1] == delimiter


def test_release_query_fails_closed_on_non_404_error(tmp_path: Path) -> None:
    """网络或服务端错误不得伪装成 Release 不存在。"""
    script = _steps_by_name(_load_workflow())["Get existing release body"]["run"]
    script = script.replace("v${{ env.app_version }}", "v3.0.0")

    result = _run_release_script(
        script,
        tmp_path,
        response="HTTP/2.0 500 Internal Server Error\n\n",
        error="GitHub API unavailable\n",
        exit_code=1,
    )

    assert result.returncode != 0
    assert "GitHub API unavailable" in result.stderr
    assert not (tmp_path / "github.env").exists()


@pytest.mark.parametrize(
    ("release_exists", "expected_command"),
    [("true", "release edit"), ("false", "release create")],
)
def test_release_publish_selects_edit_or_create(
    tmp_path: Path,
    release_exists: str,
    expected_command: str,
) -> None:
    """发布阶段按查询结果原位更新或创建 Release。"""
    script = _steps_by_name(_load_workflow())["Publish Release"]["run"]
    script = script.replace("v${{ env.app_version }}", "v3.0.0")

    result = _run_release_script(
        script,
        tmp_path,
        extra_env={"RELEASE_EXISTS": release_exists, "RELEASE_BODY": "release notes"},
    )

    assert result.returncode == 0, result.stderr
    log = (tmp_path / "gh.log").read_text(encoding="utf-8")
    assert expected_command in log
    if release_exists == "true":
        assert "--draft=false" in log
        assert "--prerelease=false" in log


def test_dependency_compat_checks_minimum_uv_version() -> None:
    """依赖兼容 job 必须断言 uv 满足最低版本，而不是只打印版本。"""
    workflow = _load_workflow(ROOT / ".github" / "workflows" / "dependency-compat.yml")
    steps = workflow["jobs"]["docker-dependencies"]["steps"]
    verify = next(step for step in steps if step.get("name") == "Verify minimum uv version")
    command = verify["run"]

    assert "['uv', '--version']" in command
    assert "Version(version) >= Version('0.12.5')" in command
    assert "assert" in command


def test_publish_refreshes_base_and_preserves_build_cache() -> None:
    """直接发布时拉取基础镜像，同时读写双架构构建缓存。"""
    workflow = _load_workflow()
    publish = _steps_by_name(workflow)["Publish multi-architecture image"]["with"]
    assert workflow["on"]["workflow_dispatch"] is None
    assert publish["push"] is True
    assert publish["pull"] is True
    assert "scope=moviepilot-v3-standard-docker," in publish["cache-to"]


def test_release_publishes_free_threaded_image_with_separate_metadata_and_cache() -> None:
    """free-threaded 发布必须使用 v3t 命名、参数和独立缓存。"""
    workflow = _load_workflow()
    indexed = _steps_by_name(workflow)

    metadata = indexed["Docker Meta free-threaded"]
    publish = indexed["Publish free-threaded multi-architecture image"]

    assert "moviepilot-v3t" in metadata["with"]["images"]
    assert "MOVIEPILOT_PYTHON_VARIANT=free-threaded" in publish["with"]["build-args"]
    assert "scope=moviepilot-v3t-docker-amd64" in publish["with"]["cache-from"]
    assert "scope=moviepilot-v3t-docker-arm64" in publish["with"]["cache-from"]


def test_release_publishes_version_and_latest_tags_for_both_image_variants() -> None:
    """正式版元数据同时发布版本号与 latest，两个变体直接复用各自发布结果。"""
    workflow = _load_workflow()
    steps = workflow["jobs"]["Docker-build"]["steps"]
    names = [step.get("name") for step in steps]
    indexed = _steps_by_name(workflow)

    for name in ("Docker Meta", "Docker Meta free-threaded"):
        tags = indexed[name]["with"]["tags"]
        assert "type=raw,value=${{ env.app_version }}" in tags
        assert "type=raw,value=latest" in tags

    assert "Promote latest image pair" not in names
    assert names.index("Docker Meta") < names.index("Publish multi-architecture image")
    assert names.index("Docker Meta free-threaded") < names.index(
        "Publish free-threaded multi-architecture image"
    )

    standard_images = indexed["Docker Meta"]["with"]["images"]
    assert "${{ secrets.DOCKER_USERNAME }}/moviepilot" in standard_images
    assert "${{ secrets.DOCKER_USERNAME }}/moviepilot-v3" in standard_images
    assert "ghcr.io/${{ github.repository }}" in standard_images


def test_beta_applies_the_same_variant_publish_contract() -> None:
    """Beta 保持两个变体的发布参数、标签和缓存隔离。"""
    workflow = _load_workflow(BETA_WORKFLOW)
    steps = workflow["jobs"]["Docker-build"]["steps"]
    names = [step.get("name") for step in steps]
    indexed = _steps_by_name(workflow)
    assert workflow["on"]["workflow_dispatch"] is None
    publish_names = (
        "Publish standard multi-architecture image",
        "Publish free-threaded multi-architecture image",
    )
    assert "MOVIEPILOT_PYTHON_VARIANT=standard" in indexed[publish_names[0]]["with"]["build-args"]
    assert "MOVIEPILOT_PYTHON_VARIANT=free-threaded" in indexed[publish_names[1]]["with"]["build-args"]
    assert "scope=moviepilot-v3-standard-docker-amd64" in indexed[publish_names[0]]["with"]["cache-from"]
    assert "scope=moviepilot-v3t-docker-amd64" in indexed[publish_names[1]]["with"]["cache-from"]
    assert "value=beta" in indexed["Docker Meta"]["with"]["tags"]
    assert "value=beta" in indexed["Docker Meta free-threaded"]["with"]["tags"]
    assert "github.run_id" not in indexed["Docker Meta"]["with"]["tags"]
    assert "github.run_id" not in indexed["Docker Meta free-threaded"]["with"]["tags"]
    assert "Promote beta image pair" not in names
