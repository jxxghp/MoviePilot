"""系统拓扑、目录大小和规则解析加速端口测试。"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.application import directory as directory_module
from app.application import rules as rules_module
from app.application.transfer import workflow as workflow_module
from app.startup.composition import domain as domain_composition


def test_directory_helper_requires_configured_topology_for_local_paths(
    monkeypatch,
) -> None:
    """真实本地路径同盘判断未装配时必须稳定失败。"""
    monkeypatch.setattr(directory_module, "_disk_topology", None)

    with pytest.raises(RuntimeError, match="本地磁盘拓扑能力尚未由启动组合根配置"):
        directory_module.DirectoryHelper()._is_same_source(
            (Path("/downloads"), "local"),
            (Path("/library"), "local"),
        )


def test_directory_helper_only_calls_topology_for_two_local_paths(
    monkeypatch,
) -> None:
    """远端存储只比较类型，本地路径才调用磁盘拓扑端口。"""
    topology = MagicMock()
    topology.is_same_disk.return_value = True
    monkeypatch.setattr(directory_module, "_disk_topology", topology)
    helper = directory_module.DirectoryHelper()

    assert helper._is_same_source(
        (Path("/remote-a"), "rclone"),
        (Path("/remote-b"), "rclone"),
    ) is True
    assert helper._is_same_source(
        (Path("/downloads"), "local"),
        (Path("/library"), "local"),
    ) is True

    topology.is_same_disk.assert_called_once_with(
        Path("/downloads"),
        Path("/library"),
    )


def test_rule_parser_uses_accelerator_when_available(monkeypatch) -> None:
    """加速器返回解析结构时应保留 pyparsing 兼容投影。"""
    accelerator = MagicMock()
    accelerator.parse_filter_rule.return_value = [["HDR", "and", ["not", "BLU"]]]
    monkeypatch.setattr(rules_module, "_filter_rule_parser", accelerator)

    parsed = rules_module.RuleParser().parse("HDR & !BLU")

    assert parsed.as_list() == [["HDR", "and", ["not", "BLU"]]]
    accelerator.parse_filter_rule.assert_called_once_with("HDR & !BLU")


@pytest.mark.parametrize("accelerator", [None, MagicMock()])
def test_rule_parser_falls_back_to_python_when_acceleration_unavailable(
    monkeypatch,
    accelerator,
) -> None:
    """未装配或普通适配器异常都必须回退到 Python 解析器。"""
    if accelerator is not None:
        accelerator.parse_filter_rule.side_effect = RuntimeError("rust unavailable")
    monkeypatch.setattr(rules_module, "_filter_rule_parser", accelerator)

    parsed = rules_module.RuleParser().parse("HDR & !BLU")

    assert parsed.as_list()


def test_job_task_size_only_reads_missing_local_size(monkeypatch) -> None:
    """目录大小端口只服务于未携带 size 的本地工作项。"""
    reader = MagicMock()
    reader.get_directory_size.return_value = 2048
    monkeypatch.setattr(workflow_module, "_directory_size", reader)

    explicit = SimpleNamespace(
        fileitem=SimpleNamespace(size=1024, storage="local", path="/explicit"),
    )
    remote = SimpleNamespace(
        fileitem=SimpleNamespace(size=None, storage="rclone", path="/remote"),
    )
    local = SimpleNamespace(
        fileitem=SimpleNamespace(size=None, storage="local", path="/downloads"),
    )

    assert workflow_module._job_task_size(explicit) == 1024
    assert workflow_module._job_task_size(remote) == 0
    assert workflow_module._job_task_size(local) == 2048
    reader.get_directory_size.assert_called_once_with(Path("/downloads"))


def test_job_task_size_requires_reader_for_missing_local_size(monkeypatch) -> None:
    """本地工作项缺失 size 且端口未装配时必须明确失败。"""
    monkeypatch.setattr(workflow_module, "_directory_size", None)
    local = SimpleNamespace(
        fileitem=SimpleNamespace(size=None, storage="local", path="/downloads"),
    )

    with pytest.raises(RuntimeError, match="本地目录大小能力尚未由启动组合根配置"):
        workflow_module._job_task_size(local)


def test_domain_composition_injects_all_system_acceleration_ports(monkeypatch) -> None:
    """领域组合根必须显式注入拓扑、规则解析和目录大小三个端口。"""
    disk = MagicMock()
    parser = MagicMock()
    size = MagicMock()
    monkeypatch.setattr(domain_composition, "configure_disk_topology", disk)
    monkeypatch.setattr(domain_composition, "configure_filter_rule_parser", parser)
    monkeypatch.setattr(domain_composition, "configure_directory_size", size)
    for name in (
        "configure_dns_resolver",
        "configure_customization_provider",
        "configure_release_groups_provider",
        "configure_custom_words_provider",
        "configure_search_source_provider",
        "configure_recognition_runtime",
        "clear_rust_parse_options_cache",
    ):
        monkeypatch.setattr(domain_composition, name, MagicMock())
    monkeypatch.setattr(
        "app.domain.projection.tmdb.configure_image_url_builder",
        MagicMock(),
    )
    monkeypatch.setattr(domain_composition, "RecognitionRuleService", MagicMock())
    monkeypatch.setattr(
        domain_composition,
        "get_runtime_setting",
        MagicMock(return_value=()),
    )

    domain_composition.compose_domain_dependencies()

    disk.assert_called_once_with(domain_composition.SystemUtils)
    parser.assert_called_once_with(domain_composition.rust_accelerator)
    size.assert_called_once_with(domain_composition.SystemUtils)
