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

import asyncio
import hashlib
import json
import re
import time
from typing import ClassVar, Optional, Type
from urllib.parse import quote

from pydantic import BaseModel, Field

from app.agent.tools.base import MoviePilotTool, ToolChain
from app.schemas import Notification
from app.agent.tools.impl.feedback_issue_state import (
    build_feedback_draft_hash,
    feedback_issue_state_store,
)
from app.core.config import settings
from app.db.user_oper import UserOper
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

# Per-user rate limit：
# - 任意两次提交之间至少 30 分钟冷却（哪怕 title/body 不同），杜绝快速刷屏
# - 24 小时滚动窗口内每用户最多 10 个 Issue，杜绝长期大量灌水
# 两者叠加：``require_admin`` 限制了谁能提，rate limit 限制了能提多少。
USER_COOLDOWN_SECONDS = 30 * 60
USER_DAILY_QUOTA = 10
USER_DAILY_WINDOW_SECONDS = 24 * 60 * 60
# 防止 _user_submissions 字典在 username 拼写漂移（"admin" / "Admin" /
# "admin "）或恶意输入下无限增长。超过此上限时按 LRU 淘汰最久未活跃的桶。
MAX_USER_SUBMISSIONS_BUCKETS = 200

# 内容质量门槛：阻止「测试 issue」「abc」等明显无意义提交。AI 在 SKILL.md
# 中已经被要求"先筛"，这里是 defense-in-depth 工具层硬门槛。
MIN_TITLE_BODY_CHARS = 8     # ``[错误报告]: `` 前缀外，标题至少 8 字
MIN_DESCRIPTION_CHARS = 50   # description 整体至少 50 字
TITLE_PREFIX = "[错误报告]:"

# 黑词单：title 或 description 命中即拒。匹配为字面包含（大小写不敏感）。
# 不用正则避免误伤合法 bug 描述。条目专注于"明显的占位 / 测试 / 乱码"。
# 注：仅做字面字符串匹配；专业对抗者可以用全角 / 同形 unicode 绕过——
# 当前威胁模型是「失控 LLM / 无意 spam」而非「对抗攻击」，可接受。
_QUALITY_BLOCKLIST = (
    "测试issue", "测试 issue", "test issue",
    "test123", "testtest", "测试测试",
    "测试一下", "测试提交", "测试请求", "测试反馈",
    "看能否跑通", "能否跑通", "跑通流程", "链路测试",
    "模拟问题", "模拟问题描述", "模拟描述", "模拟 bug", "模拟bug",
    "编造", "虚假 bug", "虚假bug",
    "asdf", "asdfasdf", "qwer", "qwerty", "qweqwe",
    "占位", "占个坑", "随便", "随便写",
    "abcabc", "xxxxxx", "xxx xxx",
    "hello world", "你好世界",
    "lorem ipsum", "dolor sit amet",
)

# logs 字段只能承载真实日志；这类短语说明 Agent 把叙述性占位内容塞进了日志。
_FABRICATED_LOG_PHRASES = (
    "无相关日志", "没有相关日志", "未捕获到相关日志",
    "这是模拟", "模拟问题", "模拟描述", "用户反馈",
)

# 结构化描述信号：工具层不做复杂语义理解，但至少要求 Agent 提交的正文
# 已经区分现象、复现和期望，避免把"用户反馈某模块异常，请协助排查"这类
# 无法复现的泛泛描述伪装成正式 Issue。
_DESCRIPTION_REQUIRED_SIGNALS = (
    ("现象", ("现象", "报错", "错误", "无法", "失败", "异常")),
    ("复现步骤", ("复现", "步骤", "触发", "操作", "调用", "点击")),
    ("期望行为", ("期望", "应该", "预期", "正常")),
)

# 检测乱码 / 重复字符行：连续 8 个或以上**相同**字符视为乱码。
# **排除**常见 Markdown / 日志分隔符：空白、`=`、`-`、`_`、`*`、`#`、
# `~`、`` ` ``、`.`、`/`、`\`、`+`、`|`。这些字符大量重复在合法日志（如
# `========`、`---- separator ----`）或 Markdown 横线（`---`）里常见，
# 不应该被判为乱码。
_REPEAT_GIBBERISH = re.compile(r"([^\s=\-_*#~`./\\+|])\1{7,}", re.UNICODE)

