"""对同一版本的标准与 free-threaded MoviePilot 镜像执行本地 A/B 验收。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

try:
    import docker
except ImportError:  # pragma: no cover - 保留无 Docker SDK 环境下的 --help
    docker = None


SCHEMA_VERSION = 2
EXIT_PASS = 0
EXIT_REGRESSION = 1
EXIT_INVALID = 2
FIXTURE_SEED = 314159
EXPECTED_FIXTURE_SHA256 = "2560135ffe9501b4d1230c973da237143c94c32d623153c2b4dabcefd1d055a1"
SAMPLE_ORDER = (
    ("v3", 1),
    ("v3t", 1),
    ("v3t", 2),
    ("v3", 2),
    ("v3", 3),
    ("v3t", 3),
)
COMMON_PREFLIGHT_IMPORTS = frozenset({
    "asyncpg",
    "bcrypt",
    "brotli",
    "crcmod-plus",
    "cryptography",
    "greenlet",
    "lxml",
    "moviepilot-rust",
    "orjson",
    "oss2",
    "pillow",
    "pillow-avif-plugin",
    "pydantic-core",
    "site-resource",
    "zstandard",
})
PROFILE_PREFLIGHT_IMPORTS = {
    "v3": frozenset({"psycopg2-binary"}),
    "v3t": frozenset({"psycopg"}),
}
REQUIRED_API_ENDPOINTS = (
    "health_ready",
    "dashboard_statistic",
    "subscribe_list",
    "system_env",
)
LAB_ENVIRONMENT = {
    "TZ": "Asia/Shanghai",
    "PUID": "0",
    "PGID": "0",
    "UMASK": "000",
    "PORT": "3001",
    "NGINX_PORT": "3000",
    "API_WORKERS": "1",
    "DB_TYPE": "sqlite",
    "CACHE_BACKEND_TYPE": "cachetools",
    "AI_AGENT_ENABLE": "false",
    "DEV": "false",
    "DEBUG": "false",
    "MOVIEPILOT_SAFE_MODE": "false",
    "MOVIEPILOT_AUTO_UPDATE": "false",
    "MOVIEPILOT_DOCKER_KEEPALIVE_ON_FAILURE": "false",
    "AUTO_UPDATE_RESOURCE": "false",
    "PLUGIN_MARKET": "",
    "PLUGIN_AUTO_RELOAD": "false",
    "PLUGIN_LOCAL_REPO_PATHS": "",
    "PLUGIN_STATISTIC_SHARE": "false",
    "SUBSCRIBE_STATISTIC_SHARE": "false",
    "USAGE_STATISTIC_SHARE": "false",
    "WORKFLOW_STATISTIC_SHARE": "false",
    "MEDIA_RECOGNIZE_SHARE": "false",
    "MP_SERVER_HOST": "",
    "GITHUB_TOKEN": "",
    "REPO_GITHUB_TOKEN": "",
    "AUTH_SITE": "",
    "SKILL_MARKET": "",
    "FANART_ENABLE": "false",
    "API_TOKEN": "moviepilot-ft-perf-token-00000001",
    "SUPERUSER": "admin",
    "SUPERUSER_PASSWORD": "MoviePilot-FT-Perf-Only-00000001!",
    "SECRET_KEY": "moviepilot-ft-perf-secret-key-00000001",
    "RESOURCE_SECRET_KEY": "moviepilot-ft-perf-resource-key-00000001",
    "LOG_LEVEL": "INFO",
}


class HarnessInvalid(RuntimeError):
    """表示输入、制品或采样合同不成立，结果不能用于性能判断。"""


def utc_now() -> str:
    """返回 JSON 使用的 UTC 时间。"""
    return datetime.now(timezone.utc).isoformat()


def build_fixture(seed: int = FIXTURE_SEED) -> list[str]:
    """生成稳定且不含用户数据的媒体标题 fixture。"""
    randomizer = random.Random(seed)
    names = ("流浪地球", "繁花", "三体", "庆余年", "长安十二时辰", "琅琊榜")
    sources = ("WEB-DL", "BluRay", "HDTV")
    codecs = ("H265", "AV1", "H264")
    pixels = ("1080p", "2160p")
    fixture = []
    for index in range(64):
        fixture.append(
            ".".join(
                (
                    randomizer.choice(names),
                    f"S{index % 6 + 1:02d}E{index % 24 + 1:02d}",
                    randomizer.choice(pixels),
                    randomizer.choice(sources),
                    randomizer.choice(codecs),
                    f"GROUP{index % 5}",
                )
            )
        )
    return fixture


def canonical_json(value: Any) -> str:
    """返回跨进程稳定的紧凑 JSON。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def fixture_hash(fixture: list[str]) -> str:
    """计算 fixture 内容身份。"""
    return hashlib.sha256(canonical_json(fixture).encode("utf-8")).hexdigest()


FIXTURE = build_fixture()
FIXTURE_SHA256 = fixture_hash(FIXTURE)
if FIXTURE_SHA256 != EXPECTED_FIXTURE_SHA256:  # pragma: no cover - import 期固定合同
    raise RuntimeError("free-threaded A/B fixture 内容发生未审计变化")


def normalize_campaign(value: str) -> str:
    """限制 campaign，使本地结果和 Docker 资源名称可安全复用。"""
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


def assert_expected_repository(reference: str, expected: str) -> None:
    """防止标准与 free-threaded 镜像参数传反或使用旧命名。"""
    repository = reference.split("@", 1)[0].rsplit("/", 1)[-1].split(":", 1)[0].lower()
    if repository != expected:
        raise HarnessInvalid(f"{reference} 的仓库名必须为 {expected}")


def parse_workers(value: str) -> tuple[int, ...]:
    """解析并发线程档位。"""
    try:
        workers = tuple(dict.fromkeys(int(item.strip()) for item in value.split(",")))
    except ValueError as error:
        raise argparse.ArgumentTypeError("--workers 必须是逗号分隔的正整数") from error
    if not workers or any(worker <= 0 for worker in workers):
        raise argparse.ArgumentTypeError("--workers 必须是逗号分隔的正整数")
    return workers


def atomic_write_json(path: Path, payload: Any) -> None:
    """原子写入 JSON，避免长采样中断留下半个文档。"""
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
            raise HarnessInvalid(f"无法连接 Docker Engine：{default_error}") from context_error


