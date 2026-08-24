"""比较标准与 free-threaded MoviePilot 镜像的 PostgreSQL 同步驱动。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import docker
except ImportError:  # pragma: no cover - 保留无 Docker SDK 环境下的 --help
    docker = None


SCHEMA_VERSION = 1
VARIANTS = (
    ("v3_psycopg2", "psycopg2"),
    ("v3_psycopg3_binary", "psycopg3"),
    ("v3t_psycopg3_c", "psycopg3"),
)
ORDERS = (
    (VARIANTS[0], VARIANTS[1], VARIANTS[2]),
    (VARIANTS[1], VARIANTS[2], VARIANTS[0]),
    (VARIANTS[2], VARIANTS[0], VARIANTS[1]),
    (VARIANTS[2], VARIANTS[1], VARIANTS[0]),
    (VARIANTS[1], VARIANTS[0], VARIANTS[2]),
    (VARIANTS[0], VARIANTS[2], VARIANTS[1]),
)


class HarnessInvalid(RuntimeError):
    """表示制品、数据库或样本不满足可比较合同。"""


def utc_now() -> str:
    """返回 JSON 使用的 UTC 时间。"""
    return datetime.now(timezone.utc).isoformat()


def normalize_campaign(value: str) -> str:
    """限制 campaign，使输出目录和 Docker 标签可安全复用。"""
    normalized = value.strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,39}", normalized):
        raise argparse.ArgumentTypeError(
            "campaign 只能包含小写字母、数字、点、下划线和短横线，最长 40 字符"
        )
    return normalized


def immutable_image(value: str) -> str:
    """只接受带完整 sha256 digest 的镜像引用。"""
    reference = value.strip()
    if not re.fullmatch(r"[^\s@]+@sha256:[0-9a-f]{64}", reference):
        raise argparse.ArgumentTypeError("镜像必须使用 repository@sha256:<64 hex> 不可变引用")
    return reference


def sample_order(round_index: int) -> tuple[tuple[str, str], ...]:
    """返回三方案的平衡全排列，消除固定位置偏置。"""
    return ORDERS[round_index % len(ORDERS)]


def atomic_write_json(path: Path, payload: Any) -> None:
    """原子写入 JSON，避免长采样中断留下半个结果。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def require_docker_client():
    """连接本机 Docker Engine。"""
    if docker is None:
        raise HarnessInvalid("缺少 docker Python SDK，请使用 MoviePilot 工作区环境执行")
    try:
        client = docker.from_env()
        client.ping()
        return client
    except Exception as default_error:
        if os.getenv("DOCKER_HOST"):
            raise HarnessInvalid(f"无法连接 Docker Engine：{default_error}") from default_error
        try:
            endpoint = subprocess.run(
                [
                    "docker",
                    "context",
                    "inspect",
                    "--format",
                    '{{ (index .Endpoints "docker").Host }}',
                ],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            if not endpoint:
                raise RuntimeError("当前 Docker context 没有 endpoint")
            client = docker.DockerClient(base_url=endpoint)
            client.ping()
            return client
        except Exception as context_error:
            raise HarnessInvalid(
                f"无法连接 Docker Engine：{default_error}"
            ) from context_error


def image_identity(client, reference: str) -> dict[str, Any]:
    """读取不可变镜像身份与 MoviePilot 源码标签。"""
    image_id = reference.rsplit("@", 1)[-1]
    try:
        image = client.images.get(reference)
    except Exception:
        try:
            image = client.images.get(image_id)
        except Exception as error:
            raise HarnessInvalid(f"本地不存在镜像 {reference}：{error}") from error
    attrs = image.attrs or {}
    labels = (attrs.get("Config") or {}).get("Labels") or {}
    revision = labels.get("org.moviepilot.source-revision") or labels.get(
        "org.opencontainers.image.revision"
    )
    version = labels.get("org.opencontainers.image.version")
    if not revision or not version:
        raise HarnessInvalid(f"镜像 {reference} 缺少源码 revision 或版本标签")
    return {
        "reference": reference,
        "runtime_reference": image.id,
        "image_id": image.id,
        "size_bytes": int(attrs.get("Size") or 0),
        "architecture": attrs.get("Architecture"),
        "source_revision": revision,
        "version": version,
    }


BENCHMARK_SCRIPT = r"""
import concurrent.futures
import hashlib
import importlib.metadata
import json
import os
import platform
import random
import statistics
import sys
import sysconfig
import threading
import time

driver = os.environ["MP_PG_DRIVER"]
dsn = os.environ["MP_PG_DSN"]
serial_queries = int(os.environ["MP_PG_SERIAL_QUERIES"])
workers = int(os.environ["MP_PG_WORKERS"])
queries_per_worker = int(os.environ["MP_PG_QUERIES_PER_WORKER"])
batch_rows = int(os.environ["MP_PG_BATCH_ROWS"])
long_transaction_seconds = float(os.environ["MP_PG_LONG_TRANSACTION_SECONDS"])
sample_key = os.environ["MP_PG_SAMPLE_KEY"]
table_name = os.environ["MP_PG_TABLE"]
if not table_name.isidentifier():
    raise RuntimeError("invalid benchmark table name")

select_sql = f"SELECT payload FROM {table_name} WHERE id = %s"
insert_sql = f"INSERT INTO {table_name} (id, payload) VALUES (%s, %s)"
sql_sha256 = hashlib.sha256(
    "\n".join((select_sql, insert_sql, "SELECT ... FOR UPDATE", "UPDATE ..."))
    .encode()
).hexdigest()
fixture_sha256 = hashlib.sha256(
    "\n".join(f"{index}:payload-{index:05d}" for index in range(10000)).encode()
).hexdigest()

gil_before = sys._is_gil_enabled()
if driver == "psycopg2":
    import psycopg2 as driver_module
    package_versions = {
        "psycopg2-binary": importlib.metadata.version("psycopg2-binary"),
        "psycopg": None,
        "psycopg-binary": None,
        "psycopg-c": None,
    }
    implementation = "psycopg2"
elif driver == "psycopg3":
    import psycopg as driver_module
    implementation = driver_module.pq.__impl__

    def optional_version(name):
        try:
            return importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            return None

    package_versions = {
        "psycopg2-binary": None,
        "psycopg": importlib.metadata.version("psycopg"),
        "psycopg-binary": optional_version("psycopg-binary"),
        "psycopg-c": optional_version("psycopg-c"),
    }
else:
    raise RuntimeError(f"unsupported driver: {driver}")
gil_after_import = sys._is_gil_enabled()
if driver == "psycopg2":
    libpq_version = driver_module.__libpq_version__
else:
    libpq_version = driver_module.pq.version()

distribution_name = {
    "psycopg2": "psycopg2-binary",
    "binary": "psycopg-binary",
    "c": "psycopg-c",
}[implementation]
distribution = importlib.metadata.distribution(distribution_name)
wheel_tags = sorted(
    line.split(":", 1)[1].strip()
    for line in (distribution.read_text("WHEEL") or "").splitlines()
    if line.startswith("Tag:")
)

def connect():
    return driver_module.connect(dsn)

def percentile(values, ratio):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int((len(ordered) - 1) * ratio))]

def latency_summary(values):
    return {
        "p50_ms": statistics.median(values),
        "p95_ms": percentile(values, 0.95),
        "max_ms": max(values),
    }

with connect() as connection:
    with connection.cursor() as cursor:
        cursor.execute(
            f"CREATE UNLOGGED TABLE IF NOT EXISTS {table_name} ("
            "id INTEGER PRIMARY KEY, payload TEXT NOT NULL)"
        )
        cursor.execute(f"SELECT count(*) FROM {table_name}")
        if cursor.fetchone()[0] < 10000:
            cursor.execute(f"TRUNCATE {table_name}")
            cursor.executemany(
                insert_sql,
                [(index, f"payload-{index:05d}") for index in range(10000)],
            )
        postgresql_settings = {}
        for setting_name in (
            "server_version",
            "max_connections",
            "shared_buffers",
            "jit",
            "synchronous_commit",
        ):
            cursor.execute("SELECT current_setting(%s)", (setting_name,))
            postgresql_settings[setting_name] = cursor.fetchone()[0]

with connect() as connection:
    with connection.cursor() as cursor:
        for row_id in range(100):
            cursor.execute(select_sql, (row_id,))
            cursor.fetchone()

randomizer = random.Random(314159)
serial_ids = [randomizer.randrange(10000) for _ in range(serial_queries)]
serial_latencies = []
serial_checksum = hashlib.sha256()
with connect() as connection:
    with connection.cursor() as cursor:
        started = time.perf_counter()
        for row_id in serial_ids:
            query_started = time.perf_counter()
            cursor.execute(select_sql, (row_id,))
            value = cursor.fetchone()[0]
            serial_checksum.update(value.encode())
            serial_latencies.append((time.perf_counter() - query_started) * 1000)
        serial_seconds = time.perf_counter() - started

def concurrent_worker(worker_index):
    worker_randomizer = random.Random(314159 + worker_index)
    checksum = hashlib.sha256()
    with connect() as connection:
        with connection.cursor() as cursor:
            for _ in range(queries_per_worker):
                row_id = worker_randomizer.randrange(10000)
                cursor.execute(select_sql, (row_id,))
                checksum.update(cursor.fetchone()[0].encode())
    return checksum.hexdigest()

concurrent_started = time.perf_counter()
with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
    concurrent_checksums = list(executor.map(concurrent_worker, range(workers)))
concurrent_seconds = time.perf_counter() - concurrent_started

write_prefix = 10000 + (
    int(hashlib.sha256(sample_key.encode()).hexdigest()[:8], 16) % 10000
) * 100000
write_rows = [
    (write_prefix + index, f"write-{sample_key}-{index:05d}")
    for index in range(batch_rows)
]
with connect() as connection:
    with connection.cursor() as cursor:
        cursor.execute(
            f"DELETE FROM {table_name} WHERE id >= %s AND id < %s",
            (write_prefix, write_prefix + batch_rows),
        )
        write_started = time.perf_counter()
        cursor.executemany(
            insert_sql,
            write_rows,
        )
        connection.commit()
        batch_write_seconds = time.perf_counter() - write_started

long_ready = threading.Event()
long_errors = []

def long_transaction():
    try:
        with connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"SELECT payload FROM {table_name} WHERE id = 1 FOR UPDATE"
                )
                long_ready.set()
                cursor.execute("SELECT pg_sleep(%s)", (long_transaction_seconds,))
                connection.commit()
    except Exception as error:
        long_errors.append(f"{type(error).__name__}: {error}")
        long_ready.set()

long_thread = threading.Thread(target=long_transaction)
long_started = time.perf_counter()
long_thread.start()
if not long_ready.wait(timeout=5):
    raise RuntimeError("long transaction did not start")
with connect() as connection:
    with connection.cursor() as cursor:
        short_started = time.perf_counter()
        cursor.execute(select_sql, (2,))
        short_value = cursor.fetchone()[0]
        short_query_seconds = time.perf_counter() - short_started
with connect() as connection:
    with connection.cursor() as cursor:
        contended_started = time.perf_counter()
        cursor.execute(
            f"UPDATE {table_name} SET payload = payload WHERE id = 1"
        )
        connection.commit()
        contended_update_seconds = time.perf_counter() - contended_started
long_thread.join(timeout=long_transaction_seconds + 5)
if long_thread.is_alive() or long_errors:
    raise RuntimeError(f"long transaction failed: {long_errors}")
long_elapsed = time.perf_counter() - long_started

with connect() as connection:
    with connection.cursor() as cursor:
        cursor.execute(
            f"DELETE FROM {table_name} WHERE id >= %s AND id < %s",
            (write_prefix, write_prefix + batch_rows),
        )

print(json.dumps({
    "postgresql_settings": postgresql_settings,
    "runtime": {
        "python_version": sys.version.split()[0],
        "platform_machine": platform.machine(),
        "soabi": sysconfig.get_config_var("SOABI"),
        "gil_before_import": gil_before,
        "gil_after_import": gil_after_import,
        "gil_after_benchmark": sys._is_gil_enabled(),
        "driver": driver,
        "implementation": implementation,
        "native_distribution": distribution_name,
        "libpq_version": libpq_version,
        "wheel_tags": wheel_tags,
        "packages": package_versions,
    },
    "sql_contract": {
        "fixture_sha256": fixture_sha256,
        "sql_sha256": sql_sha256,
    },
    "serial_query": {
        "operations": serial_queries,
        "seconds": serial_seconds,
        "throughput_ops_s": serial_queries / serial_seconds,
        "latency": latency_summary(serial_latencies),
        "checksum": serial_checksum.hexdigest(),
    },
    "concurrent_query": {
        "workers": workers,
        "operations": workers * queries_per_worker,
        "seconds": concurrent_seconds,
        "throughput_ops_s": workers * queries_per_worker / concurrent_seconds,
        "checksums": concurrent_checksums,
    },
    "batch_write": {
        "rows": batch_rows,
        "seconds": batch_write_seconds,
        "throughput_rows_s": batch_rows / batch_write_seconds,
    },
    "long_transaction": {
        "requested_seconds": long_transaction_seconds,
        "elapsed_seconds": long_elapsed,
        "parallel_short_query_seconds": short_query_seconds,
        "parallel_short_query_value": short_value,
        "contended_update_seconds": contended_update_seconds,
    },
}, sort_keys=True))
"""


def run_sample(
    client,
    *,
    image: dict[str, Any],
    postgres_container: str,
    variant: str,
    driver: str,
    round_index: int,
    position: int,
    table_name: str,
    args: argparse.Namespace,
) -> dict[str, Any]:
    """在目标镜像中执行一次独立样本。"""
    environment = {
        "MP_PG_DRIVER": driver,
        "MP_PG_DSN": args.dsn,
        "MP_PG_SERIAL_QUERIES": str(args.serial_queries),
        "MP_PG_WORKERS": str(args.workers),
        "MP_PG_QUERIES_PER_WORKER": str(args.queries_per_worker),
        "MP_PG_BATCH_ROWS": str(args.batch_rows),
        "MP_PG_LONG_TRANSACTION_SECONDS": str(args.long_transaction_seconds),
        "MP_PG_SAMPLE_KEY": f"{args.campaign}-{variant}-{round_index + 1}",
        "MP_PG_TABLE": table_name,
    }
    started = time.perf_counter()
    try:
        output = client.containers.run(
            image["runtime_reference"],
            ["python", "-c", BENCHMARK_SCRIPT],
            entrypoint="",
            environment=environment,
            network_mode=f"container:{postgres_container}",
            remove=True,
            nano_cpus=int(args.cpus * 1_000_000_000),
            mem_limit=args.memory,
            stdout=True,
            stderr=True,
        )
    except Exception as error:
        raise HarnessInvalid(f"{variant} 第 {round_index + 1} 轮执行失败：{error}") from error
    try:
        payload = json.loads(output.decode("utf-8").strip().splitlines()[-1])
    except (UnicodeDecodeError, json.JSONDecodeError, IndexError) as error:
        raise HarnessInvalid(f"{variant} 输出不是有效 JSON") from error
    payload.update(
        {
            "variant": variant,
            "round": round_index + 1,
            "position": position,
            "wall_seconds": time.perf_counter() - started,
        }
    )
    return payload


CLEANUP_SCRIPT = r"""
import os
import psycopg2

table_name = os.environ["MP_PG_TABLE"]
if not table_name.isidentifier():
    raise RuntimeError("invalid benchmark table name")
with psycopg2.connect(os.environ["MP_PG_DSN"]) as connection:
    with connection.cursor() as cursor:
        cursor.execute(f"DROP TABLE IF EXISTS {table_name}")
"""


def cleanup_table(
    client,
    *,
    image: dict[str, Any],
    postgres_container: str,
    table_name: str,
    args: argparse.Namespace,
) -> None:
    """在成功或失败后删除 campaign 独占的测试表。"""
    try:
        client.containers.run(
            image["runtime_reference"],
            ["python", "-c", CLEANUP_SCRIPT],
            entrypoint="",
            environment={"MP_PG_DSN": args.dsn, "MP_PG_TABLE": table_name},
            network_mode=f"container:{postgres_container}",
            remove=True,
            stdout=True,
            stderr=True,
        )
    except Exception as error:
        raise HarnessInvalid(f"无法清理 PostgreSQL benchmark 表：{error}") from error


def validate_sample(variant: str, sample: dict[str, Any]) -> None:
    """验证驱动、ABI 和长事务并行合同。"""
    runtime = sample["runtime"]
    if runtime["python_version"].split(".")[:2] != ["3", "14"]:
        raise HarnessInvalid(f"{variant} 不是 Python 3.14")
    if variant == "v3_psycopg2":
        expected = ("psycopg2", True)
    elif variant == "v3_psycopg3_binary":
        expected = ("binary", True)
    else:
        expected = ("c", False)
    if runtime["implementation"] != expected[0]:
        raise HarnessInvalid(
            f"{variant} 驱动实现应为 {expected[0]}，实际为 {runtime['implementation']}"
        )
    for key in ("gil_before_import", "gil_after_import", "gil_after_benchmark"):
        if runtime[key] is not expected[1]:
            raise HarnessInvalid(f"{variant} 的 {key} 不满足 GIL 合同")
    long_transaction = sample["long_transaction"]
    if long_transaction["parallel_short_query_value"] != "payload-00002":
        raise HarnessInvalid(f"{variant} 长事务并行查询结果错误")
    if (
        long_transaction["parallel_short_query_seconds"]
        >= long_transaction["requested_seconds"]
    ):
        raise HarnessInvalid(f"{variant} 的独立短查询被长事务完整阻塞")
    if (
        long_transaction["contended_update_seconds"]
        < long_transaction["requested_seconds"] * 0.5
    ):
        raise HarnessInvalid(f"{variant} 的冲突写入未等待长事务行锁")


def validate_campaign(samples: list[dict[str, Any]], rounds: int) -> None:
    """验证三方案样本数量和固定查询结果完全一致。"""
    for variant, _driver in VARIANTS:
        variant_samples = [
            sample for sample in samples if sample["variant"] == variant
        ]
        if len(variant_samples) != rounds:
            raise HarnessInvalid(f"{variant} 样本数量不完整")
    serial_checksums = {
        sample["serial_query"]["checksum"] for sample in samples
    }
    concurrent_checksums = {
        tuple(sample["concurrent_query"]["checksums"]) for sample in samples
    }
    if len(serial_checksums) != 1 or len(concurrent_checksums) != 1:
        raise HarnessInvalid("三方案查询结果校验和不一致")
    sql_contracts = {
        (sample["sql_contract"]["fixture_sha256"], sample["sql_contract"]["sql_sha256"])
        for sample in samples
    }
    if len(sql_contracts) != 1:
        raise HarnessInvalid("三方案使用的 SQL 或 fixture 不一致")
    postgresql_settings = {
        tuple(sorted(sample["postgresql_settings"].items())) for sample in samples
    }
    if len(postgresql_settings) != 1:
        raise HarnessInvalid("采样期间 PostgreSQL 服务设置发生变化")


def median_at(samples: list[dict[str, Any]], *keys: str) -> float:
    """汇总指定路径的样本中位数。"""
    values = []
    for sample in samples:
        value: Any = sample
        for key in keys:
            value = value[key]
        values.append(float(value))
    return statistics.median(values)


def summarize(samples: list[dict[str, Any]]) -> dict[str, Any]:
    """生成三方案中位数和相对标准 V3/psycopg2 的比例。"""
    grouped = {
        variant: [sample for sample in samples if sample["variant"] == variant]
        for variant, _driver in VARIANTS
    }
    metrics = {
        variant: {
            "serial_query_throughput_ops_s": median_at(
                values, "serial_query", "throughput_ops_s"
            ),
            "concurrent_query_throughput_ops_s": median_at(
                values, "concurrent_query", "throughput_ops_s"
            ),
            "batch_write_seconds": median_at(values, "batch_write", "seconds"),
            "parallel_short_query_seconds": median_at(
                values, "long_transaction", "parallel_short_query_seconds"
            ),
        }
        for variant, values in grouped.items()
    }
    baseline = metrics["v3_psycopg2"]
    ratios = {
        variant: {
            "serial_query_throughput_over_v3_psycopg2": (
                values["serial_query_throughput_ops_s"]
                / baseline["serial_query_throughput_ops_s"]
            ),
            "concurrent_query_throughput_over_v3_psycopg2": (
                values["concurrent_query_throughput_ops_s"]
                / baseline["concurrent_query_throughput_ops_s"]
            ),
            "batch_write_time_over_v3_psycopg2": (
                values["batch_write_seconds"] / baseline["batch_write_seconds"]
            ),
        }
        for variant, values in metrics.items()
    }
    return {"metrics": metrics, "ratios": ratios}


def render_report(result: dict[str, Any]) -> str:
    """生成维护者可读的 Markdown 摘要。"""
    summary = result["summary"]
    lines = [
        "# PostgreSQL driver A/B",
        "",
        f"- Campaign: `{result['campaign']}`",
        f"- PostgreSQL: `{result['postgresql']['version']}`",
        f"- Samples: `{len(result['samples'])}`",
        f"- Verdict: `{result['verdict']}`",
        "",
        "| Variant | Serial query ops/s | 16-thread query ops/s | Batch write seconds |",
        "| --- | ---: | ---: | ---: |",
    ]
    for variant, _driver in VARIANTS:
        metrics = summary["metrics"][variant]
        lines.append(
            f"| `{variant}` | {metrics['serial_query_throughput_ops_s']:.1f} | "
            f"{metrics['concurrent_query_throughput_ops_s']:.1f} | "
            f"{metrics['batch_write_seconds']:.4f} |"
        )
    lines.extend(
        [
            "",
            "性能数据用于驱动选择，不是跨机器发布阈值；硬门禁只覆盖驱动实现、GIL、SQL 结果、长事务并行和样本完整性。",
            "",
        ]
    )
    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign", required=True, type=normalize_campaign)
    parser.add_argument("--postgres-container", required=True)
    parser.add_argument("--dsn", required=True, help="仅传给隔离容器，不写入结果")
    parser.add_argument("--standard-image", required=True, type=immutable_image)
    parser.add_argument(
        "--standard-psycopg3-image", required=True, type=immutable_image
    )
    parser.add_argument("--free-threaded-image", required=True, type=immutable_image)
    parser.add_argument("--rounds", type=int, default=6)
    parser.add_argument("--serial-queries", type=int, default=2000)
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--queries-per-worker", type=int, default=250)
    parser.add_argument("--batch-rows", type=int, default=2000)
    parser.add_argument("--long-transaction-seconds", type=float, default=0.25)
    parser.add_argument("--cpus", type=float, default=2.0)
    parser.add_argument("--memory", default="1g")
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args(argv)
    for name in ("rounds", "serial_queries", "workers", "queries_per_worker", "batch_rows"):
        if getattr(args, name) <= 0:
            parser.error(f"--{name.replace('_', '-')} 必须为正整数")
    if args.long_transaction_seconds <= 0:
        parser.error("--long-transaction-seconds 必须为正数")
    if args.rounds % len(ORDERS):
        parser.error(f"--rounds 必须是 {len(ORDERS)} 的正整数倍")
    if args.cpus <= 0:
        parser.error("--cpus 必须为正数")
    return args


def main(argv: list[str] | None = None) -> int:
    """执行 A/B 并写入原始 JSON 与报告。"""
    args = parse_args(argv)
    output_dir = args.output_dir or (
        Path.cwd() / ".artifacts" / "postgresql-driver-ab" / args.campaign
    )
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "campaign": args.campaign,
        "started_at": utc_now(),
        "verdict": "invalid",
        "parameters": {
            "rounds": args.rounds,
            "serial_queries": args.serial_queries,
            "workers": args.workers,
            "queries_per_worker": args.queries_per_worker,
            "batch_rows": args.batch_rows,
            "long_transaction_seconds": args.long_transaction_seconds,
            "cpus": args.cpus,
            "memory": args.memory,
        },
        "samples": [],
    }
    client = None
    images: dict[str, dict[str, Any]] = {}
    table_name = f"mp_ab_{hashlib.sha256(args.campaign.encode()).hexdigest()[:12]}"
    try:
        client = require_docker_client()
        postgres = client.containers.get(args.postgres_container)
        postgres.reload()
        if postgres.status != "running":
            raise HarnessInvalid("PostgreSQL 容器未运行")
        version_output = postgres.exec_run(["postgres", "--version"])
        if version_output.exit_code:
            raise HarnessInvalid("无法读取 PostgreSQL 版本")
        result["postgresql"] = {
            "container_image": postgres.image.id,
            "version": version_output.output.decode().strip(),
        }
        images = {
            "v3_psycopg2": image_identity(client, args.standard_image),
            "v3_psycopg3_binary": image_identity(
                client, args.standard_psycopg3_image
            ),
            "v3t_psycopg3_c": image_identity(client, args.free_threaded_image),
        }
        revisions = {identity["source_revision"] for identity in images.values()}
        versions = {identity["version"] for identity in images.values()}
        architectures = {identity["architecture"] for identity in images.values()}
        if len(revisions) != 1 or len(versions) != 1 or len(architectures) != 1:
            raise HarnessInvalid("三个镜像必须来自相同源码 revision、产品版本和架构")
        result["images"] = images
        for round_index in range(args.rounds):
            for position, (variant, driver) in enumerate(sample_order(round_index)):
                sample = run_sample(
                    client,
                    image=images[variant],
                    postgres_container=postgres.id,
                    variant=variant,
                    driver=driver,
                    round_index=round_index,
                    position=position,
                    table_name=table_name,
                    args=args,
                )
                validate_sample(variant, sample)
                result["samples"].append(sample)
                atomic_write_json(output_dir / "results.partial.json", result)
        cleanup_table(
            client,
            image=images["v3_psycopg2"],
            postgres_container=postgres.id,
            table_name=table_name,
            args=args,
        )
        validate_campaign(result["samples"], args.rounds)
        result["postgresql"]["settings"] = result["samples"][0][
            "postgresql_settings"
        ]
        result["sql_contract"] = result["samples"][0]["sql_contract"]
        result["summary"] = summarize(result["samples"])
        result["verdict"] = "pass"
        result["finished_at"] = utc_now()
        atomic_write_json(output_dir / "results.json", result)
        (output_dir / "report.md").write_text(render_report(result), encoding="utf-8")
        partial = output_dir / "results.partial.json"
        if partial.exists():
            partial.unlink()
        return 0
    except HarnessInvalid as error:
        if client is not None and images:
            try:
                cleanup_table(
                    client,
                    image=images["v3_psycopg2"],
                    postgres_container=args.postgres_container,
                    table_name=table_name,
                    args=args,
                )
            except HarnessInvalid as cleanup_error:
                result["cleanup_error"] = str(cleanup_error)
        result["error"] = str(error)
        result["finished_at"] = utc_now()
        atomic_write_json(output_dir / "results.invalid.json", result)
        print(f"postgresql_driver_ab invalid: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
