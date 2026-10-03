"""安装阶段编译并验收 CJK 扩展；不导入应用，也不访问用户数据库。"""

import argparse
import hashlib
import os
import platform
import shlex
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

SOURCE = Path(__file__).resolve().parent
LIBRARY = "libfts5_cjk.so"


def verify(extension: Path) -> None:
    """用当前 Python 的 SQLite 验证动态加载和两字中文子串索引。"""
    with sqlite3.connect(":memory:") as connection:
        if not hasattr(connection, "enable_load_extension"):
            raise RuntimeError("当前 Python 的 SQLite 不支持加载扩展，请更换支持此能力的 Python")
        try:
            connection.enable_load_extension(True)
            connection.load_extension(str(extension))
        finally:
            connection.enable_load_extension(False)
        connection.execute("CREATE VIRTUAL TABLE probe USING fts5(content, tokenize='cjk_unicode61')")
        connection.execute("INSERT INTO probe VALUES ('电影订阅成功')")
        count = connection.execute("SELECT count(*) FROM probe WHERE probe MATCH '订阅'").fetchone()[0]
        if count != 1:
            raise RuntimeError("CJK 扩展已加载，但两字中文索引验收失败")


def fingerprint() -> str:
    """源码、构建逻辑或目标架构改变时重建，不因重复安装而重复编译。"""
    digest = hashlib.sha256(f"{sys.platform}:{platform.machine()}:{sys.maxsize}".encode())
    for name in ("install.py", "fts5_cjk.c", "vendor/sqlite3.h", "vendor/sqlite3ext.h"):
        digest.update((SOURCE / name).read_bytes())
    return digest.hexdigest()


def compiler_command(output: Path) -> list[str]:
    """选择本机 C 工具链；Windows 支持开发者终端中的 MSVC 或 MinGW。"""
    configured = os.environ.get("CC", "").strip()
    if configured:
        compiler = [configured] if Path(configured).is_file() else shlex.split(configured)
    else:
        available = next((path for name in ("cc", "gcc", "clang", "cl") if (path := shutil.which(name))), None)
        if not available:
            raise RuntimeError(
                "未找到 C 编译器：Debian/Ubuntu 安装 build-essential；macOS 执行 xcode-select --install；"
                "Windows 使用 MSVC 开发者终端或 MinGW，再运行 moviepilot install cjk"
            )
        compiler = [available]
    source = str(SOURCE / "fts5_cjk.c")
    headers = str(SOURCE / "vendor")
    if Path(compiler[0]).name.lower() in {"cl", "cl.exe"}:
        return [*compiler, "/nologo", "/LD", "/O2", f"/I{headers}", source, f"/Fe:{output}",
                f"/Fo:{output.with_suffix('.obj')}"]
    flags = ["-dynamiclib"] if sys.platform == "darwin" else ["-shared"]
    if sys.platform != "win32":
        flags.append("-fPIC")
    return [*compiler, *flags, "-O2", "-Wall", "-Wextra", f"-I{headers}", source, "-o", str(output)]


def install(extension: Path) -> None:
    """临时编译、真实加载验收后原子替换，失败时保留原有扩展。"""
    expected = fingerprint()
    stamp = extension.with_suffix(".sha256")
    if extension.is_file() and stamp.is_file() and stamp.read_text(encoding="utf-8").strip() == expected:
        try:
            verify(extension)
            return
        except (sqlite3.Error, OSError, RuntimeError):
            pass
    extension.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".fts5-cjk-", dir=extension.parent) as temporary:
        candidate = Path(temporary) / LIBRARY
        subprocess.run(compiler_command(candidate), check=True, capture_output=True, text=True, timeout=120,
                       cwd=temporary)
        verify(candidate)
        candidate.chmod(0o644)
        candidate.replace(extension)
        marker = Path(temporary) / stamp.name
        marker.write_text(expected + "\n", encoding="utf-8")
        marker.chmod(0o644)
        marker.replace(stamp)


def main() -> int:
    """提供 Docker 和 CLI 共用的安装入口，以及无编译的能力检查。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(sys.prefix) / "lib/moviepilot" / LIBRARY)
    parser.add_argument("--check", action="store_true", help="只检查已安装扩展，不编译或修改文件")
    args = parser.parse_args()
    extension = args.output.expanduser().absolute()
    try:
        if args.check:
            verify(extension)
        else:
            install(extension)
    except (OSError, RuntimeError, sqlite3.Error, subprocess.SubprocessError) as error:
        detail = error.stderr.strip() if isinstance(error, subprocess.CalledProcessError) and error.stderr else str(error)
        print(f"CJK 扩展不可用：{detail}", file=sys.stderr)
        return 1
    print(f"CJK 扩展已通过两字中文索引验证：{extension}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
