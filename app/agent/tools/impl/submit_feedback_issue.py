"""向 jxxghp/MoviePilot 上游仓库提交问题反馈 Issue 的工具。

设计要点：
- 不接受任意仓库参数，目标仓库恒定为 ``jxxghp/MoviePilot`` 后端上游，避免被
  滥用为通用 GitHub 写入通道。
- 调用前根据 ``settings.GITHUB_TOKEN`` 是否存在以及权限是否足够，分三种结局：
  1) 成功：通过 GitHub REST API ``POST /repos/jxxghp/MoviePilot/issues``
     创建 Issue，返回 ``html_url``。
  2) 无 token：返回 ``no_token`` 结局以及一个 GitHub Issue Forms 预填 URL，
     由 Agent 在 TG / 飞书机器人等渠道里给用户一个可点击链接兜底，并提示
     管理员配置 ``GITHUB_TOKEN``。
  3) Token 无写权限或被拒：返回 ``no_permission`` 结局 + 预填 URL，并提示
     重新配置一个带 ``public_repo``（或 ``repo``）scope 的 Token。
- 仅 admin 用户可触发，防止任意 TG 群成员通过 Bot 给上游刷 Issue。
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from typing import ClassVar, Optional, Type
from urllib.parse import quote

from pydantic import BaseModel, Field

from app.agent.tools.base import MoviePilotTool
from app.core.config import settings
from app.log import logger
from app.utils.http import AsyncRequestUtils


# 目标仓库恒定，不接受外部覆盖；如未来要支持前端/插件仓库反馈，新增独立 tool
# 而非把这个常量做成可配置项，避免被 prompt 注入指向任意仓库。
FEEDBACK_REPO_OWNER = "jxxghp"
FEEDBACK_REPO_NAME = "MoviePilot"
FEEDBACK_REPO = f"{FEEDBACK_REPO_OWNER}/{FEEDBACK_REPO_NAME}"
FEEDBACK_ISSUE_API = f"https://api.github.com/repos/{FEEDBACK_REPO}/issues"
FEEDBACK_ISSUE_NEW_URL = f"https://github.com/{FEEDBACK_REPO}/issues/new"
FEEDBACK_ISSUE_TEMPLATE = "bug_report.yml"
FEEDBACK_REQUEST_TIMEOUT = 15

# 允许的运行环境与问题类型枚举值，与 ``.github/ISSUE_TEMPLATE/bug_report.yml``
# 表单 ``options`` 字段严格一致；前置校验避免上游解析失败或被自动关闭。
ALLOWED_ENVIRONMENTS = ("Docker", "Windows")
ALLOWED_ISSUE_TYPES = ("主程序运行问题", "插件问题", "其他问题")

# 长度上限：参考 GitHub Issue 实际限制并留余量。
# - title 256 字符（GitHub 截断到 256，超长会被静默裁剪）
# - body 60 KB（GitHub 上限 ~65535，留 5KB 余量）
# - logs 8 KB（SKILL.md 给 agent 的软上限是 3KB；这里以 8KB 兜底，
#   再加上 redaction 仍可能膨胀，留充足余量但不放任日志吞掉整段正文）
MAX_TITLE_CHARS = 256
MAX_BODY_CHARS = 60 * 1024
MAX_LOGS_CHARS = 8 * 1024
# 预填 URL 走 GET，浏览器 / Chat 平台对 URL 长度通常限制在 4-8KB；
# logs 在 URL 路径下需要更严格的上限，给其它必填字段留余量。
MAX_URL_LOGS_CHARS = 3 * 1024

# 防止 agent 重复触发提交：60 秒内同 title+body 哈希命中视为重复。
DEDUP_TTL_SECONDS = 60

# 日志二次脱敏正则：作为 defense-in-depth，避免 agent 漏脱敏时把凭据直接
# 写进公网 issue。SKILL.md 要求 agent 主动脱敏，这里只兜最常见的高危模式。
_SENSITIVE_PATTERNS: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"(?i)(Cookie\s*:\s*)[^\r\n]+"), r"\1<REDACTED>"),
    (re.compile(r"(?i)(Set-Cookie\s*:\s*)[^\r\n]+"), r"\1<REDACTED>"),
    (
        re.compile(r"(?i)(Authorization\s*:\s*)(Bearer|Basic|Token)\s+\S+"),
        r"\1\2 <REDACTED>",
    ),
    (
        # 捕获原始分隔符（``:`` 或 ``=``）并在替换中保留，避免把 ``key: val``
        # 强制改成 ``key=<REDACTED>`` 破坏日志阅读体验
        re.compile(
            r"(?i)\b(api[_-]?key|apikey|access[_-]?token|refresh[_-]?token|"
            r"passkey|password|secret|token)(\s*[:=]\s*)['\"]?[^\s'\"&\r\n]+"
        ),
        r"\1\2<REDACTED>",
    ),
)


class SubmitFeedbackIssueInput(BaseModel):
    """向 jxxghp/MoviePilot 提交问题反馈 Issue 的输入参数模型。

    所有字段均与上游 ``bug_report.yml`` 表单字段对齐；正文与日志由调用方
    （通常是 Agent 通过 feedback-issue skill 整理）预先组织好，本工具只
    负责把这些字段稳定地拼成 GitHub Issue body / labels 并发起请求。
    """

    explanation: str = Field(
        ...,
        description="Clear explanation of why this tool is being used in the current context",
    )
    title: str = Field(
        ...,
        description=(
            "Issue title. Must follow upstream format `[错误报告]: <短描述>`. "
            "Do NOT keep the template placeholder text `请在此处简单描述你的问题`."
        ),
    )
    version: str = Field(
        ...,
        description=(
            "Current MoviePilot version, e.g. v2.12.2. If user does not know, "
            "fall back to the running backend version returned by system APIs."
        ),
    )
    environment: str = Field(
        ...,
        description=(
            "Runtime environment. Must be exactly one of: Docker / Windows."
        ),
    )
    issue_type: str = Field(
        ...,
        description=(
            "Issue category. Must be exactly one of: 主程序运行问题 / 插件问题 / 其他问题."
        ),
    )
    description: str = Field(
        ...,
        description=(
            "Markdown-formatted bug description, including 现象 / 复现步骤 / "
            "期望行为 / 已定位或推测 / 已尝试的处理 等结构化小节。"
        ),
    )
    logs: Optional[str] = Field(
        default=None,
        description=(
            "Raw backend logs related to the bug. Leave empty if not captured; "
            "do NOT fabricate."
        ),
    )


class SubmitFeedbackIssueTool(MoviePilotTool):
    """向上游 ``jxxghp/MoviePilot`` 仓库提交问题反馈 Issue。

    require_admin=True：避免任意 TG/飞书用户通过 Bot 触发后给上游刷 Issue。
    Skill 层会在 dry-run 阶段做用户确认，本工具再做枚举校验与凭据降级。
    """

    name: str = "submit_feedback_issue"
    description: str = (
        "Submit a bug-report issue to the upstream MoviePilot backend repository "
        f"({FEEDBACK_REPO}). Tries the GitHub REST API first when GITHUB_TOKEN is "
        "configured with write permission; otherwise the tool itself pushes a "
        "prefilled GitHub Issue Forms URL to the user via a separate notification "
        "message (so the URL bytes are not corrupted by LLM verbatim copy). "
        "Target repo is fixed; this tool does NOT accept arbitrary owner/repo "
        "arguments. Admin only."
    )
    args_schema: Type[BaseModel] = SubmitFeedbackIssueInput
    require_admin: bool = True
    # 工具会通过 send_tool_message 把 issue_url / prefill_url 作为独立通知推给用户，
    # 因此声明 sends_message=True，让 factory 在受限渠道场景里仍可识别该副作用。
    sends_message: bool = True

    # 进程级去重缓存：{hash: timestamp}。Agent 在 SKILL.md 的指引下不应重复
    # 提交同一问题，但低能力模型仍可能误触；在工具层做 60 秒 hash 去重作为
    # 兜底，避免上游 issue 列表被重复条目污染。
    _recent_submissions: ClassVar[dict[str, float]] = {}

    def get_tool_message(self, **kwargs) -> Optional[str]:
        """侧边消息：让用户知道 Agent 正在帮他向上游提交反馈。"""
        title = kwargs.get("title") or ""
        return f"提交问题反馈到 {FEEDBACK_REPO}：{title}".strip()

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------
    @staticmethod
    def _validate_enum(value: str, allowed: tuple, field_name: str) -> Optional[str]:
        """校验枚举字段，返回错误信息（None 表示通过）。

        枚举不合法时直接拒绝，避免发出后上游 bot/maintainer 还要手工处理。
        """
        if value not in allowed:
            return (
                f"{field_name} 必须是以下之一：{', '.join(allowed)}；"
                f"当前传入：{value!r}"
            )
        return None

    @staticmethod
    def _redact_logs(raw: str) -> str:
        """对 logs 字段做 defense-in-depth 二次脱敏。

        SKILL.md 已经要求 agent 主动脱敏，这里只兜常见的高危模式（Cookie /
        Authorization / api_key / password / token 等），避免 agent 漏脱敏
        时凭据直接进入公网 issue。"""
        out = raw
        for pattern, replacement in _SENSITIVE_PATTERNS:
            out = pattern.sub(replacement, out)
        return out

    @staticmethod
    def _truncate(text: str, limit: int, marker: str = "\n…（已截断）") -> str:
        """长度截断辅助：超出 limit 时保留前 N 字符 + 截断说明。"""
        if not text or len(text) <= limit:
            return text
        # 留出 marker 长度，避免最终输出再超 limit
        return text[: max(0, limit - len(marker))] + marker

    @classmethod
    def _sanitize_logs(cls, logs: Optional[str], limit: int) -> str:
        """两条管道（API body / prefill URL）共用的日志清洗：先脱敏再截断。

        在两处都调用同一个入口，避免任何一条路径漏掉脱敏或长度兜底——这是
        来自 review 的 high-priority 反馈：预填 URL 之前直接吃了原始 logs，
        会通过浏览器历史、消息渠道日志泄漏凭据。"""
        if not logs or not logs.strip():
            return ""
        return cls._truncate(cls._redact_logs(logs.strip()), limit)

    @classmethod
    def _build_issue_body(
        cls,
        version: str,
        environment: str,
        issue_type: str,
        description: str,
        logs: Optional[str],
    ) -> str:
        """构造与 bug_report.yml 渲染结果保持一致的 Markdown 正文。

        - 4 项 "确认" checkbox 默认勾选；通过 API 创建时模板表单不再展示，
          但保留勾选信息可让 maintainer 看到提交者已被告知规则。
        - 日志字段为空时显式标注，避免上游误以为是漏填。
        - 对 logs 做二次脱敏与长度截断，对整段 body 做最终长度兜底。
        """
        log_block = cls._sanitize_logs(logs, MAX_LOGS_CHARS) or "会话中未捕获到相关后端日志。"
        body = (
            "### 确认\n\n"
            "- [x] 我的版本是最新版本，我的版本号与 "
            "[version](https://github.com/jxxghp/MoviePilot/releases/latest) 相同。\n"
            "- [x] 我已经 [issue](https://github.com/jxxghp/MoviePilot/issues) "
            "中搜索过，确认我的问题没有被提出过。\n"
            "- [x] 我已经 [Telegram频道](https://t.me/moviepilot_channel) "
            "中搜索过，确认我的问题没有被提出过。\n"
            "- [x] 我已经修改标题，将标题中的 描述 替换为我遇到的问题。\n\n"
            f"### 当前程序版本\n\n{version}\n\n"
            f"### 运行环境\n\n{environment}\n\n"
            f"### 问题类型\n\n{issue_type}\n\n"
            f"### 问题描述\n\n{description.strip()}\n\n"
            "### 发生问题时系统日志和配置文件\n\n"
            f"```bash\n{log_block}\n```\n"
            "\n---\n"
            "_本 Issue 由 MoviePilot Agent 协助用户提交。_"
        )
        return cls._truncate(body, MAX_BODY_CHARS)

    @classmethod
    def _build_prefill_url(
        cls,
        title: str,
        version: str,
        environment: str,
        issue_type: str,
        description: str,
        logs: Optional[str],
    ) -> str:
        """生成 GitHub Issue Forms 预填链接，作为 API 通道失败时的兜底。

        字段名与 bug_report.yml 的 ``id`` 一一对应；统一使用 ``quote`` 做严格
        URL-encode（空格 → %20、换行 → %0A），避免 ``+`` 被解释成空格。

        Logs 字段在 URL 路径下走更严格的清洗：先做与 body 同源的脱敏，再截断到
        ``MAX_URL_LOGS_CHARS``（3KB）以防 URL 超长（浏览器 / Chat 平台对 GET
        URL 通常限制在 4-8KB）。这是来自 review 的 high-priority 反馈。
        """
        params = {
            "template": FEEDBACK_ISSUE_TEMPLATE,
            "title": title,
            "version": version,
            "environment": environment,
            "type": issue_type,
            "what-happened": description,
            "logs": cls._sanitize_logs(logs, MAX_URL_LOGS_CHARS),
        }
        encoded = "&".join(
            f"{quote(k, safe='')}={quote(v, safe='')}" for k, v in params.items()
        )
        return f"{FEEDBACK_ISSUE_NEW_URL}?{encoded}"

    @staticmethod
    def _classify_failure(
        status_code: Optional[int],
        headers: Optional[dict] = None,
    ) -> str:
        """把 GitHub API 错误码映射到对 Agent 友好的失败原因。

        403 同时被 GitHub 用于「无权限」和「被限流」两种语义；当
        ``X-RateLimit-Remaining`` 为 0 时优先判定为 ``rate_limited``，
        避免提示用户重新配 token 实际只是限流。"""
        headers = headers or {}
        if status_code == 401:
            return "no_permission"
        if status_code == 403:
            remaining = headers.get("X-RateLimit-Remaining") or headers.get(
                "x-ratelimit-remaining"
            )
            if remaining == "0":
                return "rate_limited"
            return "no_permission"
        if status_code == 404:
            # 404 一般是 token 完全无效或仓库被锁；对终端用户没必要细分
            return "no_permission"
        if status_code == 422:
            return "invalid_payload"
        if status_code is not None and status_code >= 500:
            return "github_unavailable"
        return "api_error"

    @classmethod
    def _check_recent_duplicate(cls, title: str, body: str) -> Optional[str]:
        """检查 60 秒内是否提交过同 title+body 的 issue。

        返回命中的 hash 字符串（仅作日志用途）；None 表示未命中。命中后
        run() 直接拒绝二次提交，避免上游 issue 列表被重复条目污染。"""
        now = time.time()
        # 同步清理过期条目，避免缓存无限增长
        expired = [
            h for h, ts in cls._recent_submissions.items()
            if now - ts > DEDUP_TTL_SECONDS
        ]
        for h in expired:
            cls._recent_submissions.pop(h, None)
        key = hashlib.sha256(
            f"{title}\x00{body}".encode("utf-8", errors="replace")
        ).hexdigest()
        if key in cls._recent_submissions:
            return key
        return None

    @classmethod
    def _record_submission(cls, title: str, body: str) -> None:
        """记录一次提交的指纹，配合 ``_check_recent_duplicate`` 实现去重。"""
        key = hashlib.sha256(
            f"{title}\x00{body}".encode("utf-8", errors="replace")
        ).hexdigest()
        cls._recent_submissions[key] = time.time()

    @staticmethod
    def _safe_response_dict(response) -> dict:
        """安全解析 HTTP 响应体为 dict。

        GitHub 个别接口（如 422 批量校验）可能返回 array 而非 dict，对结果
        直接 ``.get`` 会触发 AttributeError；这里统一返回 dict，调用方拿到的
        是空 dict 也能继续走分支判断。"""
        try:
            data = response.json()
        except Exception:  # noqa: BLE001 — 响应体非合法 JSON，回退到空 dict
            return {}
        if isinstance(data, dict):
            return data
        return {}

    @staticmethod
    def _result_payload(**fields) -> str:
        """统一以 JSON 字符串返回，便于 Agent 通过 SKILL.md 中描述的字段分支。

        注意：``issue_url`` / ``prefill_url`` 等长 URL 默认**不会**写入这个返回值，
        而是通过 ``send_tool_message`` 单独推送到用户频道，避免 LLM 逐字转述时
        因量化或 tokenizer 抖动引入字节级别的 URL 损坏（曾观察到 ``%89`` 被翻转
        成 ``%79`` 导致 GitHub 400）。Agent 只需把工具返回的 ``message`` 字段
        作为对话内的简短确认转述给用户即可。
        """
        return json.dumps(fields, ensure_ascii=False, indent=2)

    async def _push_url_to_user(self, url: str, title: str, hint: str) -> bool:
        """把 issue_url / prefill_url 作为独立通知推给当前会话用户。

        Why: TG/飞书等渠道下 LLM 转述 1KB+ 长 URL 极易出现字节翻转（低精度量化
        模型尤其常见），导致 GitHub 拒绝预填链接。直接走 ToolChain 推送可以
        让 URL 经由消息系统原文落地，跳过 LLM 转述链路。
        """
        try:
            text = f"{hint}\n\n{url}" if hint else url
            await self.send_tool_message(text, title=title)
            return True
        except Exception as e:  # noqa: BLE001 — 推送失败不应该让整个工具崩溃
            logger.warning(
                f"通过 send_tool_message 推送反馈链接失败，回退到把 URL 写入 "
                f"工具返回值: {e}"
            )
            return False

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------
    async def run(
        self,
        title: str,
        version: str,
        environment: str,
        issue_type: str,
        description: str,
        logs: Optional[str] = None,
        **kwargs,
    ) -> str:
        logger.info(
            f"执行工具: {self.name}, 标题: {title!r}, 版本: {version!r}, "
            f"环境: {environment!r}, 类型: {issue_type!r}"
        )

        # 1) 入参枚举校验：失败直接拒绝，不消耗 GitHub 调用次数
        for value, allowed, field_name in (
            (environment, ALLOWED_ENVIRONMENTS, "environment"),
            (issue_type, ALLOWED_ISSUE_TYPES, "issue_type"),
        ):
            err = self._validate_enum(value, allowed, field_name)
            if err:
                return self._result_payload(
                    success=False,
                    reason="invalid_input",
                    message=err,
                )

        # 2) 兜底硬约束：title 长度截断，避免超出 GitHub 256 字符限制
        title = self._truncate(title, MAX_TITLE_CHARS, marker="…")

        # 3) 同会话内 60 秒去重，防止 agent 多次触发提交同一问题
        body_preview = self._build_issue_body(
            version=version,
            environment=environment,
            issue_type=issue_type,
            description=description,
            logs=logs,
        )
        if self._check_recent_duplicate(title, body_preview):
            logger.info(
                f"拒绝重复提交：{title!r} 在 {DEDUP_TTL_SECONDS}s 内已提交过"
            )
            return self._result_payload(
                success=False,
                reason="duplicate",
                message=(
                    f"该问题反馈在 {DEDUP_TTL_SECONDS} 秒内已经提交过一次，"
                    "已避免重复提交。如确需重提，请稍后再次触发，或在原"
                    "Issue 页面追加评论。"
                ),
            )

        # 4) 始终先生成兜底 URL，无论后面走哪条路径都能用上
        prefill_url = self._build_prefill_url(
            title=title,
            version=version,
            environment=environment,
            issue_type=issue_type,
            description=description,
            logs=logs,
        )

        # 5) 没有 token 时直接降级到 URL 兜底
        if not settings.GITHUB_TOKEN:
            logger.warning(
                "未配置 GITHUB_TOKEN，feedback issue 降级到预填 URL 通道"
            )
            pushed = await self._push_url_to_user(
                url=prefill_url,
                title="问题反馈 - 请点击下方链接确认提交",
                hint=(
                    "MoviePilot 未配置 GitHub 写入凭据，无法自动提交。"
                    "请在浏览器 / GitHub App 中打开下方链接，勾选 4 项 ✅ 后提交即可。"
                ),
            )
            return self._result_payload(
                success=False,
                reason="no_token",
                url_delivered=pushed,
                # 仅当 send_tool_message 失败时才把 URL 退回给 LLM 兜底
                prefill_url=None if pushed else prefill_url,
                message=(
                    "MoviePilot 未配置可写入的 GitHub Token，无法自动提交 Issue；"
                    "已通过独立消息把预填链接发给用户，请在对话中简短告知"
                    "用户点击该链接完成提交，并提醒管理员后续可在系统设置中"
                    "配置一个具备 `public_repo` 权限的 GitHub Token，让以后"
                    "可以由 Agent 直接提交。"
                    if pushed
                    else
                    "MoviePilot 未配置可写入的 GitHub Token，无法自动提交 Issue。"
                    "独立消息推送失败，请把 prefill_url 原样转给用户。"
                ),
            )

        # 6) 调 GitHub REST API。POST /issues 必须带 Bearer Token；
        #    GITHUB_HEADERS 已经填好 Authorization & UA，再补 Content-Type
        #    与 Accept 以满足 GitHub 推荐头规范。复用 body_preview，避免
        #    重新构造一次（_build_issue_body 已经做了脱敏与长度兜底）。
        body = body_preview
        request_headers = {
            **settings.GITHUB_HEADERS,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
        }
        payload = {
            "title": title,
            "body": body,
            "labels": ["bug"],
        }

        # 在真正发起 API 调用前先 record，确保后续任何结果（成功 / 失败 /
        # 网络异常）都会被纳入 60 秒去重窗口，避免 agent 因 LLM loop 在短
        # 时间内反复触发提交。
        self._record_submission(title, body)

        try:
            response = await AsyncRequestUtils(
                proxies=settings.PROXY,
                headers=request_headers,
                timeout=FEEDBACK_REQUEST_TIMEOUT,
            ).post_res(FEEDBACK_ISSUE_API, json=payload)
        except Exception as e:  # noqa: BLE001 — AsyncRequestUtils 已统一拦截，这里兜底未知异常
            logger.error(f"提交反馈 Issue 时发生异常: {e}", exc_info=True)
            pushed = await self._push_url_to_user(
                url=prefill_url,
                title="问题反馈 - 网络异常，请点击链接手动提交",
                hint=(
                    "调用 GitHub API 时出现网络异常，暂时无法自动提交。"
                    "请点击下方链接在浏览器中完成提交，或稍后让 Agent 重试。"
                ),
            )
            return self._result_payload(
                success=False,
                reason="network_error",
                url_delivered=pushed,
                prefill_url=None if pushed else prefill_url,
                message=(
                    "调用 GitHub API 时网络异常；已通过独立消息把预填链接发给"
                    "用户，请在对话中告知用户稍后重试或点击链接手动提交。"
                    if pushed
                    else
                    "调用 GitHub API 时网络异常，且独立消息推送失败；"
                    "请把 prefill_url 原样转给用户。"
                ),
                error=str(e),
            )

        if response is None:
            # AsyncRequestUtils 在 RequestError 时返回 None；此时无 status_code 可读
            pushed = await self._push_url_to_user(
                url=prefill_url,
                title="问题反馈 - 网络无响应，请点击链接手动提交",
                hint=(
                    "调用 GitHub API 未收到响应。请点击下方链接在浏览器中"
                    "完成提交，或稍后让 Agent 重试。"
                ),
            )
            return self._result_payload(
                success=False,
                reason="network_error",
                url_delivered=pushed,
                prefill_url=None if pushed else prefill_url,
                message=(
                    "调用 GitHub API 未返回响应；已通过独立消息把预填链接发给"
                    "用户，请在对话中告知用户稍后重试或点击链接手动提交。"
                    if pushed
                    else
                    "调用 GitHub API 未返回响应，且独立消息推送失败；"
                    "请把 prefill_url 原样转给用户。"
                ),
            )

        if response.status_code == 201:
            data = self._safe_response_dict(response)
            html_url = data.get("html_url")
            number = data.get("number")
            logger.info(f"反馈 Issue 创建成功：#{number} {html_url}")
            pushed = False
            if html_url:
                pushed = await self._push_url_to_user(
                    url=html_url,
                    title=f"问题反馈已提交 - {FEEDBACK_REPO} #{number}",
                    hint=(
                        "你的问题已提交到 MoviePilot 上游仓库，"
                        "后续 maintainer 的回复会显示在下方 Issue 页面里。"
                    ),
                )
            return self._result_payload(
                success=True,
                issue_number=number,
                repo=FEEDBACK_REPO,
                url_delivered=pushed,
                # send 失败才把 URL 退给 LLM 转述兜底
                issue_url=None if pushed else html_url,
                message=(
                    f"Issue 已成功提交到 {FEEDBACK_REPO}#{number}，并通过独立"
                    "消息把链接推给用户，请在对话中简短告知用户提交成功并"
                    "请其等待 maintainer 回复。"
                    if pushed
                    else
                    f"Issue 已成功提交到 {FEEDBACK_REPO}#{number}。"
                    "独立消息推送失败，请把 issue_url 原样转给用户。"
                ),
            )

        reason = self._classify_failure(
            response.status_code, headers=dict(response.headers or {})
        )
        # 取 GitHub 返回的错误描述，便于排查；不暴露完整响应体避免泄漏 token 元信息
        api_data = self._safe_response_dict(response)
        api_message = api_data.get("message") if api_data else None
        if not api_message and getattr(response, "text", None):
            api_message = response.text[:200]

        logger.warning(
            f"提交反馈 Issue 失败：HTTP {response.status_code} reason={reason} "
            f"msg={api_message!r}"
        )
        if reason == "no_permission":
            hint = (
                "MoviePilot 配置的 GitHub Token 缺少写入 Issue 的权限"
                "（需要 `public_repo` 或 `repo` scope），暂时无法自动提交。"
                "请点击下方链接在浏览器或 GitHub App 中完成提交。"
            )
            llm_summary = (
                "GitHub Token 缺少写入 Issue 的权限；已通过独立消息把预填"
                "链接发给用户，请在对话中简短告知用户点击链接完成提交，"
                "并提醒管理员重新生成带 `public_repo` / `repo` scope 的"
                "Token 后续就可以由 Agent 直接提交。"
            )
        elif reason == "rate_limited":
            hint = (
                "GitHub API 已达到当前 Token 的请求限流上限，暂时无法自动"
                "提交。请稍后重试，或点击下方链接在浏览器中手动提交。"
            )
            llm_summary = (
                "GitHub API 限流（403 + X-RateLimit-Remaining=0）；已通过"
                "独立消息把预填链接发给用户，请在对话中告知用户稍后再让"
                "Agent 重试，或直接点击链接手动提交。"
            )
        elif reason == "invalid_payload":
            hint = (
                "GitHub 拒绝了本次 Issue 内容（可能包含被限制的字符或字段"
                "格式不正确）。请点击下方链接在浏览器中确认并提交。"
            )
            llm_summary = (
                "GitHub 返回 HTTP 422 拒绝了 Issue 内容；已通过独立消息把"
                "预填链接发给用户，请在对话中简短告知用户点击链接确认提交。"
            )
        elif reason == "github_unavailable":
            hint = (
                "GitHub 服务暂时不可用。请稍后重试，或点击下方链接在浏览器"
                "中手动提交。"
            )
            llm_summary = (
                "GitHub 服务暂时不可用；已通过独立消息把预填链接发给用户，"
                "请在对话中告知用户稍后重试或点击链接手动提交。"
            )
        else:
            hint = (
                "GitHub API 返回非预期错误，暂时无法自动提交。请点击下方"
                "链接在浏览器中手动提交。"
            )
            llm_summary = (
                "GitHub API 返回非预期错误；已通过独立消息把预填链接发给"
                "用户，请在对话中告知用户点击链接手动提交。"
            )

        pushed = await self._push_url_to_user(
            url=prefill_url,
            title="问题反馈 - 请点击下方链接确认提交",
            hint=hint,
        )
        return self._result_payload(
            success=False,
            reason=reason,
            url_delivered=pushed,
            prefill_url=None if pushed else prefill_url,
            message=(
                llm_summary
                if pushed
                else
                "独立消息推送失败，请把 prefill_url 原样转给用户。"
            ),
            github_message=api_message,
        )