def image_identity(client, reference: str, pull: bool) -> dict[str, Any]:
    """解析镜像本地身份与发布标签，不把可变 tag 当成事实源。"""
    runtime_reference = reference
    try:
        if pull:
            client.images.pull(reference)
        image = client.images.get(reference)
    except Exception as error:
        if pull:
            raise HarnessInvalid(f"无法读取镜像 {reference}：{error}") from error
        image_id = reference.rsplit("@", 1)[-1]
        try:
            image = client.images.get(image_id)
        except Exception as image_id_error:
            raise HarnessInvalid(f"无法读取镜像 {reference}：{error}") from image_id_error
        if image.id != image_id:
            raise HarnessInvalid(f"本地镜像 ID 与引用不一致：{reference}")
        runtime_reference = image.id
    attrs = image.attrs or {}
    labels = (attrs.get("Config") or {}).get("Labels") or {}
    source_revision = labels.get("org.moviepilot.source-revision") or labels.get(
        "org.opencontainers.image.revision"
    )
    version = labels.get("org.opencontainers.image.version")
    if not source_revision:
        raise HarnessInvalid(f"镜像 {reference} 缺少源码 revision 标签")
    if not version:
        raise HarnessInvalid(f"镜像 {reference} 缺少版本标签")
    return {
        "reference": reference,
        "runtime_reference": runtime_reference,
        "image_id": attrs.get("Id") or getattr(image, "id", ""),
        "size_bytes": int(attrs.get("Size") or 0),
        "rootfs_layers": list((attrs.get("RootFS") or {}).get("Layers") or []),
        "repo_digests": sorted(attrs.get("RepoDigests") or []),
        "source_revision": source_revision,
        "version": version,
    }


PREFLIGHT_SCRIPT = r"""
import hashlib
import importlib
import importlib.metadata
import json
import os
import platform
import subprocess
import sys
import sysconfig

def version(name):
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None

def canonical_name(distribution):
    return (distribution.metadata.get("Name") or "").strip().lower().replace("_", "-")

packages = sorted(
    f"{canonical_name(distribution)}=={distribution.version}"
    for distribution in importlib.metadata.distributions()
)
native_distributions = []
for distribution in importlib.metadata.distributions():
    native_files = sorted(
        str(path)
        for path in (distribution.files or ())
        if str(path).lower().endswith((".so", ".pyd", ".dylib"))
    )
    if not native_files:
        continue
    wheel = distribution.read_text("WHEEL") or ""
    native_distributions.append({
        "name": canonical_name(distribution),
        "version": distribution.version,
        "files": native_files,
        "wheel_tags": sorted(
            line.split(":", 1)[1].strip()
            for line in wheel.splitlines()
            if line.startswith("Tag:")
        ),
    })
native_distributions.sort(key=lambda item: (item["name"], item["version"]))

imports = {}
def probe(name, module_name):
    before = sys._is_gil_enabled()
    try:
        module = importlib.import_module(module_name)
    except Exception as error:
        imports[name] = {
            "module": module_name,
            "imported": False,
            "error": f"{type(error).__name__}: {error}",
            "gil_before": before,
            "gil_after": sys._is_gil_enabled(),
        }
        return None
    imports[name] = {
        "module": module_name,
        "imported": True,
        "gil_before": before,
        "gil_after": sys._is_gil_enabled(),
    }
    return module

moviepilot_rust = probe("moviepilot-rust", "moviepilot_rust")
probe("site-resource", "app.application.site.sites")
probe("lxml", "lxml.etree")
probe("orjson", "orjson")
probe("zstandard", "zstandard")
probe("bcrypt", "bcrypt._bcrypt")
probe("pydantic-core", "pydantic_core._pydantic_core")
probe("pillow", "PIL._imaging")
probe("pillow-avif-plugin", "pillow_avif")
probe("cryptography", "cryptography.hazmat.bindings._rust")
probe("greenlet", "greenlet._greenlet")
probe("asyncpg", "asyncpg")
probe("brotli", "brotli")
probe("oss2", "oss2")

crcmod = probe("crcmod-plus", "crcmod.crcmod")
native = {
    "crcmod_extension": bool(crcmod and crcmod._usingExtension),
}
if sysconfig.get_config_var("Py_GIL_DISABLED") == 1:
    psycopg = probe("psycopg", "psycopg")
    native.update({
        "psycopg_impl": psycopg.pq.__impl__ if psycopg else None,
    })
else:
    probe("psycopg2-binary", "psycopg2")

uv_check = subprocess.run(
    ["/usr/local/bin/uv", "pip", "check", "--python", sys.executable],
    text=True,
    capture_output=True,
    check=False,
)
runtime_group = (
    "runtime-free-threaded"
    if sysconfig.get_config_var("Py_GIL_DISABLED") == 1
    else "runtime-standard"
)
sync_environment = os.environ.copy()
sync_environment["UV_PROJECT_ENVIRONMENT"] = sys.prefix
project_sync_check = subprocess.run(
    [
        "/usr/local/bin/uv", "sync", "--project", "/app", "--locked", "--offline",
        "--inexact", "--no-dev", "--no-install-project", "--check",
        "--python", sys.executable, "--no-default-groups", "--group", runtime_group,
    ],
    text=True,
    capture_output=True,
    check=False,
    env=sync_environment,
)

print(json.dumps({
    "python_version": sys.version.split()[0],
    "python_implementation": platform.python_implementation(),
    "machine": platform.machine(),
    "libc": platform.libc_ver(),
    "soabi": sysconfig.get_config_var("SOABI"),
    "multiarch": sysconfig.get_config_var("MULTIARCH"),
    "gil_disabled": sysconfig.get_config_var("Py_GIL_DISABLED") == 1,
    "gil_enabled": sys._is_gil_enabled(),
    "thread_inherit_context": sys.flags.thread_inherit_context,
    "moviepilot_rust_version": version("moviepilot-rust"),
    "rust_available": bool(moviepilot_rust and moviepilot_rust.is_available()),
    "has_jieba_cut": callable(getattr(moviepilot_rust, "jieba_cut", None)),
    "has_zhconv_fast": callable(getattr(moviepilot_rust, "zhconv_fast", None)),
    "packages": {
        "bcrypt": version("bcrypt"),
        "brotli": version("brotli"),
        "lxml": version("lxml"),
        "orjson": version("orjson"),
        "zstandard": version("zstandard"),
        "crcmod": version("crcmod"),
        "crcmod-plus": version("crcmod-plus"),
        "psycopg": version("psycopg"),
        "psycopg2-binary": version("psycopg2-binary"),
        "zhconv-rs": version("zhconv-rs"),
    },
    "installed_packages": packages,
    "installed_packages_sha256": hashlib.sha256("\n".join(packages).encode()).hexdigest(),
    "native_distributions": native_distributions,
    "imports": imports,
    "native": native,
    "uv_pip_check": {
        "returncode": uv_check.returncode,
        "stdout": uv_check.stdout.strip(),
        "stderr": uv_check.stderr.strip(),
    },
    "uv_project_sync_check": {
        "runtime_group": runtime_group,
        "returncode": project_sync_check.returncode,
        "stdout": project_sync_check.stdout.strip(),
        "stderr": project_sync_check.stderr.strip(),
    },
    "gil_enabled_after_imports": sys._is_gil_enabled(),
}, sort_keys=True))
"""


