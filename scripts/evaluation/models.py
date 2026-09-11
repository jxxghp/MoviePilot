"""读取明确配置的模型连接，并为真实评测统计所有模型调用。"""

import json
import os
import threading
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from langchain_core.callbacks.base import BaseCallbackHandler

_PROBE_BASE_URL = "https://evaluation-probe.invalid/v1"
_PROBE_API_KEY = "evaluation-probe-only"
_CODEX_OAUTH_BASE_URL = "https://chatgpt.com/backend-api/codex"


@dataclass(frozen=True)
class ModelSettings:
    """评测连接只在控制进程使用，凭据不进入 repr 或公开报告。"""

    model: str
    base_url: str = field(repr=False)
    api_key: str = field(repr=False)
    reasoning_effort: str = "high"
    max_model_calls: int = 12
    max_output_tokens: int = 8192
    timeout_seconds: int = 180
    context_window: int = 128000
    wire_api: str = "responses"
    auth_mode: str = "explicit"
    account_id: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        """配置文件和 worker 输入共享校验，不能绕过限额或夹带 URL 凭据。"""
        if not isinstance(self.base_url, str):
            raise ValueError("模型 endpoint 无效")
        endpoint = urlsplit(self.base_url)
        if (endpoint.scheme not in {"http", "https"} or not endpoint.hostname or endpoint.username is not None
                or endpoint.password is not None or endpoint.query or endpoint.fragment):
            raise ValueError("模型 endpoint 无效")
        if not isinstance(self.model, str) or not self.model.strip():
            raise ValueError("需要明确模型名称")
        if not isinstance(self.api_key, str) or not self.api_key.strip() or any(char in self.api_key for char in "\r\n"):
            raise ValueError("所选 provider 没有可用的显式评测凭据")
        if self.reasoning_effort not in {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}:
            raise ValueError("推理预算名称无效")
        if self.wire_api not in {"responses", "chat_completions"}:
            raise ValueError("模型 provider 协议无效")
        if self.auth_mode not in {"explicit", "codex_oauth"}:
            raise ValueError("模型鉴权模式无效")
        if self.account_id is not None and (
                not isinstance(self.account_id, str)
                or not self.account_id.strip()
                or any(char in self.account_id for char in "\r\n")
        ):
            raise ValueError("模型账户标识无效")
        if self.auth_mode == "codex_oauth" and self.wire_api != "responses":
            raise ValueError("Codex OAuth 只支持 Responses provider")
        for value, minimum, maximum in (
            (self.max_model_calls, 1, 64), (self.max_output_tokens, 256, 32768),
            (self.timeout_seconds, 30, 900), (self.context_window, 4096, 2000000),
        ):
            if type(value) is not int or not minimum <= value <= maximum:
                raise ValueError("评测预算超出允许范围")

    def public_metadata(self) -> dict[str, Any]:
        """公开模型与评测预算，省略凭据、完整连接路径和用户配置文件。"""
        return {
            "requested_model": self.model, "provider_host": urlsplit(self.base_url).hostname,
            "wire_api": self.wire_api,
            "auth_mode": self.auth_mode,
            "reasoning_effort": self.reasoning_effort, "max_model_calls": self.max_model_calls,
            "max_output_tokens": self.max_output_tokens, "timeout_seconds": self.timeout_seconds,
            "harness_context_window": self.context_window,
        }


def load_codex_model_settings(
    path: Path, *, model: str | None = None, reasoning_effort: str | None = None,
    max_model_calls: int = 12, max_output_tokens: int = 8192, timeout_seconds: int = 180,
    probe_only: bool = False, use_codex_auth: bool = False,
) -> ModelSettings:
    """读取选中 provider；真实运行只接受显式凭据或用户明确选择的 Codex OAuth。"""
    config = tomllib.loads(path.read_text(encoding="utf-8"))
    provider = config.get("model_providers", {}).get(config.get("model_provider"), {})
    wire_api = provider.get("wire_api")
    if wire_api not in {"responses", "chat_completions"}:
        raise ValueError("当前真实评测入口要求明确配置的 Responses 或 Chat Completions provider")
    endpoint = provider.get("base_url")
    selected_model = model or config.get("model")
    if not isinstance(selected_model, str):
        raise ValueError("需要明确模型名称")
    key = provider.get("experimental_bearer_token")
    if not key and isinstance(provider.get("env_key"), str):
        key = os.environ.get(provider["env_key"])
    effort = reasoning_effort or config.get("model_reasoning_effort", "high")
    auth_mode = "explicit"
    account_id = None
    if use_codex_auth:
        if probe_only:
            raise ValueError("原生探针不能使用 Codex OAuth")
        if endpoint or key or provider.get("env_key") or not provider.get("requires_openai_auth"):
            raise ValueError("Codex OAuth 只适用于未自定义 endpoint 的官方 OpenAI provider")
        auth_path = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "auth.json"
        try:
            auth = json.loads(auth_path.read_text(encoding="utf-8"))
            tokens = auth.get("tokens") if isinstance(auth, dict) else None
            key = tokens.get("access_token") if isinstance(tokens, dict) else None
            account_id = tokens.get("account_id") if isinstance(tokens, dict) else None
        except (OSError, UnicodeError, ValueError, TypeError):
            raise ValueError("当前 Codex OAuth 凭据不可读") from None
        if not isinstance(key, str) or not key.strip() or not isinstance(account_id, str) or not account_id.strip():
            raise ValueError("当前 Codex OAuth 凭据不完整")
        endpoint = _CODEX_OAUTH_BASE_URL
        auth_mode = "codex_oauth"
    if probe_only:
        endpoint = endpoint or _PROBE_BASE_URL
        key = key or _PROBE_API_KEY
    return ModelSettings(selected_model, endpoint, key, effort, max_model_calls, max_output_tokens,
                         timeout_seconds, wire_api=wire_api, auth_mode=auth_mode, account_id=account_id)


