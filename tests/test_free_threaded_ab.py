"""free-threaded 镜像 A/B harness 的无 Docker 合同测试。"""

from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "perf" / "free_threaded_ab.py"


def load_harness(name: str = "free_threaded_ab_test"):
    """从脚本路径加载 harness，避免把 scripts 变成运行时 package。"""
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def digest_image(repository: str, character: str) -> str:
    """构造测试用不可变镜像引用。"""
    return f"registry.example/{repository}@sha256:{character * 64}"


def valid_preflight(variant: str) -> dict:
    """构造满足目标镜像合同的 preflight。"""
    common = {
        "python_version": "3.14.7",
        "python_implementation": "CPython",
        "moviepilot_rust_version": "0.3.0",
        "rust_available": True,
        "has_jieba_cut": True,
        "packages": {},
        "native": {},
        "installed_packages": ["moviepilot-rust==0.3.0"],
        "installed_packages_sha256": "package-hash",
        "native_distributions": [],
        "uv_pip_check": {"returncode": 0},
        "uv_project_sync_check": {"returncode": 0},
    }
    if variant == "v3":
        common.update(
            {
                "gil_disabled": False,
                "gil_enabled": True,
                "thread_inherit_context": 0,
                "has_zhconv_fast": False,
                "gil_enabled_after_imports": True,
                "packages": {
                    "bcrypt": "4.3.0",
                    "brotli": "1.2.0",
                    "crcmod": "1.7",
                    "lxml": "6.1.2",
                    "orjson": "3.12.0",
                    "psycopg": None,
                    "psycopg2-binary": "2.9.12",
                    "zhconv-rs": "0.4.1",
                },
                "imports": {
                    "moviepilot-rust": {"imported": True, "gil_after": True},
                    "psycopg2-binary": {"imported": True, "gil_after": True},
                },
            }
        )
    else:
        common.update(
            {
                "gil_disabled": True,
                "gil_enabled": False,
                "thread_inherit_context": 0,
                "has_zhconv_fast": True,
                "gil_enabled_after_imports": False,
                "packages": {
                    "bcrypt": "5.0.0",
                    "brotli": "1.2.0",
                    "crcmod": None,
                    "lxml": "7.0.0b1",
                    "orjson": "3.12.0",
                    "crcmod-plus": "2.3.1",
                    "psycopg": "3.3.4",
                    "psycopg2-binary": None,
                    "zhconv-rs": None,
                },
                "native": {
                    "crcmod_extension": True,
                    "psycopg_impl": "c",
                },
                "imports": {
                    "moviepilot-rust": {"imported": True, "gil_after": False},
                    "psycopg": {"imported": True, "gil_after": False},
                },
            }
        )
    return common


def sample(harness, variant: str, index: int, multiplier: float = 1.0) -> dict:
    """构造可汇总的真实字段形状。"""
    checksum = "same-checksum"
    application = {
        "rust_on": {
            "seconds": 0.8 * multiplier,
            "checksum": checksum,
        }
    }
    if variant == "v3":
        application["rust_off"] = {"seconds": 1.0, "checksum": checksum}
    throughput = 100.0 if variant == "v3" else 120.0
    return {
        "variant": variant,
        "sample_index": index,
        "startup": {
            "ready_seconds": 10.0 * multiplier,
            "idle": {
                "engine": {"working_set_bytes": 100_000_000},
                "processes": {
                    "totals": {
                        "rss_kib": 100_000,
                        "pss_kib": 90_000,
                        "uss_kib": 80_000,
                        "threads": 20,
                    },
                    "main_python": {"pid": 1},
                },
            },
            "api": {
                endpoint: {
                    "status": 200,
                    "p50_ms": 1.0,
                    "p95_ms": 2.0,
                    "max_ms": 3.0,
                    **(
                        {
                            "runtime": {
                                "gil_enabled": variant == "v3",
                                "rust_enabled": True,
                                "rust_required": variant == "v3t",
                            }
                        }
                        if endpoint == "system_env"
                        else {}
                    ),
                }
                for endpoint in (
                    "health_ready",
                    "dashboard_statistic",
                    "subscribe_list",
                    "system_env",
                )
            },
        },
        "hotspots": {
            "fixture_sha256": harness.FIXTURE_SHA256,
            "jieba_cut": {"available": True},
            "gil_enabled_after_hotspots": variant == "v3",
            "application": application,
            "rust_concurrency": {
                "1": {"throughput_ops_s": throughput, "checksum": checksum},
                "32": {"throughput_ops_s": throughput, "checksum": checksum},
            },
            "python_concurrency": {
                "1": {"throughput_ops_s": throughput, "checksum": checksum},
                "32": {"throughput_ops_s": throughput, "checksum": checksum},
            },
        },
        "postgresql": {
            "probe": "app.db.engine._sync_postgresql_driver",
            "result": {
                "driver": None if variant == "v3" else "psycopg",
                "scheme": "postgresql" if variant == "v3" else "postgresql+psycopg",
            },
        },
        "sqlite": {
            "sync": {"throughput_ops_s": 1000.0, "checksum": checksum},
            "async": {"throughput_ops_s": 900.0, "checksum": checksum},
        },
    }


