#!/usr/bin/env python3

from __future__ import annotations

import argparse
import asyncio
import getpass
import importlib.util
import json
import os
import platform
import secrets
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import textwrap
import urllib.parse
import uuid
import zipfile
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Optional


ROOT = Path(__file__).resolve().parents[1]
LEGACY_CONFIG_DIR = ROOT / "config"
HELPER_DIR = ROOT / "app" / "helper"
PUBLIC_DIR = ROOT / "public"
RUNTIME_DIR = ROOT / ".runtime"
NODE_DIR = RUNTIME_DIR / "node"
INSTALL_ENV_FILE = ROOT / ".moviepilot.env"
MIN_PYTHON_VERSION = (3, 11)
SUPPORTED_PYTHON_TEXT = (
    f"Python {MIN_PYTHON_VERSION[0]}.{MIN_PYTHON_VERSION[1]} 或更高版本"
)

CONFIG_DIR = LEGACY_CONFIG_DIR
LOG_DIR = CONFIG_DIR / "logs"
CACHE_DIR = CONFIG_DIR / "cache"
TEMP_DIR = CONFIG_DIR / "temp"
COOKIE_DIR = CONFIG_DIR / "cookies"
ENV_FILE = CONFIG_DIR / "app.env"

DEFAULT_NODE_VERSION = "20.12.1"
FRONTEND_LATEST_API = (
    "https://api.github.com/repos/jxxghp/MoviePilot-Frontend/releases/latest"
)
FRONTEND_TAG_API = (
    "https://api.github.com/repos/jxxghp/MoviePilot-Frontend/releases/tags/{tag}"
)
RESOURCES_MAIN_ZIP = (
    "https://github.com/jxxghp/MoviePilot-Resources/archive/refs/heads/main.zip"
)
LLM_PROVIDER_DEFAULTS = {
    "deepseek": {
        "model": "deepseek-chat",
        "base_url": "https://api.deepseek.com",
    },
    "openai": {
        "model": "gpt-4o-mini",
        "base_url": "https://api.openai.com/v1",
    },
    "google": {
        "model": "gemini-2.5-flash",
        "base_url": "",
    },
    "anthropic": {
        "model": "claude-sonnet-4-0",
        "base_url": "https://api.anthropic.com/v1",
    },
    "baidu-qianfan-coding-plan": {
        "model": "",
        "base_url": "https://qianfan.baidubce.com/v2",
    },
    "openrouter": {
        "model": "openai/gpt-4.1-mini",
        "base_url": "https://openrouter.ai/api/v1",
    },
    "groq": {
        "model": "llama-3.3-70b-versatile",
        "base_url": "https://api.groq.com/openai/v1",
    },
    "jdcloud": {
        "model": "",
        "base_url": "https://modelservice.jdcloud.com/v1",
    },
    "china-unicom": {
        "model": "",
        "base_url": "https://aigw-gzgy2.cucloud.cn:8443/v1",
        "base_url_preset": "china-unicom-coding-openai",
    },
    "china-mobile": {
        "model": "",
        "base_url": "https://ecloud.10086.cn/api",
        "base_url_preset": "china-mobile-moma",
    },
    "china-telecom": {
        "model": "",
        "base_url": "https://wishub-x6.ctyun.cn/v1",
        "base_url_preset": "china-telecom-token-service",
    },
    "kuaishou-wanqing": {
        "model": "",
        "base_url": "https://wanqing.streamlakeapi.com/api/gateway/v1/endpoints",
        "base_url_preset": "kuaishou-wanqing-usage",
    },
}
LLM_PROVIDER_FALLBACK_CHOICES = {
    "deepseek": "DeepSeek",
    "openai": "OpenAI Compatible",
    "google": "Google",
    "anthropic": "Anthropic",
    "baidu-qianfan-coding-plan": "百度千帆",
    "openrouter": "OpenRouter",
    "groq": "Groq",
    "jdcloud": "京东云",
    "china-unicom": "中国联通",
    "china-mobile": "中国移动",
    "china-telecom": "中国电信",
    "kuaishou-wanqing": "快手万擎",
}
RUNTIME_PACKAGE = {
    "name": "moviepilot-frontend-runtime",
    "private": True,
    "license": "UNLICENSED",
    "dependencies": {
        "express": "^4.18.2",
        "express-http-proxy": "^2.0.0",
    },
}


def _repo_frontend_version() -> str:
    version_file = ROOT / "version.py"
    module_name = f"moviepilot_version_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, version_file)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载版本文件：{version_file}")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    frontend_version = str(getattr(module, "FRONTEND_VERSION", "") or "").strip()
    if not frontend_version:
        raise RuntimeError(f"版本文件未定义有效的 FRONTEND_VERSION：{version_file}")
    return frontend_version
LOCAL_FRONTEND_SERVICE_SCRIPT = textwrap.dedent(
    """
    const http = require('node:http')
    const path = require('node:path')
    const express = require('express')
    const proxy = require('express-http-proxy')

    const app = express()
    const backendHost = process.env.MOVIEPILOT_BACKEND_HOST || '127.0.0.1'
    const backendPort = Number(process.env.PORT || 3001)
    const frontendPort = Number(process.env.NGINX_PORT || 3000)
    const backendHealthPath = '/api/v1/system/global?token=moviepilot'
    const backendHealthTimeoutMs = Number(process.env.MOVIEPILOT_FRONTEND_HEALTH_TIMEOUT_MS || 3000)
    const backendHealthIntervalMs = Number(process.env.MOVIEPILOT_FRONTEND_HEALTH_INTERVAL_MS || 15000)
    const backendMaxFailures = Math.max(
      Number(process.env.MOVIEPILOT_FRONTEND_MAX_FAILURES || 4),
      1
    )

    function sleep (ms) {
      return new Promise(resolve => setTimeout(resolve, ms))
    }

    function checkBackendHealth () {
      return new Promise(resolve => {
        const request = http.request(
          {
            host: backendHost,
            port: backendPort,
            path: backendHealthPath,
            method: 'GET',
            timeout: backendHealthTimeoutMs
          },
          response => {
            let body = ''
            response.setEncoding('utf8')
            response.on('data', chunk => {
              body += chunk
            })
            response.on('end', () => {
              if (response.statusCode !== 200) {
                resolve(false)
                return
              }

              try {
                const payload = JSON.parse(body)
                resolve(payload?.success !== false)
              } catch (error) {
                // 健康检查接口只要返回 200，就允许继续提供前端服务。
                resolve(true)
              }
            })
          }
        )

        request.on('timeout', () => {
          request.destroy(new Error('backend health check timeout'))
        })
        request.on('error', () => {
          resolve(false)
        })
        request.end()
      })
    }

    async function waitForBackendReady () {
      for (let attempt = 1; attempt <= backendMaxFailures; attempt += 1) {
        if (await checkBackendHealth()) {
          return true
        }

        if (attempt < backendMaxFailures) {
          await sleep(1000)
        }
      }
      return false
    }

    function startBackendWatchdog (server) {
      let consecutiveFailures = 0
      let checking = false

      const timer = setInterval(async () => {
        if (checking) {
          return
        }

        checking = true
        try {
          const healthy = await checkBackendHealth()
          if (healthy) {
            consecutiveFailures = 0
            return
          }

          consecutiveFailures += 1
          console.warn(
            `Backend health check failed (${consecutiveFailures}/${backendMaxFailures})`
          )

          if (consecutiveFailures < backendMaxFailures) {
            return
          }

          clearInterval(timer)
          console.error('Backend is unavailable, stopping frontend service')
          server.close(() => process.exit(1))
          setTimeout(() => process.exit(1), 1000).unref()
        } finally {
          checking = false
        }
      }, backendHealthIntervalMs)

      timer.unref()

      const shutdown = signal => {
        clearInterval(timer)
        console.log(`Received ${signal}, shutting down frontend service`)
        server.close(() => process.exit(0))
        setTimeout(() => process.exit(0), 1000).unref()
      }

      process.on('SIGINT', () => shutdown('SIGINT'))
      process.on('SIGTERM', () => shutdown('SIGTERM'))
    }

    // 静态文件服务目录
    app.use(express.static(__dirname))

    // 配置代理中间件将请求转发给后端 API。
    app.use(
      '/api',
      proxy(`${backendHost}:${backendPort}`, {
        proxyReqPathResolver: req => `/api${req.url}`
      })
    )

    // 配置代理中间件将 CookieCloud 请求转发给后端 API。
    app.use(
      '/cookiecloud',
      proxy(`${backendHost}:${backendPort}`, {
        proxyReqPathResolver: req => `/cookiecloud${req.url}`
      })
    )

    // 处理根路径的请求。
    app.get('/', (req, res) => {
      res.sendFile(path.join(__dirname, 'index.html'))
    })

    // 处理所有其他请求，重定向到前端入口文件。
    app.get('*', (req, res) => {
      res.sendFile(path.join(__dirname, 'index.html'))
    })

    async function bootstrap () {
      // 前端本地代理不再允许单独存活，避免设备重启后只剩前端进程。
      const backendReady = await waitForBackendReady()
      if (!backendReady) {
        console.error('Backend is unavailable, skip starting frontend service')
        process.exit(1)
      }

      const server = app.listen(frontendPort, () => {
        console.log(`Server is running on port ${frontendPort}`)
      })

      startBackendWatchdog(server)
    }

    bootstrap().catch(error => {
      console.error(`Failed to start frontend service: ${error?.message || error}`)
      process.exit(1)
    })
    """
).lstrip()
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
UNINSTALL_CONFIRM_TEXT = "UNINSTALL"
RESOURCE_FILE_PATTERNS = ("sites*", "user.sites*.bin")
AUTOSTART_ENV_KEY = "MOVIEPILOT_AUTO_START"
AUTOSTART_RUNTIME_DIR = RUNTIME_DIR / "startup"
AUTOSTART_UNIX_LAUNCHER = AUTOSTART_RUNTIME_DIR / "moviepilot-start.sh"
AUTOSTART_WINDOWS_LAUNCHER = AUTOSTART_RUNTIME_DIR / "moviepilot-start.cmd"
AUTOSTART_TIMEOUT = 120
MACOS_LAUNCH_AGENT_LABEL = "org.moviepilot.localcli"
LINUX_SYSTEMD_UNIT_NAME = "moviepilot-autostart.service"
LINUX_XDG_AUTOSTART_FILENAME = "moviepilot.desktop"
WINDOWS_STARTUP_FILENAME = "MoviePilot Startup.cmd"


def _default_config_dir() -> Path:
    if platform.system() == "Darwin":
        return Path.home() / "Library" / "Application Support" / "MoviePilot"
    return (
        Path(os.getenv("XDG_CONFIG_HOME") or (Path.home() / ".config")) / "moviepilot"
    )


def _legacy_runtime_config_exists() -> bool:
    markers = [
        LEGACY_CONFIG_DIR / "app.env",
        LEGACY_CONFIG_DIR / "user.db",
        LEGACY_CONFIG_DIR / "logs",
        LEGACY_CONFIG_DIR / "temp",
        LEGACY_CONFIG_DIR / "cache",
        LEGACY_CONFIG_DIR / "cookies",
        LEGACY_CONFIG_DIR / "sites",
    ]
    return any(marker.exists() for marker in markers)


def _read_install_env_config_dir() -> Optional[Path]:
    if not INSTALL_ENV_FILE.exists():
        return None

    for line in INSTALL_ENV_FILE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        if key.strip() != "CONFIG_DIR":
            continue
        return Path(value.strip().strip('"').strip("'")).expanduser()
    return None


def _set_config_dir(config_dir: Path) -> Path:
    global CONFIG_DIR, LOG_DIR, CACHE_DIR, TEMP_DIR, COOKIE_DIR, ENV_FILE

    CONFIG_DIR = config_dir.expanduser().resolve()
    LOG_DIR = CONFIG_DIR / "logs"
    CACHE_DIR = CONFIG_DIR / "cache"
    TEMP_DIR = CONFIG_DIR / "temp"
    COOKIE_DIR = CONFIG_DIR / "cookies"
    ENV_FILE = CONFIG_DIR / "app.env"
    os.environ["CONFIG_DIR"] = str(CONFIG_DIR)
    return CONFIG_DIR


def _write_install_env(config_dir: Path) -> None:
    INSTALL_ENV_FILE.write_text(
        f"CONFIG_DIR={shlex.quote(str(config_dir.expanduser().resolve()))}\n",
        encoding="utf-8",
    )


def _seed_default_config_files(target_dir: Path) -> None:
    for name in ("category.yaml",):
        source = LEGACY_CONFIG_DIR / name
        target = target_dir / name
        if source.exists() and not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)


def _migrate_legacy_config_if_needed(target_dir: Path) -> None:
    target_dir = target_dir.expanduser().resolve()
    if target_dir == LEGACY_CONFIG_DIR.resolve():
        return
    if not _legacy_runtime_config_exists():
        _seed_default_config_files(target_dir)
        return

    target_dir.mkdir(parents=True, exist_ok=True)
    for source in sorted(LEGACY_CONFIG_DIR.iterdir()):
        target = target_dir / source.name
        if target.exists():
            continue
        if source.is_dir():
            shutil.copytree(source, target)
        else:
            shutil.copy2(source, target)
    print_step(f"已将现有本地配置迁移到 {target_dir}")


