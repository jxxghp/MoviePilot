"""
本地持久 cell runner：以独立命名空间执行请求并保留跨请求状态。
"""

RUNNER_SOURCE = r'''
"""
自动生成的会话内核，每次请求执行一个代码单元并保留全局状态。
"""
import contextlib
import io
import json
import os
import sys
import threading
import traceback

_SENTINEL = os.environ["MOVIEPILOT_KERNEL_SENTINEL"]
_CAPTURE_LIMIT = 1000000
_SPILL_DIR = os.environ.get("MOVIEPILOT_KERNEL_SPILL_DIR", "")
_SPILL_CAP = 5000000
_PARENT_PROCESS_HANDLE = os.environ.pop("MOVIEPILOT_KERNEL_PARENT_PROCESS_HANDLE", "")
_PARENT_DEATH_FD = os.environ.pop("MOVIEPILOT_KERNEL_PARENT_DEATH_FD", "")


def _terminate_kernel():
    """
    宿主退出或代码主动结束会话时终止内核进程组或进程树。
    """
    try:
        if sys.platform == "win32":
            import subprocess
            subprocess.run(["taskkill", "/PID", str(os.getpid()), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=5, creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            import signal
            os.killpg(os.getpid(), signal.SIGKILL)
            return
    except Exception:
        pass
    os._exit(0)


def _start_parent_death_pipe_watchdog():
    """
    通过继承的管道监视 POSIX 宿主是否退出。

    宿主持有唯一的写端，异常退出时读端也会立即收到 EOF；监视线程因此能在用户代码
    执行期间清理内核，不必等待主循环再次读取标准输入。继承描述符也不依赖创建线程的寿命。
    """
    global _PARENT_DEATH_FD
    raw_fd = _PARENT_DEATH_FD
    _PARENT_DEATH_FD = ""
    if sys.platform == "win32" or not raw_fd:
        return
    try:
        fd = int(raw_fd)
        os.set_inheritable(fd, False)
    except (OSError, ValueError):
        return

    def _wait():
        """
        宿主唯一的管道写端关闭或读取失败后终止整个内核进程组。
        """
        try:
            while os.read(fd, 1):
                pass
        except OSError:
            pass
        _terminate_kernel()

    threading.Thread(target=_wait, name="moviepilot-parent-watchdog", daemon=True).start()


def _start_parent_process_watchdog():
    """
    通过 Windows 进程对象句柄监视确切的宿主进程。

    继承的 SYNCHRONIZE 句柄指向进程对象，不会因 PID 复用误判。
    句柄缺失或无效时不启用监视，避免初始化错误误杀健康内核。
    """
    global _PARENT_PROCESS_HANDLE
    raw_handle = _PARENT_PROCESS_HANDLE
    _PARENT_PROCESS_HANDLE = ""
    if sys.platform != "win32" or not raw_handle:
        return
    try:
        import ctypes
        from ctypes import wintypes

        handle = int(raw_handle)
        if handle <= 0:
            return
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.SetHandleInformation.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.DWORD,
        ]
        kernel32.SetHandleInformation.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        # 监视句柄仅供内核使用，禁止代码单元创建的子进程继续继承。
        # Windows 无法清除继承标记时关闭句柄并停用监视，避免泄漏。
        if not kernel32.SetHandleInformation(handle, 0x00000001, 0):
            kernel32.CloseHandle(handle)
            return
    except (ImportError, OSError, TypeError, ValueError):
        return

    def _wait():
        """
        等待宿主进程对象结束，关闭监视句柄并终止内核进程树。
        """
        try:
            result = kernel32.WaitForSingleObject(handle, 0xFFFFFFFF)
        finally:
            kernel32.CloseHandle(handle)
        # WAIT_OBJECT_0 表示宿主进程已经退出。
        if result == 0x00000000:
            _terminate_kernel()

    threading.Thread(target=_wait, name="moviepilot-parent-watchdog", daemon=True).start()


_start_parent_process_watchdog()
_start_parent_death_pipe_watchdog()

_real_stdout = sys.stdout

GLOBALS = {"__name__": "__main__", "__builtins__": __builtins__}


def _clip(text):
    """
    截取适合内联返回的输出文本。

    :param text: 完整输出文本
    :return: 最多一百万字符的文本，以及是否发生截断
    """
    return (text, False) if len(text) <= _CAPTURE_LIMIT else (text[:_CAPTURE_LIMIT], True)


def run_cell(request, execution_count):
    """
    在会话共享命名空间中执行一个代码单元。

    捕获标准输出、标准错误和异常；SystemExit 转为 exit 状态，其他异常转为 error 状态。

    :param request: 含 code 源码及可选 id 的请求映射
    :param execution_count: 当前会话的执行序号
    :return: 响应载荷和未截断的标准输出文本
    """
    out, err = io.StringIO(), io.StringIO()
    status, trace = "ok", ""
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            exec(compile(request["code"], "<cell>", "exec"), GLOBALS)
    except SystemExit as exc:
        status, trace = "exit", "SystemExit: " + repr(exc.code)
    except BaseException:
        status, trace = "error", traceback.format_exc()
    stdout_text, stdout_clipped = _clip(out.getvalue())
    stderr_text, stderr_clipped = _clip(err.getvalue())
    return {
        "id": request.get("id", ""), "status": status,
        "stdout": stdout_text, "stderr": stderr_text,
        "stdout_clipped": stdout_clipped, "stderr_clipped": stderr_clipped,
        "traceback": trace, "execution_count": execution_count,
    }, out.getvalue()


def _spill(text, spill_name):
    """
    将超出内联上限的标准输出写入独占创建的文件。

    :param text: 完整标准输出，编码后最多保留五百万字节
    :param spill_name: 由主循环生成的输出文件名
    :return: 文件路径；未配置目录、同名文件已存在或写入失败时返回空字符串
    """
    if not _SPILL_DIR:
        return ""
    try:
        spill_path = os.path.join(_SPILL_DIR, spill_name)
        descriptor = os.open(spill_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as f:
            f.write(text[:_SPILL_CAP].encode("utf-8", errors="replace")[:_SPILL_CAP])
        return spill_path
    except Exception:
        return ""


def _reply(payload):
    """
    用哨兵和字节长度封装响应，区分用户直接写入标准输出的内容。

    :param payload: 可序列化为 JSON 的响应映射
    """
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    _real_stdout.buffer.write(("\n" + _SENTINEL + " " + str(len(body)) + "\n").encode("utf-8"))
    _real_stdout.buffer.write(body)
    _real_stdout.buffer.flush()


def main():
    """
    顺序执行标准输入中的请求，直到输入关闭或代码显式退出。

    忽略空行和无法解码为 JSON 的输入；输出被截断时尝试保存完整内容到受限大小的文件。
    """
    execution_count = 0
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except ValueError:
            continue
        execution_count += 1
        payload, full_stdout = run_cell(request, execution_count)
        payload["stdout_bytes_total"] = sum(
            len(full_stdout[index:index + 32768].encode("utf-8", errors="replace"))
            for index in range(0, len(full_stdout), 32768)
        )
        payload["stdout_spill_path"] = (
            _spill(full_stdout, "cell_%06d_stdout.txt" % execution_count)
            if payload["stdout_clipped"] else ""
        )
        _reply(payload)
        if payload["status"] == "exit":
            _terminate_kernel()


if __name__ == "__main__":
    main()
'''
