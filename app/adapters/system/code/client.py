"""
工具 RPC 客户端及标准辅助函数，工具存根只按宿主提供的允许集合生成。
"""

import json
import keyword
from typing import Any

_HEADER = r'''
"""
自动生成的工具 RPC 存根，通过宿主进程调用当前会话允许使用的工具。
"""
import json, os, socket, shlex, threading, time

_sock = None
_UNSET = object()
# RPC 服务串行处理连接且协议没有请求标识。多个线程共用套接字时，
# 必须锁住完整的发送和接收过程，防止读取到其他调用的响应。
_call_lock = threading.Lock()

# 脚本公共辅助函数，统一处理输入解析、命令转义与重试。

def json_parse(text: str):
    """
    解析允许原始控制字符和 UTF-8 BOM 的 JSON 文本。

    terminal、web_extract 或文件输出可能包含未转义的制表符、换行符或 BOM。
    保留 #57870 中由 @woxinwuhen713-bit 提供的兼容处理。

    :param text: 待解析文本，开头的单个 BOM 会被移除
    :return: JSON 解码后的值
    :raises json.JSONDecodeError: 文本无法解码为 JSON
    """
    if isinstance(text, str) and text.startswith("﻿"):
        text = text[1:]
    return json.loads(text, strict=False)


def shell_quote(s: str) -> str:
    """
    将动态文本转义为一个 POSIX Shell 参数。

    :param s: 要插入 terminal 命令的原始参数，不是整条命令
    :return: 可作为单个 Shell 参数使用的转义文本
    """
    return shlex.quote(s)


def retry(fn, max_attempts=3, delay=2):
    """
    在调用抛出异常时按指数退避重试。

    :param fn: 无参数可调用对象，每次重试都会重新调用
    :param max_attempts: 包含首次调用在内的最大次数，应为正整数，默认 3
    :param delay: 首次重试前的等待秒数，后续等待翻倍，默认 2
    :return: 首次成功调用的结果
    :raises Exception: 所有尝试均失败时重新抛出最后一次异常
    """
    last_err = None
    for attempt in range(max_attempts):
        try:
            return fn()
        except Exception as e:
            last_err = e
            if attempt < max_attempts - 1:
                time.sleep(delay * (2 ** attempt))
    raise last_err


def _connect():
    """
    复用或建立与宿主 RPC 服务的连接。

    MOVIEPILOT_RPC_SOCKET 在 POSIX 上通常是 Unix 套接字路径；
    Windows 使用 tcp://127.0.0.1:<port> 形式的回环地址。

    :return: 缓存的套接字，新连接设置 300 秒读写超时
    :raises OSError: 套接字创建或连接失败
    """
    global _sock
    if _sock is None:
        endpoint = os.environ["MOVIEPILOT_RPC_SOCKET"]
        if endpoint.startswith("tcp://"):
            # 宿主服务仅绑定回环地址，TCP 地址格式为 tcp://host:port。
            _host_port = endpoint[len("tcp://"):]
            _host, _, _port = _host_port.rpartition(":")
            _sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            _sock.connect((_host or "127.0.0.1", int(_port)))
        else:
            _sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            _sock.connect(endpoint)
        _sock.settimeout(300)
    return _sock

def _call(tool_name, args):
    """
    串行发送工具调用并解析宿主返回值。

    :param tool_name: 宿主已授权的工具名
    :param args: 工具参数映射，按 JSON 序列化发送
    :return: JSON 解码结果；结果本身为 JSON 字符串时再解码一次
    :raises OSError: 连接失败且重试次数耗尽
    :raises RuntimeError: 宿主断开连接且重试次数耗尽
    """
    request = json.dumps({
        "tool": tool_name,
        "args": args,
        "token": os.environ.get("MOVIEPILOT_RPC_TOKEN", ""),
    }) + "\n"
    # 持久会话可能超过 RPC 服务的 300 秒空闲窗口。服务允许重新接受连接时，
    # 下个代码单元遇到连接失效会创建新套接字并重试一次。
    _attempts = 2 if os.environ.get("MOVIEPILOT_RPC_PERSISTENT") == "1" else 1
    with _call_lock:
        for _attempt in range(_attempts):
            try:
                conn = _connect()
                conn.sendall(request.encode())
                buf = b""
                while True:
                    chunk = conn.recv(65536)
                    if not chunk:
                        raise RuntimeError("Agent process disconnected")
                    buf += chunk
                    if buf.endswith(b"\n"):
                        break
                break
            except (OSError, RuntimeError):
                global _sock
                try:
                    if _sock is not None:
                        _sock.close()
                except OSError:
                    pass
                _sock = None
                if _attempt + 1 >= _attempts:
                    raise
    raw = buf.decode().strip()
    result = json.loads(raw)
    if isinstance(result, str):
        try:
            return json.loads(result)
        except (json.JSONDecodeError, TypeError):
            return result
    return result

'''


def build_client(names: tuple[str, ...], schemas: dict[str, dict[str, Any]] | None = None) -> str:
    """
    工具名来自宿主目录，禁止模型把任意源码注入存根定义。
    """
    definitions = []
    for name in names:
        if not name.isidentifier() or name.startswith('_') or keyword.iskeyword(name):
            raise ValueError('无效的工具标识')
        schema = (schemas or {}).get(name)
        if schema is None:
            description = _docstring('调用当前会话的工具并解析返回值。\n\n'
                                     ':param kwargs: 传给工具的命名参数\n'
                                     ':return: 宿主返回的 JSON 解码结果')
            definitions.append(f'def {name}(**kwargs):\n{description}\n    return _call({name!r}, kwargs)\n')
        else:
            definitions.append(_stub(name, schema))
    return _HEADER + '\n'.join(definitions)


def _stub(name: str, schema: dict[str, Any]) -> str:
    """
    按实际 schema 生成可用位置参数的签名；未提供的可选字段不伪造为 null。
    """
    properties = schema.get('properties', {})
    required = schema.get('required', [])
    names = sorted(properties, key=lambda field: field not in required)
    if any(not field.isidentifier() or field.startswith('_') or keyword.iskeyword(field) for field in names):
        raise ValueError('无效的工具参数标识')
    parameters = [field if field in required else f'{field}=_UNSET' for field in names]
    arguments = ', '.join(f'{field!r}: {field}' for field in names)
    description = _docstring('调用当前会话的工具并解析返回值。\n\n输入 Schema：'
                             + json.dumps(schema, ensure_ascii=False))
    return (f'def {name}({", ".join(parameters)}):\n{description}\n'
            f'    args = {{{arguments}}}\n'
            f'    return _call({name!r}, {{key: value for key, value in args.items() if value is not _UNSET}})\n')


def _docstring(description: str) -> str:
    """
    将工具说明编码为缩进后的多行文档字符串，防止引号或反斜杠改变生成代码。

    :param description: 可包含换行、引号和 Schema 原文的说明
    :return: 三双引号各自独占一行的 Python 文档字符串源码
    """
    lines = [json.dumps(line, ensure_ascii=False)[1:-1] for line in description.splitlines()]
    return '    """\n' + '\n'.join('    ' + line if line else '' for line in lines) + '\n    """'