def configure_config_dir(
    explicit: Optional[Path] = None,
    *,
    persist: bool = False,
    prefer_external: bool = False,
) -> Path:
    if explicit:
        config_dir = explicit.expanduser().resolve()
    elif os.getenv("CONFIG_DIR"):
        config_dir = Path(os.environ["CONFIG_DIR"]).expanduser().resolve()
    else:
        install_env_dir = _read_install_env_config_dir()
        if install_env_dir:
            config_dir = install_env_dir.resolve()
        elif prefer_external:
            config_dir = _default_config_dir().resolve()
        elif _legacy_runtime_config_exists():
            config_dir = LEGACY_CONFIG_DIR.resolve()
        else:
            config_dir = _default_config_dir().resolve()

    _set_config_dir(config_dir)
    if prefer_external:
        _migrate_legacy_config_if_needed(config_dir)
    if persist:
        _write_install_env(config_dir)
    return config_dir


def resolve_config_dir(
    explicit: Optional[Path] = None,
    *,
    prefer_external: bool = False,
) -> Path:
    """
    解析当前命令应使用的配置目录，但不写入环境变量或安装元数据。

    该函数用于交互式命令在真正持久化配置目录前，先给用户展示默认值。
    """
    if explicit:
        return explicit.expanduser().resolve()
    if os.getenv("CONFIG_DIR"):
        return Path(os.environ["CONFIG_DIR"]).expanduser().resolve()

    install_env_dir = _read_install_env_config_dir()
    if install_env_dir:
        return install_env_dir.resolve()
    if prefer_external:
        return _default_config_dir().resolve()
    if _legacy_runtime_config_exists():
        return LEGACY_CONFIG_DIR.resolve()
    return _default_config_dir().resolve()


configure_config_dir()


def print_step(message: str) -> None:
    print(f"==> {message}")


def _redact_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    if "@" not in parsed.netloc:
        return value
    host = parsed.netloc.rsplit("@", 1)[-1]
    return urllib.parse.urlunsplit(
        (parsed.scheme, host, parsed.path, parsed.query, parsed.fragment)
    )


def redact_command(command: list[str]) -> list[str]:
    redacted: list[str] = []
    for item in command:
        value = str(item)
        url_marker = value.find("://")
        equals_marker = value.find("=")
        if url_marker >= 0 and 0 <= equals_marker < url_marker:
            key, separator, url = value.partition("=")
            value = f"{key}{separator}{_redact_url(url)}"
        elif url_marker >= 0:
            value = _redact_url(value)
        redacted.append(value)
    return redacted


def build_package_install_env() -> dict[str, str]:
    env = os.environ.copy()
    package_cache_root = env.get("PACKAGE_CACHE_ROOT", "").strip() or str(CONFIG_DIR / ".cache")
    env.setdefault("PACKAGE_CACHE_ROOT", package_cache_root)
    env.setdefault("PIP_CACHE_DIR", os.path.join(package_cache_root, "pip"))
    env.setdefault("UV_CACHE_DIR", os.path.join(package_cache_root, "uv"))

    index_url = env.get("PIP_PROXY", "").strip()
    if index_url:
        env["PIP_INDEX_URL"] = index_url
        env["UV_DEFAULT_INDEX"] = index_url

    proxy = env.get("PROXY_HOST", "").strip()
    if proxy:
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            env[key] = proxy
    return env


def run(
    command: list[str],
    cwd: Optional[Path] = None,
    env: Optional[dict[str, str]] = None,
    safe_command: Optional[list[str]] = None,
) -> None:
    """
    执行安装步骤中的外部命令，并在失败时让调用方中断流程。
    """
    pretty = " ".join(safe_command or redact_command(command))
    print(f"+ {pretty}")
    subprocess.run(command, cwd=str(cwd or ROOT), check=True, env=env)


def capture(command: list[str], cwd: Optional[Path] = None) -> str:
    return subprocess.check_output(command, cwd=str(cwd or ROOT), text=True).strip()


def command_exists(name: str) -> bool:
    return shutil.which(name) is not None


def get_python_version(python_bin: str) -> tuple[int, int, int]:
    version_json = capture(
        [
            python_bin,
            "-c",
            "import json, sys; print(json.dumps(list(sys.version_info[:3])))",
        ]
    )
    version_info = json.loads(version_json)
    if not isinstance(version_info, list) or len(version_info) < 3:
        raise RuntimeError(f"无法识别 Python 版本信息：{python_bin}")
    return int(version_info[0]), int(version_info[1]), int(version_info[2])


def discover_supported_python() -> Optional[str]:
    candidates = [
        f"python3.{minor}" for minor in range(20, MIN_PYTHON_VERSION[1] - 1, -1)
    ]
    if sys.executable:
        candidates.append(sys.executable)
    candidates.extend(["python3", "python"])

    seen: set[str] = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)

        python_path = (
            candidate if os.sep in candidate else (shutil.which(candidate) or "")
        )
        if not python_path:
            continue

        try:
            version = get_python_version(python_path)
        except (OSError, subprocess.CalledProcessError, json.JSONDecodeError):
            continue
        if version >= MIN_PYTHON_VERSION:
            return python_path
    return None


DEFAULT_BOOTSTRAP_PYTHON = discover_supported_python() or sys.executable


def get_venv_python(venv_dir: Path) -> Path:
    if os.name == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def get_venv_bin_dir(venv_dir: Path) -> Path:
    if os.name == "nt":
        return venv_dir / "Scripts"
    return venv_dir / "bin"


def get_venv_pip(venv_dir: Path) -> Path:
    if os.name == "nt":
        return get_venv_bin_dir(venv_dir) / "pip.exe"
    return get_venv_bin_dir(venv_dir) / "pip"


def _ensure_uv_available_for_venv(venv_dir: Path, venv_python: Path) -> Optional[Path]:
    if os.name == "nt":
        return None

    venv_bin = get_venv_bin_dir(venv_dir)
    uv_bin = venv_bin / "uv"
    if uv_bin.exists():
        return uv_bin

    system_uv = shutil.which("uv")
    if system_uv:
        uv_target = Path(system_uv).expanduser().resolve()
        print_step(f"复用系统 uv：{uv_target}")
        if uv_bin.exists() or uv_bin.is_symlink():
            uv_bin.unlink()
        uv_bin.symlink_to(uv_target)
        return uv_bin

    print_step("当前未检测到 uv，先在虚拟环境内安装 uv")
    command = [str(venv_python), "-m", "pip", "install", "--upgrade", "pip", "uv"]
    run(command, env=build_package_install_env(), safe_command=redact_command(command))
    if uv_bin.exists():
        return uv_bin
    raise RuntimeError("uv 安装完成，但虚拟环境中未找到 uv 可执行文件")


def configure_venv_pip_compat(venv_dir: Path, venv_python: Path) -> Path:
    """
    在虚拟环境中安装 uv 并保持 pip 命令兼容，供现有安装流程复用。
    """
    if os.name == "nt":
        return get_venv_pip(venv_dir)

    _ensure_uv_available_for_venv(venv_dir, venv_python)
    venv_bin = get_venv_bin_dir(venv_dir)
    wrapper_src = ROOT / "scripts" / "uv-pip-compat.sh"
    wrapper_dst = venv_bin / "uv-pip-compat"
    shutil.copy2(wrapper_src, wrapper_dst)
    wrapper_dst.chmod(0o755)

    python_version = get_python_version(str(venv_python))
    compat_links = {
        "pip",
        "pip3",
        f"pip{python_version[0]}",
        f"pip{python_version[0]}.{python_version[1]}",
        "pip-compile",
        "pip-sync",
    }
    for link_name in compat_links:
        link_path = venv_bin / link_name
        if link_path.exists() or link_path.is_symlink():
            link_path.unlink()
        link_path.symlink_to(wrapper_dst.name)
    return get_venv_pip(venv_dir)


def ensure_supported_python(python_bin: str) -> None:
    version = get_python_version(python_bin)
    if version < MIN_PYTHON_VERSION:
        raise RuntimeError(
            f"MoviePilot 本地安装需要 {SUPPORTED_PYTHON_TEXT}，当前解释器为 {python_bin} "
            f"({version[0]}.{version[1]}.{version[2]})"
        )


def ensure_local_dirs() -> None:
    for path in (CONFIG_DIR, LOG_DIR, CACHE_DIR, TEMP_DIR, COOKIE_DIR, RUNTIME_DIR):
        path.mkdir(parents=True, exist_ok=True)
    _seed_default_config_files(CONFIG_DIR)


def _load_env_lines() -> list[str]:
    if not ENV_FILE.exists():
        return []
    return ENV_FILE.read_text(encoding="utf-8").splitlines(keepends=True)


def _serialize_env_value(value: Any) -> str:
    if isinstance(value, Path):
        value = str(value)
    if value is None:
        return '""'
    return json.dumps(value, ensure_ascii=False)


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
    new_line = f"{key}={_serialize_env_value(value)}\n"

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


def write_env_values(values: dict[str, Any]) -> None:
    for key, value in values.items():
        write_env_value(key, value)


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
    headers = [
        "-H",
        "Accept: application/vnd.github+json",
        "-H",
        "User-Agent: MoviePilot-CLI",
    ]
    if command_exists("curl"):
        return capture(["curl", "-fsSL", *headers, url])
    if command_exists("wget"):
        return capture(
            [
                "wget",
                "-qO-",
                "--header=Accept: application/vnd.github+json",
                "--header=User-Agent: MoviePilot-CLI",
                url,
            ]
        )
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
    if (
        isinstance(data, dict)
        and data.get("message")
        and isinstance(data.get("message"), str)
        and "API rate limit" in data["message"]
    ):
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
    frontend_version = (frontend_version or "").strip() or _repo_frontend_version()
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


def _write_local_frontend_service_script(target_dir: Path) -> None:
    """
    覆盖前端 release 自带的 service.js，统一使用本地 CLI 的受控代理脚本。
    """
    (target_dir / "service.js").write_text(
        LOCAL_FRONTEND_SERVICE_SCRIPT,
        encoding="utf-8",
    )


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

    raise RuntimeError(
        f"当前系统暂不支持自动安装本地 Node 运行时：{platform.system()} / {platform.machine()}"
    )


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
        _write_local_frontend_service_script(PUBLIC_DIR)
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

    _write_local_frontend_service_script(PUBLIC_DIR)

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
    return (HELPER_DIR / "user.sites.v2.bin").exists() and bool(
        list(HELPER_DIR.glob("sites*"))
    )


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


def _get_platform_tag() -> str:
    system = platform.system().lower()
    machine = platform.machine().lower()
    if system == "darwin":
        if machine in {"arm64", "aarch64"}:
            return "darwin", "arm64"
        if machine in {"x86_64", "amd64"}:
            return "darwin", "x86_64"
    elif system == "linux":
        if machine in {"aarch64", "arm64"}:
            return "linux", "aarch64"
        if machine in {"x86_64", "amd64"}:
            return "linux", "x86_64"
    elif system == "windows":
        return "windows", "amd64"
    raise RuntimeError(f"不支持的平台：{system} / {machine}")


def _get_python_version_tag() -> str:
    version = sys.version_info
    return f"cp{version.major}{version.minor}"


def _filter_resources_files(
    source_dir: Path, platform_tag: str, python_version: str
) -> list[Path]:
    matched_files: list[Path] = []
    for file in source_dir.iterdir():
        if not file.is_file():
            continue
        filename = file.name
        if filename == "user.sites.v2.bin":
            matched_files.append(file)
            continue
        if not filename.startswith("sites."):
            continue
        if platform_tag == "windows":
            if filename == f"sites.cp{python_version.replace('cp', '')}-win_amd64.pyd":
                matched_files.append(file)
        elif platform_tag == "darwin":
            if (
                filename
                == f"sites.cpython-{python_version.replace('cp', '')}-darwin.so"
            ):
                matched_files.append(file)
        elif platform_tag == "linux":
            if (
                f"cpython-{python_version.replace('cp', '')}" in filename
                and "linux-gnu" in filename
            ):
                matched_files.append(file)
    return matched_files


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

        platform_name, machine = _get_platform_tag()
        python_version = _get_python_version_tag()
        print_step(
            f"当前平台：{platform_name}-{machine}，Python 版本：{python_version}"
        )

        matched_files = _filter_resources_files(
            source_dir, platform_name, python_version
        )
        if not matched_files:
            raise RuntimeError(
                f"未找到匹配的 sites 资源文件：{platform_name} / {python_version}"
            )

        staging_dir = temp_path / "staging"
        staging_dir.mkdir(parents=True, exist_ok=True)
        for file in matched_files:
            target = staging_dir / file.name
            shutil.copy2(file, target)

        persisted = TEMP_DIR / "resources.v2"
        _remove_path(persisted)
        shutil.copytree(staging_dir, persisted)
        print_step(f"已筛选对应平台的资源文件，共 {len(matched_files)} 个")
        return persisted


def _resolve_local_resource_dir(
    resources_repo: Optional[Path], resource_dir: Optional[Path]
) -> Optional[Path]:
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


def install_resources(
    resources_repo: Optional[Path], resource_dir: Optional[Path]
) -> list[str]:
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


def _env_default(key: str, default: str = "") -> str:
    value = read_env_value(key)
    if value is None or value == "":
        return default
    return value


def _env_bool(key: str, default: bool) -> bool:
    value = read_env_value(key)
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_int(key: str, default: int) -> int:
    value = read_env_value(key)
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


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


def _prompt_secret_text(
    label: str,
    *,
    current_value: Optional[str] = None,
    allow_empty: bool = False,
    required: bool = False,
) -> str:
    while True:
        suffix = " [留空保持现有值]" if current_value not in (None, "") else ""
        prompt = f"{label}{suffix}: "
        value = getpass.getpass(prompt).strip()

        if value:
            return value
        if current_value is not None and current_value != "":
            return current_value
        if allow_empty and not required:
            return ""
        if not required:
            return ""
        print("请输入有效内容。")


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


