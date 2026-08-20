import os
import shlex
import subprocess
import textwrap
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "docker" / "launcher.sh"
UPDATER = ROOT / "docker" / "update.sh"
BASE_CONTROL_FILES = ("entrypoint.sh", "update.sh", "browser.sh", "cert.sh")


def _write_bundle(path: Path, label: str, *, extra_files: tuple[str, ...] = ()) -> None:
    path.mkdir(parents=True)
    for name in BASE_CONTROL_FILES:
        body = "#!/bin/bash\n:\n"
        if name == "entrypoint.sh":
            body = f"#!/bin/bash\nprintf '%s\\n' '{label}'\n"
        (path / name).write_text(body, encoding="utf-8")
    for name in extra_files:
        (path / name).write_text("#!/bin/bash\n:\n", encoding="utf-8")


def test_dockerfile_control_bundle_build_checks_fail_closed() -> None:
    dockerfile = (ROOT / "docker" / "Dockerfile").read_text(encoding="utf-8")

    assert (
        "FROM ghcr.io/astral-sh/uv:0.12.5@sha256:"
        "e85be844203885286c60ffad8a858d48afb6c5a5c237ca0e67f12e74b8f174b1 AS uv"
        in dockerfile
    )
    assert "COPY --from=uv /uv /usr/local/bin/uv" in dockerfile
    assert "COPY pyproject.toml uv.lock ./" in dockerfile
    assert "python3 -m venv --without-pip ${VENV_PATH}" in dockerfile
    assert "UV_PROJECT_ENVIRONMENT=${VENV_PATH} uv sync" in dockerfile
    for option in ("--locked", "--no-dev", "--no-install-project"):
        assert option in dockerfile
    assert "uv-pip-compat" not in dockerfile
    assert "requirements.in" not in dockerfile
    assert "${VENV_PATH}/bin/pip" not in dockerfile
    assert "-exec cp -f -t /usr/local/lib/moviepilot/control {} +" in dockerfile
    assert "bash -n /entrypoint.sh" in dockerfile
    assert 'ENTRYPOINT [ "/usr/bin/tini", "-g", "--", "/entrypoint.sh" ]' in dockerfile
    assert "CMD /usr/bin/curl -fsS" in dockerfile
    assert (
        'for control_script in /usr/local/lib/moviepilot/control/*.sh; do bash -n "${control_script}" || exit 1; done'
        in dockerfile
    )


