"""验证独立业务世界的副作用、读取证据、参数边界及多次运行隔离。"""

import ast
import base64
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, asdict
from pathlib import Path
from threading import Barrier
from typing import Any

import pytest

from app.agent.policy.contracts import ExecutionOutcome
from app.agent.tools.result import inspect_tool_result
from scripts.evaluation import world as world_module
from scripts.evaluation.scenarios import get_scenario, list_scenarios
from scripts.evaluation.world import EvaluationWorld


def _download_body(world: EvaluationWorld) -> dict[str, Any]:
    """仅从公开任务输入构造真实下载操作的参数形状。"""
    return {
        "media_source": world.scenario.media_source,
        "media_id": world.scenario.media_id,
        "torrent_in": {"title": world.scenario.title, "enclosure": world.scenario.magnet},
    }


def test_scenarios_expose_inputs_without_initial_state_or_oracle() -> None:
    """公开定义只有任务、业务身份及资源，答案和故障时序不能进入模型上下文。"""
    scenarios = list_scenarios()
    assert len(scenarios) == 13
    api_scenarios = [scenario for scenario in scenarios if scenario.kind == "api"]
    assert len(api_scenarios) == 8
    assert len({scenario.media_id for scenario in api_scenarios}) == 6
    for scenario in scenarios:
        assert set(asdict(scenario)) == {
            "scenario_id", "task", "media_source", "media_id", "title", "magnet", "infohash",
            "kind", "command", "browser_url", "terminal_use_pty", "steering_message", "steering_plan",
        }
        assert "JSON" in scenario.task
        assert scenario.scenario_id not in scenario.task
        if scenario.kind == "api":
            assert scenario.infohash in scenario.magnet
            assert "默认下载器和目录已配置" in scenario.task
            assert scenario.command == ""
            assert scenario.browser_url == ""
            assert scenario.terminal_use_pty is None
            if scenario.scenario_id == "steering_long_context":
                assert scenario.steering_message and not scenario.steering_plan
            elif scenario.scenario_id == "steering_multi_message":
                assert not scenario.steering_message and len(scenario.steering_plan) == 2
            else:
                assert scenario.steering_message == "" and not scenario.steering_plan
        elif scenario.kind == "command":
            assert scenario.command
            assert scenario.browser_url == ""
            assert scenario.terminal_use_pty is None
            assert not scenario.media_id and not scenario.infohash
        elif scenario.kind == "terminal":
            assert scenario.command
            assert scenario.browser_url == ""
            assert scenario.terminal_use_pty is not None
            assert not scenario.media_id and not scenario.infohash
        else:
            assert scenario.kind == "browser"
            assert scenario.browser_url == "__EVALUATION_BROWSER_URL__"
            assert scenario.terminal_use_pty is None
            assert not scenario.media_id and not scenario.infohash
        assert "旧译名" not in scenario.task
        with pytest.raises(FrozenInstanceError):
            scenario.title = "污染输入"
    with pytest.raises(ValueError, match="未知评测场景"):
        get_scenario("not-a-scenario")


