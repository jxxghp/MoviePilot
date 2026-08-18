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
        printf '%s\\n' "${{PIP_ENV[*]}}"
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
        test_connectivity_pip() {{ PIP_LOG=test; return 0; }}
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
