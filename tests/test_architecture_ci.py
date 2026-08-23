"""后端架构治理工作流的静态契约测试。"""

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

    assert "pull_request" in workflow["on"]
    assert "push" in workflow["on"]
    assert "tests/test_architecture_dependencies.py" in commands
    assert "tests/test_architecture_contract_baseline.py" in commands
    assert "scripts/architecture/baseline.py --check-host" in commands
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
