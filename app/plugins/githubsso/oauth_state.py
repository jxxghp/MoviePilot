"""GitHub 单点登录一次往返的 state、浏览器绑定与回跳地址。

state 证明「这次回调对应本机上一次发起的授权」：它由服务端生成、随授权跳转带出、
回调时取回并即刻销毁。取不回来即判定这次回调不属于任何一次本机发起的授权，整条链路
就此中止。

state 单独还不够：它随地址走，谁拿到那条回调地址谁就有它。因此每次签发再配一份只经
Cookie 发给发起授权那个浏览器的凭据，回调时两样都对得上才算数——否则攻击者可以拿自己
的授权结果诱使他人点开，让对方在毫不知情的情况下登进攻击者的账号。

授权码的一次性也由它承担：GitHub 侧的授权码本就只能兑换一次，但那是对端的承诺，
本机无从验证。state 一经取回即从存储里消失，因此同一份回调地址重放时先在 state
这一关被挡下，不必依赖对端替本机保证不重放。

回跳地址是登录成功后浏览器落回的位置，取值来自发起授权的请求。它只接受站内相对
路径：整站绝对地址与协议相对地址（``//evil.example``）都会让登录成功的那一刻把
浏览器送到站外，而票据就挂在那个地址上。
"""

from __future__ import annotations

import re
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

# 一次授权往返的有效期。跨度要容得下用户在 GitHub 侧输密码、过二次验证并确认授权，
# 又不能长到让一枚泄露的 state 长期可用
STATE_TTL_SECONDS = 300

# 存储中同时保留的 state 上限，超出时按签发先后淘汰最旧的，防止未完成的授权无限堆积
STATE_MAX_ITEMS = 512

# 登录成功后浏览器回跳的默认位置
DEFAULT_RETURN_PATH = "/"

# 可接受的回跳地址形状：以单个斜杠开头的站内相对路径，且不含反斜杠、井号与控制字符。
# 排除 ``//`` 是因为协议相对地址会跳出本站；排除反斜杠是因为部分浏览器把 ``/\`` 与
# ``//`` 等同处理，只挡前者会留下同一个出站口；排除井号是因为一次性登录票据就挂在
# 回跳地址的片段上，路径自带片段会与它撞在一起
_RETURN_PATH_PATTERN = re.compile(r"^/(?!/)[^\\#\x00-\x1f\x7f]*$")


@dataclass(frozen=True, slots=True)
class OAuthState:
    """一次授权往返在服务端留存的上下文。

    :param identity: 发起本次授权的登录入口标识，即身份绑定表 provider 列的取值
    :param return_path: 登录成功后浏览器回跳的站内相对路径
    :param binding: 与发起授权的浏览器绑定的凭据，同一取值随 Cookie 交给浏览器
    :param created_at: 签发时刻的时间戳
    """

    identity: str
    return_path: str
    binding: str
    created_at: float

    def matches_binding(self, candidate: Optional[str]) -> bool:
        """判定回调带回的浏览器凭据是否就是发起授权时发下去的那一份。

        比较走 `secrets.compare_digest`，逐字符早退的比较会把「猜对了几位」这件事
        泄露在耗时里。

        :param candidate: 回调请求 Cookie 里的浏览器凭据
        :return: 两者一致时为 True
        """
        if not isinstance(candidate, str) or not candidate:
            return False
        return secrets.compare_digest(self.binding, candidate)


def safe_return_path(value: Optional[str]) -> str:
    """把外来的回跳地址收敛成可安全跳转的站内相对路径。

    不合形状时回落到默认位置而不是报错：回跳地址是体验项，为它中断一次本来能成功的
    登录并不划算；而放行一个站外地址会把一次性登录票据随浏览器送到站外，那是账号接管。

    :param value: 请求带来的回跳地址
    :return: 站内相对路径；取值不合形状时为 ``DEFAULT_RETURN_PATH``
    """
    if not isinstance(value, str):
        return DEFAULT_RETURN_PATH
    candidate = value.strip()
    if not candidate or not _RETURN_PATH_PATTERN.match(candidate):
        return DEFAULT_RETURN_PATH
    return candidate


class OAuthStateStore:
    """按插件实例持有的 state 存储，签发一次、取回一次。

    存储只在本进程内存里：state 的寿命以分钟计，且一次往返的发起与回调都落在同一个
    进程上，落库既无必要也会把一份短命凭据写进持久介质。
    """

    def __init__(self) -> None:
        """建立空存储与其互斥锁。"""
        self._states: Dict[str, OAuthState] = {}
        self._lock = threading.RLock()

    def issue(self, identity: str, return_path: str) -> Tuple[str, str]:
        """为一次授权往返签发 state 与配套的浏览器凭据。

        两样一起发：state 随授权跳转经 GitHub 转一圈回来，谁拿到那条回调地址谁就有它；
        浏览器凭据只经 Cookie 发给发起授权的那个浏览器，不出现在任何地址里。回调时两样
        都对得上才算数，一枚被转发给别人的回调地址因此不成立——否则攻击者可以拿自己的
        授权结果诱使他人点开，让对方登进攻击者的账号。

        :param identity: 发起本次授权的登录入口标识
        :param return_path: 已收敛过的站内回跳路径
        :return: ``(state, 浏览器凭据)``
        """
        state = secrets.token_urlsafe(32)
        binding = secrets.token_urlsafe(32)
        now = time.time()
        with self._lock:
            self._evict(now)
            self._states[state] = OAuthState(
                identity=identity,
                return_path=return_path,
                binding=binding,
                created_at=now,
            )
        return state, binding

    def consume(self, state: Optional[str]) -> Optional[OAuthState]:
        """取回并销毁一枚 state。

        取回即销毁，因此同一枚 state 只能兑现一次往返；已过期的取回后同样返回空，
        不给过期票据留任何一次可用的机会。

        :param state: 回调带回的 state
        :return: 签发时留存的上下文；取值缺失、不存在或已过期时为 None
        """
        if not isinstance(state, str) or not state:
            return None
        now = time.time()
        with self._lock:
            record = self._states.pop(state, None)
            self._evict(now)
        if record is None or now - record.created_at > STATE_TTL_SECONDS:
            return None
        return record

    def clear(self) -> None:
        """清空存储，供插件停止时释放未完成的授权上下文。"""
        with self._lock:
            self._states.clear()

    def _evict(self, now: float) -> None:
        """淘汰过期的 state，并在总量超限时按签发先后补淘汰最旧的几枚。

        :param now: 当前时间戳
        """
        for state in [
            state
            for state, record in self._states.items()
            if now - record.created_at > STATE_TTL_SECONDS
        ]:
            self._states.pop(state, None)
        overflow = len(self._states) - STATE_MAX_ITEMS
        if overflow <= 0:
            return
        oldest = sorted(self._states.items(), key=lambda item: item[1].created_at)
        for state, _ in oldest[:overflow]:
            self._states.pop(state, None)
