import os
import subprocess
import textwrap
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _write_entrypoint_functions(tmp_path: Path) -> Path:
    content = (ROOT / "docker" / "entrypoint.sh").read_text(encoding="utf-8")
    browser = (ROOT / "docker" / "browser.sh").read_text(encoding="utf-8")
    marker = "# 使用env配置"
    assert marker in content
    functions = tmp_path / "entrypoint-functions.sh"
    entrypoint_functions = content.split(marker, 1)[0]
    functions.write_text(f"{entrypoint_functions}\n{browser}", encoding="utf-8")
    return functions


def _write_fake_chown(tmp_path: Path) -> Path:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    chown = fake_bin / "chown"
    chown.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            printf '%s\\n' "$*" >> "${MP_CHOWN_LOG}"
            """
        ),
        encoding="utf-8",
    )
    chown.chmod(0o755)
    gosu = fake_bin / "gosu"
    gosu.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            shift
            exec "$@"
            """
        ),
        encoding="utf-8",
    )
    gosu.chmod(0o755)
    return fake_bin


def _run_permission_case(tmp_path: Path, body: str, env: dict[str, str] | None = None) -> str:
    functions = _write_entrypoint_functions(tmp_path)
    fake_bin = _write_fake_chown(tmp_path)
    chown_log = tmp_path / "chown.log"
    app_dir = tmp_path / "app"
    resource_dir = app_dir / "app" / "application" / "site"
    public_dir = tmp_path / "public"
    home_dir = tmp_path / "home"
    config_dir = tmp_path / "config"
    (app_dir / "app" / "plugins").mkdir(parents=True)
    resource_dir.mkdir(parents=True)
    public_dir.mkdir()
    (home_dir / ".cloakbrowser").mkdir(parents=True)
    (home_dir / "runtime").mkdir()
    (config_dir / ".browser" / "cloakbrowser").mkdir(parents=True)
    (config_dir / "runtime").mkdir(exist_ok=True)
    (app_dir / "app" / "plugins" / "plugin.py").write_text("# plugin\n", encoding="utf-8")
    (resource_dir / "user.sites.v3.bin").write_text("resources\n", encoding="utf-8")
    (resource_dir / "sites.cpython-312-x86_64-linux-gnu.so").write_text("plugin\n", encoding="utf-8")
    (public_dir / "index.html").write_text("<!doctype html>\n", encoding="utf-8")
    (home_dir / ".cloakbrowser" / "chrome").write_text("browser cache\n", encoding="utf-8")
    (home_dir / "runtime" / "state").write_text("state\n", encoding="utf-8")
    (config_dir / ".browser" / "cloakbrowser" / "chrome").write_text(
        "browser cache\n", encoding="utf-8"
    )
    (config_dir / "runtime" / "state").write_text("state\n", encoding="utf-8")
    external_target = tmp_path / "external-target"
    external_target.write_text("external\n", encoding="utf-8")
    (app_dir / "external-link").symlink_to(external_target)

    case_env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "MP_CHOWN_LOG": str(chown_log),
        "ENTRYPOINT_FUNCTIONS": str(functions),
        "APP_DIR": str(app_dir),
        "PUBLIC_DIR": str(public_dir),
        "HOME_DIR": str(home_dir),
        "IMAGE_RESOURCE_DIR": str(resource_dir),
        "CONFIG_DIR": str(config_dir),
        "PUID": str(os.getuid()),
        "PGID": str(os.getgid()),
    }
    case_env.pop("UV_CACHE_DIR", None)
    if env:
        case_env.update(env)

    script = textwrap.dedent(
        f"""\
        set -euo pipefail
        source "${{ENTRYPOINT_FUNCTIONS}}"
        {body}
        """
    )
    subprocess.run(["bash", "-c", script], check=True, env=case_env)
    return chown_log.read_text(encoding="utf-8") if chown_log.exists() else ""