def _run_launcher(
    tmp_path: Path,
    source: Path,
    image: Path,
    *,
    trust_source: bool = True,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    case_env = {
        **os.environ,
        "MOVIEPILOT_SOURCE_CONTROL_DIR": str(source),
        "MOVIEPILOT_IMAGE_CONTROL_DIR": str(image),
        "MOVIEPILOT_RUNTIME_CONTROL_ROOT": str(tmp_path / "run"),
    }
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        case_env.pop(name, None)
    if extra_env:
        case_env.update(extra_env)

    command = ["/bin/bash", str(LAUNCHER)]
    if trust_source:
        command = [
            "/bin/bash",
            "-c",
            'source "$1"; source_bundle_is_trusted() { control_bundle_generation "$1" >/dev/null; }; launcher_main',
            "bootstrap-test",
            str(LAUNCHER),
        ]

    return subprocess.run(
        command,
        env=case_env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_launcher_prefers_complete_trusted_source_bundle(tmp_path: Path) -> None:
    source = tmp_path / "source"
    image = tmp_path / "image"
    _write_bundle(source, "source")
    _write_bundle(image, "image")

    result = _run_launcher(tmp_path, source, image)

    assert result.returncode == 0
    assert result.stdout == "source\n"


@pytest.mark.parametrize("missing_file", BASE_CONTROL_FILES)
def test_launcher_falls_back_when_source_bundle_is_incomplete(
    tmp_path: Path, missing_file: str
) -> None:
    source = tmp_path / "source"
    image = tmp_path / "image"
    _write_bundle(source, "source")
    _write_bundle(image, "image")
    (source / missing_file).unlink()

    result = _run_launcher(tmp_path, source, image)

    assert result.returncode == 0
    assert result.stdout == "image\n"


def test_launcher_falls_back_when_source_bundle_has_invalid_shell(tmp_path: Path) -> None:
    source = tmp_path / "source"
    image = tmp_path / "image"
    _write_bundle(source, "source")
    _write_bundle(image, "image")
    (source / "future.sh").write_text("if then\n", encoding="utf-8")

    result = _run_launcher(tmp_path, source, image)

    assert result.returncode == 0
    assert result.stdout == "image\n"


def test_launcher_falls_back_when_source_permissions_are_untrusted(tmp_path: Path) -> None:
    source = tmp_path / "source"
    image = tmp_path / "image"
    _write_bundle(source, "source")
    _write_bundle(image, "image")

    result = _run_launcher(tmp_path, source, image, trust_source=False)

    assert result.returncode == 0
    assert result.stdout == "image\n"


def test_launcher_rejects_invalid_source_and_image_bundles(tmp_path: Path) -> None:
    source = tmp_path / "source"
    image = tmp_path / "image"
    _write_bundle(source, "source")
    _write_bundle(image, "image")
    (source / "entrypoint.sh").write_text("if then\n", encoding="utf-8")
    (image / "entrypoint.sh").write_text("if then\n", encoding="utf-8")

    result = _run_launcher(tmp_path, source, image)

    assert result.returncode != 0
    assert result.stdout == ""


def test_direct_fallback_never_bypasses_invalid_image_bundle(tmp_path: Path) -> None:
    source = tmp_path / "source"
    image = tmp_path / "image"
    marker = tmp_path / "image-entrypoint-started"
    _write_bundle(source, "source")
    _write_bundle(image, "unused")
    (source / "future.sh").write_text("if then\n", encoding="utf-8")
    (image / "browser.sh").write_text("if then\n", encoding="utf-8")
    (image / "entrypoint.sh").write_text(
        f"#!/bin/bash\ntouch {marker!s}\n",
        encoding="utf-8",
    )

    result = _run_launcher(tmp_path, source, image)

    assert result.returncode != 0
    assert not marker.exists()


def test_launcher_path_cannot_be_hijacked_by_writable_venv(tmp_path: Path) -> None:
    source = tmp_path / "source"
    image = tmp_path / "image"
    fake_bin = tmp_path / "venv-bin"
    marker = tmp_path / "hijacked"
    _write_bundle(source, "source")
    _write_bundle(image, "image")
    fake_bin.mkdir()
    fake_stat = fake_bin / "stat"
    fake_stat.write_text(
        f"#!/bin/bash\ntouch {marker!s}\nprintf '0\\n'\n",
        encoding="utf-8",
    )
    fake_stat.chmod(0o755)

    result = _run_launcher(
        tmp_path,
        source,
        image,
        trust_source=False,
        extra_env={"PATH": f"{fake_bin}:{os.environ['PATH']}"},
    )

    assert result.returncode == 0
    assert result.stdout == "image\n"
    assert not marker.exists()


def test_launcher_preserves_inherited_path_and_system_tools_for_entrypoint(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    image = tmp_path / "image"
    inherited_path = "/custom/bin:/usr/local/bin:/usr/bin:/bin"
    _write_bundle(source, "unused")
    _write_bundle(image, "image")
    (source / "entrypoint.sh").write_text(
        "#!/bin/bash\ndate +%s >/dev/null\nprintf '%s\\n' \"${PATH}\"\n",
        encoding="utf-8",
    )

    result = _run_launcher(
        tmp_path,
        source,
        image,
        extra_env={"PATH": inherited_path},
    )

    assert result.returncode == 0
    assert result.stdout == (
        f"{inherited_path}:/usr/local/sbin:/usr/local/bin:"
        "/usr/sbin:/usr/bin:/sbin:/bin\n"
    )


def test_launcher_invalid_inherited_path_does_not_block_entrypoint(tmp_path: Path) -> None:
    source = tmp_path / "source"
    image = tmp_path / "image"
    _write_bundle(source, "unused")
    _write_bundle(image, "image")
    (source / "entrypoint.sh").write_text(
        "#!/bin/bash\ndate +%s >/dev/null\nprintf 'started\\n'\n",
        encoding="utf-8",
    )

    result = _run_launcher(
        tmp_path,
        source,
        image,
        extra_env={"PATH": "/definitely-invalid"},
    )

    assert result.returncode == 0
    assert result.stdout == "started\n"


def test_required_control_script_symlink_forces_bundle_fallback(tmp_path: Path) -> None:
    source = tmp_path / "source"
    image = tmp_path / "image"
    _write_bundle(source, "source", extra_files=("future.sh",))
    _write_bundle(image, "image")
    (source / "update.sh").unlink()
    (source / "update.sh").symlink_to(source / "future.sh")

    result = _run_launcher(tmp_path, source, image)

    assert result.returncode == 0
    assert result.stdout == "image\n"


def test_future_control_script_symlink_forces_bundle_fallback(tmp_path: Path) -> None:
    source = tmp_path / "source"
    image = tmp_path / "image"
    _write_bundle(source, "source")
    _write_bundle(image, "image")
    (source / "future.sh").symlink_to(source / "browser.sh")

    result = _run_launcher(tmp_path, source, image)

    assert result.returncode == 0
    assert result.stdout == "image\n"


def test_materialized_bundle_must_match_selected_generation(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _write_bundle(source, "source")
    script = textwrap.dedent(
        f"""\
        source {LAUNCHER!s}
        RUNTIME_ROOT="$2"
        materialize_control_bundle "$1" mismatched-generation
        """
    )

    result = subprocess.run(
        ["bash", "-c", script, "bootstrap-test", str(source), str(tmp_path / "run")],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert not (tmp_path / "run" / "mismatched-generation").exists()


def test_launcher_operational_failure_runs_image_entrypoint_directly(tmp_path: Path) -> None:
    source = tmp_path / "source"
    image = tmp_path / "image"
    _write_bundle(source, "source")
    _write_bundle(image, "image")
    script = textwrap.dedent(
        f"""\
        source {LAUNCHER!s}
        SOURCE_CONTROL_DIR="$1"
        IMAGE_CONTROL_DIR="$2"
        RUNTIME_ROOT="$3"
        source_bundle_is_trusted() {{ control_bundle_generation "$1" >/dev/null; }}
        materialize_control_bundle() {{ return 1; }}
        launch_with_fallback
        """
    )

    result = subprocess.run(
        ["bash", "-c", script, "bootstrap-test", str(source), str(image), str(tmp_path / "run")],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout == "image\n"
    assert "直接使用镜像内置版本启动" in result.stderr


def test_source_generation_probe_failure_never_starts_image_entrypoint(tmp_path: Path) -> None:
    source = tmp_path / "source"
    image = tmp_path / "image"
    marker = tmp_path / "image-entrypoint-started"
    _write_bundle(source, "source")
    _write_bundle(image, "unused")
    (source / "future.sh").write_text("if then\n", encoding="utf-8")
    (image / "entrypoint.sh").write_text(
        f"#!/bin/bash\ntouch {marker!s}\n",
        encoding="utf-8",
    )
    env = {
        **os.environ,
        "MOVIEPILOT_SOURCE_CONTROL_DIR": str(source),
        "MOVIEPILOT_IMAGE_CONTROL_DIR": str(image),
        "MOVIEPILOT_RUNTIME_CONTROL_ROOT": str(tmp_path / "run"),
    }

    result = subprocess.run(
        ["bash", str(LAUNCHER), "--source-generation"],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert not marker.exists()


def test_source_generation_probe_propagates_generation_failure(tmp_path: Path) -> None:
    script = textwrap.dedent(
        f"""\
        source {LAUNCHER!s}
        source_bundle_is_trusted() {{ return 0; }}
        control_bundle_generation() {{ return 1; }}
        launcher_main --source-generation
        """
    )

    result = subprocess.run(
        ["bash", "-c", script],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0


def test_control_generation_automatically_covers_future_shell_files(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _write_bundle(source, "source")
    script = textwrap.dedent(
        f"""\
        source {LAUNCHER!s}
        control_bundle_generation "$1"
        """
    )
    before = subprocess.run(
        ["bash", "-c", script, "bootstrap-test", str(source)],
        text=True,
        capture_output=True,
        check=True,
    ).stdout
    (source / "future.sh").write_text("#!/bin/bash\n:\n", encoding="utf-8")
    after = subprocess.run(
        ["bash", "-c", script, "bootstrap-test", str(source)],
        text=True,
        capture_output=True,
        check=True,
    ).stdout

    assert before != after


def test_control_generation_does_not_depend_on_bundle_directory(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    _write_bundle(first, "same")
    _write_bundle(second, "same")
    script = textwrap.dedent(
        f"""\
        source {LAUNCHER!s}
        control_bundle_generation "$1"
        """
    )

    generations = [
        subprocess.run(
            ["bash", "-c", script, "bootstrap-test", str(bundle)],
            text=True,
            capture_output=True,
            check=True,
        ).stdout
        for bundle in (first, second)
    ]

    assert generations[0] == generations[1]


@pytest.mark.parametrize(
    "fake_sha256sum",
    ("sha256sum() { return 1; }", "sha256sum() { printf '\\n'; return 0; }"),
)
def test_control_generation_rejects_hash_failures(
    tmp_path: Path, fake_sha256sum: str
) -> None:
    source = tmp_path / "source"
    _write_bundle(source, "source")
    script = textwrap.dedent(
        f"""\
        source {LAUNCHER!s}
        {fake_sha256sum}
        control_bundle_generation "$1"
        """
    )

    result = subprocess.run(
        ["bash", "-c", script, "generation-hash-test", str(source)],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert result.stdout == ""


def test_launcher_does_not_promote_proxy_host_to_process_proxy_env(tmp_path: Path) -> None:
    source = tmp_path / "source"
    image = tmp_path / "image"
    _write_bundle(source, "unused")
    _write_bundle(image, "image")
    (source / "entrypoint.sh").write_text(
        "#!/bin/bash\nprintf '%s|%s|%s|%s\\n' \"${HTTP_PROXY-unset}\" \"${HTTPS_PROXY-unset}\" \"${http_proxy-unset}\" \"${https_proxy-unset}\"\n",
        encoding="utf-8",
    )

    result = _run_launcher(
        tmp_path,
        source,
        image,
        extra_env={"PROXY_HOST": "http://package-proxy.example:7890"},
    )

    assert result.returncode == 0
    assert result.stdout == "unset|unset|unset|unset\n"


def test_launcher_preserves_explicit_standard_proxy_env(tmp_path: Path) -> None:
    source = tmp_path / "source"
    image = tmp_path / "image"
    _write_bundle(source, "unused")
    _write_bundle(image, "image")
    (source / "entrypoint.sh").write_text(
        "#!/bin/bash\nprintf '%s|%s\\n' \"${HTTP_PROXY-unset}\" \"${HTTPS_PROXY-unset}\"\n",
        encoding="utf-8",
    )

    result = _run_launcher(
        tmp_path,
        source,
        image,
        extra_env={
            "HTTP_PROXY": "http://explicit-http.example:8080",
            "HTTPS_PROXY": "http://explicit-https.example:8443",
        },
    )

    assert result.returncode == 0
    assert result.stdout == "http://explicit-http.example:8080|http://explicit-https.example:8443\n"


@pytest.mark.parametrize(
    ("launcher_args", "expected"),
    (((), "unset|unset|0\n"), (("--post-update-reexec",), "1|1|0\n")),
)
def test_launcher_owns_and_consumes_reexec_guard_state(
    tmp_path: Path, launcher_args: tuple[str, ...], expected: str
) -> None:
    source = tmp_path / "source"
    image = tmp_path / "image"
    _write_bundle(source, "unused")
    _write_bundle(image, "image")
    (source / "entrypoint.sh").write_text(
        "#!/bin/bash\nprintf '%s|%s|%s\\n' \"${MOVIEPILOT_BOOTSTRAP_UPDATE_DONE-unset}\" \"${MOVIEPILOT_BOOTSTRAP_REEXECUTED-unset}\" \"$#\"\n",
        encoding="utf-8",
    )
    script = textwrap.dedent(
        f"""\
        source {LAUNCHER!s}
        source_bundle_is_trusted() {{ control_bundle_generation "$1" >/dev/null; }}
        launch_with_fallback "$@"
        """
    )
    env = {
        **os.environ,
        "MOVIEPILOT_SOURCE_CONTROL_DIR": str(source),
        "MOVIEPILOT_IMAGE_CONTROL_DIR": str(image),
        "MOVIEPILOT_RUNTIME_CONTROL_ROOT": str(tmp_path / "run"),
        "MOVIEPILOT_BOOTSTRAP_UPDATE_DONE": "1",
        "MOVIEPILOT_BOOTSTRAP_REEXECUTED": "1",
    }

    result = subprocess.run(
        ["bash", "-c", script, "bootstrap-guard-test", *launcher_args],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout == expected


def test_updater_package_proxy_stays_command_scoped(tmp_path: Path) -> None:
    script = textwrap.dedent(
        f"""\
        CONFIG_DIR="$1"
        PROXY_HOST=http://package-proxy.example:7890
        source {UPDATER!s}
        set_package_proxy_env
        printf '%s|%s|%s|%s\\n' "${{HTTP_PROXY-unset}}" "${{HTTPS_PROXY-unset}}" "${{http_proxy-unset}}" "${{https_proxy-unset}}"
        printf '%s\\n' "${{PACKAGE_ENV[*]}}"
        """
    )
    env = dict(os.environ)
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        env.pop(name, None)

    result = subprocess.run(
        ["bash", "-c", script, "updater-proxy-test", str(tmp_path / "config")],
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )

    assert result.stdout.splitlines() == [
        "unset|unset|unset|unset",
        "HTTP_PROXY=http://package-proxy.example:7890 HTTPS_PROXY=http://package-proxy.example:7890 http_proxy=http://package-proxy.example:7890 https_proxy=http://package-proxy.example:7890",
    ]


@pytest.mark.parametrize(
    ("mode", "install_result", "expected"),
    (("false", "unused", "noop"), ("dev", "success", "updated"), ("dev", "failure", "failed")),
)
def test_updater_exposes_explicit_result(
    tmp_path: Path, mode: str, install_result: str, expected: str
) -> None:
    script = textwrap.dedent(
        f"""\
        CONFIG_DIR="$1"
        MOVIEPILOT_AUTO_UPDATE="$2"
        INSTALL_RESULT="$3"
        PIP_PROXY= PROXY_HOST= GITHUB_PROXY= GITHUB_TOKEN=
        source {UPDATER!s}
        INFO() {{ :; }}
        WARN() {{ :; }}
        ERROR() {{ :; }}
        test_connectivity_package() {{ PACKAGE_LOG=test; return 0; }}
        test_connectivity_github() {{ GITHUB_LOG=test; return 0; }}
        install_backend_and_download_resources() {{
            if [ "${{INSTALL_RESULT}}" = success ]; then
                MOVIEPILOT_UPDATE_RESULT=updated
                return 0
            fi
            return 1
        }}
        run_moviepilot_update
        printf '%s\\n' "${{MOVIEPILOT_UPDATE_RESULT}}"
        """
    )

    result = subprocess.run(
        ["bash", "-c", script, "updater-test", str(tmp_path / "config"), mode, install_result],
        text=True,
        capture_output=True,
        check=True,
    )

    assert result.stdout == f"{expected}\n"


def test_release_noop_preserves_prerelease_selection_without_probing_package_index(
    tmp_path: Path,
) -> None:
    package_probe = tmp_path / "package-probe"
    curl_log = tmp_path / "curl.log"
    comparison_log = tmp_path / "comparison.log"
    script = textwrap.dedent(
        f"""\
        CONFIG_DIR="$1"
        MOVIEPILOT_AUTO_UPDATE=release
        PIP_PROXY= PROXY_HOST= GITHUB_PROXY= GITHUB_TOKEN=
        PACKAGE_PROBE="$2"
        CURL_LOG="$3"
        COMPARISON_LOG="$4"
        source {UPDATER!s}
        INFO() {{ :; }}
        WARN() {{ :; }}
        ERROR() {{ :; }}
        test_connectivity_package() {{ touch "${{PACKAGE_PROBE}}"; return 0; }}
        test_connectivity_github() {{ CURL_OPTIONS=-sL; GITHUB_LOG=test; return 0; }}
        compare_versions() {{ printf '%s|%s\n' "$1" "$2" > "${{COMPARISON_LOG}}"; return 1; }}
        grep() {{
            if [[ "$*" == *"/app/version.py"* ]]; then
                printf '%s\n' "APP_VERSION = 'v3.0.0'"
                return 0
            fi
            command grep "$@"
        }}
        sed() {{
            if [[ "$*" == *"APP_VERSION"* ]]; then
                printf '%s\n' 'v3.0.0'
                return 0
            fi
            command sed "$@"
        }}
        curl() {{
            printf '%s\n' "$*" >> "${{CURL_LOG}}"
            printf '%s\n' '[{{"tag_name":"v2.15.6"}},{{"tag_name":"v3.1.0-beta"}},{{"tag_name":"v3.1.0-rc"}},{{"tag_name":"v3.0.0"}}]'
        }}
        run_moviepilot_update
        printf '%s\n' "${{MOVIEPILOT_UPDATE_RESULT}}"
        """
    )

    result = subprocess.run(
        [
            "bash",
            "-c",
            script,
            "release-noop-test",
            str(tmp_path / "config"),
            str(package_probe),
            str(curl_log),
            str(comparison_log),
        ],
        text=True,
        capture_output=True,
        check=True,
    )

    assert result.stdout == "noop\n"
    assert not package_probe.exists()
    curl_args = curl_log.read_text(encoding="utf-8")
    assert "/releases" in curl_args
    assert "/releases/latest" not in curl_args
    assert "--compressed" in curl_args
    assert "--fail" in curl_args
    assert "--connect-timeout 5" in curl_args
    assert "--max-time 15" in curl_args
    assert comparison_log.read_text(encoding="utf-8") == "v3.0.0|v3.1.0-rc\n"


@pytest.mark.parametrize(
    ("pyproject_changed", "lock_changed", "expected_route_calls", "expected_sync_calls"),
    (
        (False, False, 0, 0),
        (True, False, 1, 1),
        (False, True, 1, 1),
        (True, True, 1, 1),
    ),
)
def test_package_route_is_only_configured_for_changed_dependencies(
    tmp_path: Path,
    pyproject_changed: bool,
    lock_changed: bool,
    expected_route_calls: int,
    expected_sync_calls: int,
) -> None:
    uv_bin = tmp_path / "bin" / "uv"
    uv_bin.parent.mkdir(parents=True)
    uv_log = tmp_path / "uv.log"
    route_log = tmp_path / "route.log"
    uv_bin.write_text(
        "#!/bin/bash\n"
        "printf '%s|%s\\n' \"${UV_PROJECT_ENVIRONMENT:-}\" \"$*\" >> \"${UV_TEST_LOG}\"\n",
        encoding="utf-8",
    )
    uv_bin.chmod(0o755)
    update_tree = tmp_path / "update" / "App"
    update_tree.mkdir(parents=True)
    (update_tree / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    (update_tree / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    script = textwrap.dedent(
        f"""\
        CONFIG_DIR="$1"
        VENV_PATH="$2"
        TMP_PATH="$3"
        ROUTE_LOG="$4"
        PYPROJECT_CHANGED="$5"
        LOCK_CHANGED="$6"
        UV_BIN="$7"
        PIP_PROXY= PROXY_HOST=
        source {UPDATER!s}
        INFO() {{ :; }}
        WARN() {{ :; }}
        ERROR() {{ :; }}
        cmp() {{
            case "$2" in
                */pyproject.toml) [ "${{PYPROJECT_CHANGED}}" = false ] ;;
                */uv.lock) [ "${{LOCK_CHANGED}}" = false ] ;;
            esac
        }}
        configure_package_route() {{
            printf 'route\n' >> "${{ROUTE_LOG}}"
            PACKAGE_LOG=test
            PACKAGE_ENV=()
            UV_OPTIONS=()
        }}
        if dependency_manifests_changed; then
            sync_project_dependencies
        fi
        """
    )

    env = {**os.environ, "UV_TEST_LOG": str(uv_log)}
    result = subprocess.run(
        [
            "bash",
            "-c",
            script,
            "package-route-test",
            str(tmp_path / "config"),
            str(tmp_path / "venv"),
            str(tmp_path / "update"),
            str(route_log),
            str(pyproject_changed).lower(),
            str(lock_changed).lower(),
            str(uv_bin),
        ],
        text=True,
        capture_output=True,
        check=True,
        env=env,
    )

    assert result.stderr == ""
    route_calls = route_log.read_text(encoding="utf-8").splitlines() if route_log.exists() else []
    sync_calls = uv_log.read_text(encoding="utf-8").splitlines() if uv_log.exists() else []
    assert len(route_calls) == expected_route_calls
    assert len(sync_calls) == expected_sync_calls
    if expected_sync_calls:
        assert sync_calls == [
            f"{tmp_path / 'venv'}|sync --project {update_tree} "
            f"--locked --inexact --no-dev --no-install-project "
            f"--python {tmp_path / 'venv' / 'bin' / 'python3'}"
        ]


@pytest.mark.parametrize("missing_manifest", ("pyproject.toml", "uv.lock"))
def test_dependency_update_requires_complete_uv_manifests(
    tmp_path: Path,
    missing_manifest: str,
) -> None:
    update_tree = tmp_path / "update" / "App"
    update_tree.mkdir(parents=True)
    (update_tree / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    (update_tree / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    (update_tree / missing_manifest).unlink()
    route_marker = tmp_path / "route-called"
    copy_marker = tmp_path / "copy-called"
    script = textwrap.dedent(
        f"""\
        CONFIG_DIR="$1"
        TMP_PATH="$2"
        ROUTE_MARKER="$3"
        COPY_MARKER="$4"
        PIP_PROXY= PROXY_HOST=
        source {UPDATER!s}
        INFO() {{ :; }}
        WARN() {{ :; }}
        ERROR() {{ :; }}
        download_and_unzip() {{ return 0; }}
        configure_package_route() {{ touch "${{ROUTE_MARKER}}"; }}
        cp() {{ touch "${{COPY_MARKER}}"; }}
        ! install_backend_and_download_resources tags/v3.0.1.zip
        """
    )

    subprocess.run(
        [
            "bash",
            "-c",
            script,
            "incomplete-manifest-test",
            str(tmp_path / "config"),
            str(tmp_path / "update"),
            str(route_marker),
            str(copy_marker),
        ],
        text=True,
        capture_output=True,
        check=True,
    )

    assert not route_marker.exists()
    assert not copy_marker.exists()


def test_failed_dependency_sync_does_not_replace_program_files(tmp_path: Path) -> None:
    uv_bin = tmp_path / "bin" / "uv"
    uv_bin.parent.mkdir(parents=True)
    uv_bin.write_text(
        "#!/bin/bash\nprintf '%s\\n' \"$*\" >> \"${UV_LOG}\"\nexit 1\n",
        encoding="utf-8",
    )
    uv_bin.chmod(0o755)
    update_tree = tmp_path / "update" / "App"
    update_tree.mkdir(parents=True)
    (update_tree / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    (update_tree / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    copy_log = tmp_path / "copies.log"
    uv_log = tmp_path / "uv.log"
    script = textwrap.dedent(
        f"""\
        CONFIG_DIR="$1"
        VENV_PATH="$2"
        TMP_PATH="$3"
        UV_BIN="$4"
        COPY_LOG="$5"
        PIP_PROXY= PROXY_HOST=
        source {UPDATER!s}
        INFO() {{ :; }}
        WARN() {{ :; }}
        ERROR() {{ :; }}
        download_and_unzip() {{ return 0; }}
        cmp() {{ return 1; }}
        cp() {{ printf '%s\n' "$*" >> "${{COPY_LOG}}"; }}
        configure_package_route() {{ PACKAGE_LOG=test; PACKAGE_ENV=(); UV_OPTIONS=(); }}
        install_backend_and_download_resources tags/v3.0.1.zip || true
        """
    )

    subprocess.run(
        [
            "bash",
            "-c",
            script,
            "dependency-failure-test",
            str(tmp_path / "config"),
            str(tmp_path / "venv"),
            str(tmp_path / "update"),
            str(uv_bin),
            str(copy_log),
        ],
        text=True,
        capture_output=True,
        check=True,
        env={**os.environ, "UV_LOG": str(uv_log)},
    )

    assert not copy_log.exists()
    assert uv_log.read_text(encoding="utf-8") == (
        f"sync --project {update_tree} --locked --inexact --no-dev "
        f"--no-install-project --python {tmp_path / 'venv' / 'bin' / 'python3'}\n"
    )


def test_package_index_probe_is_cacheless_and_bounded(tmp_path: Path) -> None:
    timeout_log = tmp_path / "timeout.log"
    script = textwrap.dedent(
        f"""\
        CONFIG_DIR="$1"
        VENV_PATH="$2"
        TIMEOUT_LOG="$3"
        UV_BIN="$4"
        PIP_PROXY=https://packages.example/simple
        PROXY_HOST=
        source {UPDATER!s}
        timeout() {{ printf '%s\n' "$*" > "${{TIMEOUT_LOG}}"; return 124; }}
        test_connectivity_package 0 || true
        """
    )

    subprocess.run(
        [
            "bash",
            "-c",
            script,
            "package-probe-test",
            str(tmp_path / "config"),
            str(tmp_path / "venv"),
            str(timeout_log),
            "/fake/uv",
        ],
        text=True,
        capture_output=True,
        check=True,
    )

    command = timeout_log.read_text(encoding="utf-8")
    assert command.startswith("--kill-after=2s 10s env ")
    assert "UV_NO_CACHE=1" in command
    assert "UV_HTTP_TIMEOUT=5" in command
    assert "UV_HTTP_RETRIES=0" in command
    assert "/fake/uv pip install --target " in command
    assert (
        " --no-deps --default-index https://packages.example/simple pip-hello-world" in command
    )
    assert "uninstall" not in command
    probe_dir = Path(command.split("--target ", 1)[1].split(" ", 1)[0])
    assert not probe_dir.exists()


@pytest.mark.parametrize(
    ("result", "current", "next_generation", "guard", "expected"),
    (
        ("noop", "old", "new", "0", 1),
        ("failed", "old", "new", "0", 1),
        ("updated", "same", "same", "0", 1),
        ("updated", "old", "new", "0", 0),
        ("updated", "old", "new", "1", 2),
    ),
)
def test_control_bundle_reexec_decision(
    result: str,
    current: str,
    next_generation: str,
    guard: str,
    expected: int,
) -> None:
    entrypoint = (ROOT / "docker" / "entrypoint.sh").read_text(encoding="utf-8")
    functions = entrypoint.split("# 使用env配置", 1)[0]
    script = f'{functions}\ncontrol_bundle_reexec_decision "$1" "$2" "$3" "$4"\n'

    completed = subprocess.run(
        ["bash", "-c", script, "reexec-test", result, current, next_generation, guard],
        check=False,
    )

    assert completed.returncode == expected


def test_failed_generation_probe_keeps_current_control_snapshot(tmp_path: Path) -> None:
    marker = tmp_path / "reexec-decision-called"
    entrypoint = (ROOT / "docker" / "entrypoint.sh").read_text(encoding="utf-8")
    functions = entrypoint.split("# 使用env配置", 1)[0]
    script = textwrap.dedent(
        f"""\
        {functions}
        source_control_generation() {{ return 1; }}
        control_bundle_reexec_decision() {{ touch {shlex.quote(str(marker))}; return 0; }}
        WARN() {{ printf '%s\\n' "$1"; }}
        MOVIEPILOT_UPDATE_RESULT=updated
        maybe_reexec_control_bundle
        """
    )

    completed = subprocess.run(
        ["bash", "-c", script, "generation-probe-test", str(marker)],
        text=True,
        capture_output=True,
        check=True,
    )

    assert not marker.exists()
    assert "继续使用当前控制脚本快照启动" in completed.stdout