def result_for_evaluation(harness, ft_multiplier: float = 1.0) -> dict:
    """按平衡顺序构造三组 A/B。"""
    samples = [
        sample(harness, variant, index, ft_multiplier if variant == "v3t" else 1.0)
        for variant, index in harness.SAMPLE_ORDER
    ]
    return {
        "samples": samples,
        "workers": [1, 32],
        "thresholds": {
            "max_startup_ratio": 1.25,
            "max_standard_rust_on_ratio": 1.10,
            "max_ft_single_ratio": 1.25,
            "min_ft_max_worker_throughput_ratio": 1.05,
            "max_idle_memory_ratio": 1.25,
            "max_api_p95_ratio": 1.25,
        },
        "preflight": {
            variant: {"payload": valid_preflight(variant)}
            for variant in ("v3", "v3t")
        },
        "images": {
            "v3": {"size_bytes": 600_000_000},
            "v3t": {"size_bytes": 630_000_000},
        },
    }


def test_digest_inputs_and_public_image_names_are_strict() -> None:
    """拒绝可变 tag、短 digest、旧 v3t 名称及参数传反。"""
    harness = load_harness("free_threaded_ab_images")
    standard = digest_image("moviepilot-v3", "a")
    free_threaded = digest_image("moviepilot-v3t", "b")

    assert harness.immutable_image(standard) == standard
    harness.assert_expected_repository(standard, "moviepilot-v3")
    harness.assert_expected_repository(free_threaded, "moviepilot-v3t")

    for invalid in ("moviepilot-v3:latest", "moviepilot-v3@sha256:abc"):
        with pytest.raises(argparse.ArgumentTypeError):
            harness.immutable_image(invalid)
    with pytest.raises(harness.HarnessInvalid, match="moviepilot-v3t"):
        harness.assert_expected_repository(
            digest_image("moviepilot-v3-ft", "c"), "moviepilot-v3t"
        )


def test_local_image_id_is_an_immutable_offline_fallback() -> None:
    """未拉取时允许用本地 image ID 验收尚未发布的候选。"""
    harness = load_harness("free_threaded_ab_local_image")
    digest = f"sha256:{'a' * 64}"
    reference = f"moviepilot-v3@{digest}"
    image = SimpleNamespace(
        id=digest,
        attrs={
            "Id": digest,
            "RepoDigests": [],
            "Config": {
                "Labels": {
                    "org.moviepilot.source-revision": "local-source",
                    "org.opencontainers.image.version": "3.0.0-local",
                }
            },
        },
    )
    images = Mock()
    images.get.side_effect = [RuntimeError("no manifest digest"), image]
    client = SimpleNamespace(images=images)

    identity = harness.image_identity(client, reference, pull=False)

    assert identity["image_id"] == digest
    assert identity["runtime_reference"] == digest
    assert identity["source_revision"] == "local-source"
    assert identity["version"] == "3.0.0-local"
    assert images.get.call_args_list[1].args == (digest,)


def test_docker_client_uses_current_cli_context_when_default_socket_fails(
    monkeypatch,
) -> None:
    """Docker Desktop 等非默认 socket 由当前 CLI context 统一定位。"""
    harness = load_harness("free_threaded_ab_docker_context")
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    default_client = Mock()
    default_client.ping.side_effect = RuntimeError("default socket missing")
    context_client = Mock()
    docker_module = SimpleNamespace(
        from_env=Mock(return_value=default_client),
        DockerClient=Mock(return_value=context_client),
    )
    monkeypatch.setattr(harness, "docker", docker_module)
    run = Mock(return_value=SimpleNamespace(stdout="unix:///current/docker.sock\n"))
    monkeypatch.setattr(harness.subprocess, "run", run)

    assert harness.require_docker_client() is context_client
    docker_module.DockerClient.assert_called_once_with(
        base_url="unix:///current/docker.sock"
    )
    context_client.ping.assert_called_once_with()


