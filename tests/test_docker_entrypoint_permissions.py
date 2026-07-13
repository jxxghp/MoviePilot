import os
import subprocess
import textwrap
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _write_entrypoint_functions(tmp_path: Path) -> Path:
    content = (ROOT / "docker" / "entrypoint.sh").read_text(encoding="utf-8")
    marker = "# 使用env配置"
    assert marker in content
    functions = tmp_path / "entrypoint-functions.sh"
    functions.write_text(content.split(marker, 1)[0], encoding="utf-8")
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
    return fake_bin


def _run_permission_case(tmp_path: Path, body: str, env: dict[str, str] | None = None) -> str:
    functions = _write_entrypoint_functions(tmp_path)
    fake_bin = _write_fake_chown(tmp_path)
    chown_log = tmp_path / "chown.log"
    app_dir = tmp_path / "app"
    public_dir = tmp_path / "public"
    home_dir = tmp_path / "home"
    (app_dir / "app" / "plugins").mkdir(parents=True)
    public_dir.mkdir()
    (home_dir / ".cloakbrowser").mkdir(parents=True)
    (home_dir / "runtime").mkdir()
    (app_dir / "app" / "plugins" / "plugin.py").write_text("# plugin\n", encoding="utf-8")
    (public_dir / "index.html").write_text("<!doctype html>\n", encoding="utf-8")
    (home_dir / ".cloakbrowser" / "chrome").write_text("browser cache\n", encoding="utf-8")
    (home_dir / "runtime" / "state").write_text("state\n", encoding="utf-8")
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
        "CONFIG_DIR": str(tmp_path / "config"),
        "PUID": str(os.getuid()),
        "PGID": str(os.getgid()),
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
    assert f"-R moviepilot:moviepilot {tmp_path}/config /var/lib/nginx /var/log/nginx" in lines
    assert "moviepilot:moviepilot /etc/hosts /tmp" in lines
    assert not any(line.startswith("-R ") and ".cloakbrowser" in line for line in lines)
    assert not any(f"{tmp_path}/app " in line for line in lines)
    assert not any(f"{tmp_path}/public" in line for line in lines)


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
        "-fsS --max-time 2 http://127.0.0.1:4321/api/v1/system/global?token=moviepilot\n"
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
