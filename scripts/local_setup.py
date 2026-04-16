#!/usr/bin/env python3

from __future__ import annotations

import argparse
import getpass
import json
import os
import platform
import secrets
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Optional


ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = ROOT / "config"
LOG_DIR = CONFIG_DIR / "logs"
CACHE_DIR = CONFIG_DIR / "cache"
TEMP_DIR = CONFIG_DIR / "temp"
COOKIE_DIR = CONFIG_DIR / "cookies"
HELPER_DIR = ROOT / "app" / "helper"
ENV_FILE = CONFIG_DIR / "app.env"
PUBLIC_DIR = ROOT / "public"
RUNTIME_DIR = ROOT / ".runtime"
NODE_DIR = RUNTIME_DIR / "node"

DEFAULT_NODE_VERSION = "20.12.1"
FRONTEND_LATEST_API = "https://api.github.com/repos/jxxghp/MoviePilot-Frontend/releases/latest"
FRONTEND_TAG_API = "https://api.github.com/repos/jxxghp/MoviePilot-Frontend/releases/tags/{tag}"
RESOURCES_MAIN_ZIP = "https://github.com/jxxghp/MoviePilot-Resources/archive/refs/heads/main.zip"
RUNTIME_PACKAGE = {
    "name": "moviepilot-frontend-runtime",
    "private": True,
    "license": "UNLICENSED",
    "dependencies": {
        "express": "^4.18.2",
        "express-http-proxy": "^2.0.0",
    },
}
NOTIFICATION_SWITCH_TYPES = [
    "资源下载",
    "整理入库",
    "订阅",
    "站点",
    "媒体服务器",
    "手动处理",
    "插件",
    "智能体",
    "其它",
]


def print_step(message: str) -> None:
    print(f"==> {message}")


def run(command: list[str], cwd: Optional[Path] = None) -> None:
    pretty = " ".join(command)
    print(f"+ {pretty}")
    subprocess.run(command, cwd=str(cwd or ROOT), check=True)


def capture(command: list[str], cwd: Optional[Path] = None) -> str:
    return subprocess.check_output(command, cwd=str(cwd or ROOT), text=True).strip()


def command_exists(name: str) -> bool:
    return shutil.which(name) is not None


def get_venv_python(venv_dir: Path) -> Path:
    if os.name == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def ensure_supported_python(python_bin: str) -> None:
    version_json = capture([python_bin, "-c", "import json, sys; print(json.dumps(list(sys.version_info[:3])))"])
    version = tuple(json.loads(version_json))
    if version < (3, 12, 0):
        raise RuntimeError(
            f"MoviePilot 本地安装需要 Python 3.12 或更高版本，当前解释器为 {python_bin} "
            f"({version[0]}.{version[1]}.{version[2]})"
        )


def ensure_local_dirs() -> None:
    for path in (CONFIG_DIR, LOG_DIR, CACHE_DIR, TEMP_DIR, COOKIE_DIR, RUNTIME_DIR):
        path.mkdir(parents=True, exist_ok=True)


def _load_env_lines() -> list[str]:
    if not ENV_FILE.exists():
        return []
    return ENV_FILE.read_text(encoding="utf-8").splitlines(keepends=True)


def read_env_value(key: str) -> Optional[str]:
    for line in _load_env_lines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            continue
        env_key, value = line.split("=", 1)
        if env_key.strip() == key:
            return value.strip().strip('"').strip("'")
    return None


def write_env_value(key: str, value: str) -> None:
    ensure_local_dirs()
    lines = _load_env_lines()
    new_line = f"{key}={json.dumps(str(value), ensure_ascii=False)}\n"

    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            continue
        env_key, _ = line.split("=", 1)
        if env_key.strip() == key:
            lines[index] = new_line
            break
    else:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.append(new_line)

    ENV_FILE.write_text("".join(lines), encoding="utf-8")


def ensure_api_token(force_token: bool = False, token: Optional[str] = None) -> str:
    ensure_local_dirs()
    current_token = read_env_value("API_TOKEN") or ""
    if token is not None:
        token = str(token).strip()
        if len(token) < 16:
            raise ValueError("API_TOKEN 长度不能少于 16 个字符")
        write_env_value("API_TOKEN", token)
        print_step(f"已写入 API_TOKEN 到 {ENV_FILE}")
        return token

    if current_token and len(current_token) >= 16 and not force_token:
        print_step("保留现有 API_TOKEN")
        return current_token

    new_token = secrets.token_urlsafe(16)
    write_env_value("API_TOKEN", new_token)
    print_step(f"已写入 API_TOKEN 到 {ENV_FILE}")
    return new_token


