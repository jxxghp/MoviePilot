import os
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).parents[1]
MAIN_PATH = PROJECT_ROOT / "app" / "main.py"


def test_main_script_does_not_shadow_stdlib_platform(tmp_path):
    """PyCharm 脚本启动路径不得让 app/platform 遮蔽标准库 platform。"""
    probe = "\n".join(
        (
            "import runpy",
            "import sys",
            f"sys.path.insert(0, {str(MAIN_PATH.parent)!r})",
            "sys.modules.pop('platform', None)",
            f"runpy.run_path({str(MAIN_PATH)!r}, run_name='pycharm_main_probe')",
            "import platform",
            "assert hasattr(platform, 'python_implementation')",
            "assert '/app/platform/' not in str(getattr(platform, '__file__', ''))",
        )
    )
    env = {
        **os.environ,
        "CONFIG_DIR": str(tmp_path / "config"),
        "MOVIEPILOT_AUTO_UPDATE": "off",
    }

    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
