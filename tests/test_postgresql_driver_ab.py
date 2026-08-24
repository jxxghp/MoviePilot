"""PostgreSQL 驱动 A/B harness 的无 Docker 合同测试。"""

from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "perf" / "postgresql_driver_ab.py"


def load_harness():
    """从脚本路径加载 harness，避免把 scripts 变成运行时 package。"""
    spec = importlib.util.spec_from_file_location("postgresql_driver_ab_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sample(variant: str, value: float) -> dict:
    """构造满足汇总合同的单个样本。"""
    return {
        "variant": variant,
        "postgresql_settings": {"server_version": "18.4"},
        "sql_contract": {"fixture_sha256": "fixture", "sql_sha256": "sql"},
        "serial_query": {"throughput_ops_s": value},
        "concurrent_query": {"throughput_ops_s": value * 2},
        "batch_write": {"seconds": 10 / value},
        "long_transaction": {"parallel_short_query_seconds": 1 / value},
    }


def test_campaign_and_image_arguments_are_strict() -> None:
    """公开参数不得生成不安全名称或接受可变镜像标签。"""
    harness = load_harness()

    assert harness.normalize_campaign("PG-AB.1") == "pg-ab.1"
    with pytest.raises(argparse.ArgumentTypeError):
        harness.normalize_campaign("../escape")
    with pytest.raises(argparse.ArgumentTypeError):
        harness.immutable_image("moviepilot-v3:latest")
    assert harness.immutable_image(f"moviepilot-v3@sha256:{'a' * 64}")


def test_sample_order_covers_all_balanced_permutations() -> None:
    """六轮中每个方案必须各占两次首、中、末位置。"""
    harness = load_harness()

    orders = [harness.sample_order(index) for index in range(6)]

    assert len(set(orders)) == 6
    for variant in harness.VARIANTS:
        assert [order[0] for order in orders].count(variant) == 2
        assert [order[1] for order in orders].count(variant) == 2
        assert [order[2] for order in orders].count(variant) == 2


def test_validate_sample_enforces_driver_and_gil_contracts() -> None:
    """V3t 必须使用 psycopg C 且在基准结束后仍关闭 GIL。"""
    harness = load_harness()
    valid = {
        "runtime": {
            "python_version": "3.14.7",
            "implementation": "c",
            "gil_before_import": False,
            "gil_after_import": False,
            "gil_after_benchmark": False,
        },
        "long_transaction": {
            "parallel_short_query_value": "payload-00002",
            "parallel_short_query_seconds": 0.01,
            "contended_update_seconds": 0.2,
            "requested_seconds": 0.25,
        },
    }

    harness.validate_sample("v3t_psycopg3_c", valid)
    valid["runtime"]["gil_after_benchmark"] = True
    with pytest.raises(harness.HarnessInvalid, match="GIL"):
        harness.validate_sample("v3t_psycopg3_c", valid)


def test_summary_keeps_three_variants_separate() -> None:
    """汇总必须保留三方案，不能把两个标准 V3 驱动合并。"""
    harness = load_harness()
    samples = []
    for variant, _driver in harness.VARIANTS:
        samples.extend((sample(variant, 10.0), sample(variant, 20.0), sample(variant, 30.0)))

    summary = harness.summarize(samples)

    assert set(summary["metrics"]) == {variant for variant, _ in harness.VARIANTS}
    assert summary["metrics"]["v3_psycopg2"]["serial_query_throughput_ops_s"] == 20
    assert (
        summary["ratios"]["v3t_psycopg3_c"]
        ["serial_query_throughput_over_v3_psycopg2"]
        == 1
    )


def test_campaign_requires_complete_matching_results() -> None:
    """三方案缺样本或业务结果不同都不能生成性能结论。"""
    harness = load_harness()
    samples = []
    for variant, _driver in harness.VARIANTS:
        for _ in range(3):
            item = sample(variant, 10.0)
            item["serial_query"]["checksum"] = "same"
            item["concurrent_query"]["checksums"] = ["same"]
            samples.append(item)

    harness.validate_campaign(samples, rounds=3)
    samples[-1]["serial_query"]["checksum"] = "different"
    with pytest.raises(harness.HarnessInvalid, match="校验和"):
        harness.validate_campaign(samples, rounds=3)