# 日志脱敏：服务端唯一的脱敏入口（``_sanitize_logs``）。Agent 不再做客户端
# 脱敏，日志也不进入 LLM 上下文，所以这里是日志写入公网 Issue 之前的最后
# 一道防线，必须尽量覆盖 MoviePilot 本身和常见社区插件可能打印的高危凭据
# 与 PII 模式。规则按"先匹配更具体的形式、再匹配通用 key=value"的顺序排列，
# 避免通用规则吞掉特定上下文。
#
# 当前威胁模型仍是「失控 LLM / 无意 spam / 日志意外漏出」，不是「对抗攻击」；
# 全角变体 / 同形 unicode 绕过不在防护范围内。
_REDACTED = "<REDACTED>"
_REDACTED_PATH = "/<USER>/"
_REDACTED_EMAIL = "<EMAIL>"
_REDACTED_IP = "<IP>"

_SENSITIVE_PATTERNS: tuple[tuple[re.Pattern, str], ...] = (
    # ---- HTTP 头部凭据 ----------------------------------------------------
    (re.compile(r"(?i)(Cookie\s*:\s*)[^\r\n]+"), rf"\1{_REDACTED}"),
    (re.compile(r"(?i)(Set-Cookie\s*:\s*)[^\r\n]+"), rf"\1{_REDACTED}"),
    (
        re.compile(r"(?i)(Authorization\s*:\s*)(Bearer|Basic|Token)\s+\S+"),
        rf"\1\2 {_REDACTED}",
    ),
    (re.compile(r"(?i)(X-(?:Api-Key|Auth-Token|Access-Token)\s*:\s*)\S+"), rf"\1{_REDACTED}"),
    # ---- GitHub / 通用 token 字面前缀（即使没有 key= 上下文也覆盖）---------
    (re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"), _REDACTED),
    (re.compile(r"\bgho_[A-Za-z0-9]{20,}\b"), _REDACTED),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"), _REDACTED),
    (re.compile(r"\b(sk|xoxb|xoxp|xoxa)-[A-Za-z0-9-]{12,}\b"), _REDACTED),
    # ---- MoviePilot 会话 ID（``user_<userid>_<timestamp>``）：嵌入了 userid
    # 即便上下文里没出现 ``session_id=`` 前缀也得脱敏，否则 agent 模块虽被
    # meta-noise 过滤掉，其它非 noise 模块也可能在 traceback 里 echo 出这个
    # 字面值（见 #5808 教训）。
    (re.compile(r"\buser_\d{4,}_\d+\b"), _REDACTED),
    # ---- 站点 PT passkey / RSS / IM webhook --------------------------------
    (re.compile(r"(?i)\b(passkey|rsskey|authkey|access_key)=[A-Za-z0-9]{8,}"), rf"\1={_REDACTED}"),
    (
        re.compile(
            r"https?://(qyapi\.weixin\.qq\.com|oapi\.dingtalk\.com|open\.feishu\.cn|"
            r"hooks\.slack\.com|discord(?:app)?\.com/api/webhooks)/\S+"
        ),
        rf"\1/{_REDACTED}",
    ),
    # ---- 通用 key=value / key: value 凭据 + 用户身份 PII（保留原始分隔符）---
    # 用户标识字段在 #5808 实战里被发现混进 logs（Telegram numeric userid /
    # GitHub-style username）。即便 meta-noise 过滤会丢掉大多数 agent
    # framework 日志，仍可能有非 noise 模块（如 plugin / hook）打印这些
    # 字段，所以此处把"用户身份"也纳入脱敏。
    (
        re.compile(
            r"(?i)\b("
            r"api[_-]?key|apikey|access[_-]?token|refresh[_-]?token|id[_-]?token|"
            r"client[_-]?secret|client[_-]?id|app[_-]?secret|app[_-]?key|"
            r"corp[_-]?secret|corp[_-]?id|agent[_-]?id|"
            r"password|secret|token|auth|credential|"
            r"chat[_-]?id|webhook|api[_-]?token|bot[_-]?token|"
            r"user[_-]?id|userid|username|user[_-]?name|"
            r"session[_-]?id|sessionid|"
            r"open[_-]?id|openid|union[_-]?id|unionid"
            r")(\s*[:=]\s*)['\"]?[^\s'\"&\r\n]{2,}"
        ),
        rf"\1\2{_REDACTED}",
    ),
    # ---- PII：邮箱 ----------------------------------------------------------
    (
        re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"),
        _REDACTED_EMAIL,
    ),
    # ---- PII：公网 IPv4（保留 127/8、10/8、172.16/12、192.168/16 私网）------
    (
        re.compile(
            r"\b(?!(?:127|10)\.)"
            r"(?!172\.(?:1[6-9]|2\d|3[01])\.)"
            r"(?!192\.168\.)"
            r"(?:\d{1,3}\.){3}\d{1,3}\b"
        ),
        _REDACTED_IP,
    ),
    # ---- 文件路径里的用户名段 ---------------------------------------------
    (re.compile(r"/Users/[^/\s]+/"), _REDACTED_PATH),
    (re.compile(r"/home/[^/\s]+/"), _REDACTED_PATH),
    (re.compile(r"C:\\Users\\[^\\\s]+\\", re.IGNORECASE), r"C:\\Users\\<USER>\\"),
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
            "期望行为 / 已定位或推测 / 已尝试的处理 等结构化小节。Must be "
            "based on a real user-observed symptom; do not fabricate or "
            "rewrite placeholder/test requests into real-looking bugs."
        ),
    )
    original_user_request: str = Field(
        ...,
        description=(
            "Verbatim original user request that triggered issue filing. "
            "Must not be summarized or rewritten. The tool uses this field "
            "to reject test/pipeline-validation intent such as 测试 ISSUE or 看能否跑通."
        ),
    )
    diagnostics_id: str = Field(
        ...,
        description=(
            "diagnostics_id returned by collect_feedback_diagnostics. Required; logs are "
            "fetched from the server-side state store using this id. Do NOT pass log text "
            "as a separate argument — it has been removed from the schema on purpose to "
            "stop the LLM from re-transmitting multi-KB log payloads between tool calls."
        ),
    )
    confirmation_token: str = Field(
        ...,
        description=(
            "confirmation_token returned by prepare_feedback_issue after the user clicks the "
            "confirmation button. Do not invent this value."
        ),
    )