def _download_to_stdout(url: str) -> str:
    headers = ["-H", "Accept: application/vnd.github+json", "-H", "User-Agent: MoviePilot-CLI"]
    if command_exists("curl"):
        return capture(["curl", "-fsSL", *headers, url])
    if command_exists("wget"):
        return capture(["wget", "-qO-", "--header=Accept: application/vnd.github+json", "--header=User-Agent: MoviePilot-CLI", url])
    raise RuntimeError("未找到可用的下载工具，请先安装 curl 或 wget")


def download_file(url: str, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if command_exists("curl"):
        run(["curl", "-fsSL", url, "-o", str(target)])
        return
    if command_exists("wget"):
        run(["wget", "-qO", str(target), url])
        return
    raise RuntimeError("未找到可用的下载工具，请先安装 curl 或 wget")


def fetch_json(url: str) -> dict[str, Any]:
    payload = _download_to_stdout(url)
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"无法解析远程响应：{url}") from exc
    if isinstance(data, dict) and data.get("message") and isinstance(data.get("message"), str) and "API rate limit" in data["message"]:
        raise RuntimeError(f"访问 GitHub API 失败：{data['message']}")
    if not isinstance(data, dict):
        raise RuntimeError(f"接口返回格式异常：{url}")
    return data


def extract_archive(archive_path: Path, target_dir: Path) -> None:
    target_dir.mkdir(parents=True, exist_ok=True)
    if archive_path.suffix == ".zip":
        with zipfile.ZipFile(archive_path) as zip_file:
            zip_file.extractall(target_dir)
        return
    with tarfile.open(archive_path, "r:*") as tar_file:
        extract_kwargs: dict[str, Any] = {}
        if sys.version_info >= (3, 12):
            extract_kwargs["filter"] = "data"
        tar_file.extractall(target_dir, **extract_kwargs)


def _remove_path(path: Path) -> None:
    if not path.exists():
        return
    if path.is_symlink() or path.is_file():
        path.unlink()
        return
    shutil.rmtree(path)


def _resolve_frontend_release(frontend_version: str) -> tuple[str, str]:
    if frontend_version == "latest":
        release = fetch_json(FRONTEND_LATEST_API)
    else:
        release = fetch_json(FRONTEND_TAG_API.format(tag=frontend_version))

    tag_name = str(release.get("tag_name") or "").strip()
    if not tag_name:
        raise RuntimeError("未能获取前端版本号")

    for asset in release.get("assets") or []:
        if asset.get("name") == "dist.zip" and asset.get("browser_download_url"):
            return tag_name, str(asset["browser_download_url"])
    raise RuntimeError(f"前端版本 {tag_name} 未找到 dist.zip 发布资产")


def _frontend_runtime_ready(frontend_version: str) -> bool:
    version_file = PUBLIC_DIR / "version.txt"
    if not version_file.exists() or not (PUBLIC_DIR / "service.js").exists():
        return False
    if not (PUBLIC_DIR / "node_modules" / "express").exists():
        return False
    try:
        return version_file.read_text(encoding="utf-8").strip() == frontend_version
    except OSError:
        return False


def _node_platform() -> tuple[str, str]:
    system_name = platform.system().lower()
    machine = platform.machine().lower()

    if system_name == "darwin":
        if machine in {"arm64", "aarch64"}:
            return "darwin-arm64", "tar.gz"
        if machine in {"x86_64", "amd64"}:
            return "darwin-x64", "tar.gz"
    elif system_name == "linux":
        if machine in {"aarch64", "arm64"}:
            return "linux-arm64", "tar.xz"
        if machine in {"x86_64", "amd64"}:
            return "linux-x64", "tar.xz"

    raise RuntimeError(f"当前系统暂不支持自动安装本地 Node 运行时：{platform.system()} / {platform.machine()}")


def get_node_bin(node_dir: Path = NODE_DIR) -> Path:
    if os.name == "nt":
        return node_dir / "node.exe"
    return node_dir / "bin" / "node"


def get_npm_bin(node_dir: Path = NODE_DIR) -> Path:
    if os.name == "nt":
        return node_dir / "npm.cmd"
    return node_dir / "bin" / "npm"