def _run_entrypoint_case(tmp_path: Path, body: str, env: dict[str, str] | None = None) -> str:
    functions = _write_entrypoint_functions(tmp_path)
    case_env = {
        **os.environ,
        "ENTRYPOINT_FUNCTIONS": str(functions),
    }
    if env:
        case_env.update(env)

    script = textwrap.dedent(
        f"""\
        set -euo pipefail
        source "${{ENTRYPOINT_FUNCTIONS}}"
        {body}
        """
    )
    result = subprocess.run(["bash", "-c", script], check=True, env=case_env, text=True, capture_output=True)
    return result.stdout


def _resolve_browser_cache_case(
    tmp_path: Path,
    *,
    explicit: Path | None = None,
    canonical_ready: bool = False,
    legacy_ready: bool = False,
    legacy_mounted: bool = False,
) -> str:
    """在隔离目录中执行浏览器缓存路径选择合同。"""
    config_dir = tmp_path / "config"
    home_dir = tmp_path / "moviepilot"
    canonical = config_dir / ".browser" / "cloakbrowser"
    legacy = home_dir / ".cloakbrowser"
    canonical.mkdir(parents=True)
    legacy.mkdir(parents=True)
    if canonical_ready:
        (canonical / ".ready").touch()
    if legacy_ready:
        (legacy / ".ready").touch()

    return _run_entrypoint_case(
        tmp_path,
        """
        INFO() { :; }
        is_cloakbrowser_cache_ready() { [ -f "$1/.ready" ]; }
        is_path_mountpoint() { [ "${LEGACY_MOUNTED}" = "true" ] && [ "$1" = "${HOME}/.cloakbrowser" ]; }
        resolve_browser_cache_dir
        printf '%s\n' "${CLOAKBROWSER_CACHE_DIR}"
        """,
        env={
            "CONFIG_DIR": str(config_dir),
            "HOME": str(home_dir),
            "CLOAKBROWSER_CACHE_DIR": str(explicit) if explicit else "",
            "LEGACY_MOUNTED": "true" if legacy_mounted else "false",
        },
    ).strip()


def test_browser_cache_explicit_directory_has_highest_priority(tmp_path: Path) -> None:
    explicit = tmp_path / "custom-cache"

    selected = _resolve_browser_cache_case(
        tmp_path,
        explicit=explicit,
        canonical_ready=True,
        legacy_ready=True,
    )

    assert selected == str(explicit)


def test_browser_cache_rejects_relative_explicit_directory(tmp_path: Path) -> None:
    output = _run_entrypoint_case(
        tmp_path,
        """
        ERROR() { printf '%s\\n' "$1"; }
        CONFIG_DIR=/config HOME=/moviepilot CLOAKBROWSER_CACHE_DIR=relative-cache \
          resolve_browser_cache_dir || printf 'rejected\\n'
        """,
    )

    assert "CLOAKBROWSER_CACHE_DIR 必须是独立的绝对目录" in output
    assert output.endswith("rejected\n")


def test_browser_cache_normalizes_explicit_directory(tmp_path: Path) -> None:
    explicit = tmp_path / "cache" / ".." / "browser-cache"

    selected = _resolve_browser_cache_case(tmp_path, explicit=explicit)

    assert selected == str(tmp_path / "browser-cache")