class SubmitFeedbackIssueTool(MoviePilotTool):
    """向上游 ``jxxghp/MoviePilot`` 仓库提交问题反馈 Issue。

    require_admin=True：避免任意 TG/飞书用户通过 Bot 触发后给上游刷 Issue。
    Skill 层会在 dry-run 阶段做用户确认，本工具再做枚举校验与凭据降级。

    **状态持久化与并发说明**：
    - ``_recent_submissions`` 与 ``_user_submissions`` 都是 ``ClassVar``
      进程级缓存，**MoviePilot 重启后清零**。一个失控管理员只要重启容器
      就可绕过冷却 / 配额。如果将来需要更强保护，可改为持久化到
      ``SystemConfigOper`` 或 DB 表里。当前威胁模型是「失误 / 失控 LLM」
      而非「专业对抗」，可接受。
    - 这两份缓存的读写依赖 Agent 在同一事件循环里串行执行单个工具
      调用——asyncio 单线程协程模型下安全。**严禁**在多线程 /
      multiprocessing 场景下直接复用本工具实例；如有此需求，需加
      ``asyncio.Lock`` 守护写入。
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

    # Per-user rate-limit 状态：{username: [timestamp, ...]}。
    # 列表按时间顺序追加，每次检查时同步过滤掉 24h 之前的条目。仅在 admin
    # 范围内有效（require_admin 已限定调用者必须是 superuser），所以条目
    # 数量上限可控（即便所有用户都在刷，单条记录也只多到 quota+1 就被拒）。
    _user_submissions: ClassVar[dict[str, list]] = {}

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
    def _normalize_username(username: str) -> str:
        """归一化 username 作为 rate-limit 桶 key。

        防止 ``"admin"`` / ``"Admin"`` / ``" admin "`` 这种拼写漂移把同一个
        管理员散到多个桶里、绕过冷却。统一小写 + 去前后空白。空串原样返回，
        由调用方判定。"""
        return (username or "").strip().lower()

    @classmethod
    def _evict_user_submissions_if_needed(cls) -> None:
        """``_user_submissions`` 字典 key 数量上限保护。

        按桶内"最近一次提交时间戳"做 LRU，超过 ``MAX_USER_SUBMISSIONS_BUCKETS``
        时淘汰最久未活跃的桶，避免恶意 / 漂移输入把字典撑爆。"""
        if len(cls._user_submissions) <= MAX_USER_SUBMISSIONS_BUCKETS:
            return
        # 按桶内最新时间戳升序排序，前 N 个最旧的淘汰
        excess = len(cls._user_submissions) - MAX_USER_SUBMISSIONS_BUCKETS
        oldest_keys = sorted(
            cls._user_submissions.items(),
            key=lambda kv: kv[1][-1] if kv[1] else 0,
        )[:excess]
        for key, _ in oldest_keys:
            cls._user_submissions.pop(key, None)

    @classmethod
    def _check_user_rate_limit(cls, username: str) -> Optional[str]:
        """检查 per-user rate limit：30 分钟冷却 + 24h 滚动配额 10 条。

        命中冷却时间窗或日配额时返回拒绝消息（含本地化时长描述），未命中则
        返回 None。本方法不修改状态，仅读；记录由 ``_record_user_submission``
        在真正发起 API 调用前完成。"""
        key = cls._normalize_username(username)
        if not key:
            # 没有用户名识别走不下去，但 _enforce_superuser 早已拦截过；
            # 双重保险下若到此处仍无用户名直接拒绝
            return "无法识别调用用户身份，rate limit 拒绝以防误用。"
        now = time.time()
        timestamps = cls._user_submissions.get(key, [])
        # 同步清理过期条目（> 24h），保持列表短小
        active = [ts for ts in timestamps if now - ts < USER_DAILY_WINDOW_SECONDS]
        if active != timestamps:
            if active:
                cls._user_submissions[key] = active
            else:
                # 全部过期，直接把桶清掉，避免 _user_submissions 长期堆积
                # 长尾用户的空 list
                cls._user_submissions.pop(key, None)
        # 30 分钟冷却
        if active:
            since_last = now - active[-1]
            if since_last < USER_COOLDOWN_SECONDS:
                remaining = int(USER_COOLDOWN_SECONDS - since_last)
                minutes, seconds = divmod(remaining, 60)
                return (
                    f"为避免给上游刷屏，同一管理员两次提交之间至少间隔 "
                    f"{USER_COOLDOWN_SECONDS // 60} 分钟。请等 "
                    f"{minutes} 分 {seconds} 秒后再试。"
                )
        # 24h 配额
        if len(active) >= USER_DAILY_QUOTA:
            oldest = active[0]
            recover_in = int(USER_DAILY_WINDOW_SECONDS - (now - oldest))
            hours, remainder = divmod(recover_in, 3600)
            minutes = remainder // 60
            return (
                f"你今日已提交 {USER_DAILY_QUOTA} 个 Issue，已达 24 小时配额上限。"
                f"最早一条将在 {hours} 小时 {minutes} 分钟后过期，请到时再提。"
            )
        return None

    @classmethod
    def _record_user_submission(cls, username: str) -> None:
        """把本次提交时间戳记入 per-user 状态，供下次 rate limit 检查使用。"""
        key = cls._normalize_username(username)
        if not key:
            return
        cls._user_submissions.setdefault(key, []).append(time.time())
        cls._evict_user_submissions_if_needed()

    @classmethod
    def _check_content_quality(
        cls,
        title: str,
        description: str,
        original_user_request: str,
    ) -> Optional[str]:
        """内容质量门槛：长度 + 黑词单 + 乱码三重过滤。

        命中任一规则即拒绝，附带具体原因。该检查在 _enforce_superuser /
        rate_limit 之后、`_build_issue_body` 之前调用，避免无意义 issue 浪费
        上游 maintainer 的 triage 时间。

        注：``logs`` 字段已从 Agent 入参里移除，日志改为通过 ``diagnostics_id``
        在 state store 里流转，Agent 无法伪造其内容，因此这里不再对 logs
        做黑词单 / 伪造检查；脱敏仍由 ``_sanitize_logs`` 在服务端兜底。"""
        original_stripped = (original_user_request or "").strip()
        if not original_stripped:
            return (
                "缺少原始用户请求，无法判断本次提交是否来自真实故障。"
                "请传入触发反馈的用户原话，不能只传改写后的 Issue 草稿。"
            )
        # 1) title 长度（剔除 ``[错误报告]: `` 前缀后）
        title_body = title.strip()
        if title_body.startswith(TITLE_PREFIX):
            title_body = title_body[len(TITLE_PREFIX):].strip()
        if len(title_body) < MIN_TITLE_BODY_CHARS:
            return (
                f"标题正文太短（剔除 {TITLE_PREFIX!r} 前缀后只有 {len(title_body)} 字，"
                f"至少 {MIN_TITLE_BODY_CHARS} 字）。请用一句完整的话概括症状，"
                "例如「订阅刷新时 TMDB 识别返回 500」。"
            )
        # 2) description 长度
        desc_stripped = description.strip()
        if len(desc_stripped) < MIN_DESCRIPTION_CHARS:
            return (
                f"问题描述太短（{len(desc_stripped)} 字，至少 {MIN_DESCRIPTION_CHARS} 字）。"
                "请补充：现象 / 复现步骤 / 期望行为，让 maintainer 能理解问题。"
            )
        # 3) 结构信号。SKILL.md 要求 Agent 在正文里分清现象、复现、期望；
        # 工具层用关键词做保守兜底，拦住"为了跑通流程编的泛泛一句话"。
        missing_signals = [
            label
            for label, choices in _DESCRIPTION_REQUIRED_SIGNALS
            if not any(choice in desc_stripped for choice in choices)
        ]
        if missing_signals:
            return (
                "问题描述缺少可复现 bug 所需的结构信息："
                f"{' / '.join(missing_signals)}。请补充真实现象、触发步骤和期望行为，"
                "不要用模拟或泛泛描述跑通提交流程。"
            )
        # 4) 黑词单。同时检查原始用户请求 + 标题 + 描述，防止 Agent 把
        # "测试 ISSUE / 看能否跑通" 改写成真实样式 title/description 后绕过。
        haystack = "\n".join(
            part for part in (title, description, original_stripped) if part
        ).lower()
        for phrase in _QUALITY_BLOCKLIST:
            if phrase.lower() in haystack:
                return (
                    f"原始请求、标题或描述命中明显占位/测试关键词「{phrase}」，"
                    "已拒绝提交。"
                    "如果是真实问题，请用正常的中文描述具体现象。"
                )
        # 5) 乱码：连续 8 个相同字符
        match = (
            _REPEAT_GIBBERISH.search(title)
            or _REPEAT_GIBBERISH.search(description)
            or _REPEAT_GIBBERISH.search(original_stripped)
        )
        if match:
            return (
                f"标题或描述里出现疑似乱码片段「{match.group(0)[:12]}…」，"
                "请用正常文字描述问题。"
            )
        return None

    async def _enforce_superuser(self) -> Optional[str]:
        """强校验当前调用者必须是系统 superuser。

        Why: 框架的 ``MoviePilotTool._check_permission`` 仅在 9 个内置渠道
        映射 + 渠道配置齐全时才真正生效；Web 渠道、未识别渠道、缺配置等情
        况下会静默放行（见 ``app/agent/tools/base.py`` 的多条 ``return None``
        分支）。``submit_feedback_issue`` 触发的是不可逆的上游写操作，**这
        里必须独立做一道硬校验**，不能依赖框架那套渠道映射，否则任意能登
        录 MoviePilot 的用户都能向上游刷 issue。

        返回 None 表示放行；返回字符串则为拒绝原因（直接作为 LLM 可见的
        message）。"""
        username = self._username or ""
        if not username:
            return (
                "submit_feedback_issue 拒绝：当前会话没有绑定 MoviePilot 用户身份，"
                "无法确认调用者是否为系统管理员。"
            )
        # 两次尝试：DB 偶发抖动场景下短暂退避 100ms 后再试一次，避免单次失败
        # 直接卡死管理员。仍保持 fail-close：第二次还失败就拒绝。
        user = None
        last_err: Optional[Exception] = None
        for attempt in range(2):
            try:
                user = await UserOper().async_get_by_name(username)
                last_err = None
                break
            except Exception as e:  # noqa: BLE001 — DB 查询异常不能放行
                last_err = e
                logger.warning(
                    f"submit_feedback_issue 校验 superuser 时数据库异常 "
                    f"(attempt {attempt + 1}/2): {e}"
                )
                if attempt == 0:
                    await asyncio.sleep(0.1)
        if last_err is not None:
            logger.error(
                f"submit_feedback_issue 校验 superuser 重试后仍失败: {last_err}"
            )
            return (
                "submit_feedback_issue 拒绝：校验用户身份时发生数据库异常，"
                "出于安全考虑本次提交被中止。请稍后重试或联系管理员。"
            )
        if not user:
            return (
                f"submit_feedback_issue 拒绝：未在 MoviePilot 中找到用户 "
                f"{username!r}，无法确认是否为系统管理员。"
            )
        if not user.is_superuser:
            return (
                "submit_feedback_issue 拒绝：只有系统管理员（superuser）才能"
                "向上游 MoviePilot 仓库提交问题反馈，避免任意用户通过对话"
                "代理给上游刷 Issue。请联系管理员代为提交，或自行登录管理员"
                "账号后再试。"
            )
        return None

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

        Issue #5806 暴露的副作用：``send_tool_message`` 默认不抑制 TG 网页
        预览，导致一条 GitHub URL 通知会自动渲染出 "GitHub" 预览卡片；之后
        Agent 又用文本复述了一次 URL，TG 再渲染一次 → 一次提交在 TG 里展开
        成 3 条卡片。这里直接走 ``ToolChain().async_post_message`` 并显式
        ``disable_web_page_preview=True`` 关闭预览卡片，配合 SKILL.md 里
        "Acknowledge briefly, do NOT repeat the URL" 让最终用户只看到一条
        干净的链接消息。
        """
        if not self._channel or not self._source:
            # 没有可回传消息的会话上下文（典型：后台 capture），直接当推送失败处理
            logger.debug(
                "feedback issue 链接推送跳过：当前无可用消息渠道 / 来源"
            )
            return False

        text = f"{hint}\n\n{url}" if hint else url
        try:
            await ToolChain().async_post_message(
                Notification(
                    channel=self._channel,
                    source=self._source,
                    userid=self._user_id,
                    username=self._username,
                    title=title,
                    text=text,
                    disable_web_page_preview=True,
                )
            )
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
        original_user_request: str,
        diagnostics_id: str = "",
        confirmation_token: str = "",
        **kwargs,
    ) -> str:
        """执行反馈 Issue 提交流程。

        所有入参都应来自已确认的真实问题草稿；工具层会再次校验质量、结构、
        管理员身份和提交频率，避免 Agent 绕过 skill 预筛后把测试内容提交到
        上游。"""
        logger.info(
            f"执行工具: {self.name}, 标题: {title!r}, 版本: {version!r}, "
            f"环境: {environment!r}, 类型: {issue_type!r}"
        )

        # 0) 硬校验调用者必须是系统 superuser。框架的 _check_permission 在
        #    Web / 未识别渠道下会静默放行；本工具触发不可逆的上游写动作，
        #    必须独立确认调用者身份，不能依赖渠道映射。
        deny = await self._enforce_superuser()
        if deny:
            logger.warning(
                f"submit_feedback_issue 拒绝非管理员调用：username={self._username!r}"
            )
            return self._result_payload(
                success=False,
                reason="forbidden",
                message=deny,
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

        # 3) 内容质量门槛：长度 + 黑词单 + 乱码。命中表示「明显的无意义提交」，
        #    直接拒绝**不给** prefill_url——纵容也是放任，这类内容不应该被
        #    打开手动提交的旁路。
        quality_err = self._check_content_quality(
            title=title,
            description=description,
            original_user_request=original_user_request,
        )
        if quality_err:
            logger.info(
                f"拒绝低质量提交：username={self._username!r} reason={quality_err[:40]}…"
            )
            # 质量门槛已经明确拒绝后，同一轮对话不应再通过 ask_user_choice
            # 引导用户把测试 / 占位内容改写成“真实问题”。这里写入共享
            # tool context，给后续消息型工具一个硬拦截信号，避免模型不遵守
            # SKILL.md 时继续发按钮。
            self._agent_context["feedback_issue_rejected_quality"] = True
            self._agent_context["feedback_issue_rejected_quality_reason"] = quality_err
            return self._result_payload(
                success=False,
                reason="rejected_quality",
                message=quality_err,
            )

        # 4) 反馈提交前必须先由专用工具收集诊断日志。即便日志里没有命中
        #    相关片段，也要携带 collect_feedback_diagnostics 返回的
        #    diagnostics_id，证明 Agent 没有跳过日志排查。
        diagnostics = feedback_issue_state_store.get_diagnostics(
            diagnostics_id,
            session_id=self._session_id,
            user_id=self._user_id,
        )
        if not diagnostics:
            return self._result_payload(
                success=False,
                reason="diagnostics_required",
                message=(
                    "提交前必须先调用 collect_feedback_diagnostics 收集本地日志。"
                    "如果没有找到相关日志，也需要携带该工具返回的 diagnostics_id。"
                ),
            )
        # 日志固定从服务端 state store 拉取，模型不允许通过参数注入日志，
        # 避免动辄数 KB 的日志在 LLM 上下文中重复流转造成响应缓慢。
        logs = diagnostics.logs

        # 5) 反馈提交前必须先发送预览并等待用户真实点击确认。确认 token 由
        #    prepare_feedback_issue 创建、按钮 callback 标记 confirmed；模型
        #    自行声称“用户已确认”不会通过这里。
        draft_hash = build_feedback_draft_hash(
            title=title,
            version=version,
            environment=environment,
            issue_type=issue_type,
            description=description,
            original_user_request=original_user_request,
            logs=logs,
            diagnostics_id=diagnostics_id,
        )
        confirmation = feedback_issue_state_store.consume_confirmed(
            confirmation_token,
            session_id=self._session_id,
            user_id=self._user_id,
            draft_hash=draft_hash,
        )
        if not confirmation:
            return self._result_payload(
                success=False,
                reason="confirmation_required",
                message=(
                    "提交前必须先调用 prepare_feedback_issue 发送预览，并等待用户"
                    "点击确认按钮；当前 confirmation_token 无效、未确认或草稿"
                    "内容已被修改。"
                ),
            )

        # 6) Per-user rate limit：30 分钟冷却 + 24h 配额 10 条。命中后**仍**
        #    给 prefill_url，避免误伤"短时间内确实有第二个真 bug 要报"的
        #    场景——让管理员可以走浏览器手动提，但 Agent 不会代理刷上游。
        rate_err = self._check_user_rate_limit(self._username or "")
        if rate_err:
            prefill_url = self._build_prefill_url(
                title=title,
                version=version,
                environment=environment,
                issue_type=issue_type,
                description=description,
                logs=logs,
            )
            pushed = await self._push_url_to_user(
                url=prefill_url,
                title="问题反馈 - 已达提交频率上限",
                hint=rate_err + "\n\n如果确实是另一个真实问题，可点击下方链接到 GitHub 手动提交。",
            )
            logger.warning(
                f"submit_feedback_issue 触发 rate limit：username={self._username!r}"
            )
            return self._result_payload(
                success=False,
                reason="rate_limited_user",
                url_delivered=pushed,
                prefill_url=None if pushed else prefill_url,
                message=(
                    rate_err + " （已通过独立消息把手动提交的预填链接发给用户。）"
                    if pushed
                    else
                    rate_err + " （独立消息推送失败，请把 prefill_url 原样转给用户。）"
                ),
            )

        # 7) 同会话内 60 秒去重，防止 agent 多次触发提交同一问题
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

        # 通过所有前置校验，记录一次「该管理员发起了一次提交」到 rate-limit
        # 状态。**包括** no_token 兜底场景——避免管理员通过反复触发兜底来无
        # 限次刷预填 URL 给自己。
        self._record_user_submission(self._username or "")

        # 8) 始终先生成兜底 URL，无论后面走哪条路径都能用上
        prefill_url = self._build_prefill_url(
            title=title,
            version=version,
            environment=environment,
            issue_type=issue_type,
            description=description,
            logs=logs,
        )

        # 9) 没有 token 时直接降级到 URL 兜底
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

        # 10) 调 GitHub REST API。POST /issues 必须带 Bearer Token；
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

        # 在真正发起 API 调用前先 record 一次内容哈希，确保后续任何结果
        # （成功 / 失败 / 网络异常）都会被纳入 60 秒去重窗口，避免 agent
        # 因 LLM loop 或网络重试在短时间内反复触发提交。per-user rate-limit
        # 状态已经在前置校验通过后记录，这里不再重复。
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
                    "Issue 已成功提交，并通过独立通知卡片把链接发给用户。"
                    "**本轮对话只允许输出一句中文简短确认**，例如「Issue 已"
                    "提交，等待 maintainer 跟进。」——禁止重复 issue 编号 / "
                    "仓库名 / URL，禁止说「提交链接已通过通知通道发送」"
                    "之类的实现细节。通知卡片已经把全部信息展示给用户。"
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