def _prompt_provider_choice(label: str, choices: dict[str, str], default: str) -> str:
    labels = []
    normalized_map: dict[str, str] = {}
    for key, desc in choices.items():
        labels.append(f"{key}({desc})")
        normalized_map[_normalize_choice(key)] = key

    preview_limit = 12
    print("可用 LLM 提供商：")
    for item in labels[:preview_limit]:
        print(f"  {item}")
    if len(labels) > preview_limit:
        print(f"  ... 另有 {len(labels) - preview_limit} 个，可直接输入 provider id")

    while True:
        raw = input(f"{label} (默认 {default}，可直接输入 provider id): ").strip()
        if not raw:
            return default
        normalized = _normalize_choice(raw)
        if normalized in normalized_map:
            return normalized_map[normalized]

        provider_id = raw.strip().lower()
        if re.fullmatch(r"[a-z0-9][a-z0-9._-]*", provider_id):
            return provider_id
        print("请输入列表中的可选值，或合法的 provider id（小写字母/数字/.-_）。")


def _load_llm_provider_module():
    provider_path = ROOT / "app" / "agent" / "llm" / "provider.py"
    module_name = f"moviepilot_local_llm_provider_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, provider_path)
    if not spec or not spec.loader:
        raise RuntimeError("无法加载 LLM provider 模块")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _load_llm_provider_definitions_inner() -> list[dict[str, Any]]:
    provider_module = _load_llm_provider_module()
    providers = asyncio.run(provider_module.LLMProviderManager().list_providers_async())
    return providers if isinstance(providers, list) else []