HOTSPOT_SCRIPT = r"""
import concurrent.futures
import hashlib
import json
import os
import sys
import time

fixture = json.loads(os.environ["MP_FT_FIXTURE"])
expected_hash = os.environ["MP_FT_FIXTURE_SHA256"]
actual_hash = hashlib.sha256(
    json.dumps(fixture, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
).hexdigest()
assert actual_hash == expected_hash
iterations = int(os.environ["MP_FT_ITERATIONS"])
workers = tuple(int(item) for item in os.environ["MP_FT_WORKERS"].split(","))
variant = os.environ["MP_FT_VARIANT"]
sample_index = int(os.environ["MP_FT_SAMPLE_INDEX"])

import moviepilot_rust
from app.adapters.system import rust as rust_accel
from app.domain.meta.runtime import configure_recognition_runtime
from app.domain.metainfo import MetaInfo
from app.runtime.config import settings

def digest_rows(rows):
    return hashlib.sha256(
        json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

def run_application(accelerator):
    configure_recognition_runtime(
        media_extensions_provider=lambda: (),
        audio_extensions_provider=lambda: (),
        accelerator=accelerator,
    )
    started = time.perf_counter()
    rows = []
    for index in range(iterations):
        meta = MetaInfo(fixture[index % len(fixture)])
        rows.append((meta.name, meta.begin_season, meta.begin_episode, meta.resource_pix))
    elapsed = time.perf_counter() - started
    return {
        "operations": iterations,
        "seconds": elapsed,
        "throughput_ops_s": iterations / elapsed,
        "checksum": digest_rows(rows),
    }

settings.RUST_ACCEL = True
application = {}
mode_order = ["rust_on"]
if variant == "v3":
    mode_order = ["rust_on", "rust_off"] if sample_index % 2 == 0 else ["rust_off", "rust_on"]
for mode in mode_order:
    application[mode] = run_application(rust_accel if mode == "rust_on" else None)

sample_words = moviepilot_rust.jieba_cut("MoviePilot中文分词性能验证", hmm=False, cut_all=False)
assert sample_words

def parse(index):
    result = moviepilot_rust.parse_metainfo_fast(fixture[index % len(fixture)], "", {})
    return (result.get("name"), result.get("begin_season"), result.get("begin_episode"), result.get("resource_pix"))

rust_concurrency = {}
for worker in workers:
    started = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=worker) as executor:
        rows = list(executor.map(parse, range(iterations)))
    elapsed = time.perf_counter() - started
    rust_concurrency[str(worker)] = {
        "operations": iterations,
        "seconds": elapsed,
        "throughput_ops_s": iterations / elapsed,
        "checksum": digest_rows(rows),
    }

from app.foundation.text import contains_chinese, remove_punctuation

def python_cpu(index):
    value = fixture[index % len(fixture)]
    cleaned = remove_punctuation(value, replacement=" ", allow_space=True)
    score = 0
    for repeat in range(32):
        score += sum((position + repeat + 1) * ord(character) for position, character in enumerate(cleaned))
    return (cleaned, contains_chinese(cleaned), score)

python_concurrency = {}
for worker in workers:
    started = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=worker) as executor:
        rows = list(executor.map(python_cpu, range(iterations)))
    elapsed = time.perf_counter() - started
    python_concurrency[str(worker)] = {
        "operations": iterations,
        "seconds": elapsed,
        "throughput_ops_s": iterations / elapsed,
        "checksum": digest_rows(rows),
    }

print(json.dumps({
    "fixture_sha256": actual_hash,
    "application": application,
    "mode_order": mode_order,
    "rust_concurrency": rust_concurrency,
    "python_concurrency": python_concurrency,
    "jieba_cut": {"words": sample_words, "available": True},
    "gil_enabled_after_hotspots": sys._is_gil_enabled(),
}, ensure_ascii=False, sort_keys=True))
"""


POSTGRESQL_SCRIPT = r"""
import json
from app.db.engine import _sync_postgresql_driver
from app.runtime.config import settings

driver = _sync_postgresql_driver()
scheme = settings.DB_POSTGRESQL_URL(driver).split(":", 1)[0]
print(json.dumps({"driver": driver, "scheme": scheme}, sort_keys=True))
"""


SQLITE_SCRIPT = r"""
import asyncio
import hashlib
import json
import os
import sqlite3
import tempfile
import time

import aiosqlite

iterations = int(os.environ["MP_FT_ITERATIONS"])
handle, path = tempfile.mkstemp(prefix="moviepilot-ft-sqlite-", suffix=".db")
os.close(handle)

connection = sqlite3.connect(path)
connection.execute("PRAGMA journal_mode=WAL")
connection.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY, title TEXT NOT NULL, score INTEGER NOT NULL)")
rows = [(index, f"title-{index:04d}", index % 97) for index in range(512)]
started = time.perf_counter()
with connection:
    connection.executemany("INSERT INTO sample VALUES (?, ?, ?)", rows)
insert_seconds = time.perf_counter() - started

started = time.perf_counter()
sync_values = []
for index in range(iterations):
    sync_values.append(
        connection.execute(
            "SELECT title, score FROM sample WHERE id = ?", (index % len(rows),)
        ).fetchone()
    )
sync_seconds = time.perf_counter() - started
connection.close()

async def run_async():
    values = []
    async with aiosqlite.connect(path) as database:
        started_at = time.perf_counter()
        for index in range(iterations):
            async with database.execute(
                "SELECT title, score FROM sample WHERE id = ?", (index % len(rows),)
            ) as cursor:
                values.append(await cursor.fetchone())
        return values, time.perf_counter() - started_at

async_values, async_seconds = asyncio.run(run_async())
os.unlink(path)

def checksum(values):
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()

print(json.dumps({
    "rows": len(rows),
    "iterations": iterations,
    "insert_seconds": insert_seconds,
    "sync": {
        "seconds": sync_seconds,
        "throughput_ops_s": iterations / sync_seconds,
        "checksum": checksum(sync_values),
    },
    "async": {
        "seconds": async_seconds,
        "throughput_ops_s": iterations / async_seconds,
        "checksum": checksum(async_values),
    },
}, sort_keys=True))
"""


API_SCRIPT = r"""
import hashlib
import json
import os
import statistics
import time
import urllib.request

iterations = int(os.environ["MP_FT_API_ITERATIONS"])
token = os.environ["MP_FT_API_TOKEN"]
endpoints = {
    "health_ready": "/health/ready",
    "dashboard_statistic": f"/api/v1/dashboard/statistic2?token={token}",
    "subscribe_list": f"/api/v1/subscribe/list?token={token}",
    "system_env": f"/api/v1/system/env?token={token}",
}

def percentile(values, fraction):
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(len(ordered) * fraction + 0.999999) - 1))
    return ordered[index]

results = {}
for name, path in endpoints.items():
    latencies = []
    bodies = []
    status = None
    for _ in range(iterations):
        started = time.perf_counter()
        with urllib.request.urlopen(f"http://127.0.0.1:3001{path}", timeout=10) as response:
            status = response.status
            bodies.append(response.read())
        latencies.append((time.perf_counter() - started) * 1000)
    results[name] = {
        "status": status,
        "iterations": iterations,
        "p50_ms": statistics.median(latencies),
        "p95_ms": percentile(latencies, 0.95),
        "max_ms": max(latencies),
        "last_body_sha256": hashlib.sha256(bodies[-1]).hexdigest(),
    }
    if name == "system_env":
        payload = json.loads(bodies[-1])
        data = payload.get("data") or {}
        results[name]["runtime"] = {
            "rust_required": data.get("RUST_ACCEL_REQUIRED"),
            "rust_enabled": data.get("RUST_ACCEL_ENABLED"),
            "gil_enabled": data.get("PYTHON_GIL_ENABLED"),
        }

print(json.dumps(results, sort_keys=True))
"""


