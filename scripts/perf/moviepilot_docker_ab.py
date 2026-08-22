"""对两个 MoviePilot Git commit 执行可复现的 Docker 内存 A/B 测量。"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import statistics
import subprocess
import sys
import tarfile
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

try:
    import docker
except ImportError:  # pragma: no cover - 仅用于让 --help 在缺依赖环境仍可使用
    docker = None


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parents[1]
INSTRUMENT_DIR = SCRIPT_DIR / "instrument"
DEFAULT_SUBSTRATE = (
    "jxxghp/moviepilot-v3@"
    "sha256:925de1fdf1bb0312144bc818bc8ebaa999a9a159c6d14f1b48b0ff05edb7f720"
)
DEFAULT_BROWSER_SOURCE_VOLUME = "mp-perf-v3-browser-seed"
DEFAULT_SCENARIO = "idle-default"
BROWSER_SCENARIOS = ("browser-headless", "browser-headed")
AGENT_SCENARIOS = ("agent-disabled-router", "agent-tool-catalog")
SCENARIOS = (DEFAULT_SCENARIO, *BROWSER_SCENARIOS, *AGENT_SCENARIOS)
CAMPAIGN_LABEL = "org.moviepilot.perf.campaign"
ROLE_LABEL = "org.moviepilot.perf.role"
SOURCE_LABEL = "org.moviepilot.perf.source-commit"
SUBSTRATE_LABEL = "org.moviepilot.perf.substrate"
CRITICAL_SUBSTRATE_PATHS = (
    "pyproject.toml",
    "uv.lock",
    "docker/Dockerfile",
)
SEED_COMPATIBILITY_PATHS = ("database/versions",)
AGENT_HEAVY_MODULE_PREFIXES = (
    "app.agent.orchestrator",
    "app.agent.callback",
    "app.agent.llm.helper",
    "app.agent.tools.base",
    "app.agent.tools.catalog",
    "app.agent.tools.factory",
    "app.agent.tools.impl",
    "langgraph",
    "langchain",
    "langchain_core",
    "openai",
    "anthropic",
    "google.genai",
    "boto3",
    "botocore",
)
AGENT_SCHEMA_BASELINE_PREFIXES = ("langchain", "langchain_core")
AGENT_NONMATERIALIZATION_PREFIXES = tuple(
    prefix
    for prefix in AGENT_HEAVY_MODULE_PREFIXES
    if prefix not in AGENT_SCHEMA_BASELINE_PREFIXES
)
AGENT_TOOL_CATALOG_PREFIXES = (
    "app.agent.tools.base",
    "app.agent.tools.catalog",
    "app.agent.tools.factory",
    "app.agent.tools.impl",
)
MODULE_PREFIXES = (
    "lark_oapi",
    "slack_bolt",
    "slack_sdk",
    "discord",
    "plexapi",
    "telebot",
    "app.agent",
    "app.agent.tools",
    "app.modules",
    *AGENT_HEAVY_MODULE_PREFIXES,
)
BALANCED_RUN_ORDER = (
    ("before", 1),
    ("after", 1),
    ("after", 2),
    ("before", 2),
    ("before", 3),
    ("after", 3),
)
LAB_API_TOKEN = "moviepilot-perf-lab-token-00000001"
LAB_PASSWORD = "MoviePilot-Perf-Lab-Only-00000001!"
LAB_SECRET_KEY = "moviepilot-perf-secret-key-lab-only-00000001"
LAB_RESOURCE_SECRET_KEY = "moviepilot-perf-resource-key-lab-only-00000001"


OVERLAY_DOCKERFILE = r"""
ARG MP_SUBSTRATE
FROM ${MP_SUBSTRATE} AS frozen

RUN set -eux; \
    mkdir -p /frozen/plugins /frozen/site; \
    cp -a /app/app/plugins/. /frozen/plugins/; \
    rm -f /frozen/plugins/__init__.py; \
    rm -rf /frozen/plugins/__pycache__; \
    find /app/app/application/site -maxdepth 1 -type f \
      \( -name 'sites.*.so' -o -name 'user.sites.v3.bin' \) \
      -exec cp -a '{}' /frozen/site/ \;

FROM ${MP_SUBSTRATE}
ARG MP_SOURCE_COMMIT
ARG MP_CAMPAIGN

USER root
RUN rm -rf /app && mkdir -p /app/app/plugins /app/app/application/site
COPY source/ /app/
COPY --from=frozen /frozen/plugins/ /app/app/plugins/
COPY --from=frozen /frozen/site/ /app/app/application/site/

RUN cp -f /app/docker/nginx.common.conf /etc/nginx/common.conf \
    && cp -f /app/docker/nginx.template.conf /etc/nginx/nginx.template.conf \
    && cp -f /app/docker/update.sh /usr/local/bin/mp_update.sh \
    && cp -f /app/docker/entrypoint.sh /entrypoint.sh \
    && cp -f /app/docker/docker_http_proxy.conf /etc/nginx/docker_http_proxy.conf \
    && chmod +x /entrypoint.sh /usr/local/bin/mp_update.sh

LABEL org.moviepilot.perf.campaign="${MP_CAMPAIGN}" \
      org.moviepilot.perf.source-commit="${MP_SOURCE_COMMIT}" \
      org.moviepilot.perf.substrate="${MP_SUBSTRATE}"
""".lstrip()


IMAGE_FINGERPRINT_SCRIPT = r"""
import hashlib
import importlib.metadata
import json
import platform
from pathlib import Path


def tree_fingerprint(root, selected_names=None):
    root_path = Path(root)
    digest = hashlib.sha256()
    count = 0
    total = 0
    if not root_path.exists():
        return {"files": 0, "bytes": 0, "sha256": digest.hexdigest()}
    for path in sorted(item for item in root_path.rglob("*") if item.is_file()):
        if selected_names and not any(path.match(pattern) for pattern in selected_names):
            continue
        relative = path.relative_to(root_path).as_posix()
        size = path.stat().st_size
        count += 1
        total += size
        digest.update(relative.encode("utf-8", errors="surrogateescape"))
        digest.update(b"\0")
        digest.update(str(size).encode("ascii"))
        digest.update(b"\0")
        with path.open("rb") as input_file:
            for chunk in iter(lambda: input_file.read(1024 * 1024), b""):
                digest.update(chunk)
    return {"files": count, "bytes": total, "sha256": digest.hexdigest()}


packages = sorted(
    f"{distribution.metadata.get('Name', '')}=={distribution.version}"
    for distribution in importlib.metadata.distributions()
)
package_digest = hashlib.sha256("\n".join(packages).encode("utf-8")).hexdigest()
print(json.dumps({
    "python": platform.python_version(),
    "packages": {"count": len(packages), "sha256": package_digest},
    "public": tree_fingerprint("/public"),
    "plugins": tree_fingerprint("/app/app/plugins"),
    "site_resources": tree_fingerprint(
        "/app/app/application/site",
        ("sites.*.so", "user.sites.v3.bin"),
    ),
}, sort_keys=True))
"""


VOLUME_FINGERPRINT_SCRIPT = r"""
import hashlib
import json
from pathlib import Path

root = Path("/volume")
digest = hashlib.sha256()
count = 0
total = 0
if root.exists():
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix()
        size = path.stat().st_size
        count += 1
        total += size
        digest.update(relative.encode("utf-8", errors="surrogateescape"))
        digest.update(b"\0")
        digest.update(str(size).encode("ascii"))
        digest.update(b"\n")
print(json.dumps({"files": count, "bytes": total, "layout_sha256": digest.hexdigest()}))
"""


class HarnessError(RuntimeError):
    """表示测量合同无法继续成立。"""


def utc_now() -> str:
    """返回适合写入 JSON 的 UTC 时间。"""
    return datetime.now(timezone.utc).isoformat()


def atomic_write_json(path: Path, payload: Any) -> None:
    """原子写入 JSON，避免长时间测量中断后留下半个结果文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(f"{path.suffix}.tmp")
    temporary_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary_path.replace(path)