class ModelCallLimitError(RuntimeError):
    """在下一次真实请求之前拒绝超出评测预算的模型调用。"""


# strict mypy 的 follow_imports=skip 不展开第三方回调基类。
class ModelUsageTracker(BaseCallbackHandler):  # type: ignore[misc]
    """合计主模型、筛选、摘要与子代理的请求，不把缺失 usage 当成零消耗。"""

    raise_error = True
    run_inline = True

    def __init__(self, limit: int) -> None:
        """建立单次评测共享的并发安全计数器。"""
        self.limit = limit
        self._lock = threading.Lock()
        self._runs: dict[str, dict[str, Any]] = {}
        self.blocked_calls = 0

    def on_chat_model_start(self, serialized: dict[str, Any], messages: list[Any], **kwargs: Any) -> None:
        """原子执行预算判断，既不记录提示词，也不允许并行调用越过上限。"""
        run_id = str(kwargs["run_id"])
        with self._lock:
            if run_id in self._runs:
                return
            if len(self._runs) >= self.limit:
                self.blocked_calls += 1
                raise ModelCallLimitError("评测已达到模型调用上限")
            self._runs[run_id] = {"started": time.monotonic(), "completed": False, "usage": None, "model": None}

    def on_llm_end(self, response: Any, **kwargs: Any) -> None:
        """只提取用量与模型标识，不把原始响应或凭据带入统计。"""
        generation = next((item for group in getattr(response, "generations", []) for item in group), None)
        message = getattr(generation, "message", None)
        usage = getattr(message, "usage_metadata", None)
        if not isinstance(usage, dict) or not all(
            type(usage.get(key)) is int and usage[key] >= 0
            for key in ("input_tokens", "output_tokens", "total_tokens")
        ):
            usage = None
        else:
            usage = {key: usage[key] for key in ("input_tokens", "output_tokens", "total_tokens")}
        metadata = getattr(message, "response_metadata", None) or {}
        with self._lock:
            record = self._runs.get(str(kwargs["run_id"]))
            if record is not None and not record["completed"]:
                record.update(completed=True, usage=usage, model=metadata.get("model_name") or metadata.get("model"))

    def on_llm_error(self, error: BaseException, **kwargs: Any) -> None:
        """错误请求保留为用量未知，不保存可能包含私有响应的异常文本。"""
        with self._lock:
            record = self._runs.get(str(kwargs["run_id"]))
            if record is not None:
                record["error_type"] = type(error).__name__
                status = getattr(error, "status_code", None)
                record["http_status"] = status if isinstance(status, int) else None

    def snapshot(self) -> dict[str, Any]:
        """返回可审计的已知消耗及覆盖度；部分失败时数字仅为已知下界。"""
        with self._lock:
            records = list(self._runs.values())
            known = [record["usage"] for record in records if isinstance(record["usage"], dict)]
            return {
                "model_calls": len(records), "completed_model_calls": sum(record["completed"] for record in records),
                "blocked_model_calls": self.blocked_calls, "usage_complete": len(known) == len(records),
                "reported_models": sorted({record["model"] for record in records if isinstance(record["model"], str)}),
                "errors": [{key: record.get(key) for key in ("error_type", "http_status")}
                           for record in records if record.get("error_type")],
                "tokens": ({key: sum(int(usage.get(key, 0)) for usage in known)
                            for key in ("input_tokens", "output_tokens", "total_tokens")} if known else None),
            }