def _load_llm_provider_definitions(
    runtime_python: Optional[Path] = None,
) -> list[dict[str, Any]]:
    try:
        return _load_llm_provider_definitions_inner()
    except Exception as exc:
        if runtime_python and not _current_python_matches(runtime_python):
            try:
                with TemporaryDirectory() as temp_dir:
                    output_path = Path(temp_dir) / "llm-providers.json"
                    subprocess.run(
                        [
                            str(runtime_python),
                            str(Path(__file__).resolve()),
                            "query-llm-providers",
                            "--output-json-file",
                            str(output_path),
                        ],
                        cwd=str(ROOT),
                        check=True,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    data = json.loads(output_path.read_text(encoding="utf-8"))
                    if isinstance(data, list):
                        return data
            except Exception as runtime_exc:
                print_step(
                    f"当前环境暂时无法读取 LLM 提供商目录，已回退到常用平台列表：{runtime_exc}"
                )
                return []

        print_step(f"当前环境暂时无法读取 LLM 提供商目录，已回退到常用平台列表：{exc}")
        return []


def _llm_provider_choice_map(
    provider_definitions: list[dict[str, Any]],
) -> dict[str, str]:
    choices: dict[str, str] = {}
    for item in provider_definitions:
        if not isinstance(item, dict):
            continue
        if item.get("supports_api_key") is False:
            continue
        provider_id = str(item.get("id") or "").strip().lower()
        name = str(item.get("name") or provider_id).strip()
        if not provider_id or not name:
            continue
        choices[provider_id] = name
    if choices:
        return choices
    return dict(LLM_PROVIDER_FALLBACK_CHOICES)


def _llm_provider_defaults(
    provider: str,
    provider_definitions: list[dict[str, Any]],
) -> dict[str, str]:
    normalized_provider = str(provider or "").strip().lower()
    defaults = dict(LLM_PROVIDER_DEFAULTS.get(normalized_provider) or {})
    provider_meta = next(
        (
            item
            for item in provider_definitions
            if isinstance(item, dict)
            and str(item.get("id") or "").strip().lower() == normalized_provider
        ),
        None,
    )
    if isinstance(provider_meta, dict):
        default_base_url = str(provider_meta.get("default_base_url") or "").strip()
        if default_base_url:
            defaults["base_url"] = default_base_url
        base_url_presets = provider_meta.get("base_url_presets") or []
        if isinstance(base_url_presets, list) and base_url_presets:
            preset_id = str((base_url_presets[0] or {}).get("id") or "").strip()
            if preset_id:
                defaults["base_url_preset"] = preset_id

    defaults.setdefault("model", _env_default("LLM_MODEL", ""))
    defaults.setdefault("base_url", _env_default("LLM_BASE_URL", ""))
    defaults.setdefault("base_url_preset", _env_default("LLM_BASE_URL_PRESET", ""))
    return defaults


def _llm_provider_meta(
    provider: str,
    provider_definitions: list[dict[str, Any]],
) -> dict[str, Any]:
    normalized_provider = str(provider or "").strip().lower()
    provider_meta = next(
        (
            item
            for item in provider_definitions
            if isinstance(item, dict)
            and str(item.get("id") or "").strip().lower() == normalized_provider
        ),
        None,
    )
    return dict(provider_meta) if isinstance(provider_meta, dict) else {}


def _load_llm_models_inner(payload: dict[str, Any]) -> list[dict[str, Any]]:
    provider = str(payload.get("provider") or "").strip().lower()
    if not provider:
        raise RuntimeError("缺少 LLM provider")

    provider_module = _load_llm_provider_module()
    api_key = str(payload.get("api_key") or "").strip() or None
    base_url = str(payload.get("base_url") or "").strip() or None
    base_url_preset = str(payload.get("base_url_preset") or "").strip() or None
    models = asyncio.run(
        provider_module.LLMProviderManager().list_models(
            provider_id=provider,
            api_key=api_key,
            base_url=base_url,
            base_url_preset_id=base_url_preset,
            force_refresh=False,
        )
    )
    return models if isinstance(models, list) else []


def _load_llm_models(
    *,
    provider: str,
    api_key: Optional[str],
    base_url: Optional[str],
    base_url_preset: Optional[str],
    runtime_python: Optional[Path] = None,
) -> list[dict[str, Any]]:
    payload = {
        "provider": str(provider or "").strip().lower(),
        "api_key": str(api_key or "").strip(),
        "base_url": str(base_url or "").strip(),
        "base_url_preset": str(base_url_preset or "").strip(),
    }
    try:
        return _load_llm_models_inner(payload)
    except Exception as exc:
        if runtime_python and not _current_python_matches(runtime_python):
            try:
                with TemporaryDirectory() as temp_dir:
                    request_path = Path(temp_dir) / "llm-models-request.json"
                    output_path = Path(temp_dir) / "llm-models.json"
                    request_path.write_text(
                        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
                    )
                    subprocess.run(
                        [
                            str(runtime_python),
                            str(Path(__file__).resolve()),
                            "query-llm-models",
                            "--request-json-file",
                            str(request_path),
                            "--output-json-file",
                            str(output_path),
                        ],
                        cwd=str(ROOT),
                        check=True,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    data = json.loads(output_path.read_text(encoding="utf-8"))
                    if isinstance(data, list):
                        return data
            except Exception as runtime_exc:
                print_step(
                    f"当前环境暂时无法获取 {payload['provider']} 模型目录，已回退为手动输入模型名称：{runtime_exc}"
                )
                return []

        print_step(
            f"当前环境暂时无法获取 {payload['provider']} 模型目录，已回退为手动输入模型名称：{exc}"
        )
        return []


def _print_llm_models(models: list[dict[str, Any]], limit: int = 20) -> None:
    print("可用模型：")
    for index, item in enumerate(models[:limit], start=1):
        if not isinstance(item, dict):
            continue
        model_id = str(item.get("id") or "").strip()
        if not model_id:
            continue
        model_name = str(item.get("name") or model_id).strip()
        extras: list[str] = []
        if item.get("context_tokens_k"):
            extras.append(f"{item['context_tokens_k']}K")
        if item.get("supports_reasoning"):
            extras.append("reasoning")
        if item.get("supports_tools"):
            extras.append("tools")
        if item.get("supports_image_input"):
            extras.append("vision")
        extra_text = f" [{' / '.join(extras)}]" if extras else ""
        if model_name != model_id:
            print(f"  {index}. {model_id} ({model_name}){extra_text}")
        else:
            print(f"  {index}. {model_id}{extra_text}")
    if len(models) > limit:
        print(f"  ... 共 {len(models)} 个模型，可输入编号或直接输入模型名称")


def _prompt_model_choice(models: list[dict[str, Any]], default: Optional[str] = None) -> str:
    valid_models = [item for item in models if isinstance(item, dict) and item.get("id")]
    if not valid_models:
        return _prompt_text("LLM 模型名称", default=default)

    indexed_models = {
        str(index): str(item.get("id")).strip()
        for index, item in enumerate(valid_models, start=1)
    }
    default_model = str(default or indexed_models.get("1") or "").strip()
    _print_llm_models(valid_models)

    while True:
        raw = input(
            f"LLM 模型名称/编号{' [' + default_model + ']' if default_model else ''}: "
        ).strip()
        if not raw and default_model:
            return default_model
        if raw in indexed_models:
            return indexed_models[raw]
        if raw:
            return raw
        print("请输入有效模型编号或模型名称。")


def _env_llm_thinking_level_default() -> str:
    value = _normalize_choice(_env_default("LLM_THINKING_LEVEL", ""))
    alias_map = {
        "none": "off",
        "disabled": "off",
        "disable": "off",
        "enabled": "auto",
        "enable": "auto",
        "default": "auto",
        "dynamic": "auto",
    }
    normalized = alias_map.get(value, value)
    if normalized in {
        "off",
        "auto",
        "minimal",
        "low",
        "medium",
        "high",
        "max",
        "xhigh",
    }:
        return normalized
    return "auto"


def _prompt_path(label: str, *, default: Path, allow_empty: bool = False) -> str:
    value = _prompt_text(label, default=str(default), allow_empty=allow_empty)
    if not value:
        return ""
    return str(Path(value).expanduser().resolve())


def _resolve_interactive_config_dir(
    command: str, explicit_config_dir: Optional[Path]
) -> Optional[Path]:
    """
    `setup` / `init` 是最常见的本地安装入口。
    当用户没有显式传入 `--config-dir` 且当前终端可交互时，先询问一次配置目录，
    并把程序外默认路径展示出来，避免用户安装后才发现配置写到了别处。
    """
    if explicit_config_dir or command not in {"init", "setup"} or not _is_interactive():
        return explicit_config_dir

    default_config_dir = resolve_config_dir(prefer_external=True)
    print_step("安装将使用程序目录外的配置目录，直接回车可接受默认值")
    selected_path = _prompt_path("配置目录", default=default_config_dir)
    return Path(selected_path) if selected_path else default_config_dir


def _validate_superuser_name(username: str) -> Optional[str]:
    if not username:
        return "超级管理员用户名不能为空。"
    if any(char.isspace() for char in username):
        return "超级管理员用户名不能包含空白字符。"
    if len(username) > 64:
        return "超级管理员用户名长度不能超过 64 个字符。"
    return None


def _validate_superuser_password(password: str) -> Optional[str]:
    if len(password) < 6 or len(password) > 50:
        return "超级管理员密码长度需为 6 到 50 位。"

    categories = 0
    if re.search(r"[A-Za-z]", password):
        categories += 1
    if re.search(r"\d", password):
        categories += 1
    if re.search(r"[^\w\s]", password):
        categories += 1

    if categories < 2:
        return "超级管理员密码需至少包含字母、数字、特殊字符中的两类。"
    return None


def _collect_superuser_config(
    *,
    preset_username: Optional[str] = None,
    preset_password: Optional[str] = None,
) -> dict[str, str]:
    print_step("超级管理员配置")

    default_username = (
        preset_username or _env_default("SUPERUSER", "admin")
    ).strip() or "admin"
    while True:
        username = _prompt_text("超级管理员用户名", default=default_username).strip()
        error = _validate_superuser_name(username)
        if not error:
            break
        print(error)

    if preset_password is not None:
        password = preset_password.strip()
        if not password:
            return {"SUPERUSER": username}
        error = _validate_superuser_password(password)
        if error:
            raise RuntimeError(error)
        return {
            "SUPERUSER": username,
            "SUPERUSER_PASSWORD": password,
        }

    current_password = read_env_value("SUPERUSER_PASSWORD")
    while True:
        password = _prompt_secret_text(
            "超级管理员密码（留空则保留现有值或首次启动时随机生成）",
            current_value=current_password,
            allow_empty=True,
        ).strip()
        if not password:
            return {"SUPERUSER": username}

        error = _validate_superuser_password(password)
        if error:
            print(error)
            continue

        confirmed = _prompt_secret_text(
            "请再次输入超级管理员密码", required=True
        ).strip()
        if password != confirmed:
            print("两次输入的超级管理员密码不一致，请重新输入。")
            continue

        return {
            "SUPERUSER": username,
            "SUPERUSER_PASSWORD": password,
        }


def _collect_path_mapping() -> list[tuple[str, str]]:
    if not _prompt_yes_no("是否配置下载器路径映射", default=False):
        return []

    storage_path = _prompt_path(
        "MoviePilot 可访问的下载目录根路径", default=ROOT.parent / "downloads"
    )
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


def _collect_database_config() -> dict[str, Any]:
    print_step("数据库配置")
    current_db_type = _env_default("DB_TYPE", "sqlite").lower()
    if current_db_type not in {"sqlite", "postgresql"}:
        current_db_type = "sqlite"

    db_type = _prompt_choice(
        "选择数据库类型",
        {
            "sqlite": "SQLite",
            "postgresql": "PostgreSQL",
        },
        default=current_db_type,
    )

    config: dict[str, Any] = {
        "DB_TYPE": db_type,
    }
    if db_type == "sqlite":
        return config

    config.update(
        {
            "DB_POSTGRESQL_HOST": _prompt_text(
                "PostgreSQL 主机地址",
                default=_env_default("DB_POSTGRESQL_HOST", "localhost"),
            ),
            "DB_POSTGRESQL_PORT": _prompt_text(
                "PostgreSQL 端口",
                default=str(_env_int("DB_POSTGRESQL_PORT", 5432)),
            ),
            "DB_POSTGRESQL_DATABASE": _prompt_text(
                "PostgreSQL 数据库名（需已创建）",
                default=_env_default("DB_POSTGRESQL_DATABASE", "moviepilot"),
            ),
            "DB_POSTGRESQL_USERNAME": _prompt_text(
                "PostgreSQL 用户名",
                default=_env_default("DB_POSTGRESQL_USERNAME", "moviepilot"),
            ),
            "DB_POSTGRESQL_PASSWORD": _prompt_secret_text(
                "PostgreSQL 密码",
                current_value=read_env_value("DB_POSTGRESQL_PASSWORD"),
                allow_empty=True,
            ),
        }
    )
    return config


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
        apikey = _prompt_text("qBittorrent API Key（可选，5.2+ 推荐）", allow_empty=True, default="")
        username = _prompt_text("qBittorrent 用户名", default="admin") if not apikey else ""
        password = _prompt_text("qBittorrent 密码", secret=True, allow_empty=bool(apikey)) if not apikey else ""
        category = _prompt_yes_no("是否启用 qBittorrent 分类", default=False)
        return {
            "name": config_name,
            "type": "qbittorrent",
            "default": True,
            "enabled": True,
            "config": {
                "host": host,
                "apikey": apikey,
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
            "zspace": "极影视",
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
        "zspace": "http://127.0.0.1:8096",
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
    elif server_type == "zspace":
        config = {
            "host": host,
            "username": _prompt_text("极影视 用户名"),
            "password": _prompt_text("极影视 密码", secret=True),
        }
    else:
        config = {
            "host": host,
            "apikey": _prompt_text("媒体服务器 API Key", secret=True),
        }
        if server_type == "emby":
            username = _prompt_text(
                "Emby 管理员用户名（可选）", default="", allow_empty=True
            )
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
            "feishu": "飞书",
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
        api_url = _prompt_text(
            "自定义 Telegram API 地址（可选）", default="", allow_empty=True
        )
        if api_url:
            config["API_URL"] = api_url
    elif notification_type == "wechat":
        config = {
            "WECHAT_MODE": "bot",
            "WECHAT_BOT_ID": _prompt_text("企业微信机器人 ID"),
            "WECHAT_BOT_SECRET": _prompt_text("企业微信机器人 Secret", secret=True),
        }
        chat_id = _prompt_text("默认发送对象（可选）", default="", allow_empty=True)
        admins = _prompt_text(
            "管理员用户列表，多个逗号分隔（可选）", default="", allow_empty=True
        )
        if chat_id:
            config["WECHAT_BOT_CHAT_ID"] = chat_id
        if admins:
            config["WECHAT_ADMINS"] = admins
    elif notification_type == "feishu":
        config = {
            "FEISHU_APP_ID": _prompt_text("飞书应用 App ID"),
            "FEISHU_APP_SECRET": _prompt_text("飞书应用 App Secret", secret=True),
        }
        open_id = _prompt_text("默认接收用户 Open ID（可选）", default="", allow_empty=True)
        chat_id = _prompt_text("默认接收群聊 Chat ID（可选）", default="", allow_empty=True)
        admins = _prompt_text(
            "管理员 Open ID 列表，多个逗号分隔（可选）", default="", allow_empty=True
        )
        verification_token = _prompt_text("飞书事件 Verification Token（可选）", default="", allow_empty=True)
        encrypt_key = _prompt_text("飞书事件 Encrypt Key（可选）", default="", allow_empty=True)
        if open_id:
            config["FEISHU_OPEN_ID"] = open_id
        if chat_id:
            config["FEISHU_CHAT_ID"] = chat_id
        if admins:
            config["FEISHU_ADMINS"] = admins
        if verification_token:
            config["FEISHU_VERIFICATION_TOKEN"] = verification_token
        if encrypt_key:
            config["FEISHU_ENCRYPT_KEY"] = encrypt_key
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


def _collect_agent_config(
    runtime_python: Optional[Path] = None,
) -> dict[str, Any]:
    print_step("AI Agent 配置")
    enabled = _prompt_yes_no(
        "是否启用 AI 智能体",
        default=_env_bool("AI_AGENT_ENABLE", False),
    )
    if not enabled:
        return {
            "AI_AGENT_ENABLE": False,
            "AI_AGENT_GLOBAL": False,
        }

    provider_definitions = _load_llm_provider_definitions(runtime_python=runtime_python)
    provider_choices = _llm_provider_choice_map(provider_definitions)
    current_provider = _env_default("LLM_PROVIDER", "deepseek").lower()
    if current_provider not in provider_choices:
        current_provider = "deepseek"

    while True:
        provider = _prompt_provider_choice(
            "选择 LLM 提供商",
            provider_choices,
            default=current_provider,
        )
        provider_meta = _llm_provider_meta(provider, provider_definitions)
        if provider_meta.get("supports_api_key") is False:
            print_step(
                f"{provider_meta.get('name') or provider} 当前仅支持交互式授权，安装向导暂不支持，请改选可填写 API Key 的 provider。"
            )
            current_provider = "deepseek"
            continue
        break

    defaults = _llm_provider_defaults(provider, provider_definitions)
    current_model = _env_default("LLM_MODEL", defaults["model"])
    current_base_url = _env_default("LLM_BASE_URL", defaults["base_url"])
    current_base_url_preset = _env_default(
        "LLM_BASE_URL_PRESET", defaults.get("base_url_preset", "")
    )
    api_key_label = str(provider_meta.get("api_key_label") or "API Key").strip() or "API Key"
    api_key_hint = str(provider_meta.get("api_key_hint") or "").strip()
    requires_base_url = bool(provider_meta.get("requires_base_url"))
    base_url_label = (
        "自定义 Google API Base URL（可选）"
        if provider == "google"
        else "LLM Base URL（必填）"
        if requires_base_url
        else "LLM Base URL"
    )
    if api_key_hint:
        print_step(api_key_hint)

    config: dict[str, Any] = {
        "AI_AGENT_ENABLE": True,
        "AI_AGENT_GLOBAL": _prompt_yes_no(
            "是否启用全局 AI 智能体",
            default=_env_bool("AI_AGENT_GLOBAL", False),
        ),
        "LLM_PROVIDER": provider,
        "LLM_API_KEY": _prompt_secret_text(
            f"LLM {api_key_label}",
            current_value=read_env_value("LLM_API_KEY"),
            required=True,
        ),
        "LLM_THINKING_LEVEL": _prompt_choice(
            "LLM 思考模式/深度",
            choices={
                "off": "关闭思考",
                "auto": "自动",
                "minimal": "最小",
                "low": "低",
                "medium": "中",
                "high": "高",
                "max": "极高",
                "xhigh": "超高",
            },
            default=_env_llm_thinking_level_default(),
        ),
        "LLM_SUPPORT_IMAGE_INPUT": _prompt_yes_no(
            "是否启用图片输入支持",
            default=_env_bool("LLM_SUPPORT_IMAGE_INPUT", True),
        ),
        "LLM_BASE_URL_PRESET": current_base_url_preset,
    }

    base_url_presets = provider_meta.get("base_url_presets") or []
    if isinstance(base_url_presets, list):
        duplicate_value_presets = []
        normalized_current_base_url = current_base_url.strip()
        for item in base_url_presets:
            if not isinstance(item, dict):
                continue
            preset_value = str(item.get("value") or "").strip()
            preset_id = str(item.get("id") or "").strip()
            if not preset_id or preset_value != normalized_current_base_url:
                continue
            duplicate_value_presets.append(item)

        if len(duplicate_value_presets) > 1:
            choices: dict[str, str] = {}
            default_preset = current_base_url_preset
            if not default_preset or default_preset not in {
                str(item.get("id") or "").strip() for item in duplicate_value_presets
            }:
                default_preset = str((duplicate_value_presets[0] or {}).get("id") or "").strip()
            for item in duplicate_value_presets:
                preset_id = str(item.get("id") or "").strip()
                preset_label = str(item.get("label") or preset_id).strip()
                if preset_id:
                    choices[preset_id] = preset_label
            if choices:
                config["LLM_BASE_URL_PRESET"] = _prompt_choice(
                    "LLM Base URL 预设",
                    choices=choices,
                    default=default_preset,
                )

    config["LLM_BASE_URL"] = _prompt_text(
        base_url_label,
        default=current_base_url,
        allow_empty=not requires_base_url,
    )
    models = _load_llm_models(
        provider=provider,
        api_key=config["LLM_API_KEY"],
        base_url=config["LLM_BASE_URL"],
        base_url_preset=config["LLM_BASE_URL_PRESET"],
        runtime_python=runtime_python,
    )
    config["LLM_MODEL"] = _prompt_model_choice(models, default=current_model)

    return config


def _load_auth_site_definitions_inner() -> dict[str, Any]:
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))

    from app.helper.sites import SitesHelper  # noqa

    auth_sites = SitesHelper().get_authsites() or {}
    definitions: dict[str, Any] = {}
    for site_key, site_conf in auth_sites.items():
        site_name = str(site_conf.get("name") or site_key).strip()
        params: dict[str, Any] = {}
        for param_key, param_conf in (site_conf.get("params") or {}).items():
            params[param_key] = {
                "name": str(param_conf.get("name") or param_key).strip(),
                "type": str(param_conf.get("type") or "text").strip().lower(),
                "placeholder": str(param_conf.get("placeholder") or "").strip(),
                "tooltip": str(param_conf.get("tooltip") or "").strip(),
                "convert": str(param_conf.get("convert") or "").strip().lower(),
            }
        if params:
            definitions[site_key] = {
                "name": site_name,
                "params": params,
            }
    return definitions


def _load_auth_site_definitions(
    runtime_python: Optional[Path] = None,
) -> dict[str, Any]:
    try:
        return _load_auth_site_definitions_inner()
    except Exception as exc:
        if runtime_python and not _current_python_matches(runtime_python):
            try:
                with TemporaryDirectory() as temp_dir:
                    output_path = Path(temp_dir) / "auth-sites.json"
                    subprocess.run(
                        [
                            str(runtime_python),
                            str(Path(__file__).resolve()),
                            "query-auth-sites",
                            "--output-json-file",
                            str(output_path),
                        ],
                        cwd=str(ROOT),
                        check=True,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    data = json.loads(output_path.read_text(encoding="utf-8"))
                    if isinstance(data, dict):
                        return data
            except Exception as runtime_exc:
                print_step(
                    f"当前环境暂时无法读取站点认证资源，已跳过站点认证配置：{runtime_exc}"
                )
                return {}

        print_step(f"当前环境暂时无法读取站点认证资源，已跳过站点认证配置：{exc}")
        return {}


def _print_auth_sites(auth_sites: dict[str, Any]) -> None:
    print("可用认证站点：")
    items = [
        f"{site_key}({site_conf.get('name') or site_key})"
        for site_key, site_conf in sorted(auth_sites.items())
    ]
    line: list[str] = []
    for item in items:
        line.append(item)
        if len(line) >= 4:
            print(f"  {'  '.join(line)}")
            line = []
    if line:
        print(f"  {'  '.join(line)}")


def _prompt_auth_param(param_key: str, param_meta: dict[str, Any]) -> Any:
    label = str(param_meta.get("name") or param_key).strip()
    placeholder = str(param_meta.get("placeholder") or "").strip()
    tooltip = str(param_meta.get("tooltip") or "").strip()
    prompt_label = label if not placeholder else f"{label} ({placeholder})"
    if tooltip:
        print(f"{prompt_label}：{tooltip}")

    while True:
        if str(param_meta.get("type") or "text").strip().lower() == "password":
            value = _prompt_secret_text(prompt_label, required=True)
        else:
            value = _prompt_text(prompt_label)

        if str(param_meta.get("convert") or "").strip().lower() != "int":
            return value

        try:
            return int(value)
        except ValueError:
            print("请输入有效数字。")


def _collect_site_auth_config(
    runtime_python: Optional[Path] = None,
) -> Optional[dict[str, Any]]:
    print_step("用户站点认证配置")
    if not _prompt_yes_no("是否配置用户站点认证", default=False):
        return None

    auth_sites = _load_auth_site_definitions(runtime_python=runtime_python)
    if not auth_sites:
        print_step("未能读取可用站点认证清单，已跳过用户站点认证配置")
        return None

    _print_auth_sites(auth_sites)
    while True:
        selected_site = _prompt_text("请输入认证站点代号").strip().lower()
        if selected_site in auth_sites:
            break
        print("请输入上面列表中的站点代号。")

    site_conf = auth_sites[selected_site]
    print_step(f"正在配置站点认证：{site_conf.get('name') or selected_site}")
    params = {
        param_key: _prompt_auth_param(param_key, param_meta)
        for param_key, param_meta in (site_conf.get("params") or {}).items()
    }
    return {
        "site": selected_site,
        "params": params,
    }


def _collect_autostart_config() -> dict[str, Any]:
    print_step("开机自启配置")
    current_status = _autostart_status()
    default_enabled = bool(current_status.get("enabled")) or _env_bool(
        AUTOSTART_ENV_KEY, False
    )
    if current_status.get("enabled"):
        print(
            f"当前已检测到开机自启：{current_status.get('label') or _startup_platform_name()}"
        )
    else:
        print(f"当前系统将使用：{_startup_platform_name()}")

    enabled = _prompt_yes_no("是否设置开机自启", default=default_enabled)
    return {"enabled": enabled}


def run_setup_wizard(
    force_token: bool,
    runtime_python: Optional[Path] = None,
    preset_superuser: Optional[str] = None,
    preset_superuser_password: Optional[str] = None,
) -> dict[str, Any]:
    if not _is_interactive():
        raise RuntimeError(
            "交互式向导需要在终端中运行，请直接执行 moviepilot setup --wizard 或 moviepilot init --wizard"
        )

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
                    custom_token = _prompt_text(
                        "请输入新的 API_TOKEN（至少 16 位）", secret=True
                    )
                    if len(custom_token) >= 16:
                        api_token = ensure_api_token(
                            force_token=True, token=custom_token
                        )
                        break
                    print("API_TOKEN 长度不能少于 16 个字符。")
    else:
        if _prompt_yes_no("是否自动生成 API_TOKEN", default=True):
            api_token = ensure_api_token(
                force_token=force_token or bool(existing_token)
            )
        else:
            while True:
                custom_token = _prompt_text(
                    "请输入 API_TOKEN（至少 16 位）", secret=True
                )
                if len(custom_token) >= 16:
                    api_token = ensure_api_token(force_token=True, token=custom_token)
                    break
                print("API_TOKEN 长度不能少于 16 个字符。")

    return {
        "api_token": api_token,
        "env_settings": {
            **_collect_superuser_config(
                preset_username=preset_superuser,
                preset_password=preset_superuser_password,
            ),
            **_collect_database_config(),
            **_collect_agent_config(runtime_python=runtime_python),
        },
        "directories": [_collect_directory_config()],
        "downloader": _collect_downloader_config(),
        "mediaserver": _collect_media_server_config(),
        "notification": _collect_notification_config(),
        "site_auth": _collect_site_auth_config(runtime_python=runtime_python),
        "autostart": _collect_autostart_config(),
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
    max_priority = max(
        (int(item.get("priority", 0) or 0) for item in merged), default=-1
    )
    new_copy["priority"] = (
        max_priority + 1 if merged else int(new_item.get("priority", 0) or 0)
    )
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

    preferred_order = [
        switch for switch in NOTIFICATION_SWITCH_TYPES if switch in merged
    ]
    extras = [key for key in merged if key not in preferred_order]
    return [merged[key] for key in [*preferred_order, *extras]]


def _apply_local_system_config_inner(config_payload: dict[str, Any]) -> None:
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
        raise RuntimeError(
            "当前环境尚未安装 MoviePilot 运行依赖，请先执行 moviepilot install deps 或 moviepilot setup"
        ) from exc

    init_db()
    generated_password = _prepare_superuser_password_for_bootstrap()
    update_db()
    _ensure_superuser_account_inner()
    if generated_password:
        print_step(f"超级管理员初始密码：{generated_password}")

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
        current_notifications = _merge_named_item(
            current_notifications, notification_item
        )
        system_config.set(SystemConfigKey.Notifications, current_notifications)
        current_switches = system_config.get(SystemConfigKey.NotificationSwitchs) or []
        system_config.set(
            SystemConfigKey.NotificationSwitchs,
            _merge_notification_switches(current_switches),
        )

    site_auth_item = config_payload.get("site_auth")
    if (
        isinstance(site_auth_item, dict)
        and site_auth_item.get("site")
        and site_auth_item.get("params")
    ):
        system_config.set(SystemConfigKey.UserSiteAuthParams, site_auth_item)
        try:
            from app.helper.sites import SitesHelper  # noqa

            status, msg = SitesHelper().check_user(
                site_auth_item.get("site"), site_auth_item.get("params")
            )
            if status:
                print_step(f"站点认证校验成功：{msg}")
            else:
                print_step(f"已保存站点认证配置，当前校验未通过：{msg}")
        except Exception as exc:
            print_step(f"已保存站点认证配置，当前未完成校验：{exc}")

    system_config.set(SystemConfigKey.SetupWizardState, True)
    print_step("已写入本地系统配置")


def _current_python_matches(target_python: Optional[Path]) -> bool:
    if not target_python:
        return True
    current_python = Path(sys.executable).expanduser()
    target_python = target_python.expanduser()
    if not current_python.is_absolute():
        current_python = (ROOT / current_python).absolute()
    if not target_python.is_absolute():
        target_python = (ROOT / target_python).absolute()
    return str(current_python) == str(target_python)


def _ensure_superuser_account_inner() -> None:
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))

    from app.core.config import settings
    from app.core.security import get_password_hash
    from app.db.user_oper import UserOper

    username = str(settings.SUPERUSER or "").strip()
    username_error = _validate_superuser_name(username)
    if username_error:
        raise RuntimeError(username_error)

    password = str(settings.SUPERUSER_PASSWORD or "").strip()
    if password:
        password_error = _validate_superuser_password(password)
        if password_error:
            raise RuntimeError(password_error)

    user_oper = UserOper()
    user = user_oper.get_by_name(username)
    if not user:
        init_password = password or secrets.token_urlsafe(16)
        user_oper.add(
            name=username,
            email="admin@movie-pilot.org",
            hashed_password=get_password_hash(init_password),
            is_active=True,
            is_superuser=True,
            avatar="",
        )
        print_step(f"已创建超级管理员用户：{username}")
        if not password:
            print_step(f"超级管理员初始密码：{init_password}")
        return

    update_payload: dict[str, Any] = {}
    if not user.is_active:
        update_payload["is_active"] = True
    if not user.is_superuser:
        update_payload["is_superuser"] = True
    if password:
        update_payload["hashed_password"] = get_password_hash(password)

    if update_payload:
        user.update(user_oper._db, update_payload)
        if password:
            print_step(f"已同步超级管理员账号与密码：{username}")
        else:
            print_step(f"已同步超级管理员账号权限：{username}")
    else:
        print_step(f"已确认超级管理员账号：{username}")


def _prepare_superuser_password_for_bootstrap() -> Optional[str]:
    from app.core.config import settings
    from app.db.user_oper import UserOper

    username = str(settings.SUPERUSER or "").strip()
    username_error = _validate_superuser_name(username)
    if username_error:
        raise RuntimeError(username_error)

    if str(settings.SUPERUSER_PASSWORD or "").strip():
        return None

    if UserOper().get_by_name(username):
        return None

    generated_password = secrets.token_urlsafe(16)
    settings.SUPERUSER_PASSWORD = generated_password
    return generated_password


def _sync_superuser_account_inner() -> None:
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))

    try:
        from app.db.init import init_db, update_db
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "当前环境尚未安装 MoviePilot 运行依赖，请先执行 moviepilot install deps 或 moviepilot setup"
        ) from exc

    init_db()
    generated_password = _prepare_superuser_password_for_bootstrap()
    update_db()
    _ensure_superuser_account_inner()
    if generated_password:
        print_step(f"超级管理员初始密码：{generated_password}")