def install_node_runtime(node_version: str) -> Path:
    node_bin = get_node_bin()
    if node_bin.exists():
        try:
            current_version = capture([str(node_bin), "--version"]).lstrip("v")
        except subprocess.CalledProcessError:
            current_version = ""
        if current_version == node_version:
            return node_bin
        _remove_path(NODE_DIR)

    platform_tag, archive_ext = _node_platform()
    archive_name = f"node-v{node_version}-{platform_tag}.{archive_ext}"
    download_url = f"https://nodejs.org/dist/v{node_version}/{archive_name}"

    print_step(f"下载本地 Node 运行时 v{node_version} ({platform_tag})")
    with TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        archive_path = temp_path / archive_name
        extract_dir = temp_path / "extract"
        download_file(download_url, archive_path)
        extract_archive(archive_path, extract_dir)
        extracted_roots = [item for item in extract_dir.iterdir() if item.is_dir()]
        if not extracted_roots:
            raise RuntimeError("Node 运行时解压失败")
        _remove_path(NODE_DIR)
        shutil.move(str(extracted_roots[0]), str(NODE_DIR))

    node_bin = get_node_bin()
    if not node_bin.exists():
        raise RuntimeError("Node 运行时安装失败，未找到 node 可执行文件")
    print_step(f"Node 运行时已安装到 {NODE_DIR}")
    return node_bin


