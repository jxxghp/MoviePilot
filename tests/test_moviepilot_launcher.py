from pathlib import Path
import os
import stat
import subprocess

import pytest


LAUNCHER = Path(__file__).resolve().parents[1] / "moviepilot"


@pytest.mark.parametrize(
    "arguments",
    [
        ("install", "deps", "--recreate"),
        ("setup", "--recreate"),
        ("update", "backend", "--recreate"),
    ],
)
def test_recreate_commands_use_external_bootstrap_python(tmp_path, arguments):
    """所有会删除 venv 的 launcher 入口都不能由目标 venv Python 执行。"""
    root = tmp_path / "moviepilot"
    root.mkdir()
    launcher = root / "moviepilot"
    launcher.write_text(LAUNCHER.read_text(encoding="utf-8"), encoding="utf-8")
    launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR)
    (root / "scripts").mkdir()
    (root / "scripts" / "local_setup.py").write_text("# test stub\n", encoding="utf-8")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    record = tmp_path / "record"
    external_python = bin_dir / "python3.14"
    external_python.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"-\" ]; then exit 0; fi\n"
        "printf '%s\\n' \"$0 $*\" > \"$MOVIEPILOT_TEST_RECORD\"\n",
        encoding="utf-8",
    )
    external_python.chmod(external_python.stat().st_mode | stat.S_IXUSR)

    venv_python = root / "venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_script = (
        "#!/bin/sh\n"
        "printf '%s\\n' \"$0 $*\" > \"$MOVIEPILOT_TEST_RECORD\"\n"
    )
    venv_python.write_text(venv_script, encoding="utf-8")
    venv_python.chmod(venv_python.stat().st_mode | stat.S_IXUSR)
    venv_alias = venv_python.with_name("python3.14")
    venv_alias.write_text(venv_script, encoding="utf-8")
    venv_alias.chmod(venv_alias.stat().st_mode | stat.S_IXUSR)

    env = os.environ.copy()
    env["PATH"] = f"{venv_python.parent}:{bin_dir}:/usr/bin:/bin"
    env["MOVIEPILOT_TEST_RECORD"] = str(record)
    subprocess.run(
        [str(launcher), *arguments],
        cwd=root,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    invocation = record.read_text(encoding="utf-8")
    assert invocation.startswith(f"{external_python} ")


def test_recreate_accepts_explicit_external_python(tmp_path):
    """显式指定的外部 Python 可作为重建命令的执行解释器。"""
    root = tmp_path / "moviepilot"
    root.mkdir()
    launcher = root / "moviepilot"
    launcher.write_text(LAUNCHER.read_text(encoding="utf-8"), encoding="utf-8")
    launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR)
    (root / "scripts").mkdir()
    (root / "scripts" / "local_setup.py").write_text("# test stub\n", encoding="utf-8")

    explicit_python = tmp_path / "custom-python"
    record = tmp_path / "record"
    explicit_python.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"-\" ]; then exit 0; fi\n"
        "printf '%s\\n' \"$0 $*\" > \"$MOVIEPILOT_TEST_RECORD\"\n",
        encoding="utf-8",
    )
    explicit_python.chmod(explicit_python.stat().st_mode | stat.S_IXUSR)

    env = os.environ.copy()
    env["PATH"] = "/usr/bin:/bin"
    env["MOVIEPILOT_TEST_RECORD"] = str(record)
    subprocess.run(
        [
            str(launcher),
            "install",
            "deps",
            "--recreate",
            "--python",
            str(explicit_python),
        ],
        cwd=root,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    assert record.read_text(encoding="utf-8").startswith(f"{explicit_python} ")


@pytest.mark.parametrize(
    "arguments",
    [
        ("install", "deps", "--recreate"),
        ("setup", "--recreate"),
        ("update", "backend", "--recreate"),
    ],
)
@pytest.mark.parametrize("python_option", [(), ("--python", "python3.14")])
def test_recreate_excludes_custom_venv_from_external_bootstrap(
    tmp_path, arguments, python_option
):
    """自定义 --venv 目录中的解释器也不能执行重建流程。"""
    root = tmp_path / "moviepilot"
    root.mkdir()
    launcher = root / "moviepilot"
    launcher.write_text(LAUNCHER.read_text(encoding="utf-8"), encoding="utf-8")
    launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR)
    (root / "scripts").mkdir()
    (root / "scripts" / "local_setup.py").write_text("# test stub\n", encoding="utf-8")

    target_bin = tmp_path / "custom-venv" / "bin"
    target_bin.mkdir(parents=True)
    external_bin = tmp_path / "external-bin"
    external_bin.mkdir()
    record = tmp_path / "record"
    target_python = target_bin / "python3.14"
    external_python = external_bin / "python3.14"
    for python_path in (target_python, external_python):
        python_path.write_text(
            "#!/bin/sh\n"
            "if [ \"$1\" = \"-\" ]; then exit 0; fi\n"
            "printf '%s\\n' \"$0 $*\" > \"$MOVIEPILOT_TEST_RECORD\"\n",
            encoding="utf-8",
        )
        python_path.chmod(python_path.stat().st_mode | stat.S_IXUSR)

    env = os.environ.copy()
    env["PATH"] = f"{target_bin}:{external_bin}:/usr/bin:/bin"
    env["MOVIEPILOT_TEST_RECORD"] = str(record)
    subprocess.run(
        [
            str(launcher),
            *arguments,
            "--venv",
            str(target_bin.parent),
            *python_option,
        ],
        cwd=root,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    assert record.read_text(encoding="utf-8").startswith(f"{external_python} ")
