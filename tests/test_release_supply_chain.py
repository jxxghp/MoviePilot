"""正式镜像发布的供应链门禁合同。"""

from datetime import date
from pathlib import Path

from ruamel.yaml import YAML


ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "docker" / "Dockerfile"
RELEASE_WORKFLOW = ROOT / ".github" / "workflows" / "build-v3.yml"
TRIVY_IGNORE = ROOT / ".trivyignore.yaml"


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


def test_base_image_uses_refreshable_tag_and_apt_does_not_upgrade_in_place() -> None:
    """基础镜像允许获得上游更新，构建阶段不得无边界升级整套 Debian。"""
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
    """任一架构的最终漏洞扫描失败时都不得登录仓库或发布镜像。"""
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
        assert build["pull"] is True
        assert "no-cache-filters" not in build

    for name in (
        "Scan amd64 candidate vulnerabilities",
        "Scan arm64 candidate vulnerabilities",
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

    last_scan = names.index("Scan arm64 candidate vulnerabilities")
    assert last_scan < names.index("Login DockerHub")
    assert last_scan < names.index("Login GitHub Container Registry")
    assert last_scan < names.index("Publish multi-architecture image")


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
    assert "scope=moviepilot-v3-docker-amd64" in publish["cache-from"]
    assert "scope=moviepilot-v3-docker-arm64" in publish["cache-from"]


def test_release_binds_source_update_identity_to_image_payload() -> None:
    """源码更新清单与镜像必须使用同一发布快照和外部载荷身份。"""
    workflow = _load_workflow()
    steps = workflow["jobs"]["Docker-build"]["steps"]
    names = [step.get("name") for step in steps]
    indexed = _steps_by_name(workflow)
    generate = indexed["Generate Source Update Payload"]
    release = indexed["Generate Release"]

    assert names.index("Create Release Snapshot") < names.index("Generate Source Update Payload")
    assert names.index("Generate Source Update Payload") < names.index("Build amd64 candidate")
    assert generate["env"]["BACKEND_REVISION"] == (
        "${{ steps.release_snapshot.outputs.release_commit }}"
    )
    assert generate["env"]["RELEASE_GENERATION"] == (
        "${{ github.run_id }}.${{ github.run_attempt }}"
    )
    assert generate["env"]["FRONTEND_SHA256"] == (
        "${{ steps.payloads.outputs.frontend_sha256 }}"
    )
    assert generate["env"]["RESOURCES_REVISION"] == (
        "${{ steps.payloads.outputs.resources_revision }}"
    )
    assert "sha256=" in generate["run"]
    assert release["with"]["files"] == ".build/source-update-payload.json"
