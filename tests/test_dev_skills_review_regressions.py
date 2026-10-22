"""
本地开发、审查工具和技能脚本的隔离回归。
"""

import ast
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine

from scripts.architecture import baseline, coverage_ratchet
from scripts.evaluation.score import evaluate
from scripts.evaluation.world import EvaluationWorld
from scripts.perf import moviepilot_docker_ab as perf

ROOT = Path(__file__).resolve().parents[1]


def _load_script(relative, monkeypatch):
    """
    加载当前工作树脚本并在结束时恢复导入登记，不执行命令行主函数。
    """
    path = ROOT / relative
    spec = importlib.util.spec_from_file_location("review_" + path.stem.replace("-", "_"), path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


def test_coverage_write_snapshot_is_internally_consistent(tmp_path, monkeypatch) -> None:
    """
    相同比例但语句数变化的合法更新，逐文件与汇总必须使用同一份当前计数。
    """
    path = tmp_path / "baseline.json"
    report = tmp_path / "coverage.json"
    path.write_text(json.dumps(coverage_ratchet._snapshot_from_files({"app/demo.py": coverage_ratchet._metric(10, 8)})))
    report.write_text(json.dumps({"files": {"app/demo.py": {"summary": {"num_statements": 20, "covered_lines": 16}}}}))
    monkeypatch.setattr(sys, "argv", ["coverage", "--write", "--report", str(report), "--baseline", str(path)])
    assert coverage_ratchet.main() == 0
    written = json.loads(path.read_text())
    assert coverage_ratchet.validate_coverage(written) == []
    assert written["files"]["app/demo.py"]["statements"] == 20


@pytest.mark.parametrize("guard", ["TYPE_CHECKING", "typing.TYPE_CHECKING"])
def test_type_checking_else_imports_are_runtime_dependencies(tmp_path, guard) -> None:
    """
    仅类型分支排除，else 内实际运行的依赖仍纳入架构检查。
    """
    file = tmp_path / "branch.py"
    file.write_text(f"if {guard}:\n    from app.adapters.system.host import SystemUtils\nelse:\n    from app.adapters.network.http import RequestUtils\n")
    imports = list(baseline.iter_runtime_import_nodes(ast.parse(file.read_text())))
    assert [node.module for node in imports] == ["app.adapters.network.http"]


@pytest.mark.parametrize("pages", [(6, 5, 4, 3, 2, 1), (1, 2, 3, 4, 5, 6, 1)])
def test_long_context_score_rejects_reordered_or_repeated_pages(pages) -> None:
    """
    分页证据不能丢失读取顺序和重复次数。
    """
    world = EvaluationWorld("long_context")
    for page in pages:
        assert world.execute("subscription.list", query={"page": page, "count": 20})["success"]
    report = {"status": "completed", "completed": ["subscription"], "unresolved": [],
              "subscription_ids": [9001], "download_ids": [], "enabled_site_ids": []}
    assert evaluate(world, report).to_dict()["passed"] is False


def test_perf_container_reads_config_labels() -> None:
    """
    同 campaign 容器的 Docker inspect 标签位于 Config 下，可受控复用。
    """
    container = SimpleNamespace(name="isolated-review", attrs={"Config": {"Labels": {perf.CAMPAIGN_LABEL: "review"}}})
    perf.assert_owned(container, Namespace(campaign="review"), "container")


@pytest.mark.parametrize("command,flag", [("search", "--domain"), ("get_sub_domains", "--domain"),
                                         ("extract", "--url"), ("batch_search", "--query")])
def test_anysearch_missing_flag_value_exits_without_loop(command, flag) -> None:
    """
    缺少参数值应立即错误退出，不启动请求或持续占用 CPU。
    """
    result = subprocess.run(["bash", str(ROOT / "skills/anysearch/scripts/anysearch_cli.sh"), command, flag],
                            capture_output=True, text=True, timeout=2)
    assert result.returncode != 0
    assert result.stderr.strip()


def test_anysearch_help_resolves_invocation_template() -> None:
    """
    离线帮助包含可直接执行的 Bash 命令。
    """
    result = subprocess.run(["bash", str(ROOT / "skills/anysearch/scripts/anysearch_cli.sh"), "--help"],
                            capture_output=True, text=True, timeout=2)
    assert result.returncode == 0
    assert "{{LANG_INVOKE}}" not in result.stdout
    assert "bash scripts/anysearch_cli.sh" in result.stdout


def test_anysearch_propagates_mcp_tool_error(tmp_path) -> None:
    """
    HTTP 成功但 MCP isError 时，CLI 以失败退出并说明工具错误。
    """
    (tmp_path / "requests.py").write_text(
        "from types import SimpleNamespace\n"
        "def post(*args, **kwargs):\n"
        "    return SimpleNamespace(raise_for_status=lambda: None, json=lambda: "
        "{'result': {'isError': True, 'content': [{'type': 'text', 'text': 'fixture tool failure'}]}})\n"
    )
    result = subprocess.run([sys.executable, str(ROOT / "skills/anysearch/scripts/anysearch_cli.py"),
                             "search", "review"], env={**os.environ, "PYTHONPATH": str(tmp_path)},
                            capture_output=True, text=True, timeout=5)
    assert result.returncode != 0
    assert "fixture tool failure" in result.stderr


def test_anysearch_bash_propagates_mcp_tool_error(tmp_path):
    """
    Bash 入口也把HTTP成功的工具失败作为错误退出，curl边界固定为本地响应。
    """
    curl = tmp_path / "curl"
    curl.write_text("#!/bin/bash\nprintf '%s\\n' '{\"result\":{\"isError\":true,\"content\":[{\"type\":\"text\",\"text\":\"fixture tool failure\"}]}}'\n")
    curl.chmod(0o755)
    result = subprocess.run(["bash", str(ROOT / "skills/anysearch/scripts/anysearch_cli.sh"), "search", "review"],
                            env={**os.environ, "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"]},
                            capture_output=True, text=True, timeout=5)
    assert result.returncode != 0
    assert "fixture tool failure" in result.stderr