def sync_superuser_account(runtime_python: Optional[Path] = None) -> None:
    if _current_python_matches(runtime_python):
        _sync_superuser_account_inner()
        return

    run(
        [
            str(runtime_python),
            str(Path(__file__).resolve()),
            "sync-superuser",
        ],
        cwd=ROOT,
    )


def apply_local_system_config(
    config_payload: dict[str, Any], runtime_python: Optional[Path] = None
) -> None:
    if _current_python_matches(runtime_python):
        _apply_local_system_config_inner(config_payload)
        return

    with TemporaryDirectory() as temp_dir:
        payload_path = Path(temp_dir) / "moviepilot-config.json"
        payload_path.write_text(
            json.dumps(config_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        run(
            [
                str(runtime_python),
                str(Path(__file__).resolve()),
                "apply-config",
                "--config-json-file",
                str(payload_path),
            ],
            cwd=ROOT,
        )


def _apply_autostart_choice(
    autostart_payload: Optional[dict[str, Any]],
    *,
    config_dir: Path,
    runtime_python: Optional[Path],
    venv_dir: Optional[Path],
) -> None:
    if not isinstance(autostart_payload, dict):
        return

    if autostart_payload.get("enabled"):
        result = enable_autostart(
            config_dir=config_dir,
            runtime_python=runtime_python,
            venv_dir=venv_dir,
        )
        print_step(f"已启用开机自启：{result.get('method')}")
        if result.get("artifact"):
            print(f"  注册文件：{result['artifact']}")
        if result.get("note"):
            print(f"  说明：{result['note']}")
        return

    result = disable_autostart()
    removed_paths = result.get("removed_paths") or []
    if removed_paths:
        print_step("已取消开机自启注册")
    else:
        print_step("当前未配置开机自启，无需取消")


def init_local(
    *,
    resources_repo: Optional[Path],
    resource_dir: Optional[Path],
    skip_resources: bool,
    resources_ready: bool,
    force_token: bool,
    wizard: bool,
    superuser: Optional[str],
    superuser_password: Optional[str],
    runtime_python: Optional[Path] = None,
    venv_dir: Optional[Path] = None,
) -> None:
    ensure_local_dirs()

    wizard_payload: Optional[dict[str, Any]] = None
    direct_env_settings: dict[str, str] = {}
    if superuser:
        superuser = superuser.strip()
        error = _validate_superuser_name(superuser)
        if error:
            raise RuntimeError(error)
        direct_env_settings["SUPERUSER"] = superuser
    if superuser_password is not None:
        superuser_password = superuser_password.strip()
        if superuser_password:
            error = _validate_superuser_password(superuser_password)
            if error:
                raise RuntimeError(error)
        direct_env_settings["SUPERUSER_PASSWORD"] = superuser_password

    if wizard:
        wizard_payload = run_setup_wizard(
            force_token=force_token,
            runtime_python=runtime_python,
            preset_superuser=direct_env_settings.get("SUPERUSER"),
            preset_superuser_password=direct_env_settings.get("SUPERUSER_PASSWORD"),
        )
    else:
        ensure_api_token(force_token=force_token)
        if direct_env_settings:
            write_env_values(direct_env_settings)
            print_step(f"已写入环境配置到 {ENV_FILE}")

    if wizard_payload and wizard_payload.get("env_settings"):
        write_env_values(wizard_payload["env_settings"])
        print_step(f"已写入环境配置到 {ENV_FILE}")

    if skip_resources:
        if resources_ready:
            print_step("资源文件已完成同步")
        else:
            print_step("已跳过资源初始化")
    else:
        install_resources(resources_repo=resources_repo, resource_dir=resource_dir)

    if wizard_payload:
        apply_local_system_config(wizard_payload, runtime_python=runtime_python)
    elif direct_env_settings:
        sync_superuser_account(runtime_python=runtime_python)

    if wizard_payload:
        try:
            _apply_autostart_choice(
                wizard_payload.get("autostart"),
                config_dir=CONFIG_DIR,
                runtime_python=runtime_python,
                venv_dir=venv_dir,
            )
        except Exception as exc:
            print_step(f"开机自启配置未完成：{exc}")


def install_deps(*, python_bin: str, venv_dir: Path, recreate: bool) -> Path:
    """
    创建或复用本地虚拟环境，并安装后端依赖和浏览器运行时。
    """
    ensure_supported_python(python_bin)
    venv_dir = venv_dir.expanduser().resolve()
    venv_python = get_venv_python(venv_dir)
    venv_pip = get_venv_pip(venv_dir)
    print_step(f"使用 Python 解释器：{python_bin}")

    if recreate and venv_dir.exists():
        print_step(f"删除已有虚拟环境：{venv_dir}")
        shutil.rmtree(venv_dir)

    if not venv_python.exists():
        print_step(f"创建虚拟环境：{venv_dir}")
        run([python_bin, "-m", "venv", str(venv_dir)])
    else:
        print_step(f"复用已有虚拟环境：{venv_dir}")

    if os.name == "nt":
        print_step("升级 pip")
        command = [str(venv_python), "-m", "pip", "install", "--upgrade", "pip"]
        run(command, env=build_package_install_env(), safe_command=redact_command(command))
    else:
        print_step("为虚拟环境配置 uv 兼容 pip 命令")
        venv_pip = configure_venv_pip_compat(venv_dir, venv_python)

    print_step("安装项目依赖")
    command = [str(venv_pip), "install", "-r", str(ROOT / "requirements.txt")]
    run(command, env=build_package_install_env(), safe_command=redact_command(command))
    install_browser_runtime(venv_python)
    return venv_python


def install_browser_runtime(venv_python: Path) -> None:
    """
    预下载 CloakBrowser 浏览器内核，避免首次仿真登录时才拉取大文件。
    """
    print_step("安装 CloakBrowser 浏览器内核")
    run([str(venv_python), "-m", "cloakbrowser", "install"])


def _startup_platform_name() -> str:
    system = platform.system()
    if system == "Darwin":
        return "macOS LaunchAgent"
    if system == "Linux":
        return "Linux systemd/XDG"
    if system == "Windows":
        return "Windows Startup"
    return system or "unknown"


def _runtime_python_candidates(
    runtime_python: Optional[Path], venv_dir: Optional[Path]
) -> list[Path]:
    candidates: list[Path] = []
    seen: set[str] = set()

    raw_candidates = [
        runtime_python,
        get_venv_python((venv_dir or (ROOT / "venv")).expanduser().resolve()),
        Path(sys.executable) if sys.executable else None,
    ]
    for candidate in raw_candidates:
        if not candidate:
            continue
        resolved = Path(candidate).expanduser().resolve()
        key = str(resolved)
        if key in seen:
            continue
        seen.add(key)
        candidates.append(resolved)
    return candidates


def _can_run_moviepilot_cli(python_bin: Path) -> bool:
    if not python_bin.exists():
        return False

    result = subprocess.run(
        [str(python_bin), "-m", "app.cli", "--help"],
        cwd=str(ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def _resolve_runtime_python_for_startup(
    runtime_python: Optional[Path], venv_dir: Optional[Path]
) -> Path:
    for candidate in _runtime_python_candidates(runtime_python, venv_dir):
        if _can_run_moviepilot_cli(candidate):
            return candidate

    raise RuntimeError(
        "未找到可用于启动 MoviePilot 的 Python 运行环境，请先执行 moviepilot install deps 或 moviepilot setup"
    )


def _linux_user_systemd_dir() -> Path:
    return (
        Path(os.getenv("XDG_CONFIG_HOME") or (Path.home() / ".config"))
        / "systemd"
        / "user"
    )


def _linux_xdg_autostart_dir() -> Path:
    return Path(os.getenv("XDG_CONFIG_HOME") or (Path.home() / ".config")) / "autostart"


def _macos_launch_agent_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{MACOS_LAUNCH_AGENT_LABEL}.plist"


def _linux_systemd_unit_path() -> Path:
    return _linux_user_systemd_dir() / LINUX_SYSTEMD_UNIT_NAME


def _linux_xdg_autostart_path() -> Path:
    return _linux_xdg_autostart_dir() / LINUX_XDG_AUTOSTART_FILENAME


def _windows_startup_dir() -> Path:
    appdata = os.getenv("APPDATA")
    if appdata:
        return (
            Path(appdata)
            / "Microsoft"
            / "Windows"
            / "Start Menu"
            / "Programs"
            / "Startup"
        )
    return (
        Path.home()
        / "AppData"
        / "Roaming"
        / "Microsoft"
        / "Windows"
        / "Start Menu"
        / "Programs"
        / "Startup"
    )


def _windows_startup_path() -> Path:
    return _windows_startup_dir() / WINDOWS_STARTUP_FILENAME


def _launcher_paths_for_platform(system_name: Optional[str] = None) -> list[Path]:
    system_name = system_name or platform.system()
    if system_name == "Windows":
        return [AUTOSTART_WINDOWS_LAUNCHER]
    return [AUTOSTART_UNIX_LAUNCHER]


def _cleanup_startup_launchers(system_name: Optional[str] = None) -> None:
    for path in _launcher_paths_for_platform(system_name):
        if path.exists():
            _remove_path(path)

    if AUTOSTART_RUNTIME_DIR.exists() and not any(AUTOSTART_RUNTIME_DIR.iterdir()):
        AUTOSTART_RUNTIME_DIR.rmdir()


def _write_unix_startup_launcher(config_dir: Path, python_bin: Path) -> Path:
    AUTOSTART_RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    launcher_content = textwrap.dedent(
        f"""\
        #!/usr/bin/env bash
        set -euo pipefail

        export CONFIG_DIR={shlex.quote(str(config_dir))}
        cd {shlex.quote(str(ROOT))}
        exec {shlex.quote(str(python_bin))} -m app.cli start --timeout {AUTOSTART_TIMEOUT}
        """
    )
    AUTOSTART_UNIX_LAUNCHER.write_text(launcher_content, encoding="utf-8")
    AUTOSTART_UNIX_LAUNCHER.chmod(0o755)
    return AUTOSTART_UNIX_LAUNCHER


def _write_windows_startup_launcher(config_dir: Path, python_bin: Path) -> Path:
    AUTOSTART_RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    launcher_content = textwrap.dedent(
        f"""\
        @echo off
        setlocal
        set "CONFIG_DIR={config_dir}"
        cd /d "{ROOT}"
        "{python_bin}" -m app.cli start --timeout {AUTOSTART_TIMEOUT}
        endlocal
        """
    )
    AUTOSTART_WINDOWS_LAUNCHER.write_text(launcher_content, encoding="utf-8")
    return AUTOSTART_WINDOWS_LAUNCHER


def _double_quote(value: Any) -> str:
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _run_optional_command(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="ignore",
        check=False,
    )


def _last_command_line(result: subprocess.CompletedProcess[str]) -> str:
    lines = [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]
    return lines[-1] if lines else "命令未返回更多信息"


def _linux_linger_enabled() -> Optional[bool]:
    loginctl_bin = shutil.which("loginctl")
    if not loginctl_bin:
        return None

    result = _run_optional_command(
        [loginctl_bin, "show-user", getpass.getuser(), "-p", "Linger", "--value"]
    )
    if result.returncode != 0:
        return None
    value = (result.stdout or "").strip().lower()
    if value in {"yes", "no"}:
        return value == "yes"
    return None


def _autostart_status() -> dict[str, Any]:
    system_name = platform.system()
    if system_name == "Darwin":
        artifact = _macos_launch_agent_path()
        return {
            "enabled": artifact.exists(),
            "method": "launchagent",
            "label": "LaunchAgent",
            "artifact": artifact,
        }
    if system_name == "Linux":
        systemd_unit = _linux_systemd_unit_path()
        if systemd_unit.exists():
            return {
                "enabled": True,
                "method": "systemd-user",
                "label": "systemd --user",
                "artifact": systemd_unit,
                "linger_enabled": _linux_linger_enabled(),
            }
        desktop_file = _linux_xdg_autostart_path()
        return {
            "enabled": desktop_file.exists(),
            "method": "xdg-autostart" if desktop_file.exists() else "none",
            "label": "XDG autostart" if desktop_file.exists() else "not-configured",
            "artifact": desktop_file if desktop_file.exists() else None,
        }
    if system_name == "Windows":
        artifact = _windows_startup_path()
        return {
            "enabled": artifact.exists(),
            "method": "startup-folder",
            "label": "Startup Folder",
            "artifact": artifact,
        }

    return {
        "enabled": False,
        "method": "unsupported",
        "label": _startup_platform_name(),
        "artifact": None,
    }


def _enable_autostart_macos(config_dir: Path, python_bin: Path) -> dict[str, Any]:
    launcher = _write_unix_startup_launcher(config_dir=config_dir, python_bin=python_bin)
    agent_path = _macos_launch_agent_path()
    agent_path.parent.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    plist_content = textwrap.dedent(
        f"""\
        <?xml version="1.0" encoding="UTF-8"?>
        <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
        <plist version="1.0">
          <dict>
            <key>Label</key>
            <string>{MACOS_LAUNCH_AGENT_LABEL}</string>
            <key>ProgramArguments</key>
            <array>
              <string>/bin/bash</string>
              <string>{launcher}</string>
            </array>
            <key>WorkingDirectory</key>
            <string>{ROOT}</string>
            <key>RunAtLoad</key>
            <true/>
            <key>StandardOutPath</key>
            <string>{LOG_DIR / "moviepilot.launchagent.stdout.log"}</string>
            <key>StandardErrorPath</key>
            <string>{LOG_DIR / "moviepilot.launchagent.stderr.log"}</string>
          </dict>
        </plist>
        """
    )
    agent_path.write_text(plist_content, encoding="utf-8")

    uid = str(os.getuid())
    _run_optional_command(["launchctl", "bootout", f"gui/{uid}", str(agent_path)])
    bootstrap_result = _run_optional_command(
        ["launchctl", "bootstrap", f"gui/{uid}", str(agent_path)]
    )
    if bootstrap_result.returncode != 0:
        note = _last_command_line(bootstrap_result)
    else:
        enable_result = _run_optional_command(
            ["launchctl", "enable", f"gui/{uid}/{MACOS_LAUNCH_AGENT_LABEL}"]
        )
        note = (
            _last_command_line(enable_result)
            if enable_result.returncode != 0
            else "已加载到当前登录会话"
        )

    write_env_value(AUTOSTART_ENV_KEY, "true")
    return {
        "method": "LaunchAgent",
        "artifact": agent_path,
        "note": note,
    }


def _enable_autostart_linux_systemd(
    config_dir: Path, python_bin: Path
) -> Optional[dict[str, Any]]:
    systemctl_bin = shutil.which("systemctl")
    if not systemctl_bin:
        return None

    launcher = _write_unix_startup_launcher(config_dir=config_dir, python_bin=python_bin)
    unit_path = _linux_systemd_unit_path()
    unit_path.parent.mkdir(parents=True, exist_ok=True)
    unit_content = textwrap.dedent(
        f"""\
        [Unit]
        Description=MoviePilot local autostart
        Wants=network-online.target
        After=network-online.target

        [Service]
        Type=oneshot
        WorkingDirectory={ROOT}
        ExecStart=/bin/bash {_double_quote(launcher)}

        [Install]
        WantedBy=default.target
        """
    )
    unit_path.write_text(unit_content, encoding="utf-8")

    _run_optional_command([systemctl_bin, "--user", "daemon-reload"])
    enable_result = _run_optional_command(
        [systemctl_bin, "--user", "enable", LINUX_SYSTEMD_UNIT_NAME]
    )
    if enable_result.returncode != 0:
        _remove_path(unit_path)
        _run_optional_command([systemctl_bin, "--user", "daemon-reload"])
        return None

    start_result = _run_optional_command(
        [systemctl_bin, "--user", "start", LINUX_SYSTEMD_UNIT_NAME]
    )
    desktop_path = _linux_xdg_autostart_path()
    if desktop_path.exists():
        _remove_path(desktop_path)
    note = (
        _last_command_line(start_result)
        if start_result.returncode != 0
        else "已注册 systemd --user 并尝试在当前会话执行一次"
    )
    linger_enabled = _linux_linger_enabled()
    if linger_enabled is False:
        note += "；如需无人登录时随系统启动，请手动执行 sudo loginctl enable-linger $USER"

    write_env_value(AUTOSTART_ENV_KEY, "true")
    return {
        "method": "systemd --user",
        "artifact": unit_path,
        "note": note,
    }


def _enable_autostart_linux_xdg(config_dir: Path, python_bin: Path) -> dict[str, Any]:
    launcher = _write_unix_startup_launcher(config_dir=config_dir, python_bin=python_bin)
    desktop_path = _linux_xdg_autostart_path()
    desktop_path.parent.mkdir(parents=True, exist_ok=True)
    unit_path = _linux_systemd_unit_path()
    if unit_path.exists():
        _remove_path(unit_path)
        systemctl_bin = shutil.which("systemctl")
        if systemctl_bin:
            _run_optional_command([systemctl_bin, "--user", "daemon-reload"])
    desktop_content = textwrap.dedent(
        f"""\
        [Desktop Entry]
        Type=Application
        Version=1.0
        Name=MoviePilot
        Comment=Start MoviePilot on login
        Exec=/bin/bash {_double_quote(launcher)}
        Path={ROOT}
        Terminal=false
        X-GNOME-Autostart-enabled=true
        """
    )
    desktop_path.write_text(desktop_content, encoding="utf-8")
    write_env_value(AUTOSTART_ENV_KEY, "true")
    return {
        "method": "XDG autostart",
        "artifact": desktop_path,
        "note": "当前环境未启用 systemd --user，已回退为图形会话登录自启动",
    }


def _enable_autostart_windows(config_dir: Path, python_bin: Path) -> dict[str, Any]:
    launcher = _write_windows_startup_launcher(config_dir=config_dir, python_bin=python_bin)
    startup_path = _windows_startup_path()
    startup_path.parent.mkdir(parents=True, exist_ok=True)
    startup_content = textwrap.dedent(
        f"""\
        @echo off
        call "{launcher}"
        """
    )
    startup_path.write_text(startup_content, encoding="utf-8")
    write_env_value(AUTOSTART_ENV_KEY, "true")
    return {
        "method": "Startup Folder",
        "artifact": startup_path,
        "note": "将在当前用户登录 Windows 后自动启动",
    }


def enable_autostart(
    *, config_dir: Path, runtime_python: Optional[Path], venv_dir: Optional[Path]
) -> dict[str, Any]:
    config_dir = config_dir.expanduser().resolve()
    python_bin = _resolve_runtime_python_for_startup(runtime_python, venv_dir)
    system_name = platform.system()

    if system_name == "Darwin":
        return _enable_autostart_macos(config_dir=config_dir, python_bin=python_bin)
    if system_name == "Linux":
        return _enable_autostart_linux_systemd(
            config_dir=config_dir, python_bin=python_bin
        ) or _enable_autostart_linux_xdg(config_dir=config_dir, python_bin=python_bin)
    if system_name == "Windows":
        return _enable_autostart_windows(config_dir=config_dir, python_bin=python_bin)

    raise RuntimeError(f"当前系统暂不支持自动注册开机自启：{platform.system()}")


def disable_autostart() -> dict[str, Any]:
    system_name = platform.system()
    removed_paths: list[Path] = []

    if system_name == "Darwin":
        agent_path = _macos_launch_agent_path()
        uid = str(os.getuid())
        _run_optional_command(["launchctl", "bootout", f"gui/{uid}", str(agent_path)])
        if agent_path.exists():
            _remove_path(agent_path)
            removed_paths.append(agent_path)
        _cleanup_startup_launchers(system_name)
    elif system_name == "Linux":
        systemctl_bin = shutil.which("systemctl")
        unit_path = _linux_systemd_unit_path()
        desktop_path = _linux_xdg_autostart_path()
        if systemctl_bin:
            _run_optional_command(
                [systemctl_bin, "--user", "disable", LINUX_SYSTEMD_UNIT_NAME]
            )
            _run_optional_command([systemctl_bin, "--user", "daemon-reload"])
        for path in (unit_path, desktop_path):
            if path.exists():
                _remove_path(path)
                removed_paths.append(path)
        _cleanup_startup_launchers(system_name)
    elif system_name == "Windows":
        startup_path = _windows_startup_path()
        for path in (startup_path, AUTOSTART_WINDOWS_LAUNCHER):
            if path.exists():
                _remove_path(path)
                removed_paths.append(path)
        _cleanup_startup_launchers(system_name)
    else:
        raise RuntimeError(f"当前系统暂不支持自动取消开机自启：{platform.system()}")

    write_env_value(AUTOSTART_ENV_KEY, "false")
    return {"removed_paths": removed_paths}


def print_autostart_status() -> None:
    status = _autostart_status()
    if not status.get("enabled"):
        print_step(f"当前未启用开机自启（{_startup_platform_name()}）")
        return

    print_step(
        f"当前已启用开机自启：{status.get('label') or _startup_platform_name()}"
    )
    artifact = status.get("artifact")
    if artifact:
        print(f"  注册文件：{artifact}")
    linger_enabled = status.get("linger_enabled")
    if linger_enabled is False:
        print(
            "  说明：当前为 systemd --user 模式，通常会在用户登录后启动；如需无人登录即启动，请手动启用 linger。"
        )


def _read_runtime_file(path: Path) -> Optional[dict[str, Any]]:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _read_process_start_time(pid: int) -> Optional[float]:
    try:
        output = capture(["ps", "-p", str(pid), "-o", "lstart="])
    except (OSError, subprocess.CalledProcessError):
        return None

    started = output.strip()
    if not started:
        return None

    try:
        return datetime.strptime(started, "%a %b %d %H:%M:%S %Y").timestamp()
    except ValueError:
        return None


def _services_running() -> list[str]:
    running: list[str] = []
    runtime_files = {
        "backend": TEMP_DIR / "moviepilot.runtime.json",
        "frontend": TEMP_DIR / "moviepilot.frontend.runtime.json",
    }
    for name, runtime_file in runtime_files.items():
        payload = _read_runtime_file(runtime_file)
        pid = payload.get("pid") if isinstance(payload, dict) else None
        if not pid:
            continue

        try:
            pid_int = int(pid)
        except (TypeError, ValueError):
            continue

        if not _pid_exists(pid_int):
            continue

        runtime_start_time = payload.get("create_time") if isinstance(payload, dict) else None
        process_start_time = _read_process_start_time(pid_int)
        if runtime_start_time is not None and process_start_time is not None:
            try:
                if abs(process_start_time - float(runtime_start_time)) > 3:
                    continue
            except (TypeError, ValueError):
                pass

        running.append(name)
    return running


def ensure_services_stopped() -> None:
    running = _services_running()
    if running:
        raise RuntimeError(
            "检测到本地服务仍在运行（%s），请先执行 `moviepilot stop` 后再更新。"
            % ", ".join(running)
        )


def _stop_managed_services(venv_dir: Path) -> None:
    venv_dir = venv_dir.expanduser().resolve()
    venv_python = get_venv_python(venv_dir)
    if venv_python.exists():
        print_step("停止本地前后端服务")
        run(
            [str(venv_python), "-m", "app.cli", "stop", "--timeout", "30", "--force"],
            cwd=ROOT,
        )
        return

    running = _services_running()
    if running:
        raise RuntimeError(
            "检测到本地服务仍在运行（%s），但当前未找到虚拟环境 %s，无法安全停止。"
            " 请先执行 `moviepilot stop`，或在卸载时通过 `--venv PATH` 指定正确的虚拟环境目录。"
            % (", ".join(running), venv_dir)
        )


def _collect_cli_link_candidates(
    *, command_path: Optional[str] = None, launch_path: Optional[str] = None
) -> list[Path]:
    candidates: list[Path] = []
    seen: set[str] = set()
    raw_candidates = [
        launch_path,
        command_path,
        os.getenv("MOVIEPILOT_LAUNCH_PATH"),
        os.getenv("MOVIEPILOT_COMMAND_PATH"),
        shutil.which("moviepilot"),
    ]

    for raw_value in raw_candidates:
        if not raw_value:
            continue
        candidate = Path(raw_value).expanduser()
        if not candidate.is_absolute():
            candidate = (ROOT / candidate).resolve()
        try:
            key = str(candidate.resolve())
        except OSError:
            key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        candidates.append(candidate)
    return candidates


def _remove_cli_symlinks(
    *, command_path: Optional[str] = None, launch_path: Optional[str] = None
) -> list[Path]:
    removed: list[Path] = []
    script_path = (ROOT / "moviepilot").resolve()

    for candidate in _collect_cli_link_candidates(
        command_path=command_path, launch_path=launch_path
    ):
        if not candidate.is_symlink():
            continue
        try:
            if candidate.resolve() != script_path:
                continue
        except OSError:
            continue
        candidate.unlink()
        removed.append(candidate)
    return removed


def _remove_runtime_state_files() -> list[Path]:
    removed: list[Path] = []
    for path in (
        TEMP_DIR / "moviepilot.runtime.json",
        TEMP_DIR / "moviepilot.frontend.runtime.json",
    ):
        if not path.exists():
            continue
        _remove_path(path)
        removed.append(path)
    return removed


def _remove_installed_resource_files() -> list[Path]:
    removed: list[Path] = []
    seen: set[Path] = set()
    for pattern in RESOURCE_FILE_PATTERNS:
        for path in sorted(HELPER_DIR.glob(pattern)):
            if path in seen or not path.exists() or path.is_dir():
                continue
            _remove_path(path)
            removed.append(path)
            seen.add(path)
    return removed


def _remove_config_data(config_dir: Path) -> list[Path]:
    config_dir = config_dir.expanduser().resolve()
    removed: list[Path] = []

    if config_dir.exists():
        _remove_path(config_dir)
        removed.append(config_dir)
    return removed


def uninstall_local(
    *,
    venv_dir: Path,
    config_dir: Path,
    command_path: Optional[str] = None,
    launch_path: Optional[str] = None,
) -> dict[str, Any]:
    if not _is_interactive():
        raise RuntimeError("卸载命令需要在交互式终端中运行，以完成两次确认。")

    venv_dir = venv_dir.expanduser().resolve()
    config_dir = config_dir.expanduser().resolve()
    cli_links = _collect_cli_link_candidates(
        command_path=command_path, launch_path=launch_path
    )
    script_path = (ROOT / "moviepilot").resolve()
    linked_cli_paths = [
        path
        for path in cli_links
        if path.is_symlink() and path.exists() and path.resolve() == script_path
    ]
    autostart_status = _autostart_status()

    delete_config = _prompt_yes_no(
        f"是否同时删除配置目录 {config_dir}", default=False
    )

    print_step("卸载将执行以下操作")
    print(f"  - 保留源码目录：{ROOT}")
    print(f"  - 删除虚拟环境：{venv_dir}")
    print(f"  - 删除前端运行时目录：{PUBLIC_DIR}")
    print(f"  - 删除本地 Node 运行时目录：{RUNTIME_DIR}")
    print(f"  - 删除资源文件：{HELPER_DIR}/sites*、{HELPER_DIR}/user.sites*.bin")
    if linked_cli_paths:
        print("  - 删除全局 CLI 软链接：")
        for path in linked_cli_paths:
            print(f"    {path}")
    else:
        print("  - 未检测到指向当前仓库的全局 CLI 软链接")
    if autostart_status.get("enabled"):
        print(
            f"  - 取消开机自启：{autostart_status.get('label') or _startup_platform_name()}"
        )
    else:
        print("  - 当前未配置开机自启")

    if delete_config:
        print(f"  - 删除配置目录：{config_dir}")
        if config_dir == LEGACY_CONFIG_DIR.resolve():
            print("    包括 legacy config 目录中的 category.yaml 等配置文件")
    else:
        print(f"  - 保留配置目录：{config_dir}")

    if not _prompt_yes_no("第一次确认：是否继续卸载 MoviePilot", default=False):
        print_step("已取消卸载")
        return {"cancelled": True}

    confirm_text = _prompt_text(
        f"第二次确认：请输入 {UNINSTALL_CONFIRM_TEXT} 以继续",
        allow_empty=False,
    )
    if confirm_text != UNINSTALL_CONFIRM_TEXT:
        print_step("确认文本不匹配，已取消卸载")
        return {"cancelled": True}

    _stop_managed_services(venv_dir=venv_dir)
    if autostart_status.get("enabled"):
        disable_autostart()

    removed_paths: list[Path] = []
    removed_paths.extend(
        _remove_cli_symlinks(command_path=command_path, launch_path=launch_path)
    )
    removed_paths.extend(_remove_runtime_state_files())
    removed_paths.extend(_remove_installed_resource_files())
    for path in (venv_dir, RUNTIME_DIR, PUBLIC_DIR):
        if not path.exists():
            continue
        _remove_path(path)
        removed_paths.append(path)

    removed_config_paths: list[Path] = []
    if delete_config:
        removed_config_paths = _remove_config_data(config_dir)
        removed_paths.extend(removed_config_paths)
        if INSTALL_ENV_FILE.exists():
            _remove_path(INSTALL_ENV_FILE)
            removed_paths.append(INSTALL_ENV_FILE)

    print_step("卸载完成")
    if delete_config:
        print_step(f"已删除配置目录：{config_dir}")
    else:
        print_step(f"已保留配置目录：{config_dir}")
    print_step(f"源码目录仍保留在：{ROOT}")

    return {
        "cancelled": False,
        "config_deleted": delete_config,
        "removed_paths": [str(path) for path in removed_paths],
        "removed_config_paths": [str(path) for path in removed_config_paths],
    }


def _git_output(*args: str) -> str:
    return capture(["git", *args], cwd=ROOT)


def _ensure_git_clean() -> None:
    status = _git_output("status", "--porcelain", "--untracked-files=no")
    if not status.strip():
        return

    changed_files: list[str] = []
    for line in status.splitlines():
        if len(line) < 4:
            continue
        changed_files.append(line[3:].strip())

    detail = ""
    if changed_files:
        preview = "、".join(changed_files[:5])
        if len(changed_files) > 5:
            preview += " 等"
        detail = f"：{preview}"

    raise RuntimeError(
        f"检测到当前仓库有未提交的源码改动{detail}，请先提交或清理后再执行更新。"
    )


def _update_backend_ref(ref: str) -> str:
    if not (ROOT / ".git").exists():
        raise RuntimeError("当前目录不是 Git 仓库，无法更新后端代码。")

    _ensure_git_clean()
    print_step("获取远端更新")
    run(["git", "fetch", "--tags", "origin"], cwd=ROOT)

    current_branch = _git_output("rev-parse", "--abbrev-ref", "HEAD")
    if ref == "latest":
        if current_branch == "HEAD":
            raise RuntimeError(
                "当前仓库处于 detached HEAD 状态，请使用 `moviepilot update backend --ref <tag|branch>` 指定版本。"
            )
        print_step(f"更新后端代码到当前分支最新版本：{current_branch}")
        run(["git", "pull", "--ff-only", "origin", current_branch], cwd=ROOT)
        return current_branch

    print_step(f"切换后端代码到指定版本：{ref}")
    run(["git", "checkout", ref], cwd=ROOT)
    return ref


def update_backend(
    *, ref: str, python_bin: str, venv_dir: Path, recreate: bool
) -> Path:
    ensure_services_stopped()
    resolved_ref = _update_backend_ref(ref=ref)
    venv_python = install_deps(
        python_bin=python_bin, venv_dir=venv_dir, recreate=recreate
    )
    print_step(f"后端更新完成：{resolved_ref}")
    return venv_python


def handle_startup_command(
    *,
    action: str,
    config_dir: Path,
    runtime_python: Optional[Path],
    venv_dir: Optional[Path],
) -> None:
    if action == "status":
        print_autostart_status()
        return

    if action == "enable":
        result = enable_autostart(
            config_dir=config_dir,
            runtime_python=runtime_python,
            venv_dir=venv_dir,
        )
        print_step(f"已启用开机自启：{result.get('method')}")
        if result.get("artifact"):
            print(f"注册文件：{result['artifact']}")
        if result.get("note"):
            print(f"说明：{result['note']}")
        return

    if action == "disable":
        result = disable_autostart()
        removed_paths = result.get("removed_paths") or []
        if removed_paths:
            print_step("已取消开机自启注册")
            for path in removed_paths:
                print(f"已移除：{path}")
        else:
            print_step("当前未配置开机自启，无需取消")
        return

    raise RuntimeError(f"未知的 startup 动作：{action}")


def run_agent_request(
    *, message: str, session_id: Optional[str], new_session: bool, user_id: str
) -> dict[str, str]:
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))

    try:
        from app.db.init import init_db, update_db
        from app.agent import MoviePilotAgent
        from app.core.config import settings
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "当前环境尚未安装 MoviePilot 运行依赖，请先执行 moviepilot install deps 或 moviepilot setup"
        ) from exc

    if not settings.AI_AGENT_ENABLE:
        raise RuntimeError("MoviePilot 智能体未启用，请先在配置中打开 AI_AGENT_ENABLE")

    init_db()
    update_db()

    session = (session_id or "").strip()
    if new_session or not session:
        session = f"cli-{uuid.uuid4().hex[:12]}"

    async def _run_agent() -> dict[str, str]:
        agent = MoviePilotAgent(session_id=session, user_id=user_id or "cli")
        agent.suppress_user_reply = True
        await agent.process(message.strip())
        return {
            "session_id": session,
            "result": (agent._streamed_output or "").strip(),
        }

    return asyncio.run(_run_agent())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="MoviePilot 本地安装与初始化工具")
    subparsers = parser.add_subparsers(dest="command", required=True)

    install_parser = subparsers.add_parser(
        "install-deps", help="创建虚拟环境并安装后端依赖"
    )
    install_parser.add_argument(
        "--python",
        default=DEFAULT_BOOTSTRAP_PYTHON,
        help="用于创建虚拟环境的 Python 解释器，默认自动选择本地 3.11+ 版本",
    )
    install_parser.add_argument(
        "--venv", default=str(ROOT / "venv"), help="虚拟环境目录"
    )
    install_parser.add_argument(
        "--recreate", action="store_true", help="删除并重建虚拟环境"
    )
    install_parser.add_argument(
        "--config-dir", help="配置目录，默认使用程序目录外的系统配置目录"
    )

    frontend_parser = subparsers.add_parser(
        "install-frontend", help="下载前端 release 并安装本地运行时"
    )
    frontend_parser.add_argument(
        "--version", help="前端版本，默认使用 version.py 中的 FRONTEND_VERSION"
    )
    frontend_parser.add_argument(
        "--node-version", default=DEFAULT_NODE_VERSION, help="本地 Node 运行时版本"
    )
    frontend_parser.add_argument(
        "--config-dir", help="配置目录，默认使用程序目录外的系统配置目录"
    )

    resources_parser = subparsers.add_parser(
        "install-resources", help="下载资源文件并同步到 app/helper"
    )
    resources_parser.add_argument(
        "--resources-repo", help="本地 MoviePilot-Resources 仓库路径"
    )
    resources_parser.add_argument("--resource-dir", help="直接指定 resources.v2 目录")
    resources_parser.add_argument(
        "--config-dir", help="配置目录，默认使用程序目录外的系统配置目录"
    )

    init_parser = subparsers.add_parser("init", help="初始化本地配置与资源文件")
    init_parser.add_argument(
        "--resources-repo", help="本地 MoviePilot-Resources 仓库路径"
    )
    init_parser.add_argument("--resource-dir", help="直接指定 resources.v2 目录")
    init_parser.add_argument(
        "--skip-resources", action="store_true", help="只初始化配置，不同步资源文件"
    )
    init_parser.add_argument(
        "--force-token", action="store_true", help="强制重置 API_TOKEN"
    )
    init_parser.add_argument(
        "--wizard", action="store_true", help="启动交互式初始化向导"
    )
    init_parser.add_argument("--superuser", help="预设超级管理员用户名")
    init_parser.add_argument("--superuser-password", help="预设超级管理员密码")
    init_parser.add_argument(
        "--config-dir", help="配置目录，默认使用程序目录外的系统配置目录"
    )

    setup_parser = subparsers.add_parser(
        "setup", help="执行 install-deps、install-frontend、install-resources 和 init"
    )
    setup_parser.add_argument(
        "--python",
        default=DEFAULT_BOOTSTRAP_PYTHON,
        help="用于创建虚拟环境的 Python 解释器，默认自动选择本地 3.11+ 版本",
    )
    setup_parser.add_argument("--venv", default=str(ROOT / "venv"), help="虚拟环境目录")
    setup_parser.add_argument(
        "--recreate", action="store_true", help="删除并重建虚拟环境"
    )
    setup_parser.add_argument(
        "--frontend-version", help="前端版本，默认使用 version.py 中的 FRONTEND_VERSION"
    )
    setup_parser.add_argument(
        "--node-version", default=DEFAULT_NODE_VERSION, help="本地 Node 运行时版本"
    )
    setup_parser.add_argument(
        "--resources-repo", help="本地 MoviePilot-Resources 仓库路径"
    )
    setup_parser.add_argument("--resource-dir", help="直接指定 resources.v2 目录")
    setup_parser.add_argument(
        "--skip-resources", action="store_true", help="只初始化配置，不同步资源文件"
    )
    setup_parser.add_argument(
        "--force-token", action="store_true", help="强制重置 API_TOKEN"
    )
    setup_parser.add_argument(
        "--wizard", action="store_true", help="安装完成后启动交互式初始化向导"
    )
    setup_parser.add_argument("--superuser", help="预设超级管理员用户名")
    setup_parser.add_argument("--superuser-password", help="预设超级管理员密码")
    setup_parser.add_argument(
        "--config-dir", help="配置目录，默认使用程序目录外的系统配置目录"
    )

    uninstall_parser = subparsers.add_parser(
        "uninstall", help="卸载本地安装产物，并可选删除配置目录"
    )
    uninstall_parser.add_argument(
        "--venv", default=str(ROOT / "venv"), help="虚拟环境目录"
    )
    uninstall_parser.add_argument(
        "--config-dir", help="配置目录，默认使用当前安装配置"
    )

    agent_parser = subparsers.add_parser(
        "agent", help="直接向 MoviePilot 智能体发送一次请求"
    )
    agent_parser.add_argument("message", nargs="+", help="发给智能体的文本请求")
    agent_parser.add_argument("--session", help="会话 ID，默认自动生成")
    agent_parser.add_argument(
        "--new-session", action="store_true", help="忽略传入会话，强制创建新会话"
    )
    agent_parser.add_argument(
        "--user-id", default="cli", help="智能体上下文中的用户 ID"
    )
    agent_parser.add_argument(
        "--config-dir", help="配置目录，默认使用程序目录外的系统配置目录"
    )

    update_parser = subparsers.add_parser("update", help="更新本地后端、前端或全部组件")
    update_parser.add_argument(
        "target", choices=["backend", "frontend", "all"], help="更新目标"
    )
    update_parser.add_argument(
        "--ref", default="latest", help="后端 Git 版本，默认 latest"
    )
    update_parser.add_argument(
        "--frontend-version", help="前端版本，默认使用 version.py 中的 FRONTEND_VERSION"
    )
    update_parser.add_argument(
        "--node-version", default=DEFAULT_NODE_VERSION, help="本地 Node 运行时版本"
    )
    update_parser.add_argument(
        "--python",
        default=DEFAULT_BOOTSTRAP_PYTHON,
        help="用于安装后端依赖的 Python 解释器，默认自动选择本地 3.11+ 版本",
    )
    update_parser.add_argument(
        "--venv", default=str(ROOT / "venv"), help="虚拟环境目录"
    )
    update_parser.add_argument(
        "--recreate", action="store_true", help="删除并重建虚拟环境"
    )
    update_parser.add_argument(
        "--skip-resources", action="store_true", help="更新 all 时跳过资源同步"
    )
    update_parser.add_argument(
        "--config-dir", help="配置目录，默认使用程序目录外的系统配置目录"
    )

    startup_parser = subparsers.add_parser(
        "startup", help="注册、取消或查看本地开机自启"
    )
    startup_parser.add_argument(
        "action", choices=["enable", "disable", "status"], help="开机自启动作"
    )
    startup_parser.add_argument(
        "--venv", default=str(ROOT / "venv"), help="虚拟环境目录"
    )
    startup_parser.add_argument(
        "--config-dir", help="配置目录，默认使用当前安装配置"
    )

    apply_config_parser = subparsers.add_parser("apply-config", help=argparse.SUPPRESS)
    apply_config_parser.add_argument(
        "--config-json-file", required=True, help=argparse.SUPPRESS
    )

    sync_superuser_parser = subparsers.add_parser(
        "sync-superuser", help=argparse.SUPPRESS
    )
    sync_superuser_parser.add_argument("--config-dir", help=argparse.SUPPRESS)

    query_auth_sites_parser = subparsers.add_parser(
        "query-auth-sites", help=argparse.SUPPRESS
    )
    query_auth_sites_parser.add_argument(
        "--output-json-file", required=True, help=argparse.SUPPRESS
    )

    query_llm_providers_parser = subparsers.add_parser(
        "query-llm-providers", help=argparse.SUPPRESS
    )
    query_llm_providers_parser.add_argument(
        "--output-json-file", required=True, help=argparse.SUPPRESS
    )

    query_llm_models_parser = subparsers.add_parser(
        "query-llm-models", help=argparse.SUPPRESS
    )
    query_llm_models_parser.add_argument(
        "--request-json-file", required=True, help=argparse.SUPPRESS
    )
    query_llm_models_parser.add_argument(
        "--output-json-file", required=True, help=argparse.SUPPRESS
    )

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    explicit_config_dir = (
        Path(args.config_dir) if getattr(args, "config_dir", None) else None
    )
    explicit_config_dir = _resolve_interactive_config_dir(
        args.command, explicit_config_dir
    )
    persist_config_commands = {
        "install-deps",
        "install-frontend",
        "install-resources",
        "init",
        "setup",
        "agent",
        "update",
    }
    config_dir = configure_config_dir(
        explicit=explicit_config_dir,
        persist=args.command in persist_config_commands,
        prefer_external=args.command in persist_config_commands,
    )

    try:
        if args.command == "install-deps":
            venv_python = install_deps(
                python_bin=args.python,
                venv_dir=Path(args.venv),
                recreate=args.recreate,
            )
            print_step(f"后端依赖安装完成，可执行：{venv_python} -m app.cli")
            print_step(f"当前配置目录：{config_dir}")
            return 0

        if args.command == "install-frontend":
            result = install_frontend(
                frontend_version=args.version, node_version=args.node_version
            )
            print_step(f"前端安装完成，版本：{result['version']}")
            return 0

        if args.command == "install-resources":
            install_resources(
                resources_repo=Path(args.resources_repo)
                if args.resources_repo
                else None,
                resource_dir=Path(args.resource_dir) if args.resource_dir else None,
            )
            return 0

        if args.command == "init":
            init_local(
                resources_repo=Path(args.resources_repo)
                if args.resources_repo
                else None,
                resource_dir=Path(args.resource_dir) if args.resource_dir else None,
                skip_resources=args.skip_resources,
                resources_ready=False,
                force_token=args.force_token,
                wizard=args.wizard,
                superuser=args.superuser,
                superuser_password=args.superuser_password,
                runtime_python=None,
                venv_dir=ROOT / "venv",
            )
            print_step("初始化完成")
            print_step(f"当前配置目录：{config_dir}")
            return 0

        if args.command == "setup":
            venv_python = install_deps(
                python_bin=args.python,
                venv_dir=Path(args.venv),
                recreate=args.recreate,
            )
            install_frontend(
                frontend_version=args.frontend_version, node_version=args.node_version
            )
            resources_installed = False
            if not args.skip_resources:
                install_resources(
                    resources_repo=Path(args.resources_repo)
                    if args.resources_repo
                    else None,
                    resource_dir=Path(args.resource_dir) if args.resource_dir else None,
                )
                resources_installed = True
            init_local(
                resources_repo=Path(args.resources_repo)
                if args.resources_repo
                else None,
                resource_dir=Path(args.resource_dir) if args.resource_dir else None,
                skip_resources=args.skip_resources or resources_installed,
                resources_ready=resources_installed,
                force_token=args.force_token,
                wizard=args.wizard,
                superuser=args.superuser,
                superuser_password=args.superuser_password,
                runtime_python=venv_python,
                venv_dir=Path(args.venv),
            )
            print_step(f"本地环境已完成安装与初始化：{venv_python}")
            print_step(f"当前配置目录：{config_dir}")
            return 0

        if args.command == "uninstall":
            uninstall_local(
                venv_dir=Path(args.venv),
                config_dir=config_dir,
                command_path=os.getenv("MOVIEPILOT_COMMAND_PATH"),
                launch_path=os.getenv("MOVIEPILOT_LAUNCH_PATH"),
            )
            return 0

        if args.command == "agent":
            result = run_agent_request(
                message=" ".join(args.message),
                session_id=args.session,
                new_session=args.new_session,
                user_id=args.user_id,
            )
            if result.get("session_id"):
                print_step(f"智能体会话：{result['session_id']}")
            print(result.get("result") or "")
            return 0

        if args.command == "update":
            ensure_services_stopped()
            if args.target in {"backend", "all"}:
                update_backend(
                    ref=args.ref,
                    python_bin=args.python,
                    venv_dir=Path(args.venv),
                    recreate=args.recreate,
                )
            if args.target in {"frontend", "all"}:
                frontend_result = install_frontend(
                    frontend_version=args.frontend_version,
                    node_version=args.node_version,
                )
                print_step(f"前端更新完成，版本：{frontend_result['version']}")
            if args.target == "all" and not args.skip_resources:
                install_resources(resources_repo=None, resource_dir=None)
                print_step("资源文件已同步到最新")
            print_step(f"更新完成，当前配置目录：{config_dir}")
            return 0

        if args.command == "startup":
            runtime_python = None
            if args.action == "enable":
                runtime_python = _resolve_runtime_python_for_startup(
                    None, Path(args.venv)
                )
            handle_startup_command(
                action=args.action,
                config_dir=config_dir,
                runtime_python=runtime_python,
                venv_dir=Path(args.venv),
            )
            return 0

        if args.command == "apply-config":
            payload = json.loads(
                Path(args.config_json_file).read_text(encoding="utf-8")
            )
            if not isinstance(payload, dict):
                raise RuntimeError("配置负载格式错误")
            _apply_local_system_config_inner(payload)
            return 0

        if args.command == "sync-superuser":
            _sync_superuser_account_inner()
            return 0

        if args.command == "query-auth-sites":
            payload = _load_auth_site_definitions_inner()
            Path(args.output_json_file).write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            return 0

        if args.command == "query-llm-providers":
            payload = _load_llm_provider_definitions_inner()
            Path(args.output_json_file).write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            return 0

        if args.command == "query-llm-models":
            payload = json.loads(Path(args.request_json_file).read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise RuntimeError("模型查询负载格式错误")
            models = _load_llm_models_inner(payload)
            Path(args.output_json_file).write_text(
                json.dumps(models, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
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
