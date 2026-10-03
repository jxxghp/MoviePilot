"""本地持久 cell runner：以独立命名空间执行请求并保留跨请求状态。"""

RUNNER_SOURCE = r'''
"""Auto-generated session-kernel runner. One exec cell per request."""
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
    """Terminate the process group/tree when its owning host disappears."""
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
    """POSIX twin of the Windows handle watchdog: exit when the parent dies.

    The host holds the only write end of an inherited pipe; a blocking read
    returns EOF the instant the host exits by ANY means (SIGKILL, OOM, crash),
    even while user code is executing. Stdin EOF alone is not enough because
    the main loop only sees it between cells. The inherited descriptor also
    avoids binding supervision to the lifetime of a spawning thread.
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
        """Retire the whole kernel group when the host's only write end closes."""
        try:
            while os.read(fd, 1):
                pass
        except OSError:
            pass
        _terminate_kernel()

    threading.Thread(target=_wait, name="moviepilot-parent-watchdog", daemon=True).start()


def _start_parent_process_watchdog():
    """Exit when the exact Windows parent process object is signaled.

    The inherited SYNCHRONIZE handle names a process object, not a reusable
    PID. Missing or invalid handles fail open so watchdog setup can never kill
    an otherwise healthy kernel.
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
        # This process needs the handle, but user code spawned by a cell must
        # not pass it any further. If Windows refuses to clear inheritance,
        # disable the watchdog rather than leak the handle into cell children.
        if not kernel32.SetHandleInformation(handle, 0x00000001, 0):
            kernel32.CloseHandle(handle)
            return
    except (ImportError, OSError, TypeError, ValueError):
        return

    def _wait():
        """Wait for the exact parent process object, then retire its kernel tree."""
        try:
            result = kernel32.WaitForSingleObject(handle, 0xFFFFFFFF)
        finally:
            kernel32.CloseHandle(handle)
        if result == 0x00000000:  # WAIT_OBJECT_0: the parent exited
            _terminate_kernel()

    threading.Thread(target=_wait, name="moviepilot-parent-watchdog", daemon=True).start()


_start_parent_process_watchdog()
_start_parent_death_pipe_watchdog()

_real_stdout = sys.stdout

GLOBALS = {"__name__": "__main__", "__builtins__": __builtins__}


def _clip(text):
    """Bound inline character capture while preserving a separate full stdout spill."""
    return (text, False) if len(text) <= _CAPTURE_LIMIT else (text[:_CAPTURE_LIMIT], True)


def run_cell(request, execution_count):
    """Exec one cell; returns (response payload, FULL stdout text)."""
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
    """Best-effort: write the FULL clipped stdout to disk, return its path or ""."""
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
    """Frame each result independently of direct writes to file descriptor one."""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    _real_stdout.buffer.write(("\n" + _SENTINEL + " " + str(len(body)) + "\n").encode("utf-8"))
    _real_stdout.buffer.write(body)
    _real_stdout.buffer.flush()


def main():
    """Read sequential cells until stdin closes or a cell explicitly exits."""
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