def install_frontend(frontend_version: str, node_version: str) -> dict[str, str]:
    version_tag, download_url = _resolve_frontend_release(frontend_version)
    node_bin = install_node_runtime(node_version)

    if _frontend_runtime_ready(version_tag):
        print_step(f"前端发布包已是最新版本：{version_tag}")
        return {"version": version_tag, "node": str(node_bin)}

    print_step(f"下载前端发布包：{version_tag}")
    with TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        archive_path = temp_path / "dist.zip"
        extract_dir = temp_path / "extract"
        download_file(download_url, archive_path)
        extract_archive(archive_path, extract_dir)
        dist_dir = extract_dir / "dist"
        if not dist_dir.exists():
            raise RuntimeError("前端发布包中未找到 dist 目录")
        _remove_path(PUBLIC_DIR)
        shutil.move(str(dist_dir), str(PUBLIC_DIR))

    runtime_package = dict(RUNTIME_PACKAGE)
    runtime_package["version"] = version_tag
    (PUBLIC_DIR / "package.json").write_text(
        json.dumps(runtime_package, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    npm_bin = get_npm_bin()
    if not npm_bin.exists():
        raise RuntimeError("未找到 npm 可执行文件，Node 运行时可能损坏")

    print_step("安装前端运行依赖")
    run(
        [
            str(npm_bin),
            "install",
            "--no-fund",
            "--no-audit",
            "--omit=dev",
        ],
        cwd=PUBLIC_DIR,
    )
    return {"version": version_tag, "node": str(node_bin)}


def local_resource_status() -> bool:
    return (HELPER_DIR / "user.sites.v2.bin").exists() and bool(list(HELPER_DIR.glob("sites*")))


def copy_resource_files(source_dir: Path) -> list[str]:
    if not source_dir.is_dir():
        raise FileNotFoundError(f"资源目录不存在：{source_dir}")

    copied: list[str] = []
    for source in sorted(source_dir.iterdir()):
        if source.is_dir():
            continue
        target = HELPER_DIR / source.name
        shutil.copy2(source, target)
        copied.append(source.name)

    if not copied:
        raise RuntimeError(f"资源目录中未找到可复制文件：{source_dir}")
    print_step(f"已同步资源文件到 {HELPER_DIR}")
    return copied


def _download_resources_dir() -> Path:
    with TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        archive_path = temp_path / "resources.zip"
        extract_dir = temp_path / "extract"
        print_step("下载资源包")
        download_file(RESOURCES_MAIN_ZIP, archive_path)
        extract_archive(archive_path, extract_dir)
        source_dir = extract_dir / "MoviePilot-Resources-main" / "resources.v2"
        if not source_dir.exists():
            raise RuntimeError("资源压缩包中未找到 resources.v2 目录")
        staging_dir = temp_path / "staging"
        shutil.copytree(source_dir, staging_dir)
        persisted = TEMP_DIR / "resources.v2"
        _remove_path(persisted)
        shutil.copytree(staging_dir, persisted)
        return persisted


def _resolve_local_resource_dir(resources_repo: Optional[Path], resource_dir: Optional[Path]) -> Optional[Path]:
    if resource_dir:
        resolved = resource_dir.expanduser().resolve()
        if resolved.is_dir():
            return resolved
        raise FileNotFoundError(f"资源目录不存在：{resolved}")

    if resources_repo:
        repo_dir = resources_repo.expanduser().resolve()
        candidates = [
            repo_dir / "resources.v2",
            repo_dir / "resources" / "resources.v2",
            repo_dir / "resources" / "v2",
            repo_dir / "resources.v2",
        ]
        for candidate in candidates:
            if candidate.is_dir():
                return candidate
        raise FileNotFoundError(f"未在 {repo_dir} 下找到 resources.v2 目录")
    return None


def install_resources(resources_repo: Optional[Path], resource_dir: Optional[Path]) -> list[str]:
    ensure_local_dirs()
    source_dir = _resolve_local_resource_dir(resources_repo, resource_dir)
    if source_dir is None:
        source_dir = _download_resources_dir()
    copied = copy_resource_files(source_dir)
    print_step(f"资源初始化完成，共处理 {len(copied)} 个文件")
    return copied


def _is_interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _normalize_choice(value: str) -> str:
    return value.strip().lower().replace("_", "").replace("-", "")


def _prompt_text(
    label: str,
    *,
    default: Optional[str] = None,
    allow_empty: bool = False,
    secret: bool = False,
) -> str:
    while True:
        suffix = f" [{default}]" if default not in (None, "") and not secret else ""
        prompt = f"{label}{suffix}: "
        value = getpass.getpass(prompt) if secret else input(prompt)
        value = value.strip()

        if not value and default is not None:
            return str(default)
        if value:
            return value
        if allow_empty:
            return ""
        print("请输入有效内容，或使用回车接受默认值。")


def _prompt_yes_no(label: str, default: bool = True) -> bool:
    suffix = "Y/n" if default else "y/N"
    while True:
        raw = input(f"{label} [{suffix}]: ").strip().lower()
        if not raw:
            return default
        if raw in {"y", "yes"}:
            return True
        if raw in {"n", "no"}:
            return False
        print("请输入 y 或 n。")


def _prompt_choice(label: str, choices: dict[str, str], default: str) -> str:
    labels = []
    normalized_map: dict[str, str] = {}
    for key, desc in choices.items():
        labels.append(f"{key}({desc})")
        normalized_map[_normalize_choice(key)] = key

    while True:
        raw = input(f"{label} [{'/'.join(labels)}] (默认 {default}): ").strip()
        if not raw:
            return default
        normalized = _normalize_choice(raw)
        if normalized in normalized_map:
            return normalized_map[normalized]
        print("请输入列表中的可选值。")


def _prompt_path(label: str, *, default: Path, allow_empty: bool = False) -> str:
    value = _prompt_text(label, default=str(default), allow_empty=allow_empty)
    if not value:
        return ""
    return str(Path(value).expanduser().resolve())


def _collect_path_mapping() -> list[tuple[str, str]]:
    if not _prompt_yes_no("是否配置下载器路径映射", default=False):
        return []

    storage_path = _prompt_path("MoviePilot 可访问的下载目录根路径", default=ROOT.parent / "downloads")
    download_path = _prompt_text("下载器中对应的目录根路径", default="/downloads")
    return [(storage_path, download_path)]


def _collect_directory_config() -> dict[str, Any]:
    default_download_dir = ROOT.parent / "downloads"
    default_library_dir = ROOT.parent / "media"

    print_step("目录配置")
    download_path = _prompt_path("下载目录", default=default_download_dir)
    library_path = _prompt_path("媒体库目录", default=default_library_dir)
    transfer_type = _prompt_choice(
        "整理方式",
        {
            "link": "硬链接",
            "softlink": "软链接",
            "copy": "复制",
            "move": "移动",
        },
        default="link",
    )
    return {
        "name": "默认目录",
        "priority": 0,
        "storage": "local",
        "download_path": download_path,
        "monitor_type": "downloader",
        "monitor_mode": "fast",
        "transfer_type": transfer_type,
        "overwrite_mode": "latest",
        "library_path": library_path,
        "library_storage": "local",
        "renaming": False,
        "scraping": False,
        "notify": True,
        "download_type_folder": False,
        "download_category_folder": False,
        "library_type_folder": True,
        "library_category_folder": False,
    }


def _collect_downloader_config() -> Optional[dict[str, Any]]:
    print_step("下载器配置")
    downloader_type = _prompt_choice(
        "选择下载器类型",
        {
            "skip": "跳过",
            "qbittorrent": "qBittorrent",
            "transmission": "Transmission",
        },
        default="skip",
    )
    if downloader_type == "skip":
        return None

    config_name = _prompt_text("下载器名称", default=downloader_type)
    if downloader_type == "qbittorrent":
        host = _prompt_text("qBittorrent 地址", default="http://127.0.0.1:8080")
        username = _prompt_text("qBittorrent 用户名", default="admin")
        password = _prompt_text("qBittorrent 密码", secret=True)
        category = _prompt_yes_no("是否启用 qBittorrent 分类", default=False)
        return {
            "name": config_name,
            "type": "qbittorrent",
            "default": True,
            "enabled": True,
            "config": {
                "host": host,
                "username": username,
                "password": password,
                "category": category,
            },
            "path_mapping": _collect_path_mapping(),
        }

    host = _prompt_text("Transmission RPC 地址", default="http://127.0.0.1:9091")
    username = _prompt_text("Transmission 用户名", allow_empty=True, default="")
    password = _prompt_text("Transmission 密码", allow_empty=True, secret=True)
    return {
        "name": config_name,
        "type": "transmission",
        "default": True,
        "enabled": True,
        "config": {
            "host": host,
            "username": username,
            "password": password,
        },
        "path_mapping": _collect_path_mapping(),
    }


def _collect_media_server_config() -> Optional[dict[str, Any]]:
    print_step("媒体服务器配置")
    server_type = _prompt_choice(
        "选择媒体服务器类型",
        {
            "skip": "跳过",
            "emby": "Emby",
            "jellyfin": "Jellyfin",
            "plex": "Plex",
        },
        default="skip",
    )
    if server_type == "skip":
        return None

    config_name = _prompt_text("媒体服务器名称", default=server_type)
    default_host = {
        "emby": "http://127.0.0.1:8096",
        "jellyfin": "http://127.0.0.1:8096",
        "plex": "http://127.0.0.1:32400",
    }[server_type]
    host = _prompt_text("媒体服务器地址", default=default_host)
    play_host = _prompt_text("外部访问地址（可选）", default="", allow_empty=True)

    if server_type == "plex":
        config = {
            "host": host,
            "token": _prompt_text("Plex Token", secret=True),
        }
    else:
        config = {
            "host": host,
            "apikey": _prompt_text("媒体服务器 API Key", secret=True),
        }
        if server_type == "emby":
            username = _prompt_text("Emby 管理员用户名（可选）", default="", allow_empty=True)
            if username:
                config["username"] = username

    if play_host:
        config["play_host"] = play_host

    return {
        "name": config_name,
        "type": server_type,
        "enabled": True,
        "config": config,
        "sync_libraries": [],
    }


def _collect_notification_config() -> Optional[dict[str, Any]]:
    print_step("消息通知配置")
    notification_type = _prompt_choice(
        "选择通知渠道类型",
        {
            "skip": "跳过",
            "telegram": "Telegram",
            "wechat": "企业微信机器人",
            "slack": "Slack",
        },
        default="skip",
    )
    if notification_type == "skip":
        return None

    config_name = _prompt_text("通知渠道名称", default=notification_type)
    if notification_type == "telegram":
        config = {
            "TELEGRAM_TOKEN": _prompt_text("Telegram Bot Token", secret=True),
            "TELEGRAM_CHAT_ID": _prompt_text("Telegram Chat ID"),
        }
        api_url = _prompt_text("自定义 Telegram API 地址（可选）", default="", allow_empty=True)
        if api_url:
            config["API_URL"] = api_url
    elif notification_type == "wechat":
        config = {
            "WECHAT_MODE": "bot",
            "WECHAT_BOT_ID": _prompt_text("企业微信机器人 ID"),
            "WECHAT_BOT_SECRET": _prompt_text("企业微信机器人 Secret", secret=True),
        }
        chat_id = _prompt_text("默认发送对象（可选）", default="", allow_empty=True)
        admins = _prompt_text("管理员用户列表，多个逗号分隔（可选）", default="", allow_empty=True)
        if chat_id:
            config["WECHAT_BOT_CHAT_ID"] = chat_id
        if admins:
            config["WECHAT_ADMINS"] = admins
    else:
        config = {
            "SLACK_OAUTH_TOKEN": _prompt_text("Slack OAuth Token", secret=True),
            "SLACK_APP_TOKEN": _prompt_text("Slack App Token", secret=True),
        }
        channel = _prompt_text("Slack 默认频道（可选）", default="", allow_empty=True)
        if channel:
            config["SLACK_CHANNEL"] = channel

    return {
        "name": config_name,
        "type": notification_type,
        "enabled": True,
        "config": config,
        "switchs": list(NOTIFICATION_SWITCH_TYPES),
    }


def run_setup_wizard(force_token: bool) -> dict[str, Any]:
    if not _is_interactive():
        raise RuntimeError("交互式向导需要在终端中运行，请直接执行 moviepilot setup --wizard 或 moviepilot init --wizard")

    print_step("启动本地初始化向导，直接回车可接受默认值，部分步骤可选择跳过")

    existing_token = read_env_value("API_TOKEN") or ""
    if existing_token and len(existing_token) >= 16 and not force_token:
        if _prompt_yes_no("检测到现有 API_TOKEN，是否继续使用", default=True):
            api_token = ensure_api_token(force_token=False)
        else:
            if _prompt_yes_no("是否自动生成新的 API_TOKEN", default=True):
                api_token = ensure_api_token(force_token=True)
            else:
                while True:
                    custom_token = _prompt_text("请输入新的 API_TOKEN（至少 16 位）", secret=True)
                    if len(custom_token) >= 16:
                        api_token = ensure_api_token(force_token=True, token=custom_token)
                        break
                    print("API_TOKEN 长度不能少于 16 个字符。")
    else:
        if _prompt_yes_no("是否自动生成 API_TOKEN", default=True):
            api_token = ensure_api_token(force_token=force_token or bool(existing_token))
        else:
            while True:
                custom_token = _prompt_text("请输入 API_TOKEN（至少 16 位）", secret=True)
                if len(custom_token) >= 16:
                    api_token = ensure_api_token(force_token=True, token=custom_token)
                    break
                print("API_TOKEN 长度不能少于 16 个字符。")

    return {
        "api_token": api_token,
        "directories": [_collect_directory_config()],
        "downloader": _collect_downloader_config(),
        "mediaserver": _collect_media_server_config(),
        "notification": _collect_notification_config(),
    }


def _merge_named_item(existing_items: list[dict], new_item: dict) -> list[dict]:
    merged = list(existing_items or [])
    new_name = new_item.get("name")
    for index, item in enumerate(merged):
        if item.get("name") == new_name:
            merged[index] = new_item
            return merged
    merged.append(new_item)
    return merged


def _merge_directory_item(existing_items: list[dict], new_item: dict) -> list[dict]:
    merged = list(existing_items or [])
    for index, item in enumerate(merged):
        if item.get("name") == new_item.get("name") or (
            item.get("download_path") == new_item.get("download_path")
            and item.get("library_path") == new_item.get("library_path")
        ):
            new_copy = dict(new_item)
            new_copy["priority"] = item.get("priority", new_item.get("priority", 0))
            merged[index] = new_copy
            return merged

    new_copy = dict(new_item)
    max_priority = max((int(item.get("priority", 0) or 0) for item in merged), default=-1)
    new_copy["priority"] = max_priority + 1 if merged else int(new_item.get("priority", 0) or 0)
    merged.append(new_copy)
    return merged


def _merge_notification_switches(existing_items: list[dict]) -> list[dict]:
    merged: dict[str, dict] = {}
    for item in existing_items or []:
        switch_type = str(item.get("type") or "").strip()
        if switch_type:
            merged[switch_type] = dict(item)

    for switch_type in NOTIFICATION_SWITCH_TYPES:
        merged.setdefault(
            switch_type,
            {
                "type": switch_type,
                "action": "all",
            },
        )

    preferred_order = [switch for switch in NOTIFICATION_SWITCH_TYPES if switch in merged]
    extras = [key for key in merged if key not in preferred_order]
    return [merged[key] for key in [*preferred_order, *extras]]


def apply_local_system_config(config_payload: dict[str, Any]) -> None:
    for directory in config_payload.get("directories") or []:
        download_path = directory.get("download_path")
        library_path = directory.get("library_path")
        if download_path:
            Path(download_path).mkdir(parents=True, exist_ok=True)
        if library_path:
            Path(library_path).mkdir(parents=True, exist_ok=True)

    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))

    try:
        from app.db.init import init_db, update_db
        from app.db.systemconfig_oper import SystemConfigOper
        from app.schemas.types import SystemConfigKey
    except ModuleNotFoundError as exc:
        raise RuntimeError("当前环境尚未安装 MoviePilot 运行依赖，请先执行 moviepilot install deps 或 moviepilot setup") from exc

    init_db()
    update_db()

    system_config = SystemConfigOper()
    directory_items = config_payload.get("directories") or []
    if directory_items:
        current_directories = system_config.get(SystemConfigKey.Directories) or []
        for item in directory_items:
            current_directories = _merge_directory_item(current_directories, item)
        system_config.set(SystemConfigKey.Directories, current_directories)

    downloader_item = config_payload.get("downloader")
    if downloader_item:
        current_downloaders = system_config.get(SystemConfigKey.Downloaders) or []
        current_downloaders = _merge_named_item(current_downloaders, downloader_item)
        system_config.set(SystemConfigKey.Downloaders, current_downloaders)

    mediaserver_item = config_payload.get("mediaserver")
    if mediaserver_item:
        current_servers = system_config.get(SystemConfigKey.MediaServers) or []
        current_servers = _merge_named_item(current_servers, mediaserver_item)
        system_config.set(SystemConfigKey.MediaServers, current_servers)

    notification_item = config_payload.get("notification")
    if notification_item:
        current_notifications = system_config.get(SystemConfigKey.Notifications) or []
        current_notifications = _merge_named_item(current_notifications, notification_item)
        system_config.set(SystemConfigKey.Notifications, current_notifications)
        current_switches = system_config.get(SystemConfigKey.NotificationSwitchs) or []
        system_config.set(SystemConfigKey.NotificationSwitchs, _merge_notification_switches(current_switches))

    system_config.set(SystemConfigKey.SetupWizardState, True)
    print_step("已写入本地系统配置")


