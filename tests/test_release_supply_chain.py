"""正式镜像发布的供应链门禁合同。"""

from datetime import date
from pathlib import Path

import pytest
from ruamel.yaml import YAML

from scripts.normalize_audit_requirements import normalize_requirements


ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "docker" / "Dockerfile"
RELEASE_WORKFLOW = ROOT / ".github" / "workflows" / "build-v3.yml"
BETA_WORKFLOW = ROOT / ".github" / "workflows" / "beta.yml"
TRIVY_IGNORE = ROOT / ".trivyignore.yaml"


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


def test_base_image_uses_refreshable_tag_and_apt_does_not_upgrade_in_place() -> None:
    """基础镜像允许获得上游更新，构建阶段不得无边界升级整套 Debian。"""
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
    assert "\n    util-linux \\\n" in dockerfile


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
        assert "pip-audit==2.10.1" in audit
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
        assert scan["uses"] == (
            "aquasecurity/trivy-action@"
            "a9c7b0f06e461e9d4b4d1711f154ee024b8d7ab8"
        )
        assert scan["with"].items() >= {
            "version": "v0.70.0",
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


def test_release_promotes_latest_only_after_both_versioned_images() -> None:
    """只有两个版本制品都发布成功后才可移动 latest 标签。"""
    workflow = _load_workflow()
    steps = workflow["jobs"]["Docker-build"]["steps"]
    names = [step.get("name") for step in steps]
    indexed = _steps_by_name(workflow)

    assert "value=latest" not in indexed["Docker Meta"]["with"]["tags"]
    assert "value=latest" not in indexed["Docker Meta free-threaded"]["with"]["tags"]
    assert names.index("Publish multi-architecture image") < names.index("Promote latest image pair")
    assert names.index("Publish free-threaded multi-architecture image") < names.index(
        "Promote latest image pair"
    )
    promote = indexed["Promote latest image pair"]["run"]
    assert "moviepilot-v3:latest" not in promote
    assert 'ghcr_repository="${GITHUB_REPOSITORY,,}"' in promote
    assert "ghcr.io/${GITHUB_REPOSITORY}" not in promote
    assert "ghcr.io/${ghcr_repository}-v3" in promote
    assert "ghcr.io/${ghcr_repository}-v3t" in promote
    assert '"${image}:latest"' in promote
    assert '"${image}:${app_version}"' in promote


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
