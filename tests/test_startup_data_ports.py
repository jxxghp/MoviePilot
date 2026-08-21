"""生产组合根必须装配全部应用数据端口。

端口未登记时取用方一律抛 RuntimeError，测试却因 ``tests/conftest.py`` 的 autouse 注入
永远拿得到实现——漏接线在测试里没有任何症状，只有真实进程才会在第一次调用时炸开。
因此断言必须在不加载本仓 conftest 的干净子进程里跑：先确认端口全部未登记（证明子进程
没有被环境预先装配、断言不是恒真），再调用生产组合根，最后确认端口全部就绪。
"""
import json
import subprocess
import sys
import tempfile

from fastapi import FastAPI

from app.startup import lifecycle


# 端口探针：``模块:表达式``，取值成功即视为已登记，抛 RuntimeError 视为未登记。
PORT_PROBES: dict[str, str] = {
    "API 数据端口": "app.api.data:get_api_data_ports()",
    "编排数据端口": "app.application.orchestration.data:get_chain_data_ports()",
    "Agent 数据端口": "app.application.agentdata:get_agent_data_ports()",
    "订阅写入端口": "app.application.subscription.write:_get_subscribe_writer(None)",
    "整理历史端口": "app.application.history:_get_transfer_history_writer(None)",
    "系统配置服务": "app.application.configuration:get_configured_system_config()",
    "站点查询服务": "app.application.site.query:get_configured_site_query_service()",
    "Agent 会话服务": "app.application.messaging.chat:get_configured_agent_chat_service()",
    "用户配置服务": "app.application.security.userconfig:get_configured_user_configuration()",
    "按 ID 用户查询": "app.application.security.user:get_configured_user_id_lookup()",
    "按用户名查询": "app.application.security.user:get_configured_user_name_lookup()",
    "按渠道用户查询": "app.application.security.user:get_configured_user_channel_lookup()",
    "认证服务": "app.application.security.auth:get_configured_auth_service()",
    "身份绑定端口": "app.application.security.auth:_configured_identity_repository",
    "自动建号端口": "app.application.security.auth:_configured_user_provisioning_repository",
}

_PROBE_SCRIPT = """
import importlib
import json

from app.testing.bootstrap import prepare_backend

prepare_backend()

from app.startup.dataports_initializer import configure_data_ports

PROBES = json.loads({probes!r})


def snapshot():
    state = {{}}
    for name, probe in PROBES.items():
        module_name, expression = probe.split(":", 1)
        module = importlib.import_module(module_name)
        try:
            state[name] = eval(expression, vars(module)) is not None
        except RuntimeError:
            state[name] = False
    return state


before = snapshot()
configure_data_ports()
print("RESULT " + json.dumps({{"before": before, "after": snapshot()}}))
"""


def _run_probe_subprocess() -> dict[str, dict[str, bool]]:
    """在不加载本仓 conftest 的干净解释器里采集端口装配前后的状态。

    :return: ``{"before": {...}, "after": {...}}``，值为端口是否已登记
    """
    script = _PROBE_SCRIPT.format(probes=json.dumps(PORT_PROBES))
    with tempfile.TemporaryDirectory() as tmp:
        completed = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            env={"PATH": "/usr/bin:/bin", "CONFIG_DIR": tmp, "PYTHONPATH": "."},
            timeout=600,
        )
    marker = "RESULT "
    line = next(
        (
            item[len(marker):]
            for item in completed.stdout.splitlines()
            if item.startswith(marker)
        ),
        None,
    )
    assert line, (
        "端口探测子进程未产出结果："
        f"{completed.stdout[-2000:]}{completed.stderr[-4000:]}"
    )
    return json.loads(line)


def test_composition_root_configures_every_application_data_port() -> None:
    """生产组合根一次调用就应登记全部应用数据端口。"""
    result = _run_probe_subprocess()

    unconfigured_before = [name for name, ready in result["before"].items() if not ready]
    assert unconfigured_before == list(PORT_PROBES), (
        "子进程在调用组合根前已有端口被装配，断言会恒真："
        f"{sorted(set(PORT_PROBES) - set(unconfigured_before))}"
    )
    missing_after = sorted(name for name, ready in result["after"].items() if not ready)
    assert not missing_after, f"生产组合根未装配以下数据端口：{missing_after}"


def test_data_port_stage_runs_after_database_and_before_routes() -> None:
    """数据端口装配必须排在数据库就绪之后、路由之前，且安全模式同样启用。"""
    manifest = lifecycle.get_lifecycle_manifest(FastAPI(), safe_mode=False)
    orders = {
        item["name"]: item["start_order"]
        for item in manifest
        if item["start_order"] is not None
    }

    assert "数据端口装配" in orders, f"启动清单缺少数据端口装配阶段：{sorted(orders)}"
    assert orders["数据库连接预算"] < orders["数据端口装配"] < orders["路由"]
    safe_names = {
        item["name"]
        for item in lifecycle.get_lifecycle_manifest(FastAPI(), safe_mode=True)
    }
    assert "数据端口装配" in safe_names