def test_fixture_and_sample_order_are_stable() -> None:
    """固定 seed、内容 hash 和交替顺序防止样本漂移。"""
    harness = load_harness("free_threaded_ab_fixture")

    assert harness.fixture_hash(harness.build_fixture()) == harness.FIXTURE_SHA256
    assert harness.FIXTURE_SHA256 == harness.EXPECTED_FIXTURE_SHA256
    assert len(harness.FIXTURE) == 64
    assert harness.SAMPLE_ORDER == (
        ("v3", 1),
        ("v3t", 1),
        ("v3t", 2),
        ("v3", 2),
        ("v3", 3),
        ("v3t", 3),
    )


def test_preflight_enforces_runtime_and_native_profiles() -> None:
    """标准与 FT 镜像必须满足互斥 ABI、GIL、Rust 和原生依赖合同。"""
    harness = load_harness("free_threaded_ab_preflight")

    assert harness.validate_preflight("v3", valid_preflight("v3")) == []
    assert harness.validate_preflight("v3t", valid_preflight("v3t")) == []

    leaked = valid_preflight("v3")
    leaked["has_zhconv_fast"] = True
    assert any("has_zhconv_fast" in item for item in harness.validate_preflight("v3", leaked))

    unsafe = valid_preflight("v3t")
    unsafe["gil_enabled_after_imports"] = True
    unsafe["packages"]["psycopg2-binary"] = "2.9.12"
    errors = harness.validate_preflight("v3t", unsafe)
    assert any("GIL" in item or "gil_enabled_after_imports" in item for item in errors)
    assert any("标准原生依赖" in item for item in errors)


def test_evaluation_distinguishes_invalid_regression_and_pass() -> None:
    """合同错误优先 invalid，有效性能失败才归为 regression。"""
    harness = load_harness("free_threaded_ab_evaluation")

    summary, invalid, regressions = harness.evaluate_samples(result_for_evaluation(harness))
    assert summary["ratios"]["ft_max_worker_throughput_over_v3"] == pytest.approx(1.2)
    assert summary["installed_packages"]["v3"]["count"] == 1
    assert invalid == []
    assert regressions == []

    regression = result_for_evaluation(harness, ft_multiplier=1.5)
    _, invalid, regressions = harness.evaluate_samples(regression)
    assert invalid == []
    assert any("startup" in item for item in regressions)

    broken = result_for_evaluation(harness)
    broken["samples"][0]["postgresql"]["result"]["scheme"] = "postgresql+psycopg"
    summary, invalid, regressions = harness.evaluate_samples(broken)
    assert summary == {}
    assert any("PostgreSQL" in item for item in invalid)
    assert regressions == []


def test_markdown_and_exit_codes_preserve_machine_verdict(tmp_path: Path, monkeypatch) -> None:
    """JSON verdict、Markdown 摘要与 0/1/2 进程状态保持一致。"""
    harness = load_harness("free_threaded_ab_exit")
    base = {
        "schema_version": harness.SCHEMA_VERSION,
        "fixture": {"sha256": harness.FIXTURE_SHA256, "count": 64},
        "source_revision": "abc123",
        "images": {
            "v3": {"reference": digest_image("moviepilot-v3", "a")},
            "v3t": {"reference": digest_image("moviepilot-v3t", "b")},
        },
        "summary": {},
        "invalid_reasons": [],
        "regressions": [],
    }
    markdown = harness.build_markdown({**base, "verdict": "pass"})
    assert "moviepilot-v3t@sha256" in markdown
    assert harness.FIXTURE_SHA256 in markdown

    argv = [
        "--standard-image",
        digest_image("moviepilot-v3", "a"),
        "--free-threaded-image",
        digest_image("moviepilot-v3t", "b"),
        "--campaign",
        "fake",
        "--output-dir",
        str(tmp_path),
    ]
    for verdict, expected in (("pass", 0), ("regression", 1), ("invalid", 2)):
        monkeypatch.setattr(
            harness,
            "execute_campaign",
            lambda _args, value=verdict: {"verdict": value},
        )
        assert harness.main(argv) == expected
