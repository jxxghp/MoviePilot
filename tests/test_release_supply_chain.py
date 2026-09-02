"""正式镜像发布的供应链门禁合同。"""

import os
import shutil
import subprocess
from datetime import date
from pathlib import Path

import pytest
from ruamel.yaml import YAML

from scripts.normalize_audit_requirements import normalize_requirements

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "docker" / "Dockerfile"
RELEASE_WORKFLOW = ROOT / ".github" / "workflows" / "build-v3.yml"
BETA_WORKFLOW = ROOT / ".github" / "workflows" / "beta.yml"
PR_AGENT_WORKFLOW = ROOT / ".github" / "workflows" / "pr-agent.yml"
TRIVY_IGNORE = ROOT / ".trivyignore.yaml"
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
    "aquasecurity/trivy-action@v0.36.0",
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
            "CHANGELOG": "generated changelog",
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
    """基础镜像允许更新，并仅显式刷新运行时安全包而非整套 Debian。"""
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")

    assert 'ARG MOVIEPILOT_PYTHON_VERSION="3.14.7"' in dockerfile
    assert "FROM python:${MOVIEPILOT_PYTHON_VERSION}-slim-trixie AS base" in dockerfile
    assert "python:${MOVIEPILOT_PYTHON_VERSION}-slim-trixie@sha256:" not in dockerfile
    free_threaded_stage = dockerfile.split(
        "FROM prepare_venv_common AS prepare_venv_free-threaded",
        maxsplit=1,
    )[1]
    assert "ARG MOVIEPILOT_PYTHON_VERSION" in free_threaded_stage
    assert 'uv python install --no-bin "${MOVIEPILOT_PYTHON_VERSION}t"' in free_threaded_stage
    assert "apt-get upgrade" not in dockerfile
    assert "\n    openssl \\\n" in dockerfile
    assert "\n    util-linux \\\n" in dockerfile


def test_rclone_image_uses_cve_2026_46603_patched_build() -> None:
    """rclone 制品必须固定到包含 x/image 漏洞修复的不可变镜像。"""
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")

    assert "rclone/rclone:1.75.0" not in dockerfile
    patched_rclone_image = "rclone/rclone:beta@sha256:d6f5448594ecefefcf09cfeaf85cb7a21a866328032576ce2c1813e7b59c66dc"
    assert f"FROM {patched_rclone_image} AS rclone" in dockerfile


def test_release_audits_locked_runtime_dependencies_before_building() -> None:
    """正式版和 Beta 构建前必须分别审计两套锁定运行依赖。"""
    for workflow_path in (RELEASE_WORKFLOW, BETA_WORKFLOW):
        workflow = _load_workflow(workflow_path)
        steps = workflow["jobs"]["Docker-build"]["steps"]
        names = [step.get("name") for step in steps]
        audit = _steps_by_name(workflow)["Audit locked Python dependencies"]["run"]

        first_candidate = next(name for name in names if name and name.startswith("Build "))
        assert names.index("Audit locked Python dependencies") < names.index(first_candidate)
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


def test_release_scans_both_architectures_before_registry_login_and_publish() -> None:
    """两个 Python 变体的各架构扫描都必须在登录仓库和发布前完成。"""
    workflow = _load_workflow()
    trivy_env = workflow["jobs"]["Docker-build"]["env"]
    assert trivy_env["TRIVY_SKIP_DIRS"] == "/usr/share/java"
    assert trivy_env["TRIVY_SKIP_JAVA_DB_UPDATE"] == "true"
    steps = workflow["jobs"]["Docker-build"]["steps"]
    names = [step.get("name") for step in steps]
    indexed = _steps_by_name(workflow)

    expected_candidates = {
        "Build amd64 candidate": ("linux/amd64", "moviepilot-v3-candidate:linux-amd64"),
        "Build arm64 candidate": ("linux/arm64/v8", "moviepilot-v3-candidate:linux-arm64"),
        "Build free-threaded amd64 candidate": (
            "linux/amd64",
            "moviepilot-v3t-candidate:linux-amd64",
        ),
        "Build free-threaded arm64 candidate": (
            "linux/arm64/v8",
            "moviepilot-v3t-candidate:linux-arm64",
        ),
    }
    for name, (platform, tag) in expected_candidates.items():
        build = indexed[name]["with"]
        assert build["platforms"] == platform
        assert build["load"] is True
        assert build["push"] is False
        assert build["tags"] == tag
        assert build["pull"] is True
        assert "no-cache-filters" not in build
        expected_variant = "free-threaded" if "free-threaded" in name else "standard"
        assert f"MOVIEPILOT_PYTHON_VARIANT={expected_variant}" in build["build-args"]

    for name in (
        "Scan amd64 candidate vulnerabilities",
        "Scan arm64 candidate vulnerabilities",
        "Scan free-threaded amd64 candidate vulnerabilities",
        "Scan free-threaded arm64 candidate vulnerabilities",
    ):
        scan = indexed[name]
        assert scan["with"]["cache-dir"] == "${{ runner.temp }}/trivy"
        assert scan["uses"] == "aquasecurity/trivy-action@v0.36.0"
        assert scan["with"].items() >= {
            "version": "latest",
            "scanners": "vuln",
            "vuln-type": "os,library",
            "severity": "HIGH,CRITICAL",
            "ignore-unfixed": True,
            "trivyignores": ".trivyignore.yaml",
            "exit-code": 1,
        }.items()

    last_scan = max(
        names.index(name)
        for name in (
            "Scan amd64 candidate vulnerabilities",
            "Scan arm64 candidate vulnerabilities",
            "Scan free-threaded amd64 candidate vulnerabilities",
            "Scan free-threaded arm64 candidate vulnerabilities",
        )
    )
    assert last_scan < names.index("Login DockerHub")
    assert last_scan < names.index("Login GitHub Container Registry")
    assert last_scan < names.index("Publish multi-architecture image")
    assert last_scan < names.index("Publish free-threaded multi-architecture image")


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