def test_anysearch_node_propagates_mcp_tool_error():
    """
    Node CLI只替换HTTPS进程边界，工具错误不能返回成功状态。
    """
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node runtime is unavailable")
    path = str(ROOT / "skills/anysearch/scripts/anysearch_cli.js")
    source = '''
const https = require('https');
const { EventEmitter } = require('events');
https.request = (_options, callback) => {
  const request = new EventEmitter();
  request.setTimeout = () => {};
  request.write = () => {};
  request.end = () => {
    const response = new EventEmitter();
    response.statusCode = 200;
    callback(response);
    response.emit('data', JSON.stringify({result: {isError: true, content: [{type: 'text', text: 'fixture tool failure'}]}}));
    response.emit('end');
  };
  return request;
};
''' + "process.argv = ['node', " + json.dumps(path) + ", 'search', 'review'];\nrequire(" + json.dumps(path) + ");\n"
    result = subprocess.run([node, "-e", source], capture_output=True, text=True, timeout=5)
    assert result.returncode != 0
    assert "fixture tool failure" in result.stderr


@pytest.mark.parametrize("sql", ["SELECT 'update' AS action", "SELECT 'chapter; title' AS title",
                                 "SELECT 1 /* update; ignored */", "SELECT 'it''s delete' AS text"])
def test_readonly_sql_ignores_literals_and_comments(monkeypatch, sql) -> None:
    """
    字符串和注释内的写关键词或分号不能提高普通查询所需权限。
    """
    module = _load_script("skills/database-operation/scripts/mp-db.py", monkeypatch)
    assert module._is_supported_statement(sql, False)


@pytest.mark.parametrize("sql", ["DELETE FROM sample", "WITH removed AS (DELETE FROM sample RETURNING *) SELECT * FROM removed",
                                 "SELECT 1; DELETE FROM sample", "SELECT 'unclosed", "SELECT 1 /* unclosed",
                                 "SELECT 1; SELECT 2", "SELECT 1;;", "/* read */ UPDATE sample SET value=1"])
def test_readonly_sql_still_rejects_writes_or_invalid_statements(monkeypatch, sql):
    """
    忽略字面值后仍拒绝真实写入、多语句及未闭合词法结构。
    """
    module = _load_script("skills/database-operation/scripts/mp-db.py", monkeypatch)
    assert not module._is_supported_statement(sql, False)


def test_readonly_sql_executes_one_real_query_and_rejects_mutation(tmp_path, monkeypatch, capsys):
    """
    实际 SQLite 查询保留字面值并正确终止注释，拒绝写入后数据仍完整。
    """
    module = _load_script("skills/database-operation/scripts/mp-db.py", monkeypatch)
    engine = create_engine("sqlite:///" + str(tmp_path / "review.db"))
    monkeypatch.setattr(module, "_build_engine", lambda: engine)
    try:
        assert module.run_query("SELECT 'chapter; update' AS title; -- fixture comment") == 0
        assert json.loads(capsys.readouterr().out)["rows"] == [{"title": "chapter; update"}]
        assert module.run_query("CREATE TABLE sample (id INTEGER)") == 1
        assert "require --write" in capsys.readouterr().err
        assert module.run_query("SELECT 1; CREATE TABLE sample (id INTEGER)", allow_write=True) == 1
    finally:
        engine.dispose()