def test_browser_cache_rejects_managed_root_directory(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    output = _run_entrypoint_case(
        tmp_path,
        """
        ERROR() { printf '%s\\n' "$1"; }
        CONFIG_DIR="${CASE_CONFIG_DIR}" HOME=/moviepilot \
          CLOAKBROWSER_CACHE_DIR="${CASE_CONFIG_DIR}" \
          resolve_browser_cache_dir || printf 'rejected\\n'
        """,
        env={"CASE_CONFIG_DIR": str(config_dir)},
    )

    assert "CLOAKBROWSER_CACHE_DIR 不能占用受管根目录" in output
    assert output.endswith("rejected\n")


def test_browser_cache_prefers_valid_config_cache(tmp_path: Path) -> None:
    selected = _resolve_browser_cache_case(
        tmp_path,
        canonical_ready=True,
        legacy_ready=True,
    )

    assert selected == str(tmp_path / "config" / ".browser" / "cloakbrowser")


def test_browser_cache_reuses_valid_prerelease_v3_cache(tmp_path: Path) -> None:
    selected = _resolve_browser_cache_case(tmp_path, legacy_ready=True)

    assert selected == str(tmp_path / "moviepilot" / ".cloakbrowser")


def test_browser_cache_reuses_empty_prerelease_v3_mount(tmp_path: Path) -> None:
    selected = _resolve_browser_cache_case(tmp_path, legacy_mounted=True)

    assert selected == str(tmp_path / "moviepilot" / ".cloakbrowser")


def test_browser_cache_new_install_uses_config_cache(tmp_path: Path) -> None:
    selected = _resolve_browser_cache_case(tmp_path)

    assert selected == str(tmp_path / "config" / ".browser" / "cloakbrowser")


def test_browser_install_preserves_upstream_auto_update_default(tmp_path: Path) -> None:
    selected = tmp_path / "cache"
    output = _run_entrypoint_case(
        tmp_path,
        """
        INFO() { :; }
        gosu() {
          printf '%s|%s|%s\n' "${CLOAKBROWSER_AUTO_UPDATE-unset}" "${CLOAKBROWSER_CACHE_DIR}" "$*"
        }
        unset CLOAKBROWSER_AUTO_UPDATE
        CLOAKBROWSER_CACHE_DIR="${SELECTED_CACHE}"
        VENV_PATH=/runtime install_browser_kernel
        """,
        env={"SELECTED_CACHE": str(selected)},
    ).strip()

    assert output == (
        f"unset|{selected}|moviepilot:moviepilot "
        "/runtime/bin/python3 -m cloakbrowser install"
    )


def test_browser_install_failure_does_not_block_startup(tmp_path: Path) -> None:
    output = _run_entrypoint_case(
        tmp_path,
        """
        WARN() { printf '%s\n' "$1"; }
        is_cloakbrowser_cache_ready() { return 1; }
        install_browser_kernel() { return 1; }
        ensure_browser_kernel
        printf 'continued\n'
        """,
    )

    assert "浏览器内核安装失败，首次使用时将重试" in output
    assert output.endswith("continued\n")


def test_ready_browser_cache_skips_startup_install(tmp_path: Path) -> None:
    output = _run_entrypoint_case(
        tmp_path,
        """
        INFO() { printf '%s\n' "$1"; }
        is_cloakbrowser_cache_ready() { return 0; }
        install_browser_kernel() { printf 'unexpected-install\n'; }
        CLOAKBROWSER_CACHE_DIR=/config/.browser/cloakbrowser
        ensure_browser_kernel
        """,
    )

    assert output == "CloakBrowser 浏览器内核已就绪\n"


def test_missing_browser_cache_runs_startup_install(tmp_path: Path) -> None:
    output = _run_entrypoint_case(
        tmp_path,
        """
        is_cloakbrowser_cache_ready() { return 1; }
        install_browser_kernel() { printf 'installed\n'; }
        CLOAKBROWSER_CACHE_DIR=/config/.browser/cloakbrowser
        ensure_browser_kernel
        """,
    )

    assert output == "installed\n"


def test_browser_install_is_centralized_in_startup() -> None:
    entrypoint = (ROOT / "docker" / "entrypoint.sh").read_text(encoding="utf-8")
    browser = (ROOT / "docker" / "browser.sh").read_text(encoding="utf-8")
    updater = (ROOT / "docker" / "update.sh").read_text(encoding="utf-8")
    startup = entrypoint.split("# 使用env配置", 1)[1]

    assert "-m cloakbrowser install" not in entrypoint
    assert browser.count("-m cloakbrowser install") == 2
    assert "-m cloakbrowser install" not in updater
    assert startup.index('source "${MP_CONTROL_DIR:-/usr/local/lib/moviepilot/control}/update.sh"') < startup.index(
        'source "${MP_CONTROL_DIR:-/usr/local/lib/moviepilot/control}/browser.sh"'
    ) < startup.index("resolve_browser_cache_dir") < startup.index("ensure_browser_kernel")


def test_image_paths_are_not_chowned_by_default_regardless_of_owner(tmp_path: Path) -> None:
    log = _run_permission_case(
        tmp_path,
        'force_chown_image_paths_if_requested "${APP_DIR}" "${PUBLIC_DIR}"',
        env={"PUID": "999999", "PGID": "999999"},
    )

    assert log == ""


def test_image_paths_force_chown_uses_recursive_repair(tmp_path: Path) -> None:
    log = _run_permission_case(
        tmp_path,
        'MOVIEPILOT_FORCE_CHOWN=true force_chown_image_paths_if_requested "${APP_DIR}" "${PUBLIC_DIR}"',
    )

    assert log.startswith("-R moviepilot:moviepilot ")
    assert "/app" in log
    assert "/public" in log


def test_force_chown_keeps_source_control_directory_root_owned(tmp_path: Path) -> None:
    app_dir = tmp_path / "app"

    log = _run_permission_case(
        tmp_path,
        """
        mkdir -p "${APP_DIR}/docker"
        printf '#!/bin/bash\\n' > "${APP_DIR}/docker/launcher.sh"
        MOVIEPILOT_FORCE_CHOWN=true force_chown_image_paths_if_requested "${APP_DIR}" "${PUBLIC_DIR}"
        """,
    )

    lines = log.splitlines()
    assert f"root:root {app_dir} {app_dir}/docker" in lines
    assert not any(line.startswith("-R ") and f"{app_dir}/docker" in line for line in lines)
    assert f"-R moviepilot:moviepilot {app_dir}/app" in lines
    assert f"-R moviepilot:moviepilot {tmp_path}/public" in lines


def test_image_paths_force_chown_accepts_numeric_and_yes_values(tmp_path: Path) -> None:
    for force_value in ("1", "YES"):
        case_path = tmp_path / force_value.lower()
        case_path.mkdir()
        log = _run_permission_case(
            case_path,
            f'MOVIEPILOT_FORCE_CHOWN={force_value} force_chown_image_paths_if_requested "${{APP_DIR}}" "${{PUBLIC_DIR}}"',
        )

        assert log.startswith("-R moviepilot:moviepilot ")
        assert "/app" in log
        assert "/public" in log


def test_plugin_directory_skips_chown_when_owner_matches(tmp_path: Path) -> None:
    log = _run_permission_case(
        tmp_path,
        'chown_plugin_runtime_path "${APP_DIR}/app/plugins"',
    )

    assert log == ""


def test_plugin_directory_chowns_only_root_directory_when_owner_mismatches(tmp_path: Path) -> None:
    log = _run_permission_case(
        tmp_path,
        'chown_plugin_runtime_path "${APP_DIR}/app/plugins"',
        env={"PUID": "999999", "PGID": "999999"},
    )

    assert log == f"-h moviepilot:moviepilot {tmp_path}/app/app/plugins\n"


def test_home_permissions_skip_cloakbrowser_cache_by_default(tmp_path: Path) -> None:
    log = _run_permission_case(
        tmp_path,
        'HOME="${HOME_DIR}" correct_home_permissions',
    )

    lines = log.splitlines()
    assert f"moviepilot:moviepilot {tmp_path}/home" in lines
    assert f"-h moviepilot:moviepilot {tmp_path}/home/.cloakbrowser" in lines
    assert f"-R moviepilot:moviepilot {tmp_path}/home/runtime" in lines
    assert not any(line.startswith("-R ") and ".cloakbrowser" in line for line in lines)


def test_home_permissions_force_chown_repairs_cloakbrowser_cache(tmp_path: Path) -> None:
    log = _run_permission_case(
        tmp_path,
        'MOVIEPILOT_FORCE_CHOWN=yes HOME="${HOME_DIR}" correct_home_permissions',
    )

    assert f"-R moviepilot:moviepilot {tmp_path}/home/.cloakbrowser" in log


def test_runtime_writable_paths_are_still_corrected(tmp_path: Path) -> None:
    log = _run_permission_case(
        tmp_path,
        'HOME="${HOME_DIR}" correct_file_permissions',
        env={"PUID": "999999", "PGID": "999999"},
    )

    lines = log.splitlines()
    assert f"moviepilot:moviepilot {tmp_path}/home" in lines
    assert f"-h moviepilot:moviepilot {tmp_path}/home/.cloakbrowser" in lines
    assert f"-R moviepilot:moviepilot {tmp_path}/home/runtime" in lines
    assert f"moviepilot:moviepilot {tmp_path}/config" in lines
    assert f"-R moviepilot:moviepilot {tmp_path}/config/runtime" in lines
    assert "-R moviepilot:moviepilot /var/lib/nginx /var/log/nginx" in lines
    assert "moviepilot:moviepilot /etc/hosts /tmp" in lines
    assert f"-R moviepilot:moviepilot {tmp_path}/app/app/application/site" in lines
    assert not any(line.startswith("-R ") and ".cloakbrowser" in line for line in lines)
    assert not any(line.startswith("-R ") and "/.browser" in line for line in lines)
    assert not any(f"{tmp_path}/app " in line for line in lines)
    assert not any(f"{tmp_path}/public" in line for line in lines)


def test_external_package_cache_is_repaired_without_chowning_parent(
    tmp_path: Path,
) -> None:
    external_cache = tmp_path / "package-cache" / "uv"
    log = _run_permission_case(
        tmp_path,
        """
        gosu() { shift; "$@"; }
        UV_CACHE_DIR="${EXTERNAL_CACHE}" HOME="${HOME_DIR}" correct_file_permissions
        """,
        env={"EXTERNAL_CACHE": str(external_cache)},
    )

    assert f"-R moviepilot:moviepilot {external_cache}" in log.splitlines()
    assert not any(
        line.endswith(str(external_cache.parent)) for line in log.splitlines()
    )


def test_external_package_cache_write_probe_failure_is_fatal(tmp_path: Path) -> None:
    output = _run_entrypoint_case(
        tmp_path,
        """
        ERROR() { printf '%s\n' "$1"; }
        chown() { :; }
        gosu() { return 1; }
        CONFIG_DIR="${CASE_CONFIG_DIR}"
        VENV_PATH="${CASE_VENV_PATH}"
        UV_CACHE_DIR="${CASE_CACHE_DIR}"
        if correct_package_cache_permissions; then
          printf 'unexpected-success\n'
        else
          printf 'rejected\n'
        fi
        """,
        env={
            "CASE_CONFIG_DIR": str(tmp_path / "config"),
            "CASE_VENV_PATH": str(tmp_path / "venv"),
            "CASE_CACHE_DIR": str(tmp_path / "external-cache"),
        },
    )

    assert "uv 缓存目录不可写" in output
    assert output.endswith("rejected\n")


def test_explicit_browser_cache_subtree_is_not_scanned_by_permission_repair(
    tmp_path: Path,
) -> None:
    explicit_cache = tmp_path / "config" / "runtime" / "browser-cache"
    explicit_cache.mkdir(parents=True)
    (explicit_cache / "chromium").write_text("payload\n", encoding="utf-8")
    log = _run_permission_case(
        tmp_path,
        'CLOAKBROWSER_CACHE_DIR="${EXPLICIT_CACHE}" HOME="${HOME_DIR}" correct_file_permissions',
        env={"EXPLICIT_CACHE": str(explicit_cache)},
    )

    lines = log.splitlines()
    assert f"-R moviepilot:moviepilot {tmp_path}/config/runtime" not in lines
    assert any(f"{tmp_path}/config/runtime/state" in line for line in lines)
    assert not any("browser-cache" in line for line in lines)
    assert f"-R moviepilot:moviepilot {tmp_path}/home/runtime" in lines


def test_explicit_top_level_browser_cache_is_not_scanned_by_permission_repair(
    tmp_path: Path,
) -> None:
    explicit_cache = tmp_path / "config" / "browser-cache"
    log = _run_permission_case(
        tmp_path,
        """
        mkdir -p "${EXPLICIT_CACHE}/chromium"
        CLOAKBROWSER_CACHE_DIR="${EXPLICIT_CACHE}" HOME="${HOME_DIR}" correct_file_permissions
        """,
        env={"EXPLICIT_CACHE": str(explicit_cache)},
    )

    lines = log.splitlines()
    assert f"-h moviepilot:moviepilot {explicit_cache}" in lines
    assert not any(line.startswith("-R ") and str(explicit_cache) in line for line in lines)


def test_site_resource_permissions_are_repaired_even_when_owner_matches(tmp_path: Path) -> None:
    log = _run_permission_case(
        tmp_path,
        'correct_site_resource_permissions',
    )

    lines = log.splitlines()
    assert f"-R moviepilot:moviepilot {tmp_path}/app/app/application/site" in lines
    assert not any(line.startswith("-R ") and f"{tmp_path}/app " in line for line in lines)
    assert not any(line.startswith("-R ") and f"{tmp_path}/public" in line for line in lines)


def test_backend_ready_log_uses_configured_ports(tmp_path: Path) -> None:
    curl_log = tmp_path / "curl.log"
    output = _run_entrypoint_case(
        tmp_path,
        """
        INFO() { printf '[INFO] %s\\n' "$1"; }
        curl() {
          printf '%s\\n' "$*" > "${CURL_LOG}"
          return 0
        }
        PORT=4321 NGINX_PORT=8765 wait_backend_ready 1 2 "$$"
        """,
        env={"CURL_LOG": str(curl_log)},
    )

    assert curl_log.read_text(encoding="utf-8") == (
        "-fsS --max-time 2 http://127.0.0.1:4321/health/ready\n"
    )
    assert "MoviePilot Web 已可访问" in output
    assert "后端就绪耗时" in output
    assert "后端端口 4321" in output
    assert "前端端口 8765" in output


def test_backend_ready_timeout_falls_back_to_default_for_invalid_value(tmp_path: Path) -> None:
    output = _run_entrypoint_case(
        tmp_path,
        """
        WARN() { printf '[WARN] %s\\n' "$1"; }
        curl() { return 1; }
        MOVIEPILOT_BACKEND_READY_TIMEOUT=invalid wait_backend_ready 1 2 999999 || true
        """,
    )

    assert "MOVIEPILOT_BACKEND_READY_TIMEOUT=invalid 无效，使用默认 300 秒" in output
    assert "后端服务启动完成探测已停止：后端进程已退出" in output


def test_backend_ready_timeout_accepts_leading_zero_decimal(tmp_path: Path) -> None:
    output = _run_entrypoint_case(
        tmp_path,
        """
        INFO() { printf '[INFO] %s\\n' "$1"; }
        WARN() { printf '[WARN] %s\\n' "$1"; }
        curl() { return 0; }
        MOVIEPILOT_BACKEND_READY_TIMEOUT=08 wait_backend_ready 1 2 "$$"
        """,
    )

    assert "MOVIEPILOT_BACKEND_READY_TIMEOUT=08 无效" not in output
    assert "MoviePilot Web 已可访问" in output


def test_backend_failure_keepalive_contract_is_explicit() -> None:
    """后端异常默认保活诊断，显式关闭后才退出容器。"""
    content = (ROOT / "docker" / "entrypoint.sh").read_text(encoding="utf-8")
    function = content.split(
        "function diagnostic_keepalive() {", 1
    )[1].split("\n}", 1)[0]

    assert 'MOVIEPILOT_DOCKER_KEEPALIVE_ON_FAILURE:-true' in function
    assert 'if [ "${keepalive}" = "false" ]' in function
    assert 'graceful_exit "$exit_code" "python_exit"' in function
    assert "容器将保持运行以便执行 moviepilot doctor" in function