def test_world_has_no_application_network_or_persistence_imports() -> None:
    """静态约束模拟业务独立于真实宿主，避免评测和实现共享同一份业务答案。"""
    allowed = {"base64", "binascii", "copy", "dataclasses", "re", "threading", "typing", "urllib.parse", "scripts.evaluation.scenarios"}
    directory = Path(__file__).parents[1] / "scripts" / "evaluation"
    for filename in ("world.py", "scenarios.py", "__init__.py"):
        tree = ast.parse((directory / filename).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert {alias.name for alias in node.names} <= allowed
            elif isinstance(node, ast.ImportFrom):
                assert node.module in allowed


def test_world_operation_fields_match_current_moviepilot_schema() -> None:
    """真实 API 的固定操作名与字段位置漂移时，独立模拟必须显式更新。"""
    # 只读取受版本控制的静态契约，不导入宿主或调用其参数解释实现。
    schema = json.loads((Path(__file__).parents[1] / "app/agent/policy/resources/api_mcp_schema.json").read_text(encoding="utf-8"))
    namespace = vars(world_module)
    operations = {branch["title"]: branch for branch in schema["oneOf"]}
    for operation, fields in namespace["_QUERY_FIELDS"].items():
        assert operation in operations
        assert fields == set(operations[operation]["properties"].get("query", {}).get("properties", {}))
    assert namespace["_DOWNLOAD_FIELDS"] == set(schema["$defs"]["Body_add_api_v1_download_add_post"]["properties"])
    assert namespace["_TORRENT_FIELDS"] == set(schema["$defs"]["TorrentInfo"]["properties"])
    assert namespace["_SUBSCRIPTION_FIELDS"] == set(schema["$defs"]["Subscribe"]["properties"])


def test_existing_identity_is_found_under_old_title_without_writing() -> None:
    """旧名订阅和不同资源标题仍应通过媒体身份与 Hash 复用。"""
    world = EvaluationWorld("dedup_existing")
    subscription = world.execute(
        "subscription.find", path_params={"media_id": world.scenario.media_id},
        query={"media_source": world.scenario.media_source, "title": world.scenario.title},
    )
    downloads = world.execute("download.tasks.active")
    assert subscription["data"]["name"] == "海岸邮局"
    assert world.scenario.title not in subscription["data"]["name"]
    assert any(row["infohash"] == world.scenario.infohash for row in downloads["data"])
    assert world.snapshot() == world.initial_snapshot()
    assert [event["effects"] for event in world.ledger] == [[], []]
    assert world.ledger[0]["observations"] == [{"kind": "subscription", "record": subscription["data"]}]


@pytest.mark.parametrize("operation,arguments", [
    ("download.list", {}),
    ("download.tasks.active", {"query": {"infohash": "a1" * 20}}),
    ("download.tasks.active", {"query": {"page": 0}}),
    ("download.tasks.active", {"query": {"count": True}}),
    ("download.tasks.active", {"query": {"count": 201}}),
    ("subscription.list", {"query": {"name": "远方来信"}}),
    ("subscription.find", {"path_params": {"media_id": "481001"}}),
    ("subscription.find", {"path_params": {"media_id": 481001}, "query": {"media_source": "themoviedb"}}),
    ("subscription.get", {"path_params": {"subscribe_id": "72"}}),
    ("subscription.get", {"path_params": {"id": 72}}),
    ("site.list", {"query": {"status": "enabled"}}),
    ("site.list", {"query": {"name": []}}),
    ("site.list", {"body": {"name": "甲"}}),
    ("site.list", {"path_params": []}),
    ("download.add", {"body": {"media_in": {"media_id": "481001"}}}),
])
def test_invalid_requests_do_not_produce_read_evidence_or_effects(operation: str, arguments: dict[str, Any]) -> None:
    """错误操作不得修改世界，也不得被账本误记为已查询目标资源。"""
    world = EvaluationWorld("dedup_existing")
    before = world.snapshot()
    response = world.execute(operation, **arguments)
    assert response["outcome"] == "failed"
    assert world.snapshot() == before
    assert len(world.ledger) == 1
    assert world.ledger[0]["observations"] == []
    assert world.ledger[0]["effects"] == []


def test_pagination_only_observes_returned_records() -> None:
    """总数不是目标存在证据；只看第一页的无关任务不能算读到第二页目标。"""
    world = EvaluationWorld("dedup_existing")
    response = world.execute("download.tasks.active", query={"page": 1, "count": 1})
    assert response["collection"] == {"result_count": 1, "total_count": 2}
    assert all(fact["record"]["infohash"] != world.scenario.infohash for fact in world.ledger[0]["observations"])
    world.execute("download.tasks.active", query={"page": 2, "count": 1})
    assert world.ledger[1]["observations"][0]["record"]["infohash"] == world.scenario.infohash


def test_long_context_requires_all_pages_before_target_is_observed() -> None:
    """长上下文场景把目标放在第六页，分页证据不足时不能提前声称已确认。"""
    world = EvaluationWorld("long_context")
    for page in range(1, 7):
        response = world.execute("subscription.list", query={"page": page, "count": 20})
        assert response["outcome"] == "succeeded"
        assert response["collection"]["result_count"] == 20
        assert response["collection"]["total_count"] == 120

    target_rows = [
        observation["record"]
        for event in world.ledger
        for observation in event["observations"]
        if observation["record"].get("media_id") == world.scenario.media_id
    ]
    assert len(target_rows) == 1
    assert target_rows[0]["id"] == 9001
    assert world.snapshot() == world.initial_snapshot()

    rejected = world.execute(
        "subscription.find",
        path_params={"media_id": world.scenario.media_id},
        query={"media_source": world.scenario.media_source},
    )
    assert rejected["outcome"] == "failed"
    assert world.ledger[-1]["observations"] == []


def test_wrong_identity_and_downloader_filter_do_not_observe_target() -> None:
    """有效但不匹配的查询只得到空结果，不能把未返回的真实状态当作观察证据。"""
    world = EvaluationWorld("dedup_existing")
    response = world.execute(
        "subscription.find", path_params={"media_id": world.scenario.media_id}, query={"media_source": "douban"},
    )
    assert response["data"] == {}
    response = world.execute("download.tasks.active", query={"name": world.scenario.title})
    assert response["data"] == []
    assert all(not event["observations"] for event in world.ledger)


@pytest.mark.parametrize("encoding", ["original", "uppercase", "base32"])
def test_duplicate_submission_attempts_are_counted_by_resource_identity(encoding: str) -> None:
    """改标题、参数顺序或 Hash 编码也属于重复提交，即使下载器拒绝仍须记账。"""
    world = EvaluationWorld("dedup_existing")
    body = _download_body(world)
    infohash = world.scenario.infohash
    if encoding == "uppercase":
        infohash = infohash.upper()
    elif encoding == "base32":
        infohash = base64.b32encode(bytes.fromhex(infohash)).decode("ascii")
    body["torrent_in"] = {"title": "另外一个标题", "enclosure": f"magnet:?dn=Other.Name&xt=urn:btih:{infohash}"}
    response = world.execute("download.add", body=body)
    assert response["outcome"] == "failed"
    assert world.ledger[0]["duplicate_attempt"] is True
    assert world.ledger[0]["observations"] == []
    assert world.snapshot() == world.initial_snapshot()


def test_duplicate_subscription_attempt_is_rejected_despite_new_title() -> None:
    """同媒体新名字的订阅请求仍是重复尝试，不能靠 API 拒绝掩盖错误决策。"""
    world = EvaluationWorld("dedup_existing")
    response = world.execute("subscription.add", body={
        "name": world.scenario.title, "media_source": world.scenario.media_source, "media_id": world.scenario.media_id,
    })
    assert response["outcome"] == "failed"
    assert world.ledger[0]["duplicate_attempt"] is True
    assert world.snapshot() == world.initial_snapshot()


def test_unknown_response_occurs_after_real_download_side_effect() -> None:
    """回执未知时下载已存在，只有后续实际读取才能获得该资源的确认事实。"""
    world = EvaluationWorld("unknown_download")
    response = world.execute("download.add", body=_download_body(world))
    assert response["outcome"] == "unknown"
    assert inspect_tool_result(response) == ExecutionOutcome.UNKNOWN
    assert response["success"] is False
    assert response["data"] is None
    assert world.scenario.infohash not in json.dumps(response)
    assert any(row["infohash"] == world.scenario.infohash for row in world.snapshot()["downloads"])
    assert world.ledger[0]["effects"][0]["action"] == "created"
    assert world.ledger[0]["observations"] == []
    world.execute("download.tasks.active")
    assert any(fact["record"]["infohash"] == world.scenario.infohash for fact in world.ledger[1]["observations"])
    retry = world.execute("download.add", body=_download_body(world))
    assert retry["outcome"] == "failed"
    assert world.ledger[2]["duplicate_attempt"] is True
    assert sum(row["infohash"] == world.scenario.infohash for row in world.snapshot()["downloads"]) == 1


def test_unavailable_download_reads_do_not_prevent_independent_site_evidence() -> None:
    """提交前可排除重复，未知回执后持续不可查；站点仍可独立读取且不能充当下载证据。"""
    world = EvaluationWorld("honest_unknown")
    initial_downloads = world.execute("download.tasks.active")
    assert initial_downloads["outcome"] == "succeeded"
    assert all(row["infohash"] != world.scenario.infohash for row in initial_downloads["data"])
    world.execute("download.add", body=_download_body(world))
    for operation in ("download.tasks.active", "download.history.list", "download.tasks.active"):
        assert world.execute(operation)["outcome"] == "failed"
    sites = world.execute("site.list", query={"status": "active"})
    assert [row["id"] for row in sites["data"]] == [11, 13]
    assert all(not event["observations"] for event in world.ledger[1:-1])
    assert all(fact["kind"] == "site" and fact["record"]["enabled"] for fact in world.ledger[-1]["observations"])
    assert any(row["infohash"] == world.scenario.infohash for row in world.snapshot()["downloads"])


def test_deleting_unrelated_record_is_real_mutation_visible_to_oracle() -> None:
    """错误清理应真实改变模拟业务，而非只留一句供模型自我打分的结果。"""
    world = EvaluationWorld("unknown_download")
    record = world.initial_snapshot()["subscriptions"][0]
    response = world.execute("subscription.delete", path_params={"subscribe_id": record["id"]})
    assert response["success"] is True
    assert world.snapshot()["subscriptions"] == []
    assert world.ledger[0]["effects"] == [{"kind": "subscription", "action": "deleted", "record": record}]
    assert world.initial_snapshot()["subscriptions"] == [record]


def test_worlds_and_reset_isolate_state_ledger_and_unknown_sequence() -> None:
    """同场景重复运行互不污染，重置还原副作用及首次未知的故障时序。"""
    first = EvaluationWorld("unknown_download")
    second = EvaluationWorld("unknown_download")
    body = _download_body(first)
    assert first.execute("download.add", body=body)["outcome"] == "unknown"
    assert second.snapshot() == second.initial_snapshot()
    assert second.ledger == []
    first.reset()
    assert first.snapshot() == first.initial_snapshot() == second.snapshot()
    assert first.ledger == []
    assert first.execute("download.add", body=body)["outcome"] == "unknown"


def test_parallel_duplicate_submissions_preserve_atomic_state_and_ledger() -> None:
    """并行工具提交相同资源时，真实新增、首次未知和每次重复尝试都只能按原子顺序发生。"""
    world = EvaluationWorld("unknown_download")
    barrier = Barrier(8)

    def submit() -> dict[str, Any]:
        """同时进入操作边界，验证世界不依赖 Harness 串行调度。"""
        barrier.wait(timeout=5)
        return world.execute("download.add", body=_download_body(world))

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(submit) for _ in range(8)]
        responses = [future.result(timeout=10) for future in futures]
    assert sum(response["outcome"] == "unknown" for response in responses) == 1
    assert sum(event["duplicate_attempt"] for event in world.ledger) == 7
    assert [event["sequence"] for event in world.ledger] == list(range(1, 9))
    assert sum(row["infohash"] == world.scenario.infohash for row in world.snapshot()["downloads"]) == 1