def test_pr_agent_keeps_pull_request_target_api_only_boundary() -> None:
    """带凭据的 PR 审查只读 GitHub API，不 checkout 或执行 PR 分支代码。"""
    workflow = _load_workflow(PR_AGENT_WORKFLOW)
    assert "pull_request_target" in workflow["on"]
    assert workflow["permissions"] == {
        "contents": "read",
        "pull-requests": "write",
        "issues": "write",
    }
    steps = workflow["jobs"]["pr-agent"]["steps"]
    assert len(steps) == 1
    review_step = steps[0]
    assert review_step["uses"] == "docker://ghcr.io/infinitypacer/pr-review-runner:latest"
    assert "run" not in review_step


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
        ("HTTP/2.0 404 Not Found\n\n", 1, "false", "generated changelog"),
    ],
)
def test_release_query_preserves_existing_body_or_handles_explicit_404(
    tmp_path: Path,
    response: str,
    exit_code: int,
    expected_exists: str,
    expected_body: str,
) -> None:
    """已有 Release 保留正文，只有明确 404 才使用自动变更记录。"""
    script = _steps_by_name(_load_workflow())["Get existing release body"]["run"]
    script = script.replace("v${{ env.app_version }}", "v3.0.0")

    result = _run_release_script(script, tmp_path, response=response, exit_code=exit_code)

    assert result.returncode == 0, result.stderr
    output = (tmp_path / "github.output").read_text(encoding="utf-8")
    environment = (tmp_path / "github.env").read_text(encoding="utf-8")
    assert f"exists={expected_exists}" in output
    assert expected_body in environment


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


def test_vulnerability_ignores_are_scoped_justified_and_time_bounded() -> None:
    """漏洞豁免必须限定制品范围，并保留复查期限和接受理由。"""
    yaml = YAML(typ="safe")
    vulnerabilities = yaml.load(TRIVY_IGNORE.read_text(encoding="utf-8"))["vulnerabilities"]

    for vulnerability in vulnerabilities:
        assert vulnerability["paths"]
        assert vulnerability["purls"]
        assert vulnerability["statement"]
        assert isinstance(vulnerability["expired_at"], date)


def test_publish_reuses_scanned_architecture_caches_without_refreshing_base() -> None:
    """发布构建复用已扫描候选缓存，不得在扫描后重新拉取未审计基础镜像。"""
    workflow = _load_workflow()
    publish = _steps_by_name(workflow)["Publish multi-architecture image"]["with"]

    assert workflow["on"]["workflow_dispatch"] is None
    assert publish["platforms"] == "linux/amd64\nlinux/arm64/v8\n"
    assert publish["push"] is True
    assert publish["pull"] is False
    assert "scope=moviepilot-v3-standard-docker-amd64" in publish["cache-from"]
    assert "scope=moviepilot-v3-standard-docker-arm64" in publish["cache-from"]


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


def test_beta_applies_the_same_variant_scan_and_publish_contract() -> None:
    """Beta 也必须在发布两个变体前完成各架构漏洞扫描。"""
    workflow = _load_workflow(BETA_WORKFLOW)
    trivy_env = workflow["jobs"]["Docker-build"]["env"]
    assert trivy_env["TRIVY_SKIP_DIRS"] == "/usr/share/java"
    assert trivy_env["TRIVY_SKIP_JAVA_DB_UPDATE"] == "true"
    steps = workflow["jobs"]["Docker-build"]["steps"]
    names = [step.get("name") for step in steps]
    indexed = _steps_by_name(workflow)

    assert workflow["on"]["workflow_dispatch"] is None
    for name in (
        "Build standard amd64 candidate",
        "Build standard arm64 candidate",
        "Build free-threaded amd64 candidate",
        "Build free-threaded arm64 candidate",
    ):
        assert indexed[name]["with"]["load"] is True
        assert indexed[name]["with"]["push"] is False

    scan_names = (
        "Scan standard amd64 candidate vulnerabilities",
        "Scan standard arm64 candidate vulnerabilities",
        "Scan free-threaded amd64 candidate vulnerabilities",
        "Scan free-threaded arm64 candidate vulnerabilities",
    )
    publish_names = (
        "Publish standard multi-architecture image",
        "Publish free-threaded multi-architecture image",
    )
    last_scan = max(names.index(name) for name in scan_names)
    assert all(last_scan < names.index(name) for name in publish_names)
    assert "MOVIEPILOT_PYTHON_VARIANT=standard" in indexed[publish_names[0]]["with"]["build-args"]
    assert "MOVIEPILOT_PYTHON_VARIANT=free-threaded" in indexed[publish_names[1]]["with"]["build-args"]
    assert "scope=moviepilot-v3-standard-docker-amd64" in indexed[publish_names[0]]["with"]["cache-from"]
    assert "scope=moviepilot-v3t-docker-amd64" in indexed[publish_names[1]]["with"]["cache-from"]
    assert "value=beta" in indexed["Docker Meta"]["with"]["tags"]
    assert "value=beta" in indexed["Docker Meta free-threaded"]["with"]["tags"]
    assert "github.run_id" not in indexed["Docker Meta"]["with"]["tags"]
    assert "github.run_id" not in indexed["Docker Meta free-threaded"]["with"]["tags"]
    assert "Promote beta image pair" not in names