def init_local(
    *,
    resources_repo: Optional[Path],
    resource_dir: Optional[Path],
    skip_resources: bool,
    resources_ready: bool,
    force_token: bool,
    wizard: bool,
) -> None:
    ensure_local_dirs()

    wizard_payload: Optional[dict[str, Any]] = None
    if wizard:
        wizard_payload = run_setup_wizard(force_token=force_token)
    else:
        ensure_api_token(force_token=force_token)

    if skip_resources:
        if resources_ready:
            print_step("资源文件已完成同步")
        else:
            print_step("已跳过资源初始化")
    else:
        install_resources(resources_repo=resources_repo, resource_dir=resource_dir)

    if wizard_payload:
        apply_local_system_config(wizard_payload)


def install_deps(*, python_bin: str, venv_dir: Path, recreate: bool) -> Path:
    ensure_supported_python(python_bin)
    venv_dir = venv_dir.expanduser().resolve()
    venv_python = get_venv_python(venv_dir)

    if recreate and venv_dir.exists():
        print_step(f"删除已有虚拟环境：{venv_dir}")
        shutil.rmtree(venv_dir)

    if not venv_python.exists():
        print_step(f"创建虚拟环境：{venv_dir}")
        run([python_bin, "-m", "venv", str(venv_dir)])
    else:
        print_step(f"复用已有虚拟环境：{venv_dir}")

    print_step("升级 pip")
    run([str(venv_python), "-m", "pip", "install", "--upgrade", "pip"])

    print_step("安装项目依赖")
    run([str(venv_python), "-m", "pip", "install", "-r", str(ROOT / "requirements.txt")])
    return venv_python


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="MoviePilot 本地安装与初始化工具")
    subparsers = parser.add_subparsers(dest="command", required=True)

    install_parser = subparsers.add_parser("install-deps", help="创建虚拟环境并安装后端依赖")
    install_parser.add_argument("--python", default=sys.executable, help="用于创建虚拟环境的 Python 解释器")
    install_parser.add_argument("--venv", default=str(ROOT / "venv"), help="虚拟环境目录")
    install_parser.add_argument("--recreate", action="store_true", help="删除并重建虚拟环境")

    frontend_parser = subparsers.add_parser("install-frontend", help="下载前端 release 并安装本地运行时")
    frontend_parser.add_argument("--version", default="latest", help="前端版本，默认 latest")
    frontend_parser.add_argument("--node-version", default=DEFAULT_NODE_VERSION, help="本地 Node 运行时版本")

    resources_parser = subparsers.add_parser("install-resources", help="下载资源文件并同步到 app/helper")
    resources_parser.add_argument("--resources-repo", help="本地 MoviePilot-Resources 仓库路径")
    resources_parser.add_argument("--resource-dir", help="直接指定 resources.v2 目录")

    init_parser = subparsers.add_parser("init", help="初始化本地配置与资源文件")
    init_parser.add_argument("--resources-repo", help="本地 MoviePilot-Resources 仓库路径")
    init_parser.add_argument("--resource-dir", help="直接指定 resources.v2 目录")
    init_parser.add_argument("--skip-resources", action="store_true", help="只初始化配置，不同步资源文件")
    init_parser.add_argument("--force-token", action="store_true", help="强制重置 API_TOKEN")
    init_parser.add_argument("--wizard", action="store_true", help="启动交互式初始化向导")

    setup_parser = subparsers.add_parser("setup", help="执行 install-deps、install-frontend、install-resources 和 init")
    setup_parser.add_argument("--python", default=sys.executable, help="用于创建虚拟环境的 Python 解释器")
    setup_parser.add_argument("--venv", default=str(ROOT / "venv"), help="虚拟环境目录")
    setup_parser.add_argument("--recreate", action="store_true", help="删除并重建虚拟环境")
    setup_parser.add_argument("--frontend-version", default="latest", help="前端版本，默认 latest")
    setup_parser.add_argument("--node-version", default=DEFAULT_NODE_VERSION, help="本地 Node 运行时版本")
    setup_parser.add_argument("--resources-repo", help="本地 MoviePilot-Resources 仓库路径")
    setup_parser.add_argument("--resource-dir", help="直接指定 resources.v2 目录")
    setup_parser.add_argument("--skip-resources", action="store_true", help="只初始化配置，不同步资源文件")
    setup_parser.add_argument("--force-token", action="store_true", help="强制重置 API_TOKEN")
    setup_parser.add_argument("--wizard", action="store_true", help="安装完成后启动交互式初始化向导")

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    try:
        if args.command == "install-deps":
            venv_python = install_deps(
                python_bin=args.python,
                venv_dir=Path(args.venv),
                recreate=args.recreate,
            )
            print_step(f"后端依赖安装完成，可执行：{venv_python} -m app.cli")
            return 0

        if args.command == "install-frontend":
            result = install_frontend(frontend_version=args.version, node_version=args.node_version)
            print_step(f"前端安装完成，版本：{result['version']}")
            return 0

        if args.command == "install-resources":
            install_resources(
                resources_repo=Path(args.resources_repo) if args.resources_repo else None,
                resource_dir=Path(args.resource_dir) if args.resource_dir else None,
            )
            return 0

        if args.command == "init":
            init_local(
                resources_repo=Path(args.resources_repo) if args.resources_repo else None,
                resource_dir=Path(args.resource_dir) if args.resource_dir else None,
                skip_resources=args.skip_resources,
                resources_ready=False,
                force_token=args.force_token,
                wizard=args.wizard,
            )
            print_step("初始化完成")
            return 0

        if args.command == "setup":
            venv_python = install_deps(
                python_bin=args.python,
                venv_dir=Path(args.venv),
                recreate=args.recreate,
            )
            install_frontend(frontend_version=args.frontend_version, node_version=args.node_version)
            resources_installed = False
            if not args.skip_resources:
                install_resources(
                    resources_repo=Path(args.resources_repo) if args.resources_repo else None,
                    resource_dir=Path(args.resource_dir) if args.resource_dir else None,
                )
                resources_installed = True
            init_local(
                resources_repo=Path(args.resources_repo) if args.resources_repo else None,
                resource_dir=Path(args.resource_dir) if args.resource_dir else None,
                skip_resources=args.skip_resources or resources_installed,
                resources_ready=resources_installed,
                force_token=args.force_token,
                wizard=args.wizard,
            )
            print_step(f"本地环境已完成安装与初始化：{venv_python}")
            return 0
    except subprocess.CalledProcessError as exc:
        print(f"命令执行失败，退出码：{exc.returncode}", file=sys.stderr)
        return exc.returncode
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