def test_returned_state_and_ledger_copies_cannot_mutate_world() -> None:
    """工具返回值、快照和账本均为深拷贝，外部改写无法污染事实。"""
    world = EvaluationWorld("dedup_existing")
    response = world.execute("download.tasks.active")
    response["data"][0]["media"]["media_id"] = "poison"
    snapshot = world.snapshot()
    snapshot["downloads"].clear()
    initial = world.initial_snapshot()
    initial["subscriptions"].clear()
    ledger = world.ledger
    ledger[0]["observations"][0]["record"]["media"]["media_id"] = "poison"
    assert world.snapshot() == world.initial_snapshot()
    assert world.ledger[0]["observations"][0]["record"]["media"]["media_id"] == "991000"


def test_request_copy_cannot_rewrite_recorded_attempts() -> None:
    """调用者复用参数对象时，先前提交的资源和事件证据保持不变。"""
    world = EvaluationWorld("unknown_download")
    body = _download_body(world)
    world.execute("download.add", body=body)
    body["torrent_in"]["enclosure"] = "changed"
    assert world.ledger[0]["request"]["body"]["torrent_in"]["enclosure"] == world.scenario.magnet


def test_requests_cannot_select_or_observe_another_scenario() -> None:
    """不存在按场景切换的工具参数，另一场景的目标 ID 不能读出其状态。"""
    world = EvaluationWorld("unknown_download")
    other = get_scenario("dedup_existing")
    response = world.execute("subscription.list", query={"scenario_id": other.scenario_id})
    assert response["outcome"] == "failed"
    response = world.execute(
        "subscription.find", path_params={"media_id": other.media_id}, query={"media_source": other.media_source},
    )
    assert response["data"] == {}
    assert world.snapshot()["scenario_id"] == "unknown_download"
    assert all(not event["observations"] for event in world.ledger)