def run_json_command(
    client,
    image: str,
    script: str,
    *,
    environment: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    """在无网络临时容器中执行 JSON 探针。"""
    try:
        output = client.containers.run(
            image,
            ["-c", script],
            entrypoint="/opt/venv/bin/python",
            environment=environment or {},
            network_disabled=True,
            remove=True,
            stdout=True,
            stderr=True,
        )
        text = output.decode("utf-8") if isinstance(output, bytes) else str(output)
        return json.loads(text.strip().splitlines()[-1])
    except Exception as error:
        raise HarnessInvalid(f"镜像探针执行失败：{error}") from error


def exec_json_in_container(
    container,
    script: str,
    *,
    environment: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    """在已经启动的候选容器中执行 JSON 探针。"""
    command = ["/usr/bin/env"]
    command.extend(f"{key}={value}" for key, value in (environment or {}).items())
    command.extend(["/opt/venv/bin/python", "-c", script])
    result = container.exec_run(command)
    output = result.output.decode("utf-8", errors="replace")
    if result.exit_code != 0:
        raise HarnessInvalid(f"运行态探针失败（exit={result.exit_code}）：{output[-2000:]}")
    return json.loads(output.strip().splitlines()[-1])


def capture_engine_stats(container) -> dict[str, int]:
    """读取容器 cgroup 内存与网络累计值。"""
    try:
        stats = container.stats(stream=False, one_shot=True)
    except TypeError:  # pragma: no cover - 旧 Docker SDK 兼容
        stats = container.stats(stream=False)
    memory = stats.get("memory_stats") or {}
    detail = memory.get("stats") or {}
    usage = int(memory.get("usage") or 0)
    inactive = int(detail.get("inactive_file") or detail.get("total_inactive_file") or 0)
    networks = stats.get("networks") or {}
    return {
        "memory_current_bytes": usage,
        "inactive_file_bytes": inactive,
        "working_set_bytes": max(0, usage - inactive),
        "network_rx_bytes": sum(int(item.get("rx_bytes") or 0) for item in networks.values()),
        "network_tx_bytes": sum(int(item.get("tx_bytes") or 0) for item in networks.values()),
    }


def capture_processes(container) -> dict[str, Any]:
    """读取容器进程的 RSS、PSS、USS 与线程数。"""
    result = container.exec_run(["/bin/bash", "/app/scripts/perf/instrument/collect_proc.sh"])
    output = result.output.decode("utf-8", errors="replace")
    if result.exit_code != 0:
        raise HarnessInvalid(f"进程采样失败：{output[-2000:]}")
    processes = []
    for line in output.splitlines()[1:]:
        fields = line.split("\t", 8)
        if len(fields) != 9:
            continue
        pid, ppid, threads, rss, pss, uss, comm, executable, command_line = fields
        processes.append({
            "pid": int(pid),
            "ppid": int(ppid),
            "threads": int(threads),
            "rss_kib": int(rss),
            "pss_kib": int(pss),
            "uss_kib": int(uss),
            "comm": comm,
            "executable": executable,
            "cmdline": command_line,
        })
    if not processes:
        raise HarnessInvalid("进程采样结果为空")
    python_processes = [
        process
        for process in processes
        if "python" in Path(process["executable"]).name.lower()
    ]
    return {
        "items": sorted(processes, key=lambda item: item["pid"]),
        "totals": {
            "rss_kib": sum(item["rss_kib"] for item in processes),
            "pss_kib": sum(item["pss_kib"] for item in processes),
            "uss_kib": sum(item["uss_kib"] for item in processes),
            "threads": sum(item["threads"] for item in processes),
        },
        "main_python": max(
            python_processes, key=lambda item: item["pss_kib"], default=None
        ),
    }


def capture_runtime_measurement(container) -> dict[str, Any]:
    """采集同一时点的 cgroup 与进程内存。"""
    return {
        "captured_at": utc_now(),
        "engine": capture_engine_stats(container),
        "processes": capture_processes(container),
    }


def validate_preflight(variant: str, payload: dict[str, Any]) -> list[str]:
    """验证解释器、GIL、Rust 与互斥原生依赖合同。"""
    errors = []
    packages = payload.get("packages") or {}
    native = payload.get("native") or {}
    imports = payload.get("imports") or {}
    required_imports = COMMON_PREFLIGHT_IMPORTS | PROFILE_PREFLIGHT_IMPORTS[variant]
    missing_imports = sorted(required_imports - imports.keys())
    failed_imports = sorted(
        name for name in required_imports if not (imports.get(name) or {}).get("imported")
    )
    if not str(payload.get("python_version") or "").startswith("3.14."):
        errors.append("Python 必须为 3.14.x")
    if payload.get("python_implementation") != "CPython":
        errors.append("运行时必须为 CPython")
    if not str(payload.get("moviepilot_rust_version") or "").startswith("0.3."):
        errors.append("moviepilot-rust 必须为 0.3.x")
    if not payload.get("rust_available") or not payload.get("has_jieba_cut"):
        errors.append("moviepilot-rust 必须可用并提供 jieba_cut")
    if (payload.get("uv_project_sync_check") or {}).get("returncode") != 0:
        errors.append("uv 项目 profile 一致性检查未通过")
    pip_check = payload.get("uv_pip_check") or {}
    pip_check_errors = [
        line.strip()
        for line in str(pip_check.get("stderr") or "").splitlines()
        if line.strip().startswith("The package `")
    ]
    allowed_pip_check_errors = [
        "The package `oss2` requires `crcmod>=1.7`, but it's not installed"
    ]
    if pip_check_errors and pip_check_errors != allowed_pip_check_errors:
        errors.append("uv pip check 出现未声明的包元数据不兼容")
    if not pip_check_errors and pip_check.get("returncode") != 0:
        errors.append("uv pip check 未通过且未返回可识别的不兼容项")
    if missing_imports:
        errors.append(f"缺少核心组件导入结果：{', '.join(missing_imports)}")
    elif failed_imports:
        errors.append(f"核心组件导入失败：{', '.join(failed_imports)}")
    for package, version in {
        "brotli": "1.2.0",
        "orjson": "3.12.0",
    }.items():
        if packages.get(package) != version:
            errors.append(f"{package} 必须为 {version}")
    if variant == "v3":
        expected = {
            "gil_disabled": False,
            "gil_enabled": True,
            "thread_inherit_context": 0,
            "has_zhconv_fast": True,
            "gil_enabled_after_imports": True,
        }
        for key, value in expected.items():
            if payload.get(key) != value:
                errors.append(f"标准镜像 {key} 应为 {value!r}")
        if packages.get("lxml") != "6.1.2":
            errors.append("标准镜像必须使用 lxml 6.1.2")
        if packages.get("bcrypt") != "4.3.0":
            errors.append("标准镜像必须使用 bcrypt 4.3.0")
        if packages.get("crcmod-plus") != "2.3.1" or packages.get("crcmod") is not None:
            errors.append("标准镜像必须使用 crcmod-plus 2.3.1")
        if packages.get("zhconv-rs") is not None:
            errors.append("标准镜像不得保留 zhconv-rs")
        if packages.get("psycopg2-binary") is None:
            errors.append("标准镜像必须保留 psycopg2-binary")
        if packages.get("psycopg") is not None:
            errors.append("标准镜像不得混入 psycopg profile")
        if not native.get("crcmod_extension"):
            errors.append("标准镜像必须使用 crcmod-plus 原生实现")
    else:
        expected = {
            "gil_disabled": True,
            "gil_enabled": False,
            "thread_inherit_context": 0,
            "has_zhconv_fast": True,
            "gil_enabled_after_imports": False,
        }
        for key, value in expected.items():
            if payload.get(key) != value:
                errors.append(f"free-threaded 镜像 {key} 应为 {value!r}")
        required_versions = {
            "bcrypt": "5.0.0",
            "lxml": "7.0.0b1",
            "orjson": "3.12.0",
            "crcmod-plus": "2.3.1",
            "psycopg": "3.3.4",
        }
        for package, version in required_versions.items():
            if packages.get(package) != version:
                errors.append(f"free-threaded 镜像 {package} 必须为 {version}")
        if (
            packages.get("zhconv-rs") is not None
            or packages.get("psycopg2-binary") is not None
            or packages.get("crcmod") is not None
        ):
            errors.append("free-threaded 镜像不得混入标准原生依赖 profile")
        if native.get("psycopg_impl") != "c" or not native.get("crcmod_extension"):
            errors.append("free-threaded 镜像必须使用 psycopg C 与 crcmod 原生实现")
        gil_enabling_imports = sorted(
            name
            for name, result in imports.items()
            if result.get("imported") and result.get("gil_after") is not False
        )
        if gil_enabling_imports:
            errors.append(
                "free-threaded 核心组件导入后启用了 GIL："
                + ", ".join(gil_enabling_imports)
            )
    return errors


def startup_sample(client, args: argparse.Namespace, image: str, variant: str, index: int) -> dict[str, Any]:
    """用空白配置卷完成 readiness、API 与资源采样。"""
    name = f"mpftab-{args.campaign}-{variant}-{index}"
    volume_name = f"{name}-config"
    labels = {"org.moviepilot.perf.campaign": args.campaign, "org.moviepilot.perf.role": "ft-ab"}
    volume = None
    container = None
    try:
        volume = client.volumes.create(name=volume_name, labels=labels)
        environment = dict(LAB_ENVIRONMENT)
        environment["MOVIEPILOT_BACKEND_READY_TIMEOUT"] = str(args.ready_timeout)
        container = client.containers.create(
            image,
            name=name,
            detach=True,
            environment=environment,
            volumes={volume.name: {"bind": "/config", "mode": "rw"}},
            network_disabled=True,
            nano_cpus=int(args.cpus * 1_000_000_000),
            mem_limit=args.memory,
            memswap_limit=args.memory,
            labels=labels,
        )
        started = time.monotonic()
        container.start()
        deadline = started + args.ready_timeout
        while time.monotonic() < deadline:
            container.reload()
            if container.status != "running":
                raise HarnessInvalid(f"{name} 在 readiness 前退出")
            probe = container.exec_run(
                ["curl", "-fsS", "--max-time", "2", "http://127.0.0.1:3001/health/ready"]
            )
            if probe.exit_code == 0:
                break
            time.sleep(0.5)
        else:
            raise HarnessInvalid(f"{name} readiness 超时")
        ready_seconds = time.monotonic() - started
        time.sleep(args.settle_seconds)
        idle = capture_runtime_measurement(container)
        api = exec_json_in_container(
            container,
            API_SCRIPT,
            environment={
                "MP_FT_API_ITERATIONS": str(args.api_iterations),
                "MP_FT_API_TOKEN": LAB_ENVIRONMENT["API_TOKEN"],
            },
        )
        post_api = capture_runtime_measurement(container)
        logs = container.logs(stdout=True, stderr=True).decode("utf-8", errors="replace")
        runtime_log_lines = [
            line
            for line in logs.splitlines()
            if "GIL" in line or "free-threaded" in line or "Rust" in line
        ]
        return {
            "ready_seconds": ready_seconds,
            "settle_seconds": args.settle_seconds,
            "idle": idle,
            "api": api,
            "post_api": post_api,
            "runtime_log_lines": runtime_log_lines,
        }
    except HarnessInvalid:
        raise
    except Exception as error:
        raise HarnessInvalid(f"启动样本 {name} 执行失败：{error}") from error
    finally:
        try:
            if container is not None:
                container.remove(force=True, v=False)
        except Exception:
            pass
        try:
            if volume is not None:
                volume.remove(force=True)
        except Exception:
            pass


def execute_sample(client, args: argparse.Namespace, variant: str, index: int, image: str) -> dict[str, Any]:
    """执行单个启动、热点与 PostgreSQL 驱动样本。"""
    hotspot_environment = {
        "MP_FT_FIXTURE": canonical_json(FIXTURE),
        "MP_FT_FIXTURE_SHA256": FIXTURE_SHA256,
        "MP_FT_ITERATIONS": str(args.iterations),
        "MP_FT_WORKERS": ",".join(str(worker) for worker in args.workers),
        "MP_FT_VARIANT": variant,
        "MP_FT_SAMPLE_INDEX": str(index),
        "CONFIG_DIR": "/tmp/moviepilot-ft-ab",
    }
    result = {
        "variant": variant,
        "sample_index": index,
        "image": image,
        "started_at": utc_now(),
        "startup": startup_sample(client, args, image, variant, index),
        "hotspots": run_json_command(
            client, image, HOTSPOT_SCRIPT, environment=hotspot_environment
        ),
        "sqlite": run_json_command(
            client,
            image,
            SQLITE_SCRIPT,
            environment={"MP_FT_ITERATIONS": str(args.iterations)},
        ),
        "postgresql": {
            "probe": "app.db.engine._sync_postgresql_driver",
            "result": run_json_command(
                client,
                image,
                POSTGRESQL_SCRIPT,
                environment={"CONFIG_DIR": "/tmp/moviepilot-ft-ab"},
            ),
        },
    }
    result["completed_at"] = utc_now()
    return result


def median_metric(samples: list[dict[str, Any]], variant: str, path: tuple[str, ...]) -> float:
    """读取某一变体的数值路径并返回中位数。"""
    values = []
    for sample in samples:
        if sample["variant"] != variant:
            continue
        value: Any = sample
        for key in path:
            value = value[key]
        values.append(float(value))
    if len(values) != 3:
        raise HarnessInvalid(f"{variant} 的 {'.'.join(path)} 必须有三个有效样本")
    return statistics.median(values)


def evaluate_samples(result: dict[str, Any]) -> tuple[dict[str, Any], list[str], list[str]]:
    """验证样本语义并根据显式阈值区分 pass 与 regression。"""
    samples = result["samples"]
    invalid = []
    regressions = []
    for sample in samples:
        variant = sample["variant"]
        hotspots = sample["hotspots"]
        startup = sample["startup"]
        expected_gil = variant == "v3"
        if hotspots.get("fixture_sha256") != FIXTURE_SHA256:
            invalid.append(f"{variant}-{sample['sample_index']} fixture hash 不一致")
        if not hotspots.get("jieba_cut", {}).get("available"):
            invalid.append(f"{variant}-{sample['sample_index']} jieba_cut 未执行")
        if variant == "v3t" and hotspots.get("gil_enabled_after_hotspots") is not False:
            invalid.append(f"{variant}-{sample['sample_index']} 热点后 GIL 被重新启用")
        application = hotspots.get("application") or {}
        if variant == "v3":
            if "rust_off" not in application:
                invalid.append(f"{variant}-{sample['sample_index']} 缺少 Rust 关闭样本")
            elif application["rust_off"]["checksum"] != application["rust_on"]["checksum"]:
                invalid.append(f"{variant}-{sample['sample_index']} Rust 开关改变识别语义")
        expected_driver = None if variant == "v3" else "psycopg"
        expected_scheme = "postgresql" if variant == "v3" else "postgresql+psycopg"
        if sample["postgresql"].get("result") != {
            "driver": expected_driver,
            "scheme": expected_scheme,
        }:
            invalid.append(f"{variant}-{sample['sample_index']} PostgreSQL 驱动合同错误")
        sqlite = sample.get("sqlite") or {}
        if (sqlite.get("sync") or {}).get("checksum") != (sqlite.get("async") or {}).get(
            "checksum"
        ):
            invalid.append(f"{variant}-{sample['sample_index']} SQLite 同步/异步结果不一致")
        if not (startup.get("idle") or {}).get("processes", {}).get("main_python"):
            invalid.append(f"{variant}-{sample['sample_index']} 未采集到主 Python 进程")
        api_results = startup.get("api") or {}
        missing_endpoints = sorted(set(REQUIRED_API_ENDPOINTS) - api_results.keys())
        if missing_endpoints:
            invalid.append(
                f"{variant}-{sample['sample_index']} 缺少 API 样本：{', '.join(missing_endpoints)}"
            )
        for endpoint in REQUIRED_API_ENDPOINTS:
            api_result = api_results.get(endpoint) or {}
            if api_result.get("status") != 200:
                invalid.append(
                    f"{variant}-{sample['sample_index']} API {endpoint} 状态不是 200"
                )
        runtime = api_results.get("system_env", {}).get("runtime") or {}
        if runtime.get("gil_enabled") is not expected_gil:
            invalid.append(f"{variant}-{sample['sample_index']} API GIL 状态错误")
        if runtime.get("rust_enabled") is not True:
            invalid.append(f"{variant}-{sample['sample_index']} API Rust 状态未启用")
        if runtime.get("rust_required") is not (variant == "v3t"):
            invalid.append(f"{variant}-{sample['sample_index']} API Rust required 状态错误")
        for hotspot_name in ("rust_concurrency", "python_concurrency"):
            checksums = {
                item["checksum"]
                for item in (hotspots.get(hotspot_name) or {}).values()
            }
            if len(checksums) != 1:
                invalid.append(
                    f"{variant}-{sample['sample_index']} {hotspot_name} 结果不稳定"
                )

    application_checksums = {
        sample["hotspots"]["application"]["rust_on"]["checksum"]
        for sample in samples
    }
    if len(application_checksums) != 1:
        invalid.append("两个镜像或重复样本的 Rust-on 识别语义不一致")
    for hotspot_name in ("rust_concurrency", "python_concurrency"):
        cross_sample_checksums = {
            next(iter(sample["hotspots"][hotspot_name].values()))["checksum"]
            for sample in samples
        }
        if len(cross_sample_checksums) != 1:
            invalid.append(f"两个镜像或重复样本的 {hotspot_name} 语义不一致")
    sqlite_checksums = {
        sample["sqlite"]["sync"]["checksum"]
        for sample in samples
    }
    if len(sqlite_checksums) != 1:
        invalid.append("两个镜像或重复样本的 SQLite 结果不一致")

    if invalid:
        return {}, invalid, regressions

    workers = result["workers"]
    max_worker = str(max(workers))
    endpoints = REQUIRED_API_ENDPOINTS
    preflight = result["preflight"]
    package_sets = {
        variant: set(preflight[variant]["payload"]["installed_packages"])
        for variant in ("v3", "v3t")
    }
    summary = {
        "image_size_bytes": {
            variant: result["images"][variant]["size_bytes"]
            for variant in ("v3", "v3t")
        },
        "installed_packages": {
            variant: {
                "count": len(package_sets[variant]),
                "sha256": preflight[variant]["payload"]["installed_packages_sha256"],
            }
            for variant in ("v3", "v3t")
        },
        "package_profile_diff": {
            "v3_only": sorted(package_sets["v3"] - package_sets["v3t"]),
            "v3t_only": sorted(package_sets["v3t"] - package_sets["v3"]),
        },
        "native_distribution_count": {
            variant: len(preflight[variant]["payload"]["native_distributions"])
            for variant in ("v3", "v3t")
        },
        "startup_ready_seconds": {
            variant: median_metric(samples, variant, ("startup", "ready_seconds"))
            for variant in ("v3", "v3t")
        },
        "idle_working_set_bytes": {
            variant: median_metric(
                samples, variant, ("startup", "idle", "engine", "working_set_bytes")
            )
            for variant in ("v3", "v3t")
        },
        "idle_process_totals": {
            metric: {
                variant: median_metric(
                    samples,
                    variant,
                    ("startup", "idle", "processes", "totals", metric),
                )
                for variant in ("v3", "v3t")
            }
            for metric in ("rss_kib", "pss_kib", "uss_kib", "threads")
        },
        "api": {
            endpoint: {
                metric: {
                    variant: median_metric(
                        samples,
                        variant,
                        ("startup", "api", endpoint, metric),
                    )
                    for variant in ("v3", "v3t")
                }
                for metric in ("p50_ms", "p95_ms", "max_ms")
            }
            for endpoint in endpoints
        },
        "sqlite_throughput_ops_s": {
            mode: {
                variant: median_metric(
                    samples, variant, ("sqlite", mode, "throughput_ops_s")
                )
                for variant in ("v3", "v3t")
            }
            for mode in ("sync", "async")
        },
        "application_seconds": {
            "v3_python": median_metric(
                samples, "v3", ("hotspots", "application", "rust_off", "seconds")
            ),
            "v3_rust": median_metric(
                samples, "v3", ("hotspots", "application", "rust_on", "seconds")
            ),
            "v3t_rust": median_metric(
                samples, "v3t", ("hotspots", "application", "rust_on", "seconds")
            ),
        },
        "max_worker": int(max_worker),
        "python_throughput_ops_s": {
            str(worker): {
                variant: median_metric(
                    samples,
                    variant,
                    ("hotspots", "python_concurrency", str(worker), "throughput_ops_s"),
                )
                for variant in ("v3", "v3t")
            }
            for worker in workers
        },
        "rust_throughput_ops_s": {
            str(worker): {
                variant: median_metric(
                    samples,
                    variant,
                    ("hotspots", "rust_concurrency", str(worker), "throughput_ops_s"),
                )
                for variant in ("v3", "v3t")
            }
            for worker in workers
        },
    }
    thresholds = result["thresholds"]
    startup_ratio = (
        summary["startup_ready_seconds"]["v3t"]
        / summary["startup_ready_seconds"]["v3"]
    )
    v3_rust_ratio = (
        summary["application_seconds"]["v3_rust"]
        / summary["application_seconds"]["v3_python"]
    )
    v3t_rust_ratio = (
        summary["application_seconds"]["v3t_rust"]
        / summary["application_seconds"]["v3_rust"]
    )
    ft_throughput_ratio = (
        summary["python_throughput_ops_s"][max_worker]["v3t"]
        / summary["python_throughput_ops_s"][max_worker]["v3"]
    )
    memory_ratio = summary["idle_working_set_bytes"]["v3t"] / summary[
        "idle_working_set_bytes"
    ]["v3"]
    api_p95_ratios = {
        endpoint: values["p95_ms"]["v3t"] / values["p95_ms"]["v3"]
        for endpoint, values in summary["api"].items()
    }
    summary["ratios"] = {
        "startup_ft_over_v3": startup_ratio,
        "v3_rust_over_python": v3_rust_ratio,
        "v3t_rust_over_v3_rust": v3t_rust_ratio,
        "ft_max_worker_throughput_over_v3": ft_throughput_ratio,
        "idle_working_set_ft_over_v3": memory_ratio,
        "api_p95_ft_over_v3": api_p95_ratios,
    }
    if startup_ratio > thresholds["max_startup_ratio"]:
        regressions.append("free-threaded startup 超出允许比例")
    if v3_rust_ratio > thresholds["max_v3_rust_over_python_ratio"]:
        regressions.append("标准镜像启用 Rust 后热点变慢")
    if v3t_rust_ratio > thresholds["max_v3t_rust_over_v3_rust_ratio"]:
        regressions.append("V3t Rust 相对 V3 Rust 应用热点退化")
    if ft_throughput_ratio < thresholds["min_ft_max_worker_throughput_ratio"]:
        regressions.append("free-threaded 高并发热点未达到最低吞吐收益")
    if memory_ratio > thresholds["max_idle_memory_ratio"]:
        regressions.append("free-threaded 空载 working set 超出允许比例")
    for endpoint, ratio in api_p95_ratios.items():
        if ratio > thresholds["max_api_p95_ratio"]:
            regressions.append(f"free-threaded API {endpoint} p95 超出允许比例")
    return summary, invalid, regressions


def build_markdown(result: dict[str, Any]) -> str:
    """生成不包含本机路径、凭据或用户数据的审核报告。"""
    lines = [
        "# MoviePilot free-threaded A/B",
        "",
        f"- Verdict: **{result['verdict']}**",
        f"- Schema: `{result['schema_version']}`",
        f"- Fixture: `{result['fixture']['sha256']}` ({result['fixture']['count']} titles)",
        f"- Source revision: `{result.get('source_revision') or 'unverified'}`",
        f"- Standard: `{result['images']['v3']['reference']}`",
        f"- Free-threaded: `{result['images']['v3t']['reference']}`",
        "",
    ]
    if result.get("summary"):
        summary = result["summary"]
        lines.extend(
            [
                "| Metric | v3 | v3t |",
                "| --- | ---: | ---: |",
                (
                    "| Startup ready median (s) | "
                    f"{summary['startup_ready_seconds']['v3']:.3f} | "
                    f"{summary['startup_ready_seconds']['v3t']:.3f} |"
                ),
                (
                    "| Image size (MiB) | "
                    f"{summary['image_size_bytes']['v3'] / 1024 / 1024:.1f} | "
                    f"{summary['image_size_bytes']['v3t'] / 1024 / 1024:.1f} |"
                ),
                (
                    "| Idle working set (MiB) | "
                    f"{summary['idle_working_set_bytes']['v3'] / 1024 / 1024:.1f} | "
                    f"{summary['idle_working_set_bytes']['v3t'] / 1024 / 1024:.1f} |"
                ),
                (
                    "| Main workload PSS (MiB) | "
                    f"{summary['idle_process_totals']['pss_kib']['v3'] / 1024:.1f} | "
                    f"{summary['idle_process_totals']['pss_kib']['v3t'] / 1024:.1f} |"
                ),
                (
                    f"| {summary['max_worker']}-thread pure Python probe (ops/s) | "
                    f"{summary['python_throughput_ops_s'][str(summary['max_worker'])]['v3']:.1f} | "
                    f"{summary['python_throughput_ops_s'][str(summary['max_worker'])]['v3t']:.1f} |"
                ),
                (
                    "| SQLite sync throughput (ops/s) | "
                    f"{summary['sqlite_throughput_ops_s']['sync']['v3']:.1f} | "
                    f"{summary['sqlite_throughput_ops_s']['sync']['v3t']:.1f} |"
                ),
                "",
            ]
        )
        lines.extend(
            [
                "| Application fixture median (s) | V3 + Python | V3 + Rust | V3t + Rust |",
                "| --- | ---: | ---: | ---: |",
                (
                    "| Media recognition | "
                    f"{summary['application_seconds']['v3_python']:.6f} | "
                    f"{summary['application_seconds']['v3_rust']:.6f} | "
                    f"{summary['application_seconds']['v3t_rust']:.6f} |"
                ),
                "",
                "| API p95 (ms) | v3 | v3t |",
                "| --- | ---: | ---: |",
                *(
                    f"| {endpoint} | {values['p95_ms']['v3']:.3f} | "
                    f"{values['p95_ms']['v3t']:.3f} |"
                    for endpoint, values in summary["api"].items()
                ),
                "",
                "| Pure Python CPU probe (ops/s) | v3 | v3t |",
                "| --- | ---: | ---: |",
                *(
                    f"| {workers} threads | {values['v3']:.1f} | {values['v3t']:.1f} |"
                    for workers, values in summary["python_throughput_ops_s"].items()
                ),
                "",
                "| Direct Rust ABI probe (ops/s) | v3 | v3t |",
                "| --- | ---: | ---: |",
                *(
                    f"| {workers} threads | {values['v3']:.1f} | {values['v3t']:.1f} |"
                    for workers, values in summary["rust_throughput_ops_s"].items()
                ),
                "",
            ]
        )
    for title, key in (("Invalid reasons", "invalid_reasons"), ("Regressions", "regressions")):
        if result.get(key):
            lines.append(f"## {title}")
            lines.append("")
            lines.extend(f"- {item}" for item in result[key])
            lines.append("")
    lines.extend(
        [
            "完整 preflight、交替样本、热点校验和与阈值见 `results.json`。",
            "该本地 harness 使用空白配置卷和无外网容器，不读取真实 app.env、数据库或凭据。",
            "",
        ]
    )
    return "\n".join(lines)


def campaign_directory(args: argparse.Namespace) -> Path:
    """返回 campaign 输出目录。"""
    return args.output_dir.expanduser().resolve() / args.campaign


def write_results(args: argparse.Namespace, result: dict[str, Any]) -> None:
    """写入聚合 JSON、Markdown 与单样本 JSON。"""
    output = campaign_directory(args)
    for sample in result.get("samples") or []:
        atomic_write_json(
            output / "samples" / f"{sample['variant']}-{sample['sample_index']}.json",
            sample,
        )
    atomic_write_json(output / "results.json", result)
    report = output / "report.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(build_markdown(result), encoding="utf-8")


def execute_campaign(args: argparse.Namespace) -> dict[str, Any]:
    """执行完整 preflight 和三组平衡 A/B，并始终生成可判定结果。"""
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "campaign": args.campaign,
        "created_at": utc_now(),
        "fixture": {"seed": FIXTURE_SEED, "sha256": FIXTURE_SHA256, "count": len(FIXTURE)},
        "images": {
            "v3": {"reference": args.standard_image},
            "v3t": {"reference": args.free_threaded_image},
        },
        "workers": list(args.workers),
        "iterations": args.iterations,
        "api_iterations": args.api_iterations,
        "settle_seconds": args.settle_seconds,
        "sample_order": [f"{variant}-{index}" for variant, index in SAMPLE_ORDER],
        "thresholds": {
            "max_startup_ratio": args.max_startup_ratio,
            "max_v3_rust_over_python_ratio": args.max_v3_rust_over_python_ratio,
            "max_v3t_rust_over_v3_rust_ratio": args.max_v3t_rust_over_v3_rust_ratio,
            "min_ft_max_worker_throughput_ratio": args.min_ft_max_worker_throughput_ratio,
            "max_idle_memory_ratio": args.max_idle_memory_ratio,
            "max_api_p95_ratio": args.max_api_p95_ratio,
        },
        "preflight": {},
        "samples": [],
        "summary": {},
        "invalid_reasons": [],
        "regressions": [],
        "verdict": "invalid",
    }
    try:
        assert_expected_repository(args.standard_image, "moviepilot-v3")
        assert_expected_repository(args.free_threaded_image, "moviepilot-v3t")
        client = require_docker_client()
        for variant, reference in (
            ("v3", args.standard_image),
            ("v3t", args.free_threaded_image),
        ):
            result["images"][variant] = image_identity(client, reference, args.pull)
        identities = result["images"]
        if identities["v3"]["source_revision"] != identities["v3t"]["source_revision"]:
            raise HarnessInvalid("两个镜像不是同一源码 revision")
        if identities["v3"]["version"] != identities["v3t"]["version"]:
            raise HarnessInvalid("两个镜像不是同一版本")
        result["source_revision"] = identities["v3"]["source_revision"]
        for variant in ("v3", "v3t"):
            payload = run_json_command(
                client, identities[variant]["runtime_reference"], PREFLIGHT_SCRIPT
            )
            errors = validate_preflight(variant, payload)
            result["preflight"][variant] = {"payload": payload, "errors": errors}
            result["invalid_reasons"].extend(f"{variant}: {error}" for error in errors)
        if result["invalid_reasons"]:
            raise HarnessInvalid("preflight 未通过")
        for variant, index in SAMPLE_ORDER:
            sample = execute_sample(
                client, args, variant, index, identities[variant]["runtime_reference"]
            )
            result["samples"].append(sample)
            atomic_write_json(
                campaign_directory(args) / "samples" / f"{variant}-{index}.json", sample
            )
        summary, invalid, regressions = evaluate_samples(result)
        result["summary"] = summary
        result["invalid_reasons"].extend(invalid)
        result["regressions"].extend(regressions)
        if result["invalid_reasons"]:
            result["verdict"] = "invalid"
        elif result["regressions"]:
            result["verdict"] = "regression"
        else:
            result["verdict"] = "pass"
    except HarnessInvalid as error:
        if str(error) not in result["invalid_reasons"]:
            result["invalid_reasons"].append(str(error))
        result["verdict"] = "invalid"
    except Exception as error:  # pragma: no cover - Docker 实机异常兜底
        result["invalid_reasons"].append(
            f"未预期的 harness 失败：{type(error).__name__}: {error}"
        )
        result["verdict"] = "invalid"
    result["completed_at"] = utc_now()
    write_results(args, result)
    return result


def build_parser() -> argparse.ArgumentParser:
    """构建本地 A/B 命令行。"""
    parser = argparse.ArgumentParser(
        description="MoviePilot moviepilot-v3 vs moviepilot-v3t local A/B harness"
    )
    parser.add_argument("--standard-image", type=immutable_image, required=True)
    parser.add_argument("--free-threaded-image", type=immutable_image, required=True)
    parser.add_argument("--campaign", type=normalize_campaign, required=True)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(tempfile.gettempdir()) / "moviepilot-free-threaded-ab",
    )
    parser.add_argument("--pull", action="store_true", help="按 digest 拉取两个镜像")
    parser.add_argument("--iterations", type=int, default=1024)
    parser.add_argument("--api-iterations", type=int, default=100)
    parser.add_argument("--settle-seconds", type=float, default=3.0)
    parser.add_argument("--workers", type=parse_workers, default=parse_workers("1,8,16,32"))
    parser.add_argument("--cpus", type=float, default=4.0)
    parser.add_argument("--memory", default="2g")
    parser.add_argument("--ready-timeout", type=int, default=300)
    parser.add_argument("--max-startup-ratio", type=float, default=1.25)
    parser.add_argument("--max-v3-rust-over-python-ratio", type=float, default=1.10)
    parser.add_argument(
        "--max-v3t-rust-over-v3-rust-ratio", type=float, default=1.25
    )
    parser.add_argument(
        "--min-ft-max-worker-throughput-ratio", type=float, default=1.05
    )
    parser.add_argument("--max-idle-memory-ratio", type=float, default=1.25)
    parser.add_argument("--max-api-p95-ratio", type=float, default=1.25)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    """执行 harness，并以 0/1/2 区分 pass/regression/invalid。"""
    parser = build_parser()
    args = parser.parse_args(argv)
    if (
        args.iterations <= 0
        or args.api_iterations <= 0
        or args.settle_seconds < 0
        or args.cpus <= 0
        or args.ready_timeout <= 0
    ):
        parser.error("iterations、api-iterations、cpus 与 ready-timeout 必须大于 0")
    thresholds = (
        args.max_startup_ratio,
        args.max_v3_rust_over_python_ratio,
        args.max_v3t_rust_over_v3_rust_ratio,
        args.min_ft_max_worker_throughput_ratio,
        args.max_idle_memory_ratio,
        args.max_api_p95_ratio,
    )
    if any(value <= 0 for value in thresholds):
        parser.error("所有性能比例阈值必须大于 0")
    result = execute_campaign(args)
    print(f"Verdict: {result['verdict']}")
    print(f"Results: {campaign_directory(args) / 'results.json'}")
    print(f"Report: {campaign_directory(args) / 'report.md'}")
    return {
        "pass": EXIT_PASS,
        "regression": EXIT_REGRESSION,
        "invalid": EXIT_INVALID,
    }[result["verdict"]]


if __name__ == "__main__":
    raise SystemExit(main())