def test_readonly_query_literal_limit_does_not_bypass_row_bound(tmp_path, monkeypatch, capsys):
    """
    字面值与注释内的LIMIT不影响实际查询默认行数限制。
    """
    module = _load_script("skills/database-operation/scripts/mp-db.py", monkeypatch)
    engine = create_engine("sqlite:///" + str(tmp_path / "bounded.db"))
    monkeypatch.setattr(module, "_build_engine", lambda: engine)
    try:
        assert module.run_query("/* review */ SELECT 'limit 999' AS value UNION ALL SELECT 'second'", limit=1) == 0
        assert json.loads(capsys.readouterr().out)["row_count"] == 1
    finally:
        engine.dispose()


@pytest.mark.parametrize("force", [False, True])
def test_plugin_pull_protects_excluded_local_file(tmp_path, monkeypatch, force) -> None:
    """
    用户排除的本地路径不受远端同路径写入覆盖，即便强制拉取也保留。
    """
    module = _load_script("skills/publish-moviepilot-plugin/scripts/publish_plugin.py", monkeypatch)
    directory = tmp_path / "plugins.v2/myplugin"
    directory.mkdir(parents=True)
    (directory / "__init__.py").write_text("# review\n")
    local = directory / ".env"
    local.write_text("local-fixture\n")
    prefix = "plugins.v2/myplugin"
    remote = {f"{prefix}/__init__.py": module.FileState(f"{prefix}/__init__.py", b"# review\n"),
              f"{prefix}/.env": module.FileState(f"{prefix}/.env", b"remote-fixture\n")}
    context = {"repo": "fixture/review", "branch": "v3", "plugin_id": "MyPlugin", "local_repo": tmp_path,
               "layout": module.Layout("package.v2.json", "plugins.v2"), "remote_prefix": prefix,
               "client": SimpleNamespace(list_files=lambda _prefix: remote, get_file=lambda _path: None),
               "excludes": list(module.DEFAULT_EXCLUDES), "includes": []}
    monkeypatch.setattr(module, "build_context", lambda _args: context)
    result = module.pull(Namespace(force=force, dry_run=False))
    assert local.read_text() == "local-fixture\n"
    assert f"{prefix}/.env" in result["changes"]["rejected"]
    assert f"{prefix}/.env" not in result["changes"]["create"]


def test_plugin_pull_preserves_unpublished_package_entry(tmp_path, monkeypatch) -> None:
    """
    未强制拉取不能覆盖待发布的插件 package 元数据。
    """
    module = _load_script("skills/publish-moviepilot-plugin/scripts/publish_plugin.py", monkeypatch)
    directory = tmp_path / "plugins.v2/myplugin"
    directory.mkdir(parents=True)
    (directory / "__init__.py").write_text("# review\n")
    package = tmp_path / "package.v2.json"
    original = {"MyPlugin": {"version": "2.0.0"}, "Other": {"version": "3.0.0"}}
    package.write_text(json.dumps(original))
    prefix = "plugins.v2/myplugin"
    remote = {f"{prefix}/__init__.py": module.FileState(f"{prefix}/__init__.py", b"# review\n")}
    context = {"repo": "fixture/review", "branch": "v3", "plugin_id": "MyPlugin", "local_repo": tmp_path,
               "layout": module.Layout("package.v2.json", "plugins.v2"), "remote_prefix": prefix,
               "client": SimpleNamespace(list_files=lambda _prefix: remote, get_file=lambda path: module.FileState(
                   path, json.dumps({"MyPlugin": {"version": "1.0.0"}}).encode())),
               "excludes": list(module.DEFAULT_EXCLUDES), "includes": []}
    monkeypatch.setattr(module, "build_context", lambda _args: context)
    result = module.pull(Namespace(force=False, dry_run=False))
    assert result["changes"]["conflicts"]
    assert json.loads(package.read_text()) == original


def test_plugin_discovery_resolves_each_candidate_layout(tmp_path, monkeypatch) -> None:
    """
    auto 布局按候选仓实际文件判断，工作目录不能决定全部仓库布局。
    """
    module = _load_script("skills/publish-moviepilot-plugin/scripts/publish_plugin.py", monkeypatch)
    legacy = tmp_path / "legacy"
    (legacy / "plugins/myplugin").mkdir(parents=True)
    (legacy / "package.json").write_text('{"MyPlugin": {"version": "1.0.0"}}')
    empty = tmp_path / "backend"
    empty.mkdir()
    monkeypatch.chdir(empty)
    monkeypatch.setattr(module, "load_moviepilot_settings", lambda: SimpleNamespace(
        PLUGIN_LOCAL_REPO_PATHS=str(legacy), ROOT_PATH=tmp_path))
    assert module.resolve_local_repo("", "MyPlugin", "auto") == legacy
