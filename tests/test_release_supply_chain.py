"""正式镜像发布的供应链门禁合同。"""

from pathlib import Path

from ruamel.yaml import YAML


ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "docker" / "Dockerfile"
RELEASE_WORKFLOW = ROOT / ".github" / "workflows" / "build-v3.yml"


def _load_workflow() -> dict:
    """以 YAML 1.2 解析正式发布工作流。"""
    yaml = YAML(typ="safe")
    return yaml.load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))


def _steps_by_name(workflow: dict) -> dict[str, dict]:
    """按名称索引发布步骤，顺序仍由原列表校验。"""
    return {
        step["name"]: step
        for step in workflow["jobs"]["Docker-build"]["steps"]
        if "name" in step
    }


def test_base_image_refresh_is_explicit_and_apt_does_not_upgrade_in_place() -> None:
    """基础镜像刷新必须显式，构建阶段不得无边界升级整套 Debian。"""
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")

    assert "FROM python:3.14.7-slim-trixie AS base" in dockerfile
    assert "python:3.14.7-slim-trixie@sha256:" not in dockerfile
    assert "apt-get upgrade" not in dockerfile
    assert "\n    util-linux \\\n" in dockerfile


def test_release_audits_locked_runtime_dependencies_before_building() -> None:
    """发布构建前必须审计带哈希的锁定运行时依赖。"""
    workflow = _load_workflow()
    steps = workflow["jobs"]["Docker-build"]["steps"]
    names = [step.get("name") for step in steps]
    audit = _steps_by_name(workflow)["Audit locked Python dependencies"]["run"]

    assert names.index("Audit locked Python dependencies") < names.index("Build amd64 candidate")
    assert "uv export --quiet --locked --no-dev --no-emit-project" in audit
    assert "pip-audit==2.10.1" in audit
    for option in ("--require-hashes", "--disable-pip", "--strict"):
        assert option in audit


def test_release_scans_both_architectures_before_registry_login_and_publish() -> None:
    """任一架构的最终 OS 包扫描失败时都不得登录仓库或发布镜像。"""
    workflow = _load_workflow()
    steps = workflow["jobs"]["Docker-build"]["steps"]
    names = [step.get("name") for step in steps]
    indexed = _steps_by_name(workflow)

    expected_candidates = {
        "Build amd64 candidate": ("linux/amd64", "moviepilot-v3-candidate:linux-amd64"),
        "Build arm64 candidate": ("linux/arm64/v8", "moviepilot-v3-candidate:linux-arm64"),
    }
    for name, (platform, tag) in expected_candidates.items():
        build = indexed[name]["with"]
        assert build["platforms"] == platform
        assert build["load"] is True
        assert build["push"] is False
        assert build["tags"] == tag
        assert "inputs.refresh_base_image" in build["pull"]
        assert "inputs.refresh_base_image" in build["no-cache-filters"]
        assert "prepare_package,final" in build["no-cache-filters"]

    for name in ("Scan amd64 candidate OS packages", "Scan arm64 candidate OS packages"):
        scan = indexed[name]
        assert scan["uses"] == (
            "aquasecurity/trivy-action@"
            "a9c7b0f06e461e9d4b4d1711f154ee024b8d7ab8"
        )
        assert scan["with"].items() >= {
            "version": "v0.70.0",
            "scanners": "vuln",
            "vuln-type": "os",
            "severity": "HIGH,CRITICAL",
            "ignore-unfixed": True,
            "exit-code": 1,
        }.items()

    last_scan = names.index("Scan arm64 candidate OS packages")
    assert last_scan < names.index("Login DockerHub")
    assert last_scan < names.index("Login GitHub Container Registry")
    assert last_scan < names.index("Publish multi-architecture image")


def test_publish_reuses_scanned_architecture_caches_without_refreshing_base() -> None:
    """发布构建复用已扫描候选缓存，不得在扫描后重新拉取未审计基础镜像。"""
    workflow = _load_workflow()
    dispatch = workflow["on"]["workflow_dispatch"]["inputs"]["refresh_base_image"]
    publish = _steps_by_name(workflow)["Publish multi-architecture image"]["with"]

    assert dispatch == {
        "description": "强制刷新基础镜像和系统包层",
        "required": False,
        "default": False,
        "type": "boolean",
    }
    assert publish["platforms"] == "linux/amd64\nlinux/arm64/v8\n"
    assert publish["push"] is True
    assert publish["pull"] is False
    assert "scope=moviepilot-v3-docker-amd64" in publish["cache-from"]
    assert "scope=moviepilot-v3-docker-arm64" in publish["cache-from"]
