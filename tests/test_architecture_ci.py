"""后端架构治理工作流的静态契约测试。"""

import configparser
from pathlib import Path

from ruamel.yaml import YAML

PROJECT_ROOT = Path(__file__).parents[1]
WORKFLOW_ROOT = PROJECT_ROOT / ".github" / "workflows"


def _load_workflow(name: str) -> dict:
    """以 YAML 1.2 解析 GitHub Actions 工作流。"""
    yaml = YAML(typ="safe")
    return yaml.load((WORKFLOW_ROOT / name).read_text(encoding="utf-8"))


def _step_commands(workflow: dict, job_name: str) -> str:
    """拼接指定 job 的命令，便于断言关键门禁没有被移除。"""
    return "\n".join(
        step.get("run", "")
        for step in workflow["jobs"][job_name]["steps"]
    )


def test_unit_test_workflow_has_independent_host_architecture_gate():
    """主仓 PR 与推送必须在全量分片外快速执行宿主架构门禁。"""
    workflow = _load_workflow("test.yml")
    commands = _step_commands(workflow, "architecture")
    steps = workflow["jobs"]["architecture"]["steps"]
    semantic_step = next(
        step for step in steps if step.get("name") == "Check event semantic policy"
    )
    snapshot_step = next(
        step for step in steps if step.get("name") == "Check host architecture snapshot"
    )

    assert "pull_request" in workflow["on"]
    assert "push" in workflow["on"]
    assert "tests/test_architecture_dependencies.py" in commands
    assert "tests/test_architecture_adapter_imports.py" in commands
    assert "tests/test_architecture_egress.py" in commands
    assert "tests/test_architecture_event_facts.py" in semantic_step["run"]
    assert "tests/test_architecture_event_policy.py" in semantic_step["run"]
    assert "tests/test_architecture_dependencies.py" in semantic_step["run"]
    assert "tests/test_architecture_adapter_imports.py" in semantic_step["run"]
    assert "tests/test_architecture_egress.py" in semantic_step["run"]
    assert "scripts/architecture/event_policy.py" in semantic_step["run"]
    assert "scripts/architecture/baseline.py" not in semantic_step["run"]
    assert not {
        "tests/test_architecture_dependencies.py",
        "tests/test_architecture_adapter_imports.py",
        "tests/test_architecture_egress.py",
        "tests/test_architecture_event_facts.py",
        "tests/test_architecture_event_policy.py",
    } & set(snapshot_step["run"].split())
    assert steps.index(semantic_step) < steps.index(snapshot_step)
    assert "tests/test_architecture_contract_baseline.py" in commands
    assert "scripts/architecture/baseline.py --check-host" in commands
    assert commands.count("scripts/architecture/baseline.py --check-host") == 1
    assert "scripts/architecture/ruff_ratchet.py" in commands
    assert "scripts/architecture/mypy_ratchet.py" in commands
    assert "scripts/architecture/ruff_ratchet.py --write" not in commands
    assert "scripts/architecture/mypy_ratchet.py --write" not in commands
    assert "scripts/architecture/service_locator.py" in commands
    assert "scripts/startup/performance.py --check --repeat 3" in commands


def test_official_plugin_observation_is_scheduled_and_never_writes_fixture():
    """跨仓观察应定时或手工运行，只上传语义报告而不刷新 fixture。"""
    workflow = _load_workflow("architecture-observe.yml")
    commands = _step_commands(workflow, "observe")
    steps = workflow["jobs"]["observe"]["steps"]

    assert "schedule" in workflow["on"]
    assert "workflow_dispatch" in workflow["on"]
    assert "--check-plugins" in commands
    assert "--report official-plugin-architecture-report.json" in commands
    assert "--write" not in commands
    assert any(
        step.get("uses", "").startswith("actions/upload-artifact@")
        for step in steps
    )


