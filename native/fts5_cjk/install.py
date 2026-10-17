"""
安装阶段编译并验收 CJK 扩展；不导入应用，也不访问用户数据库。
"""

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

# 构建输入以安装脚本所在目录为根，不依赖调用时的工作目录。
SOURCE = Path(__file__).resolve().parent

# 各平台统一使用此制品文件名，由当前解释器的 SQLite 动态加载。
LIBRARY = "libfts5_cjk.so"


def verify(extension: Path) -> None:
    """
    用当前解释器的内存 SQLite 验证动态加载和两字中文索引。

    只操作内存库，不访问用户数据库；加载尝试结束后关闭扩展加载权限。

    :param extension: 待加载的本地扩展文件路径
    :raises RuntimeError: 解释器不支持扩展加载或两字中文查询未命中
    :raises sqlite3.Error: 扩展加载或索引操作失败
    """
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
    """
    计算当前平台、处理器架构、解释器位数和编译输入的构建指纹。

    包含安装脚本、C 源码和 SQLite 头文件的完整内容；注释修改也会改变指纹。
    环境变量 CC 和编译器版本不在指纹中。

    :return: SHA256 十六进制指纹，用于判断已有扩展是否需要重建
    """
    digest = hashlib.sha256(f"{sys.platform}:{platform.machine()}:{sys.maxsize}".encode())
    for name in ("install.py", "fts5_cjk.c", "vendor/sqlite3.h", "vendor/sqlite3ext.h"):
        digest.update((SOURCE / name).read_bytes())
    return digest.hexdigest()


def compiler_command(output: Path) -> list[str]:
    """
    为当前平台构造扩展编译命令，不执行编译。

    优先使用 CC；未设置时依次查找 cc、gcc、clang、cl。Windows 支持 MSVC 或 MinGW，
    macOS 使用动态库参数，其他平台使用共享库参数。

    :param output: 动态库输出路径，MSVC 对象文件放在相同位置并使用 .obj 后缀
    :return: 可直接传给 subprocess 的参数列表
    :raises RuntimeError: 未配置 CC 且找不到支持的 C 编译器
    """
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
    """
    复验可复用扩展，或在临时目录编译并验收后安装。

    扩展与指纹匹配且复验成功时跳过编译；重建使用同目录临时路径并限制编译为 120 秒。
    编译或验收失败时保留旧扩展；验收后先替换扩展，再单独替换指纹标记，两者不是同一原子事务。

    :param extension: 最终扩展文件路径；同目录保存 .sha256 构建标记
    :raises OSError: 源码读取或安装文件操作失败
    :raises RuntimeError: 找不到编译器或扩展能力验收失败
    :raises sqlite3.Error: 扩展加载或索引验证失败
    :raises subprocess.SubprocessError: 编译失败或超时
    """
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
    """
    解析安装或只读检查选项，并向终端输出能力验收结果。

    默认安装到当前解释器前缀下的 lib/moviepilot；--output 可指定路径，--check 只验证现有扩展。

    :return: 成功返回 0，安装或检查失败返回 1；参数解析错误由 argparse 处理
    """
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