def write_text(path: Path, content: str) -> None:
    """创建父目录并写入 UTF-8 文本。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def run_command(
    command: list[str],
    *,
    cwd: Optional[Path] = None,
    log_path: Optional[Path] = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """不经过 shell 执行命令，并在需要时保存完整输出。"""
    result = subprocess.run(
        command,
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
    )
    if log_path:
        write_text(log_path, result.stdout + result.stderr)
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        tail = "\n".join(detail[-20:])
        raise HarnessError(
            f"命令失败（exit={result.returncode}）：{' '.join(command)}\n{tail}"
        )
    return result


def require_docker_client():
    """连接 Docker Engine，并在依赖或 daemon 不可用时给出明确错误。"""
    if docker is None:
        raise HarnessError(
            "缺少 docker Python SDK，请使用 MoviePilot 工作区运行环境执行"
        )
    try:
        client = docker.from_env()
        client.ping()
        return client
    except Exception as default_error:
        context = run_command(
            ["docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"],
            check=False,
        )
        context_host = context.stdout.strip()
        if not context_host:
            raise HarnessError(
                f"无法连接 Docker Engine：{default_error}"
            ) from default_error
        try:
            client = docker.DockerClient(base_url=context_host)
            client.ping()
            return client
        except Exception as context_error:
            raise HarnessError(
                f"无法通过当前 Docker context 连接 Engine：{context_error}"
            ) from context_error


def normalize_campaign(value: str) -> str:
    """限制 campaign 名称，确保 Docker 资源名和标签可安全复用。"""
    normalized = value.strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,39}", normalized):
        raise argparse.ArgumentTypeError(
            "campaign 只能包含小写字母、数字、点、下划线和短横线，最长 40 字符"
        )
    return normalized


def parse_points(value: str) -> list[float]:
    """解析以分钟为单位的升序采样点。"""
    try:
        points = sorted({float(item.strip()) for item in value.split(",")})
    except ValueError as error:
        raise argparse.ArgumentTypeError("采样点必须是逗号分隔的分钟数") from error
    if not points or any(point < 0 for point in points):
        raise argparse.ArgumentTypeError("采样点不得为空或小于 0")
    return points


def campaign_directory(args: argparse.Namespace) -> Path:
    """返回当前 campaign 的本地结果目录。"""
    return args.output_dir.expanduser().resolve() / args.campaign


def resource_prefix(args: argparse.Namespace) -> str:
    """生成本工具拥有的 Docker 资源名前缀。"""
    return f"mpperf-{args.campaign}"


def image_tag(args: argparse.Namespace, variant: str) -> str:
    """返回 Before 或 After 派生镜像标签。"""
    return f"moviepilot-perf:{args.campaign}-{variant}"


def labels(args: argparse.Namespace, role: str) -> dict[str, str]:
    """给 Docker 资源附加可审计的精确所有权标签。"""
    return {CAMPAIGN_LABEL: args.campaign, ROLE_LABEL: role}


def resolve_platform(client, requested: str) -> str:
    """将 auto 解析为 Docker daemon 的原生 Linux 架构。"""
    if requested != "auto":
        return requested
    architecture = str(client.info().get("Architecture") or "").lower()
    aliases = {
        "aarch64": "arm64",
        "arm64": "arm64",
        "x86_64": "amd64",
        "amd64": "amd64",
    }
    if architecture not in aliases:
        raise HarnessError(f"无法把 Docker 架构 {architecture!r} 映射为目标平台")
    return f"linux/{aliases[architecture]}"


def git_output(repo: Path, *arguments: str) -> str:
    """执行只读 Git 命令并返回去除尾部换行的输出。"""
    result = run_command(["git", *arguments], cwd=repo)
    return result.stdout.strip()


def resolve_git_ref(repo: Path, ref: str) -> str:
    """把用户给出的 ref 固定为 commit SHA。"""
    return git_output(repo, "rev-parse", "--verify", f"{ref}^{{commit}}")


def git_path_changed(repo: Path, before: str, after: str, paths: Iterable[str]) -> bool:
    """判断两个 commit 在指定运行时 substrate 输入上是否有差异。"""
    result = run_command(
        ["git", "diff", "--quiet", f"{before}..{after}", "--", *paths],
        cwd=repo,
        check=False,
    )
    if result.returncode not in (0, 1):
        raise HarnessError(result.stderr.strip() or "git diff 执行失败")
    return result.returncode == 1


def assert_commit_order(repo: Path, before: str, after: str) -> None:
    """要求 After 位于 Before 之后，避免比较两条无关历史。"""
    result = run_command(
        ["git", "merge-base", "--is-ancestor", before, after],
        cwd=repo,
        check=False,
    )
    if result.returncode != 0:
        raise HarnessError("After commit 必须是 Before commit 的后代")


def ensure_substrate(client, args: argparse.Namespace, pull: bool):
    """取得冻结 substrate，只有显式要求时才访问镜像仓库。"""
    if pull:
        run_command(
            ["docker", "pull", "--platform", args.platform, args.substrate],
            log_path=campaign_directory(args) / "substrate-pull.log",
        )
    try:
        return client.images.get(args.substrate)
    except Exception as error:
        raise HarnessError(
            f"本地不存在 substrate {args.substrate}；如需下载请添加 --pull-substrate"
        ) from error


def image_fingerprint(client, image: str) -> dict[str, Any]:
    """在不启动主程序和网络的临时容器中计算运行时资产指纹。"""
    try:
        output = client.containers.run(
            image,
            command=["-c", IMAGE_FINGERPRINT_SCRIPT],
            entrypoint="python3",
            network_disabled=True,
            remove=True,
            stdout=True,
            stderr=True,
        )
        return json.loads(output.decode("utf-8"))
    except Exception as error:
        raise HarnessError(f"无法计算镜像 {image} 的资产指纹：{error}") from error


def build_overlay_image(
    args: argparse.Namespace,
    variant: str,
    commit: str,
) -> dict[str, Any]:
    """从冻结 substrate 构建仅替换指定 Git commit 源码的派生镜像。"""
    campaign_dir = campaign_directory(args)
    with tempfile.TemporaryDirectory(
        prefix=f"mpperf-{args.campaign}-{variant}-"
    ) as temp:
        context = Path(temp)
        source_dir = context / "source"
        source_dir.mkdir()
        archive_path = context / "source.tar"
        run_command(
            ["git", "archive", "--format=tar", "--output", str(archive_path), commit],
            cwd=args.repo,
        )
        with tarfile.open(archive_path, "r") as archive:
            try:
                archive.extractall(source_dir, filter="data")
            except TypeError:  # pragma: no cover - Python 3.11 早期补丁版本兼容
                archive.extractall(source_dir)
        archive_path.unlink()
        write_text(context / "Dockerfile", OVERLAY_DOCKERFILE)

        tag = image_tag(args, variant)
        build_log = campaign_dir / f"build-{variant}.log"
        run_command(
            [
                "docker",
                "build",
                "--pull=false",
                "--platform",
                args.platform,
                "--build-arg",
                f"MP_SUBSTRATE={args.substrate}",
                "--build-arg",
                f"MP_SOURCE_COMMIT={commit}",
                "--build-arg",
                f"MP_CAMPAIGN={args.campaign}",
                "--tag",
                tag,
                "--file",
                str(context / "Dockerfile"),
                str(context),
            ],
            log_path=build_log,
        )

    client = require_docker_client()
    image = client.images.get(tag)
    image_labels = image.attrs.get("Config", {}).get("Labels") or {}
    if image_labels.get(SOURCE_LABEL) != commit:
        raise HarnessError(f"镜像 {tag} 的 source commit 标签校验失败")
    return {
        "variant": variant,
        "tag": tag,
        "image_id": image.id,
        "source_commit": commit,
        "fingerprint": image_fingerprint(client, tag),
    }


def command_build(args: argparse.Namespace) -> dict[str, Any]:
    """构建 Before/After 派生镜像并记录冻结输入。"""
    client = require_docker_client()
    args.platform = resolve_platform(client, args.platform)
    args.repo = args.repo.expanduser().resolve()
    if not (args.repo / ".git").exists():
        raise HarnessError(f"不是 MoviePilot Git 仓库：{args.repo}")

    campaign_dir = campaign_directory(args)
    campaign_dir.mkdir(parents=True, exist_ok=True)
    substrate = ensure_substrate(client, args, args.pull_substrate)
    substrate_labels = substrate.attrs.get("Config", {}).get("Labels") or {}
    substrate_revision = substrate_labels.get("org.opencontainers.image.revision")
    if not substrate_revision:
        raise HarnessError("substrate 缺少 org.opencontainers.image.revision 标签")

    before_commit = resolve_git_ref(args.repo, args.before_ref)
    after_commit = resolve_git_ref(args.repo, args.after_ref)
    resolve_git_ref(args.repo, substrate_revision)
    assert_commit_order(args.repo, before_commit, after_commit)

    if git_path_changed(
        args.repo,
        substrate_revision,
        before_commit,
        CRITICAL_SUBSTRATE_PATHS,
    ):
        raise HarnessError(
            "substrate revision 到 Before commit 的依赖或 Docker substrate 输入已变化，"
            "禁止使用源码 overlay A/B"
        )
    if git_path_changed(
        args.repo,
        before_commit,
        after_commit,
        CRITICAL_SUBSTRATE_PATHS,
    ):
        raise HarnessError(
            "Before/After 的依赖或 Docker substrate 输入已变化，禁止使用源码 overlay A/B"
        )
    if git_path_changed(
        args.repo,
        before_commit,
        after_commit,
        SEED_COMPATIBILITY_PATHS,
    ):
        raise HarnessError(
            "Before/After 的数据库迁移集合不同，禁止复用同一个迁移后 SQLite seed"
        )

    substrate_fingerprint = image_fingerprint(client, args.substrate)
    before_image = build_overlay_image(args, "before", before_commit)
    after_image = build_overlay_image(args, "after", after_commit)
    for key in ("python", "packages", "public", "plugins", "site_resources"):
        if before_image["fingerprint"].get(key) != after_image["fingerprint"].get(key):
            raise HarnessError(f"Before/After 冻结资产指纹不一致：{key}")

    manifest = {
        "schema_version": 1,
        "campaign": args.campaign,
        "generated_at": utc_now(),
        "platform": args.platform,
        "before_ref": args.before_ref,
        "after_ref": args.after_ref,
        "before_commit": before_commit,
        "after_commit": after_commit,
        "critical_substrate_paths": list(CRITICAL_SUBSTRATE_PATHS),
        "seed_compatibility_paths": list(SEED_COMPATIBILITY_PATHS),
        "substrate": {
            "reference": args.substrate,
            "image_id": substrate.id,
            "source_revision": substrate_revision,
            "repo_digests": substrate.attrs.get("RepoDigests") or [],
            "fingerprint": substrate_fingerprint,
        },
        "images": {"before": before_image, "after": after_image},
    }
    atomic_write_json(campaign_dir / "build.json", manifest)
    print(f"Build manifest: {campaign_dir / 'build.json'}")
    return manifest


def load_build_manifest(args: argparse.Namespace) -> dict[str, Any]:
    """读取并校验当前 campaign 的构建结果。"""
    path = campaign_directory(args) / "build.json"
    if not path.exists():
        raise HarnessError("缺少 build.json，请先执行 build")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("campaign") != args.campaign:
        raise HarnessError("build.json 的 campaign 不匹配")
    return payload


def fixed_environment(args: argparse.Namespace, instrument: bool) -> dict[str, str]:
    """返回不依赖用户 app.env、外部服务或真实凭据的固定空载配置。"""
    environment = {
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
        "MOVIEPILOT_BACKEND_READY_TIMEOUT": str(args.ready_timeout),
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
        "BROWSER_EMULATION": "cloakbrowser",
        "FANART_ENABLE": "false",
        "API_TOKEN": LAB_API_TOKEN,
        "SUPERUSER": "admin",
        "SUPERUSER_PASSWORD": LAB_PASSWORD,
        "SECRET_KEY": LAB_SECRET_KEY,
        "RESOURCE_SECRET_KEY": LAB_RESOURCE_SECRET_KEY,
        "LOG_LEVEL": "INFO",
    }
    if instrument:
        environment.update(
            {
                "PYTHONPATH": "/opt/moviepilot-perf/instrument",
                "MP_PERF_OUTPUT_DIR": "/opt/moviepilot-perf/out/modules",
                "MP_PERF_SCENARIO": getattr(args, "scenario", DEFAULT_SCENARIO),
                "MP_PERF_ACTIVATION_TIMEOUT": str(
                    getattr(args, "activation_timeout", 180)
                ),
                "MP_PERF_AGENT_MODULE_PREFIXES": ",".join(AGENT_HEAVY_MODULE_PREFIXES),
            }
        )
    return environment


def get_volume(client, name: str):
    """返回命名 volume；不存在时返回 None。"""
    try:
        return client.volumes.get(name)
    except docker.errors.NotFound:
        return None


def assert_owned(resource, args: argparse.Namespace, kind: str) -> None:
    """删除或复用资源前验证 campaign 标签，避免误伤用户资源。"""
    resource_labels = resource.attrs.get("Labels") or {}
    if resource_labels.get(CAMPAIGN_LABEL) != args.campaign:
        raise HarnessError(f"拒绝操作非本 campaign 的 {kind}：{resource.name}")


def prepare_volume(
    client,
    args: argparse.Namespace,
    name: str,
    role: str,
    replace: bool,
):
    """创建工具拥有的命名 volume，并按显式 replace 处理同名旧资源。"""
    existing = get_volume(client, name)
    if existing:
        assert_owned(existing, args, "volume")
        if not replace:
            raise HarnessError(f"volume 已存在：{name}；如需重建请使用 --replace")
        existing.remove(force=True)
    return client.volumes.create(name=name, labels=labels(args, role))


def clone_volume(client, image: str, source: str, target: str) -> None:
    """在无网络、无主程序的短容器中复制命名 volume。"""
    try:
        client.containers.run(
            image,
            command=["-c", "cp -a /source/. /target/"],
            entrypoint="/bin/sh",
            network_disabled=True,
            remove=True,
            volumes={
                source: {"bind": "/source", "mode": "ro"},
                target: {"bind": "/target", "mode": "rw"},
            },
        )
    except Exception as error:
        raise HarnessError(f"复制 volume {source} → {target} 失败：{error}") from error


def volume_fingerprint(client, image: str, volume_name: str) -> dict[str, Any]:
    """记录 volume 文件数量、总大小和路径布局哈希，不读取配置内容。"""
    try:
        output = client.containers.run(
            image,
            command=["-c", VOLUME_FINGERPRINT_SCRIPT],
            entrypoint="python3",
            network_disabled=True,
            remove=True,
            volumes={volume_name: {"bind": "/volume", "mode": "ro"}},
        )
        return json.loads(output.decode("utf-8"))
    except Exception as error:
        raise HarnessError(f"无法计算 volume {volume_name} 指纹：{error}") from error


def ensure_internal_network(client, args: argparse.Namespace):
    """创建或复用当前 campaign 的无外网 Docker network。"""
    name = f"{resource_prefix(args)}-internal"
    try:
        network = client.networks.get(name)
        assert_owned(network, args, "network")
        if not network.attrs.get("Internal"):
            raise HarnessError(f"network {name} 不是 internal network")
        return network
    except docker.errors.NotFound:
        return client.networks.create(
            name,
            driver="bridge",
            internal=True,
            labels=labels(args, "measurement-network"),
        )


def remove_owned_container(client, args: argparse.Namespace, name: str) -> None:
    """移除同 campaign 遗留容器，绝不按模糊前缀删除。"""
    try:
        container = client.containers.get(name)
    except docker.errors.NotFound:
        return
    assert_owned(container, args, "container")
    container.remove(force=True, v=False)


def create_app_container(
    client,
    args: argparse.Namespace,
    *,
    image: str,
    name: str,
    role: str,
    config_volume: str,
    browser_volume: str,
    network_name: str,
    output_dir: Optional[Path],
):
    """按固定资源和挂载合同创建 MoviePilot 容器。"""
    remove_owned_container(client, args, name)
    volume_mounts: dict[str, dict[str, str]] = {
        config_volume: {"bind": "/config", "mode": "rw"},
        browser_volume: {"bind": "/moviepilot/.cloakbrowser", "mode": "rw"},
    }
    instrument = output_dir is not None
    if output_dir:
        output_dir.mkdir(parents=True, exist_ok=True)
        volume_mounts[str(INSTRUMENT_DIR)] = {
            "bind": "/opt/moviepilot-perf/instrument",
            "mode": "ro",
        }
        volume_mounts[str(output_dir.resolve())] = {
            "bind": "/opt/moviepilot-perf/out",
            "mode": "rw",
        }
    return client.containers.create(
        image,
        name=name,
        detach=True,
        environment=fixed_environment(args, instrument=instrument),
        volumes=volume_mounts,
        network=network_name,
        nano_cpus=int(args.cpus * 1_000_000_000),
        mem_limit=args.memory,
        memswap_limit=args.memory,
        pids_limit=2048,
        shm_size="256m",
        labels=labels(args, role),
    )


def container_running(container) -> bool:
    """刷新并返回容器是否仍在运行。"""
    container.reload()
    return container.status == "running"


def wait_for_exec_success(
    container,
    command: list[str],
    timeout: float,
    description: str,
) -> float:
    """轮询容器内的无副作用探针并返回耗时秒数。"""
    started = time.monotonic()
    deadline = started + timeout
    while time.monotonic() < deadline:
        if not container_running(container):
            raise HarnessError(f"等待{description}时容器提前退出")
        result = container.exec_run(command)
        if result.exit_code == 0:
            return time.monotonic() - started
        time.sleep(0.5)
    raise HarnessError(f"等待{description}超时（{timeout:.0f}s）")


def wait_for_ready(container, started_at: float, timeout: float) -> float:
    """等待公开 health endpoint 完成完整同步启动阶段。"""
    deadline = started_at + timeout
    command = [
        "curl",
        "-fsS",
        "--max-time",
        "2",
        "http://127.0.0.1:3001/health/ready",
    ]
    while time.monotonic() < deadline:
        if not container_running(container):
            raise HarnessError("等待 health ready 时容器提前退出")
        result = container.exec_run(command)
        if result.exit_code == 0:
            return time.monotonic() - started_at
        time.sleep(0.5)
    raise HarnessError(f"等待 health ready 超时（{timeout:.0f}s）")


def assert_no_app_env(container) -> None:
    """只检查 app.env 不存在；绝不读取文件内容。"""
    result = container.exec_run(["/bin/sh", "-c", "test ! -e /config/app.env"])
    if result.exit_code != 0:
        raise HarnessError("测量 config volume 出现 app.env，已停止以避免读取用户配置")


def capture_engine_stats(container) -> dict[str, Any]:
    """从 Docker Engine API 读取原始 cgroup 和网络累计值。"""
    try:
        stats = container.stats(stream=False, one_shot=True)
    except TypeError:  # pragma: no cover - 旧 Docker SDK 兼容
        stats = container.stats(stream=False)
    memory_stats = stats.get("memory_stats") or {}
    memory_detail = memory_stats.get("stats") or {}
    memory_current = int(memory_stats.get("usage") or 0)
    inactive_file = int(
        memory_detail.get("inactive_file")
        or memory_detail.get("total_inactive_file")
        or 0
    )
    networks = stats.get("networks") or {}
    rx_bytes = sum(int(item.get("rx_bytes") or 0) for item in networks.values())
    tx_bytes = sum(int(item.get("tx_bytes") or 0) for item in networks.values())
    return {
        "memory_current_bytes": memory_current,
        "inactive_file_bytes": inactive_file,
        "working_set_bytes": max(memory_current - inactive_file, 0),
        "network_rx_bytes": rx_bytes,
        "network_tx_bytes": tx_bytes,
    }


def capture_processes(container) -> dict[str, Any]:
    """读取采样开始时已有进程的 PSS/USS/RSS 与线程数。"""
    result = container.exec_run(
        ["/bin/bash", "/opt/moviepilot-perf/instrument/collect_proc.sh"]
    )
    if result.exit_code != 0:
        raise HarnessError(
            "进程采样失败：" + result.output.decode("utf-8", errors="replace")
        )
    lines = result.output.decode("utf-8", errors="replace").splitlines()
    processes: list[dict[str, Any]] = []
    for line in lines[1:]:
        fields = line.split("\t", 8)
        if len(fields) != 9:
            continue
        pid, ppid, threads, rss, pss, uss, comm, executable, command_line = fields
        processes.append(
            {
                "pid": int(pid),
                "ppid": int(ppid),
                "threads": int(threads),
                "rss_kib": int(rss),
                "pss_kib": int(pss),
                "uss_kib": int(uss),
                "comm": comm,
                "executable": executable,
                "cmdline": command_line,
            }
        )
    if not processes:
        raise HarnessError("进程采样结果为空")
    python_processes = [
        process
        for process in processes
        if "python" in Path(process["executable"]).name.lower()
    ]
    main_python = max(python_processes, key=lambda item: item["pss_kib"], default=None)
    xvfb_processes = [
        process
        for process in processes
        if "xvfb" in f"{process['comm']} {process['cmdline']}".lower()
    ]
    return {
        "items": sorted(processes, key=lambda item: item["pid"]),
        "totals": {
            "rss_kib": sum(item["rss_kib"] for item in processes),
            "pss_kib": sum(item["pss_kib"] for item in processes),
            "uss_kib": sum(item["uss_kib"] for item in processes),
            "threads": sum(item["threads"] for item in processes),
        },
        "main_python": main_python,
        "xvfb": {
            "count": len(xvfb_processes),
            "pss_kib": sum(item["pss_kib"] for item in xvfb_processes),
        },
    }


def capture_modules(
    container,
    output_dir: Path,
    main_python: Optional[dict[str, Any]],
) -> dict[str, Any]:
    """通过已注入的 SIGUSR1 handler 获取目标进程自身的 sys.modules。"""
    if not main_python:
        raise HarnessError("未找到主 Python 进程，无法采集 sys.modules")
    modules_dir = output_dir / "modules"
    modules_dir.mkdir(parents=True, exist_ok=True)
    existing = set(modules_dir.glob("modules-*.txt"))
    result = container.exec_run(["kill", "-USR1", str(main_python["pid"])])
    if result.exit_code != 0:
        raise HarnessError("向主 Python 进程发送 SIGUSR1 失败")
    deadline = time.monotonic() + 5
    snapshot_path: Optional[Path] = None
    while time.monotonic() < deadline:
        candidates = set(modules_dir.glob("modules-*.txt")) - existing
        if candidates:
            snapshot_path = max(candidates, key=lambda path: path.stat().st_mtime_ns)
            break
        time.sleep(0.05)
    if snapshot_path is None:
        raise HarnessError("主 Python 进程没有写出 sys.modules 快照")
    content = snapshot_path.read_bytes()
    names = [line for line in content.decode("utf-8").splitlines() if line]
    prefix_counts = {
        prefix: sum(
            1 for name in names if name == prefix or name.startswith(f"{prefix}.")
        )
        for prefix in MODULE_PREFIXES
    }
    return {
        "count": len(names),
        "sha256": hashlib.sha256(content).hexdigest(),
        "prefix_counts": prefix_counts,
        "raw_file": snapshot_path.relative_to(output_dir).as_posix(),
    }


def capture_measurement(
    container,
    output_dir: Path,
    minute: float,
    settled_at: float,
) -> dict[str, Any]:
    """按低干扰顺序采集 Engine、进程和模块三层数据。"""
    captured_at = time.monotonic()
    engine = capture_engine_stats(container)
    processes = capture_processes(container)
    modules = capture_modules(container, output_dir, processes["main_python"])
    return {
        "target_minute": minute,
        "elapsed_seconds": captured_at - settled_at,
        "captured_at": utc_now(),
        "engine": engine,
        "processes": processes,
        "modules": modules,
    }


def redact_logs(content: str) -> str:
    """移除实验室凭据值和启动生成的本地实例标识。"""
    redacted = content
    for secret_value in (
        LAB_API_TOKEN,
        LAB_PASSWORD,
        LAB_SECRET_KEY,
        LAB_RESOURCE_SECRET_KEY,
    ):
        redacted = redacted.replace(secret_value, "<redacted-lab-value>")
    redacted = re.sub(
        r"(当前用户UUID[:：]\s*)[^\s]+",
        r"\1<redacted-lab-id>",
        redacted,
    )
    return redacted


def save_container_logs(container, path: Path) -> None:
    """保存脱敏后的完整容器日志。"""
    try:
        raw = container.logs(stdout=True, stderr=True, timestamps=True)
        write_text(path, redact_logs(raw.decode("utf-8", errors="replace")))
    except Exception as error:
        write_text(path, f"无法读取容器日志：{error}\n")


def stop_and_remove_container(container, timeout: int) -> dict[str, Any]:
    """优雅停止工具拥有的容器，并在超时后限定到该容器强制清理。"""
    outcome: dict[str, Any] = {"requested_at": utc_now()}
    try:
        if container_running(container):
            started = time.monotonic()
            container.stop(timeout=timeout)
            outcome["elapsed_seconds"] = time.monotonic() - started
        container.reload()
        outcome["exit_code"] = container.attrs.get("State", {}).get("ExitCode")
    except Exception as error:
        outcome["error"] = str(error)
    finally:
        try:
            container.remove(force=True, v=False)
        except docker.errors.NotFound:
            pass
    return outcome


def seed_volume_names(args: argparse.Namespace) -> tuple[str, str]:
    """返回迁移后 SQLite 和预热浏览器两个 seed volume 名称。"""
    prefix = resource_prefix(args)
    return f"{prefix}-config-seed", f"{prefix}-browser-seed"


def command_seed(args: argparse.Namespace) -> dict[str, Any]:
    """生成不含 app.env 的迁移后 SQLite 和预热浏览器 seed。"""
    client = require_docker_client()
    build = load_build_manifest(args)
    before_image = build["images"]["before"]["tag"]
    config_seed_name, browser_seed_name = seed_volume_names(args)
    if not args.browser_source_volume and not args.allow_browser_download:
        raise HarnessError(
            "seed 需要 --browser-source-volume，或显式 --allow-browser-download 执行一次预热"
        )
    if args.browser_source_volume:
        if get_volume(client, args.browser_source_volume) is None:
            raise HarnessError(
                f"浏览器来源 volume 不存在：{args.browser_source_volume}"
            )
    config_seed = prepare_volume(
        client, args, config_seed_name, "config-seed", args.replace
    )
    browser_seed = prepare_volume(
        client, args, browser_seed_name, "browser-seed", args.replace
    )
    if args.browser_source_volume:
        clone_volume(
            client,
            before_image,
            args.browser_source_volume,
            browser_seed.name,
        )
    before_browser = volume_fingerprint(client, before_image, browser_seed.name)
    if before_browser["files"] == 0 and not args.allow_browser_download:
        raise HarnessError("浏览器 seed 为空，且未允许一次性下载")

    network = ensure_internal_network(client, args)
    network_name = "bridge" if args.allow_browser_download else network.name
    container_name = f"{resource_prefix(args)}-seed"
    seed_dir = campaign_directory(args) / "seed"
    seed_dir.mkdir(parents=True, exist_ok=True)
    container = create_app_container(
        client,
        args,
        image=before_image,
        name=container_name,
        role="seed",
        config_volume=config_seed.name,
        browser_volume=browser_seed.name,
        network_name=network_name,
        output_dir=None,
    )
    started_at = time.monotonic()
    result: dict[str, Any] = {
        "schema_version": 1,
        "campaign": args.campaign,
        "generated_at": utc_now(),
        "image": before_image,
        "browser_source": (
            "named-volume" if args.browser_source_volume else "one-time-download"
        ),
        "browser_before": before_browser,
    }
    success = False
    try:
        container.start()
        ready_seconds = wait_for_ready(container, started_at, args.ready_timeout)
        settled_wait = wait_for_exec_success(
            container,
            ["/bin/sh", "-c", "test -e /var/log/nginx/__moviepilot__"],
            args.settle_timeout,
            "后台初始化完成标志",
        )
        assert_no_app_env(container)
        result.update(
            {
                "http_ready_seconds": ready_seconds,
                "settled_wait_seconds_after_ready": settled_wait,
                "engine_at_settled": capture_engine_stats(container),
            }
        )
        success = True
    except Exception as error:
        result["error"] = str(error)
        raise
    finally:
        save_container_logs(container, seed_dir / "container.log")
        result["shutdown"] = stop_and_remove_container(container, args.stop_timeout)
        if success:
            result["browser_after"] = volume_fingerprint(
                client, before_image, browser_seed.name
            )
        atomic_write_json(campaign_directory(args) / "seed.json", result)
        if not success:
            for volume in (config_seed, browser_seed):
                try:
                    volume.remove(force=True)
                except Exception:
                    pass
    print(f"Seed manifest: {campaign_directory(args) / 'seed.json'}")
    return result


def require_seed_volumes(client, args: argparse.Namespace) -> tuple[str, str]:
    """确认 seed 已完成且两个命名 volume 仍然存在。"""
    seed_path = campaign_directory(args) / "seed.json"
    if not seed_path.exists():
        raise HarnessError("缺少 seed.json，请先执行 seed")
    seed = json.loads(seed_path.read_text(encoding="utf-8"))
    if seed.get("error"):
        raise HarnessError("seed.json 记录了失败，必须重新生成 seed")
    names = seed_volume_names(args)
    for name in names:
        volume = get_volume(client, name)
        if volume is None:
            raise HarnessError(f"seed volume 不存在：{name}")
        assert_owned(volume, args, "seed volume")
    return names


def sample_volume_names(
    args: argparse.Namespace,
    variant: str,
    index: int,
) -> tuple[str, str]:
    """返回单个样本的隔离配置和浏览器卷名称。"""
    scenario = getattr(args, "scenario", DEFAULT_SCENARIO)
    scenario_segment = "" if scenario == DEFAULT_SCENARIO else f"-{scenario}"
    prefix = f"{resource_prefix(args)}{scenario_segment}-{variant}-{index}"
    return f"{prefix}-config", f"{prefix}-browser"


def sample_result_directory(
    args: argparse.Namespace,
    variant: str,
    index: int,
) -> Path:
    """返回单个样本的原始结果目录。"""
    scenario = getattr(args, "scenario", DEFAULT_SCENARIO)
    sample_root = campaign_directory(args) / "samples"
    if scenario == DEFAULT_SCENARIO:
        return sample_root / f"{variant}-{index}"
    return sample_root / scenario / f"{variant}-{index}"


def capture_activation_snapshot(
    container,
    output_dir: Path,
    phase: str,
) -> dict[str, Any]:
    """采集场景动作边界的 Engine、进程和进程内 import 状态。"""
    engine = capture_engine_stats(container)
    processes = capture_processes(container)
    modules = capture_modules(container, output_dir, processes["main_python"])
    return {
        "phase": phase,
        "captured_at": utc_now(),
        "engine": engine,
        "processes": processes,
        "modules": modules,
    }


def evaluate_browser_activation(
    scenario: str,
    pre: dict[str, Any],
    post: dict[str, Any],
    marker: dict[str, Any],
    expected_pid: Optional[int] = None,
) -> dict[str, Any]:
    """按场景不变量判断浏览器与 display 的真实激活是否有效。"""
    pre_xvfb = pre["processes"]["xvfb"]
    post_xvfb = post["processes"]["xvfb"]
    browser = marker.get("browser") or {}
    managed_resource = browser.get("managed_resource") or {}
    managed_before = managed_resource.get("before") or {}
    managed_after = managed_resource.get("after") or {}
    before_observations = managed_before.get("observations") or []
    after_observations = managed_after.get("observations") or []
    observation_prefix_matches = (
        after_observations[: len(before_observations)] == before_observations
    )
    new_observations = (
        after_observations[len(before_observations) :]
        if observation_prefix_matches
        else after_observations
    )
    display_starts = [
        item
        for item in new_observations
        if item.get("operation") == "activate" and item.get("outcome") == "started"
    ]
    display_successes = [
        item
        for item in new_observations
        if item.get("operation") == "activate" and item.get("outcome") == "succeeded"
    ]
    display_start_reasons = [item.get("reason") for item in display_starts]
    before_generation = (managed_before.get("snapshot") or {}).get("generation")
    after_generation = (managed_after.get("snapshot") or {}).get("generation")
    errors: list[str] = []
    if marker.get("scenario") != scenario:
        errors.append("进程内 marker 的场景与采集请求不一致")
    if expected_pid is not None and marker.get("pid") != expected_pid:
        errors.append("进程内 marker 不是目标 MoviePilot Python 进程写出")
    if not marker.get("success") or not browser.get("success"):
        errors.append("主 MoviePilot Python 进程未完成浏览器激活")
    if browser.get("retained_contexts") != 1:
        errors.append("激活后必须保留一个浏览器上下文供 post activation 采样")
    if not managed_before.get("available") or not managed_after.get("available"):
        errors.append("主进程未提供 host.display managed resource 观测")
    process_single_flight = browser.get("single_flight_probe") or {}

    single_flight = {
        "requested": scenario == "browser-headed",
        "concurrent_callers": int(process_single_flight.get("concurrent_callers") or 0),
        "successful_callers": int(process_single_flight.get("successful_callers") or 0),
        "xvfb_process_delta": int(post_xvfb["count"]) - int(pre_xvfb["count"]),
        "generation_before": before_generation,
        "generation_after": after_generation,
        "activation_start_count": len(display_starts),
        "activation_success_count": len(display_successes),
        "activation_start_reasons": display_start_reasons,
        "observation_prefix_matches": observation_prefix_matches,
        "passed": None,
    }
    if scenario == "browser-headless":
        if pre_xvfb["count"] != 0 or post_xvfb["count"] != 0:
            errors.append("headless 激活前后都不得存在 Xvfb")
        if display_starts or before_generation != after_generation:
            errors.append("headless 激活不得申请 host.display")
    elif scenario == "browser-headed":
        if pre_xvfb["count"] != 0:
            errors.append("headed 冷激活前必须没有 Xvfb")
        if post_xvfb["count"] != 1:
            errors.append("headed 并发激活后必须恰好存在一个 Xvfb")
        single_flight["passed"] = (
            single_flight["concurrent_callers"] == 2
            and single_flight["successful_callers"] == 2
            and single_flight["xvfb_process_delta"] == 1
            and single_flight["activation_start_count"] == 1
            and single_flight["activation_success_count"] == 1
            and single_flight["activation_start_reasons"] == ["headed_browser_launch"]
            and before_generation is not None
            and after_generation == before_generation + 1
        )
        if not single_flight["passed"]:
            errors.append("headed 并发请求未证明 display single-flight")
    else:
        errors.append(f"未知浏览器场景：{scenario}")

    return {
        "passed": not errors,
        "errors": errors,
        "expected": ("Xvfb 0→0" if scenario == "browser-headless" else "Xvfb 0→1"),
        "observed": {
            "pre_xvfb_count": pre_xvfb["count"],
            "pre_xvfb_pss_kib": pre_xvfb["pss_kib"],
            "post_xvfb_count": post_xvfb["count"],
            "post_xvfb_pss_kib": post_xvfb["pss_kib"],
        },
        "single_flight": single_flight,
    }


def _agent_prefix_counts(snapshot: dict[str, Any]) -> dict[str, int]:
    """从模块快照提取 PERF-003 Agent 重模块哨兵。"""
    counts = snapshot["modules"].get("prefix_counts") or {}
    return {
        prefix: int(counts.get(prefix) or 0) for prefix in AGENT_HEAVY_MODULE_PREFIXES
    }


def evaluate_agent_activation(
    scenario: str,
    pre: dict[str, Any],
    post: dict[str, Any],
    marker: dict[str, Any],
    expected_pid: Optional[int] = None,
) -> dict[str, Any]:
    """验证禁用态路由与首次工具目录的惰性物化不变量。"""
    agent = marker.get("agent") or {}
    observations = agent.get("observations") or {}
    runtime_before = observations.get("before") or {}
    runtime_after = observations.get("after") or {}
    prefix_before = _agent_prefix_counts(pre)
    prefix_after = _agent_prefix_counts(post)
    forbidden_before = {
        prefix: prefix_before[prefix]
        for prefix in AGENT_NONMATERIALIZATION_PREFIXES
        if prefix_before[prefix]
    }
    pre_xvfb = pre["processes"]["xvfb"]
    post_xvfb = post["processes"]["xvfb"]
    network_delta = {
        "rx_bytes": int(post["engine"]["network_rx_bytes"])
        - int(pre["engine"]["network_rx_bytes"]),
        "tx_bytes": int(post["engine"]["network_tx_bytes"])
        - int(pre["engine"]["network_tx_bytes"]),
    }
    errors: list[str] = []

    if marker.get("scenario") != scenario:
        errors.append("进程内 marker 的场景与采集请求不一致")
    if expected_pid is not None and marker.get("pid") != expected_pid:
        errors.append("进程内 marker 不是目标 MoviePilot Python 进程写出")
    if not marker.get("success") or not agent.get("success"):
        errors.append("主 MoviePilot Python 进程未完成 Agent 场景动作")
    if forbidden_before:
        errors.append("Agent 场景动作前已经加载必须延迟物化的模块")
    if pre_xvfb["count"] != 0 or post_xvfb["count"] != 0:
        errors.append("Agent 场景不得物化 Xvfb")
    if not runtime_before.get("available") or not runtime_after.get("available"):
        errors.append("主进程未提供轻量 Agent runtime 只读观测")
    if runtime_before.get("tool_factory_materialized") is not False:
        errors.append("Agent 场景动作前工具工厂必须未物化")
    if any(network_delta.values()):
        errors.append("Agent 场景动作产生了容器网络收发")

    revision = {"plugin": None, "factory": None}
    action_summary: dict[str, Any]
    if scenario == "agent-disabled-router":
        router = agent.get("router_openapi") or {}
        if router.get("ai_agent_enable") is not False:
            errors.append("router/OpenAPI 场景必须运行在 AI_AGENT_ENABLE=false")
        if router.get("missing_routes") or router.get("missing_openapi_paths"):
            errors.append("禁用态缺少 Agent 相关 router 或 OpenAPI path")
        forbidden_after = {
            prefix: prefix_after[prefix]
            for prefix in AGENT_NONMATERIALIZATION_PREFIXES
            if prefix_after[prefix]
        }
        if forbidden_after:
            errors.append("生成完整 OpenAPI 后加载了必须延迟物化的模块")
        if runtime_after.get("tool_factory_materialized") is not False:
            errors.append("生成完整 OpenAPI 不得物化工具工厂")
        action_summary = {
            "route_count": router.get("route_count"),
            "openapi_path_count": router.get("openapi_path_count"),
            "openapi_sha256": router.get("openapi_sha256"),
        }
    elif scenario == "agent-tool-catalog":
        catalog = agent.get("tool_catalog") or {}
        if prefix_after["app.agent.tools.factory"] < 1:
            errors.append("首次工具目录动作后未加载工具工厂")
        if prefix_after["app.agent.tools.impl"] < 1:
            errors.append("首次工具目录动作后未加载工具实现")
        allowed_prefixes = {
            *AGENT_TOOL_CATALOG_PREFIXES,
            *AGENT_SCHEMA_BASELINE_PREFIXES,
        }
        unexpected_prefixes = {
            prefix: count
            for prefix, count in prefix_after.items()
            if prefix not in allowed_prefixes and count
        }
        if unexpected_prefixes:
            errors.append("首次工具目录动作加载了非目录所需的 Agent/provider 重模块")
        if runtime_after.get("tool_factory_materialized") is not True:
            errors.append("首次工具目录动作后工具工厂未标记为已物化")
        if (
            not catalog.get("success")
            or not catalog.get("tool_count")
            or catalog.get("schema_count") != catalog.get("tool_count")
            or catalog.get("catalog_entry_count") != catalog.get("tool_count")
            or catalog.get("collision_names")
            or not catalog.get("schema_digests_complete")
            or not catalog.get("repeat_stable")
            or catalog.get("repeat_tool_count") != catalog.get("tool_count")
        ):
            errors.append("工具目录、JSON Schema 或重复读取稳定性不满足合同")
        if catalog.get("plugin_revision") is None or not catalog.get(
            "factory_revision"
        ):
            errors.append("工具目录缺少 plugin/factory revision")
        revision = {
            "plugin": catalog.get("plugin_revision"),
            "factory": catalog.get("factory_revision"),
        }
        action_summary = {
            "tool_count": catalog.get("tool_count"),
            "schema_count": catalog.get("schema_count"),
            "schemas_sha256": catalog.get("schemas_sha256"),
            "collision_names": catalog.get("collision_names") or [],
            "repeat_stable": catalog.get("repeat_stable"),
        }
    else:
        errors.append(f"未知 Agent 场景：{scenario}")
        action_summary = {}

    return {
        "passed": not errors,
        "errors": errors,
        "expected": (
            "router/OpenAPI 完整且必须延迟物化的模块保持 0"
            if scenario == "agent-disabled-router"
            else "首次工具目录后仅物化工具域及 Schema 基线"
        ),
        "observed": {
            "pre_xvfb_count": pre_xvfb["count"],
            "post_xvfb_count": post_xvfb["count"],
            "prefix_before": prefix_before,
            "prefix_after": prefix_after,
            "network_delta": network_delta,
            "tool_factory_materialized_before": runtime_before.get(
                "tool_factory_materialized"
            ),
            "tool_factory_materialized_after": runtime_after.get(
                "tool_factory_materialized"
            ),
        },
        "action": action_summary,
        "revision": revision,
    }


def evaluate_scenario_activation(
    scenario: str,
    pre: dict[str, Any],
    post: dict[str, Any],
    marker: dict[str, Any],
    expected_pid: Optional[int] = None,
) -> dict[str, Any]:
    """按场景族分派外部采样验收。"""
    if scenario in BROWSER_SCENARIOS:
        return evaluate_browser_activation(
            scenario,
            pre,
            post,
            marker,
            expected_pid=expected_pid,
        )
    if scenario in AGENT_SCENARIOS:
        return evaluate_agent_activation(
            scenario,
            pre,
            post,
            marker,
            expected_pid=expected_pid,
        )
    raise HarnessError(f"未知激活场景：{scenario}")


def activate_sample_scenario(
    container,
    output_dir: Path,
    scenario: str,
    timeout: float,
) -> dict[str, Any]:
    """通过 SIGUSR2 让目标 MoviePilot 解释器执行动作并回收 marker。"""
    pre = capture_activation_snapshot(container, output_dir, "pre-activation")
    main_python = pre["processes"]["main_python"]
    if not main_python:
        raise HarnessError("未找到主 Python 进程，无法触发测量场景")
    marker_path = output_dir / "modules" / f"activation-{main_python['pid']}.json"
    marker_path.unlink(missing_ok=True)

    requested_at = time.monotonic()
    result = container.exec_run(["kill", "-USR2", str(main_python["pid"])])
    if result.exit_code != 0:
        raise HarnessError("向主 Python 进程发送场景激活信号失败")
    deadline = requested_at + timeout
    while time.monotonic() < deadline:
        if marker_path.exists():
            break
        if not container_running(container):
            raise HarnessError("等待场景激活 marker 时容器提前退出")
        time.sleep(0.05)
    if not marker_path.exists():
        raise HarnessError(f"测量场景动作在 {timeout:.0f}s 内未完成")

    marker_received_at = time.monotonic()
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    post = capture_activation_snapshot(container, output_dir, "post-activation")
    validation = evaluate_scenario_activation(
        scenario,
        pre,
        post,
        marker,
        expected_pid=main_python["pid"],
    )
    return {
        "scenario": scenario,
        "trigger": "SIGUSR2-to-main-python",
        "main_python_pid": main_python["pid"],
        "orchestrator_elapsed_seconds": marker_received_at - requested_at,
        "post_capture_elapsed_seconds": time.monotonic() - marker_received_at,
        "worker_elapsed_seconds": marker.get("elapsed_seconds"),
        "pre": pre,
        "post": post,
        "marker": marker,
        "validation": validation,
    }


def command_sample(args: argparse.Namespace) -> dict[str, Any]:
    """执行一个隔离样本并在约定时间点采集完整指标。"""
    scenario = getattr(args, "scenario", DEFAULT_SCENARIO)
    if scenario != DEFAULT_SCENARIO and args.variant != "after":
        raise HarnessError("非默认场景只用于验证包含候选公共 API 的 After 版本")
    client = require_docker_client()
    build = load_build_manifest(args)
    config_seed, browser_seed = require_seed_volumes(client, args)
    image = build["images"][args.variant]["tag"]
    output_dir = sample_result_directory(args, args.variant, args.index)
    if output_dir.exists():
        if not args.replace:
            raise HarnessError(
                f"样本结果已存在：{output_dir}；如需重测请使用 --replace"
            )
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True)

    config_volume_name, browser_volume_name = sample_volume_names(
        args, args.variant, args.index
    )
    config_volume = prepare_volume(
        client, args, config_volume_name, "sample-config", replace=True
    )
    browser_volume = prepare_volume(
        client, args, browser_volume_name, "sample-browser", replace=True
    )
    clone_volume(client, image, config_seed, config_volume.name)
    clone_volume(client, image, browser_seed, browser_volume.name)
    browser_before = volume_fingerprint(client, image, browser_volume.name)
    network = ensure_internal_network(client, args)
    scenario_segment = "" if scenario == DEFAULT_SCENARIO else f"-{scenario}"
    container_name = (
        f"{resource_prefix(args)}{scenario_segment}-{args.variant}-{args.index}"
    )
    role = (
        f"sample-{args.variant}-{args.index}"
        if scenario == DEFAULT_SCENARIO
        else f"sample-{scenario}-{args.variant}-{args.index}"
    )
    container = create_app_container(
        client,
        args,
        image=image,
        name=container_name,
        role=role,
        config_volume=config_volume.name,
        browser_volume=browser_volume.name,
        network_name=network.name,
        output_dir=output_dir,
    )
    result: dict[str, Any] = {
        "schema_version": 1,
        "campaign": args.campaign,
        "variant": args.variant,
        "sample_index": args.index,
        "scenario": scenario,
        "source_commit": build[f"{args.variant}_commit"],
        "image": image,
        "started_at": utc_now(),
        "points_minutes": args.points,
        "resources": {
            "cpus": args.cpus,
            "memory": args.memory,
            "network": "internal",
            "database": "sqlite-seed-clone",
            "browser": "prewarmed-seed-clone",
            "scenario": scenario,
        },
        "browser_before": browser_before,
        "measurements": [],
    }
    started_at = time.monotonic()
    try:
        container.start()
        ready_seconds = wait_for_ready(container, started_at, args.ready_timeout)
        ready_at = time.monotonic()
        wait_for_exec_success(
            container,
            ["/bin/sh", "-c", "test -e /var/log/nginx/__moviepilot__"],
            args.settle_timeout,
            "后台初始化完成标志",
        )
        settled_at = time.monotonic()
        assert_no_app_env(container)
        result["http_ready_seconds"] = ready_seconds
        result["settled_seconds"] = settled_at - started_at
        result["settled_wait_seconds_after_ready"] = settled_at - ready_at
        measurement_origin_at = settled_at

        if scenario != DEFAULT_SCENARIO:
            activation = activate_sample_scenario(
                container,
                output_dir,
                scenario,
                args.activation_timeout,
            )
            result["activation"] = activation
            atomic_write_json(output_dir / "result.partial.json", result)
            if not activation["validation"]["passed"]:
                details = "; ".join(activation["validation"]["errors"])
                raise HarnessError(f"{scenario} 场景激活不满足验收条件：{details}")
            measurement_origin_at = time.monotonic()
            result["measurement_origin"] = "post-activation"
        else:
            result["measurement_origin"] = "settled"

        for point in args.points:
            deadline = measurement_origin_at + point * 60
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(remaining)
            if not container_running(container):
                raise HarnessError(f"容器在 {point:g}m 采样前退出")
            print(f"[{args.variant}-{args.index}] sampling {point:g}m")
            result["measurements"].append(
                capture_measurement(
                    container,
                    output_dir,
                    point,
                    measurement_origin_at,
                )
            )
            atomic_write_json(output_dir / "result.partial.json", result)
        assert_no_app_env(container)
    except Exception as error:
        result["error"] = str(error)
        raise
    finally:
        save_container_logs(container, output_dir / "container.log")
        result["shutdown"] = stop_and_remove_container(container, args.stop_timeout)
        try:
            result["browser_after"] = volume_fingerprint(
                client, image, browser_volume.name
            )
        except Exception as error:
            result["browser_after_error"] = str(error)
        result["completed_at"] = utc_now()
        atomic_write_json(output_dir / "result.json", result)
        partial = output_dir / "result.partial.json"
        partial.unlink(missing_ok=True)
        for volume in (config_volume, browser_volume):
            try:
                volume.remove(force=True)
            except Exception:
                pass
        update_aggregate_results(args)
    return result


def load_sample_results(args: argparse.Namespace) -> list[dict[str, Any]]:
    """读取当前 campaign 已完成或失败的所有样本结果。"""
    sample_root = campaign_directory(args) / "samples"
    results = []
    if not sample_root.exists():
        return results
    for path in sorted(sample_root.rglob("result.json")):
        results.append(json.loads(path.read_text(encoding="utf-8")))
    return results


def median(values: Iterable[float]) -> Optional[float]:
    """空序列返回 None，否则返回浮点中位数。"""
    items = list(values)
    return statistics.median(items) if items else None


def measurement_at(result: dict[str, Any], minute: float) -> Optional[dict[str, Any]]:
    """按浮点容差返回目标采样点。"""
    for measurement in result.get("measurements") or []:
        if abs(float(measurement["target_minute"]) - minute) < 1e-9:
            return measurement
    return None


def format_mib(value: Optional[float]) -> str:
    """把字节数格式化为 MiB。"""
    if value is None:
        return "—"
    return f"{value / 1024 / 1024:.1f}"


def format_kib_as_mib(value: Optional[float]) -> str:
    """把 KiB 数格式化为 MiB。"""
    if value is None:
        return "—"
    return f"{value / 1024:.1f}"


def format_bytes_as_kib(value: Optional[float]) -> str:
    """把字节数格式化为 KiB。"""
    if value is None:
        return "—"
    return f"{value / 1024:.1f}"


def build_markdown_report(
    build: dict[str, Any],
    seed: Optional[dict[str, Any]],
    samples: list[dict[str, Any]],
) -> str:
    """生成不含本机路径和凭据的 Markdown 汇总。"""
    scenarios = sorted(
        {sample.get("scenario", DEFAULT_SCENARIO) for sample in samples}
    ) or [DEFAULT_SCENARIO]
    show_scenario = any(scenario != DEFAULT_SCENARIO for scenario in scenarios)
    points = sorted(
        {
            float(measurement["target_minute"])
            for sample in samples
            for measurement in sample.get("measurements") or []
        }
    )
    lines = [
        "# MoviePilot Docker A/B 测量结果",
        "",
        f"- Campaign：`{build['campaign']}`",
        f"- Platform：`{build['platform']}`",
        f"- Before：`{build['before_commit']}`",
        f"- After：`{build['after_commit']}`",
        f"- Substrate：`{build['substrate']['reference']}`",
        "- working set：`memory.current - inactive_file`",
        "- 配置：迁移后 SQLite seed、空插件配置、Agent 关闭、浏览器缓存预热、internal network",
        "",
    ]
    if seed:
        lines.extend(
            [
                "## Seed",
                "",
                f"- 浏览器来源：`{seed.get('browser_source', 'unknown')}`",
                f"- HTTP ready：{seed.get('http_ready_seconds', 0):.2f}s",
                f"- 浏览器文件数：{seed.get('browser_after', {}).get('files', 0)}",
                "",
            ]
        )

    headers = (
        (["场景"] if show_scenario else [])
        + ["版本", "样本", "HTTP ready(s)"]
        + [f"{point:g}m WS(MiB)" for point in points]
        + [
            "末次 Python PSS(MiB)",
            "末次 Python USS(MiB)",
            "Python Threads",
            "末次 Xvfb PSS(MiB)",
            "RX(KiB)",
            "TX(KiB)",
            "sys.modules",
            "状态",
        ]
    )
    lines.extend(["## 原始样本", "", "| " + " | ".join(headers) + " |"])
    lines.append("| " + " | ".join(["---"] * len(headers)) + " |")
    variant_order = {"before": 0, "after": 1}
    for sample in sorted(
        samples,
        key=lambda item: (
            item.get("scenario", DEFAULT_SCENARIO),
            variant_order.get(item["variant"], 99),
            item["sample_index"],
        ),
    ):
        row = ([sample.get("scenario", DEFAULT_SCENARIO)] if show_scenario else []) + [
            sample["variant"],
            str(sample["sample_index"]),
            f"{sample.get('http_ready_seconds', 0):.2f}"
            if "http_ready_seconds" in sample
            else "—",
        ]
        for point in points:
            target = measurement_at(sample, point)
            row.append(
                format_mib(target["engine"]["working_set_bytes"] if target else None)
            )
        final = (
            sample.get("measurements", [])[-1] if sample.get("measurements") else None
        )
        python_pss = (
            final.get("processes", {}).get("main_python", {}).get("pss_kib")
            if final and final.get("processes", {}).get("main_python")
            else None
        )
        python_uss = (
            final.get("processes", {}).get("main_python", {}).get("uss_kib")
            if final and final.get("processes", {}).get("main_python")
            else None
        )
        python_threads = (
            final.get("processes", {}).get("main_python", {}).get("threads")
            if final and final.get("processes", {}).get("main_python")
            else None
        )
        xvfb_pss = (
            final.get("processes", {}).get("xvfb", {}).get("pss_kib") if final else None
        )
        row.extend(
            [
                format_kib_as_mib(python_pss),
                format_kib_as_mib(python_uss),
                str(python_threads) if python_threads is not None else "—",
                format_kib_as_mib(xvfb_pss),
                format_bytes_as_kib(
                    final.get("engine", {}).get("network_rx_bytes") if final else None
                ),
                format_bytes_as_kib(
                    final.get("engine", {}).get("network_tx_bytes") if final else None
                ),
                str(final.get("modules", {}).get("count")) if final else "—",
                "失败" if sample.get("error") else "完成",
            ]
        )
        lines.append("| " + " | ".join(row) + " |")

    browser_activated_samples = [
        sample
        for sample in samples
        if sample.get("activation")
        and sample.get("scenario", DEFAULT_SCENARIO) in BROWSER_SCENARIOS
    ]
    if browser_activated_samples:
        activation_headers = [
            "场景",
            "版本",
            "样本",
            "激活(s)",
            "Pre WS(MiB)",
            "Post WS(MiB)",
            "Pre Python PSS(MiB)",
            "Post Python PSS(MiB)",
            "Pre Xvfb",
            "Post Xvfb",
            "Post Xvfb PSS(MiB)",
            "Activation RX Δ(KiB)",
            "Activation TX Δ(KiB)",
            "Browser",
            "Single-flight generation/start",
            "验收",
        ]
        lines.extend(
            [
                "",
                "## 场景激活",
                "",
                "| " + " | ".join(activation_headers) + " |",
                "| " + " | ".join(["---"] * len(activation_headers)) + " |",
            ]
        )
        for sample in sorted(
            browser_activated_samples,
            key=lambda item: (
                item.get("scenario", DEFAULT_SCENARIO),
                variant_order.get(item["variant"], 99),
                item["sample_index"],
            ),
        ):
            activation = sample["activation"]
            pre = activation["pre"]
            post = activation["post"]
            marker = activation["marker"]
            validation = activation["validation"]
            pre_python = pre["processes"].get("main_python") or {}
            post_python = post["processes"].get("main_python") or {}
            single_flight = validation["single_flight"]
            activation_row = [
                sample.get("scenario", DEFAULT_SCENARIO),
                sample["variant"],
                str(sample["sample_index"]),
                f"{float(activation.get('worker_elapsed_seconds') or 0):.2f}",
                format_mib(pre["engine"]["working_set_bytes"]),
                format_mib(post["engine"]["working_set_bytes"]),
                format_kib_as_mib(pre_python.get("pss_kib")),
                format_kib_as_mib(post_python.get("pss_kib")),
                str(pre["processes"]["xvfb"]["count"]),
                str(post["processes"]["xvfb"]["count"]),
                format_kib_as_mib(post["processes"]["xvfb"]["pss_kib"]),
                format_bytes_as_kib(
                    post["engine"]["network_rx_bytes"]
                    - pre["engine"]["network_rx_bytes"]
                ),
                format_bytes_as_kib(
                    post["engine"]["network_tx_bytes"]
                    - pre["engine"]["network_tx_bytes"]
                ),
                "成功" if marker.get("success") else "失败",
                (
                    f"{single_flight.get('generation_after')}/"
                    f"{single_flight.get('activation_start_count')}"
                    if single_flight.get("passed") is True
                    else "不适用"
                    if single_flight.get("passed") is None
                    else "失败"
                ),
                "通过" if validation["passed"] else "失败",
            ]
            lines.append("| " + " | ".join(activation_row) + " |")

    agent_activated_samples = [
        sample
        for sample in samples
        if sample.get("activation")
        and sample.get("scenario", DEFAULT_SCENARIO) in AGENT_SCENARIOS
    ]
    if agent_activated_samples:
        agent_headers = [
            "场景",
            "样本",
            "动作(s)",
            "Pre/Post WS(MiB)",
            "Pre/Post Python PSS(MiB)",
            "Pre/Post sys.modules",
            "Factory observation",
            "模块哨兵 Pre",
            "模块哨兵 Post",
            "Router/OpenAPI 或 Tools/Schemas",
            "Plugin/Factory revision",
            "Action RX/TX Δ(KiB)",
            "验收",
        ]
        lines.extend(
            [
                "",
                "## Agent 场景动作",
                "",
                "| " + " | ".join(agent_headers) + " |",
                "| " + " | ".join(["---"] * len(agent_headers)) + " |",
            ]
        )

        def format_prefix_counts(counts: dict[str, int]) -> str:
            """仅展开已加载前缀，全部未加载时输出明确零状态。"""
            loaded = [f"{prefix}={count}" for prefix, count in counts.items() if count]
            return ", ".join(loaded) if loaded else "全部 0"

        for sample in sorted(
            agent_activated_samples,
            key=lambda item: (
                item.get("scenario", DEFAULT_SCENARIO),
                item["sample_index"],
            ),
        ):
            activation = sample["activation"]
            pre = activation["pre"]
            post = activation["post"]
            validation = activation["validation"]
            observed = validation["observed"]
            action = validation["action"]
            revision = validation["revision"]
            pre_python = pre["processes"].get("main_python") or {}
            post_python = post["processes"].get("main_python") or {}
            if sample.get("scenario") == "agent-disabled-router":
                action_result = (
                    f"{action.get('route_count')}/{action.get('openapi_path_count')}"
                )
            else:
                action_result = (
                    f"{action.get('tool_count')}/{action.get('schema_count')}; "
                    f"repeat={'Y' if action.get('repeat_stable') else 'N'}; "
                    f"collision={len(action.get('collision_names') or [])}"
                )
            factory_revision = str(revision.get("factory") or "")
            revision_result = (
                f"{revision.get('plugin')}/{factory_revision[:12]}"
                if factory_revision
                else "不适用"
            )
            agent_row = [
                sample.get("scenario", DEFAULT_SCENARIO),
                str(sample["sample_index"]),
                f"{float(activation.get('worker_elapsed_seconds') or 0):.2f}",
                f"{format_mib(pre['engine']['working_set_bytes'])}/"
                f"{format_mib(post['engine']['working_set_bytes'])}",
                f"{format_kib_as_mib(pre_python.get('pss_kib'))}/"
                f"{format_kib_as_mib(post_python.get('pss_kib'))}",
                f"{pre['modules'].get('count')}/{post['modules'].get('count')}",
                (
                    f"{observed.get('tool_factory_materialized_before')}→"
                    f"{observed.get('tool_factory_materialized_after')}"
                ),
                format_prefix_counts(observed.get("prefix_before") or {}),
                format_prefix_counts(observed.get("prefix_after") or {}),
                action_result,
                revision_result,
                (
                    f"{format_bytes_as_kib(post['engine']['network_rx_bytes'] - pre['engine']['network_rx_bytes'])}/"
                    f"{format_bytes_as_kib(post['engine']['network_tx_bytes'] - pre['engine']['network_tx_bytes'])}"
                ),
                "通过" if validation["passed"] else "失败",
            ]
            lines.append("| " + " | ".join(agent_row) + " |")

    sentinel_samples = [sample for sample in samples if sample.get("measurements")]
    if sentinel_samples:
        lines.extend(
            [
                "",
                "## Agent 模块哨兵",
                "",
                "每行记录该样本所有定时采样点的最大模块数；精确时间点数据保留在 JSON。",
                "`langchain` 与 `langchain_core` 只记录 Schema 基线，不参与归零门禁。",
                "",
                "| 场景 | 版本 | 样本 | 重模块峰值 |",
                "| --- | --- | --- | --- |",
            ]
        )
        for sample in sorted(
            sentinel_samples,
            key=lambda item: (
                item.get("scenario", DEFAULT_SCENARIO),
                variant_order.get(item["variant"], 99),
                item["sample_index"],
            ),
        ):
            peaks = {
                prefix: max(
                    int(
                        measurement.get("modules", {})
                        .get("prefix_counts", {})
                        .get(prefix, 0)
                    )
                    for measurement in sample["measurements"]
                )
                for prefix in AGENT_HEAVY_MODULE_PREFIXES
            }
            peak_text = (
                ", ".join(
                    f"{prefix}={count}" for prefix, count in peaks.items() if count
                )
                or "全部 0"
            )
            lines.append(
                f"| {sample.get('scenario', DEFAULT_SCENARIO)} | "
                f"{sample['variant']} | {sample['sample_index']} | {peak_text} |"
            )

    lines.extend(["", "## 中位数对照", ""])
    for scenario in scenarios:
        scenario_samples = [
            sample
            for sample in samples
            if sample.get("scenario", DEFAULT_SCENARIO) == scenario
        ]
        if show_scenario:
            lines.extend([f"### `{scenario}`", ""])
        if points:
            lines.append("| 时间点 | Before(MiB) | After(MiB) | 净差(MiB) | 变化 |")
            lines.append("| --- | ---: | ---: | ---: | ---: |")
            for point in points:
                before_values = [
                    measurement_at(sample, point)["engine"]["working_set_bytes"]
                    for sample in scenario_samples
                    if sample["variant"] == "before" and measurement_at(sample, point)
                ]
                after_values = [
                    measurement_at(sample, point)["engine"]["working_set_bytes"]
                    for sample in scenario_samples
                    if sample["variant"] == "after" and measurement_at(sample, point)
                ]
                before_median = median(before_values)
                after_median = median(after_values)
                if before_median is None or after_median is None:
                    lines.append(f"| {point:g}m | — | — | — | — |")
                    continue
                delta = after_median - before_median
                percent = delta / before_median * 100 if before_median else 0
                lines.append(
                    f"| {point:g}m | {format_mib(before_median)} | "
                    f"{format_mib(after_median)} | {delta / 1024 / 1024:.1f} | "
                    f"{percent:.1f}% |"
                )
            lines.append("")

    lines.extend(["## 启动时间", ""])
    for scenario in scenarios:
        scenario_samples = [
            sample
            for sample in samples
            if sample.get("scenario", DEFAULT_SCENARIO) == scenario
        ]
        ready_before = median(
            sample["http_ready_seconds"]
            for sample in scenario_samples
            if sample["variant"] == "before" and "http_ready_seconds" in sample
        )
        ready_after = median(
            sample["http_ready_seconds"]
            for sample in scenario_samples
            if sample["variant"] == "after" and "http_ready_seconds" in sample
        )
        scenario_prefix = f"`{scenario}`：" if show_scenario else ""
        if ready_before is not None and ready_after is not None:
            startup_change = (
                (ready_after - ready_before) / ready_before * 100 if ready_before else 0
            )
            lines.append(
                f"{scenario_prefix}Before 中位数 {ready_before:.2f}s，"
                f"After 中位数 {ready_after:.2f}s，变化 {startup_change:.1f}%。"
            )
        else:
            lines.append(f"{scenario_prefix}样本尚不完整。")
    lines.extend(
        [
            "",
            "## 说明",
            "",
            "- 完整 Engine、进程、线程、网络和模块前缀数据见 `results.json`。",
            "- 每个 `sys.modules` 完整名称清单位于对应样本的 `modules/` 目录。",
            "- 报告不包含 app.env、真实 token、密码或本机挂载路径。",
            "",
        ]
    )
    return "\n".join(lines)


def update_aggregate_results(args: argparse.Namespace) -> None:
    """汇总当前 campaign 的 build、seed 和所有样本。"""
    campaign_dir = campaign_directory(args)
    build_path = campaign_dir / "build.json"
    if not build_path.exists():
        return
    build = json.loads(build_path.read_text(encoding="utf-8"))
    seed_path = campaign_dir / "seed.json"
    seed = (
        json.loads(seed_path.read_text(encoding="utf-8"))
        if seed_path.exists()
        else None
    )
    samples = load_sample_results(args)
    aggregate = {
        "schema_version": 1,
        "generated_at": utc_now(),
        "build": build,
        "seed": seed,
        "samples": samples,
    }
    atomic_write_json(campaign_dir / "results.json", aggregate)
    write_text(
        campaign_dir / "report.md",
        build_markdown_report(build, seed, samples),
    )


def cleanup_runtime_resources(client, args: argparse.Namespace) -> dict[str, list[str]]:
    """仅按 campaign 标签清理容器、volume 和 internal network。"""
    removed: dict[str, list[str]] = {"containers": [], "volumes": [], "networks": []}
    label_filter = f"{CAMPAIGN_LABEL}={args.campaign}"
    for container in client.containers.list(all=True, filters={"label": label_filter}):
        name = container.name
        container.remove(force=True, v=False)
        removed["containers"].append(name)
    for volume in client.volumes.list(filters={"label": label_filter}):
        name = volume.name
        volume.remove(force=True)
        removed["volumes"].append(name)
    for network in client.networks.list(filters={"label": label_filter}):
        name = network.name
        try:
            network.remove()
            removed["networks"].append(name)
        except Exception as error:
            raise HarnessError(f"清理 network {name} 失败：{error}") from error
    return removed


def command_cleanup(args: argparse.Namespace) -> dict[str, Any]:
    """清理当前 campaign 的 Docker 资源，保留本地结果文件。"""
    client = require_docker_client()
    removed: dict[str, Any] = cleanup_runtime_resources(client, args)
    removed["images"] = []
    if args.images:
        build_path = campaign_directory(args) / "build.json"
        if build_path.exists():
            build = json.loads(build_path.read_text(encoding="utf-8"))
            for variant in ("before", "after"):
                tag = build.get("images", {}).get(variant, {}).get("tag")
                if not tag:
                    continue
                try:
                    image = client.images.get(tag)
                except docker.errors.ImageNotFound:
                    continue
                image_labels = image.attrs.get("Config", {}).get("Labels") or {}
                if image_labels.get(CAMPAIGN_LABEL) != args.campaign:
                    raise HarnessError(f"拒绝删除非本 campaign 镜像：{tag}")
                client.images.remove(tag, force=False, noprune=True)
                removed["images"].append(tag)
    atomic_write_json(campaign_directory(args) / "cleanup.json", removed)
    print(json.dumps(removed, ensure_ascii=False, indent=2))
    return removed


def command_run(args: argparse.Namespace) -> None:
    """构建、生成 seed，并按平衡顺序串行执行三组 Before/After。"""
    command_build(args)
    command_seed(args)
    completed = False
    try:
        for variant, index in BALANCED_RUN_ORDER:
            sample_args = argparse.Namespace(**vars(args))
            sample_args.variant = variant
            sample_args.index = index
            sample_args.replace = args.replace
            command_sample(sample_args)
        completed = True
    finally:
        update_aggregate_results(args)
        if not args.keep_resources:
            client = require_docker_client()
            cleanup_runtime_resources(client, args)
    if completed:
        print(f"Results: {campaign_directory(args) / 'results.json'}")
        print(f"Report: {campaign_directory(args) / 'report.md'}")


def add_common_build_arguments(parser: argparse.ArgumentParser) -> None:
    """添加 build 与 run 共用的 ref 和 substrate 参数。"""
    parser.add_argument("--before-ref", default="upstream/v3", help="Before Git ref")
    parser.add_argument("--after-ref", default="HEAD", help="After Git ref")
    parser.add_argument(
        "--pull-substrate",
        action="store_true",
        help="显式从镜像仓库拉取冻结 substrate",
    )


def add_seed_arguments(parser: argparse.ArgumentParser) -> None:
    """添加 seed 与 run 共用的浏览器缓存参数。"""
    browser_group = parser.add_mutually_exclusive_group(required=False)
    browser_group.add_argument(
        "--browser-source-volume",
        help=(
            "从已有命名 volume 复制 CloakBrowser 缓存，不访问外网；"
            f"默认 {DEFAULT_BROWSER_SOURCE_VOLUME}"
        ),
    )
    browser_group.add_argument(
        "--allow-browser-download",
        action="store_true",
        help="允许 seed 阶段一次性联网下载 CloakBrowser；样本仍使用 internal network",
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="替换同 campaign 的 seed 或样本结果",
    )


def build_parser() -> argparse.ArgumentParser:
    """构建命令行解析器。"""
    parser = argparse.ArgumentParser(
        description="MoviePilot V3 reproducible Docker A/B measurement harness"
    )
    parser.add_argument("--campaign", type=normalize_campaign, required=True)
    parser.add_argument("--repo", type=Path, default=PROJECT_ROOT)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(tempfile.gettempdir()) / "moviepilot-perf-results",
        help="结果根目录，默认位于系统临时目录",
    )
    parser.add_argument("--substrate", default=DEFAULT_SUBSTRATE)
    parser.add_argument(
        "--platform",
        default="auto",
        help="Docker 平台，默认使用 daemon 原生架构；正式数据禁止 QEMU 跨架构",
    )
    parser.add_argument("--cpus", type=float, default=4.0)
    parser.add_argument("--memory", default="2g")
    parser.add_argument("--ready-timeout", type=int, default=300)
    parser.add_argument("--settle-timeout", type=int, default=300)
    parser.add_argument(
        "--activation-timeout",
        type=int,
        default=180,
        help="进程内场景激活完成 marker 的等待秒数",
    )
    parser.add_argument("--stop-timeout", type=int, default=120)

    subparsers = parser.add_subparsers(dest="command", required=True)
    build = subparsers.add_parser(
        "build", help="构建冻结 substrate 的 Before/After overlay 镜像"
    )
    add_common_build_arguments(build)

    seed = subparsers.add_parser(
        "seed", help="创建迁移后 SQLite 和预热浏览器 seed volume"
    )
    add_seed_arguments(seed)

    sample = subparsers.add_parser("sample", help="执行一个 Before 或 After 样本")
    sample.add_argument("--variant", choices=("before", "after"), required=True)
    sample.add_argument("--index", type=int, choices=(1, 2, 3), required=True)
    sample.add_argument(
        "--points", type=parse_points, default=parse_points("1,5,10,30")
    )
    sample.add_argument(
        "--scenario",
        choices=SCENARIOS,
        default=DEFAULT_SCENARIO,
        help="样本场景；默认保持 PERF-001 idle-default 行为",
    )
    sample.add_argument("--replace", action="store_true")

    run = subparsers.add_parser("run", help="完整执行 build、seed 和三组平衡 A/B")
    add_common_build_arguments(run)
    add_seed_arguments(run)
    run.add_argument("--points", type=parse_points, default=parse_points("1,5,10,30"))
    run.add_argument(
        "--keep-resources",
        action="store_true",
        help="完成或失败后保留 seed volume 和 internal network 供诊断",
    )

    cleanup = subparsers.add_parser("cleanup", help="清理当前 campaign 的 Docker 资源")
    cleanup.add_argument(
        "--images", action="store_true", help="同时移除 Before/After 派生镜像"
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    """解析参数并执行选定阶段。"""
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command in {"seed", "run"}:
        if not args.browser_source_volume and not args.allow_browser_download:
            args.browser_source_volume = DEFAULT_BROWSER_SOURCE_VOLUME
    if args.cpus <= 0:
        parser.error("--cpus 必须大于 0")
    if (
        args.ready_timeout <= 0
        or args.settle_timeout <= 0
        or args.activation_timeout <= 0
        or args.stop_timeout <= 0
    ):
        parser.error("timeout 必须大于 0")
    try:
        if args.command == "build":
            command_build(args)
        elif args.command == "seed":
            command_seed(args)
        elif args.command == "sample":
            command_sample(args)
        elif args.command == "run":
            command_run(args)
        elif args.command == "cleanup":
            command_cleanup(args)
        else:  # pragma: no cover - argparse 已保证不可达
            parser.error(f"未知命令：{args.command}")
    except HarnessError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