def test_coverage_job_runs_full_suite_and_read_only_ratchet() -> None:
    """PR 与推送的 Coverage job 必须串行采集全量报告并只读检查低水位。"""
    workflow = _load_workflow("test.yml")
    coverage_job = workflow["jobs"]["coverage"]
    steps = coverage_job["steps"]
    assert workflow["on"]["pull_request"]["branches"] == ["v3"]
    assert workflow["on"]["push"]["branches"] == ["v3"]
    assert "workflow_dispatch" in workflow["on"]
    assert coverage_job["runs-on"] == "ubuntu-latest"
    assert coverage_job["timeout-minutes"] == 20
    assert "if" not in coverage_job

    setup_step = next(step for step in steps if step.get("name") == "Set up uv")
    install_step = next(
        step for step in steps if step.get("name") == "Install dependencies"
    )
    generate_step = next(
        step for step in steps if step.get("name") == "Generate coverage reports"
    )
    assert generate_step["timeout-minutes"] == 15
    upload_step = next(
        step for step in steps if step.get("name") == "Upload coverage report"
    )
    ratchet_step = next(
        step for step in steps if step.get("name") == "Check coverage ratchet"
    )
    assert setup_step["with"]["python-version"] == "3.14"
    assert install_step["run"] == "uv sync --locked"
    commands = generate_step["run"]
    expected_commands = [
        "python -m coverage erase",
        "python -m coverage run tests/run.py --serial",
        "python -m coverage report",
        "python -m coverage json",
        "python -m coverage xml",
    ]
    positions = [commands.index(command) for command in expected_commands]
    assert positions == sorted(positions)
    assert ratchet_step["run"].endswith("scripts/architecture/coverage_ratchet.py")
    assert steps.index(generate_step) < steps.index(upload_step) < steps.index(ratchet_step)

    all_commands = _step_commands(workflow, "coverage")
    assert "--write" not in all_commands
    assert "|| true" not in all_commands
    assert all(step.get("continue-on-error") is not True for step in steps)
    assert all("always()" not in str(step.get("if", "")) for step in steps)

    coverage_config = configparser.ConfigParser()
    coverage_config.read(PROJECT_ROOT / ".coveragerc", encoding="utf-8")
    assert coverage_config["run"]["source"].strip() == "app"
    assert "app/plugins/*/*" in coverage_config["run"]["omit"].splitlines()


def test_pylint_workflow_runs_for_v3_pull_requests_and_pushes():
    """改动文件应硬门禁，而全仓存量问题只能生成建议性报告。"""
    workflow = _load_workflow("pylint.yml")
    commands = _step_commands(workflow, "pylint")

    assert workflow["on"]["pull_request"]["branches"] == ["v3"]
    assert workflow["on"]["push"]["branches"] == ["v3"]
    assert "changed-python-files.txt" in commands
    assert "xargs uv run --locked --no-sync pylint" in commands
    assert "pylint app/" in commands
    assert "--output-format=json > pylint-report.json || true" in commands

    full_report_step = next(
        step
        for step in workflow["jobs"]["pylint"]["steps"]
        if step.get("name") == "Generate full advisory report"
    )
    assert "|| true" in full_report_step["run"]


def test_upload_artifact_actions_share_node24_major():
    """所有工件上传入口必须共用 Node 24 的 v7 主版本，避免 CI 告警与版本分叉。"""
    actions_by_workflow: dict[str, list[str]] = {}
    for workflow_path in WORKFLOW_ROOT.glob("*.yml"):
        workflow = _load_workflow(workflow_path.name)
        upload_actions = [
            step["uses"]
            for job in workflow.get("jobs", {}).values()
            for step in job.get("steps", [])
            if step.get("uses", "").startswith("actions/upload-artifact@")
        ]
        if upload_actions:
            actions_by_workflow[workflow_path.name] = upload_actions

    assert actions_by_workflow == {
        "architecture-observe.yml": ["actions/upload-artifact@v7"],
        "native-dependency-update.yml": ["actions/upload-artifact@v7"],
        "pylint.yml": ["actions/upload-artifact@v7"],
        "site-adapter-collector.yml": ["actions/upload-artifact@v7"],
        "test.yml": ["actions/upload-artifact@v7"],
    }


def test_native_dependency_update_probe_has_narrow_automatic_triggers():
    """昂贵的三平台探针只跟随生产安装边界变化，并保留手工验收入口。"""
    workflow = _load_workflow("native-dependency-update.yml")
    expected_paths = [
        "app/adapters/external/market.py",
        "app/adapters/system/host.py",
        "app/adapters/system/package.py",
        "app/runtime/dependencies.py",
        "scripts/probe_native_dependency_update.py",
        ".github/workflows/native-dependency-update.yml",
    ]

    assert workflow["on"]["pull_request"]["paths"] == expected_paths
    assert workflow["on"]["push"]["paths"] == expected_paths
    assert "workflow_dispatch" in workflow["on"]
    assert set(workflow["jobs"]) == {"probe"}
