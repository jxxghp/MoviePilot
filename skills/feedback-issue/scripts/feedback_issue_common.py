"""feedback-issue skill 脚本共享逻辑。"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote, urlparse


def _find_repo_root() -> Path:
    """从当前工作目录和脚本路径向上查找 MoviePilot 仓库根目录。"""
    script_path = Path(__file__).resolve()
    candidates = [Path.cwd().resolve(), *Path.cwd().resolve().parents]
    candidates.extend([script_path.parent, *script_path.parents])
    for candidate in candidates:
        if (candidate / "app" / "core" / "config.py").is_file():
            return candidate
    return script_path.parents[3]


REPO_ROOT = _find_repo_root()
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.core.config import settings  # noqa: E402


FEEDBACK_REPO_OWNER = "jxxghp"
FEEDBACK_REPO_NAME = "MoviePilot"
FEEDBACK_REPO = f"{FEEDBACK_REPO_OWNER}/{FEEDBACK_REPO_NAME}"
FEEDBACK_ISSUE_TEMPLATE = "bug_report.yml"
FEEDBACK_REQUEST_TIMEOUT = 15

_GITHUB_REPO_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

ALLOWED_ENVIRONMENTS = ("Docker", "Windows")
FEATURE_ISSUE_TYPE = "功能请求"
ALLOWED_ISSUE_TYPES = ("主程序运行问题", "插件问题", FEATURE_ISSUE_TYPE, "其他问题")

MAX_TITLE_CHARS = 256
MAX_BODY_CHARS = 60 * 1024
MAX_LOGS_CHARS = 8 * 1024
MAX_URL_LOGS_CHARS = 3 * 1024
MAX_PREVIEW_LOGS_CHARS = 3 * 1024
MAX_DOCTOR_SUMMARY_CHARS = 2 * 1024

DEDUP_TTL_SECONDS = 60
USER_COOLDOWN_SECONDS = 30 * 60
USER_DAILY_QUOTA = 10
USER_DAILY_WINDOW_SECONDS = 24 * 60 * 60
MAX_USER_SUBMISSIONS_BUCKETS = 200

MIN_TITLE_BODY_CHARS = 8
MIN_DESCRIPTION_CHARS = 50
TITLE_PREFIX = "[错误报告]:"
TITLE_PREFIXES = (TITLE_PREFIX, "[功能请求]:")

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

_FABRICATED_LOG_PHRASES = (
    "无相关日志", "没有相关日志", "未捕获到相关日志",
    "这是模拟", "模拟问题", "模拟描述", "用户反馈",
)

_DESCRIPTION_REQUIRED_SIGNALS = (
    ("现象", ("现象", "报错", "错误", "无法", "失败", "异常")),
    ("复现步骤", ("复现", "步骤", "触发", "操作", "调用", "点击")),
    ("期望行为", ("期望", "应该", "预期", "正常")),
)
_FEATURE_DESCRIPTION_REQUIRED_SIGNALS = (
    ("需求背景", ("需求背景", "背景", "痛点", "原因", "为什么", "场景")),
    ("使用场景", ("使用场景", "场景", "用户", "当我", "希望在", "需要在")),
    ("期望能力", ("期望", "希望", "支持", "能够", "可以", "新增", "功能")),
)

_REPEAT_GIBBERISH = re.compile(r"([^\s=\-_*#~`./\\+|])\1{7,}", re.UNICODE)

_REDACTED = "<REDACTED>"
_REDACTED_PATH = "/<USER>/"
_REDACTED_EMAIL = "<EMAIL>"
_REDACTED_IP = "<IP>"

_SENSITIVE_PATTERNS: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"(?i)(Cookie\s*:\s*)[^\r\n]+"), rf"\1{_REDACTED}"),
    (re.compile(r"(?i)(Set-Cookie\s*:\s*)[^\r\n]+"), rf"\1{_REDACTED}"),
    (
        re.compile(r"(?i)(Authorization\s*:\s*)(Bearer|Basic|Token)\s+\S+"),
        rf"\1\2 {_REDACTED}",
    ),
    (re.compile(r"(?i)(X-(?:Api-Key|Auth-Token|Access-Token)\s*:\s*)\S+"), rf"\1{_REDACTED}"),
    (re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"), _REDACTED),
    (re.compile(r"\bgho_[A-Za-z0-9]{20,}\b"), _REDACTED),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"), _REDACTED),
    (re.compile(r"\b(sk|xoxb|xoxp|xoxa)-[A-Za-z0-9-]{12,}\b"), _REDACTED),
    (re.compile(r"\buser_\d{4,}_\d+\b"), _REDACTED),
    (re.compile(r"(?i)\b(passkey|rsskey|authkey|access_key)=[A-Za-z0-9]{8,}"), rf"\1={_REDACTED}"),
    (
        re.compile(
            r"https?://(qyapi\.weixin\.qq\.com|oapi\.dingtalk\.com|open\.feishu\.cn|"
            r"hooks\.slack\.com|discord(?:app)?\.com/api/webhooks)/\S+"
        ),
        rf"\1/{_REDACTED}",
    ),
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
    (
        re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"),
        _REDACTED_EMAIL,
    ),
    (
        re.compile(
            r"\b(?!(?:127|10)\.)"
            r"(?!172\.(?:1[6-9]|2\d|3[01])\.)"
            r"(?!192\.168\.)"
            r"(?:\d{1,3}\.){3}\d{1,3}\b"
        ),
        _REDACTED_IP,
    ),
    (re.compile(r"/Users/[^/\s]+/"), _REDACTED_PATH),
    (re.compile(r"/home/[^/\s]+/"), _REDACTED_PATH),
    (re.compile(r"C:\\Users\\[^\\\s]+\\", re.IGNORECASE), r"C:\\Users\\<USER>\\"),
)


def feedback_runtime_dir() -> Path:
    """返回 feedback-issue 脚本使用的运行时目录并确保存在。"""
    runtime_dir = settings.TEMP_PATH / "feedback-issue"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    return runtime_dir


def runtime_file(prefix: str, suffix: str) -> Path:
    """在运行时目录下生成一个随机文件路径。"""
    safe_prefix = re.sub(r"[^a-zA-Z0-9_-]+", "-", prefix).strip("-") or "feedback"
    return feedback_runtime_dir() / f"{safe_prefix}-{uuid.uuid4().hex[:12]}{suffix}"


def ensure_runtime_file(path: str | Path) -> Path:
    """校验脚本间传递的文件必须位于 feedback-issue 运行时目录内。"""
    candidate = Path(path).expanduser().resolve()
    runtime_dir = feedback_runtime_dir().resolve()
    if not candidate.is_relative_to(runtime_dir):
        raise ValueError(f"只允许读取 feedback-issue 运行时目录内的文件: {candidate}")
    return candidate


def read_json_file(path: str | Path) -> dict[str, Any]:
    """读取 JSON 文件并确保顶层对象是 dict。"""
    json_path = Path(path).expanduser()
    data = json.loads(json_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"JSON 顶层必须是对象: {json_path}")
    return data


def write_json_file(path: str | Path, payload: dict[str, Any]) -> Path:
    """把 JSON 对象写入文件并返回实际路径。"""
    json_path = Path(path)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return json_path


def validate_enum(value: str, allowed: tuple[str, ...], field_name: str) -> Optional[str]:
    """校验枚举字段，返回错误信息；通过时返回 None。"""
    if value not in allowed:
        return (
            f"{field_name} 必须是以下之一：{', '.join(allowed)}；"
            f"当前传入：{value!r}"
        )
    return None


def normalize_target_repo(target_repo: Optional[str]) -> str:
    """把目标仓库规范化为 GitHub 的 owner/repo 形式。"""
    repo = (target_repo or FEEDBACK_REPO).strip()
    if not repo:
        return FEEDBACK_REPO
    repo = repo.removesuffix(".git").strip("/")
    if repo.startswith(("http://", "https://")):
        parsed = urlparse(repo)
        if (parsed.hostname or "").lower() not in {"github.com", "www.github.com"}:
            raise ValueError(f"目标仓库只支持 GitHub 地址：{target_repo}")
        parts = [part for part in parsed.path.strip("/").split("/") if part]
        if len(parts) < 2:
            raise ValueError(f"GitHub 仓库地址缺少 owner/repo：{target_repo}")
        repo = f"{parts[0]}/{parts[1].removesuffix('.git')}"
    if not _GITHUB_REPO_PATTERN.fullmatch(repo):
        raise ValueError(f"目标仓库必须是 owner/repo 或 GitHub 仓库 URL：{target_repo}")
    return repo


def issue_api_url(target_repo: Optional[str]) -> str:
    """返回指定仓库的 GitHub Issues API 地址。"""
    return f"https://api.github.com/repos/{normalize_target_repo(target_repo)}/issues"


def issue_new_url(target_repo: Optional[str]) -> str:
    """返回指定仓库的新建 Issue 页面地址。"""
    return f"https://github.com/{normalize_target_repo(target_repo)}/issues/new"


def validate_target_repo_for_issue(issue_type: str, target_repo: str) -> Optional[str]:
    """校验 Issue 类型与目标仓库是否匹配，避免插件问题误投主仓库。"""
    if issue_type == "插件问题" and target_repo == FEEDBACK_REPO:
        return (
            "issue_type 为「插件问题」时必须把 target_repo 设置为插件所属 GitHub 仓库，"
            f"不能提交到主仓库 {FEEDBACK_REPO}。"
        )
    return None


def issue_labels(issue_type: str, target_repo: Optional[str]) -> list[str]:
    """返回提交 Issue 时应使用的标签列表。"""
    if issue_type == FEATURE_ISSUE_TYPE:
        return ["feature request"]
    if normalize_target_repo(target_repo) == FEEDBACK_REPO:
        return ["bug"]
    return []


def redact_logs(raw: str) -> str:
    """对日志文本做统一脱敏，覆盖常见 token、Cookie、PII 和本机路径。"""
    out = raw
    for pattern, replacement in _SENSITIVE_PATTERNS:
        out = pattern.sub(replacement, out)
    return out


def truncate(text: str, limit: int, marker: str = "\n...（已截断）") -> str:
    """按字符数截断文本并附加截断标记。"""
    if not text or len(text) <= limit:
        return text
    return text[: max(0, limit - len(marker))] + marker


def sanitize_logs(logs: Optional[str], limit: int) -> str:
    """清洗日志：去空白、脱敏并按指定长度截断。"""
    if not logs or not logs.strip():
        return ""
    return truncate(redact_logs(logs.strip()), limit)


def build_issue_body(
    *,
    version: str,
    environment: str,
    issue_type: str,
    description: str,
    logs: Optional[str],
    target_repo: Optional[str] = None,
) -> str:
    """构造与上游 bug_report.yml 表单渲染接近的 Issue Markdown 正文。"""
    repo = normalize_target_repo(target_repo)
    log_block = sanitize_logs(logs, MAX_LOGS_CHARS) or "会话中未捕获到相关后端日志。"
    if issue_type == FEATURE_ISSUE_TYPE:
        body = (
            "### 需求类型\n\n"
            f"{FEATURE_ISSUE_TYPE}\n\n"
            f"### 当前程序版本\n\n{version}\n\n"
            f"### 运行环境\n\n{environment}\n\n"
            f"### 目标仓库\n\n{repo}\n\n"
            f"### 需求描述\n\n{description.strip()}\n\n"
            "### 补充诊断信息\n\n"
            f"```text\n{log_block}\n```\n"
            "\n---\n"
            "_本 Issue 由 MoviePilot Agent 协助用户提交。_"
        )
        return truncate(body, MAX_BODY_CHARS)

    body = (
        "### 确认\n\n"
        "- [x] 我的版本是最新版本，我的版本号与 "
        "[version](https://github.com/jxxghp/MoviePilot/releases/latest) 相同。\n"
        f"- [x] 我已经 [issue](https://github.com/{repo}/issues) "
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
    return truncate(body, MAX_BODY_CHARS)


def build_prefill_url(
    *,
    title: str,
    version: str,
    environment: str,
    issue_type: str,
    description: str,
    logs: Optional[str],
    target_repo: Optional[str] = None,
) -> str:
    """生成 GitHub Issue Forms 预填 URL，供无 token 或 API 失败时手动提交。"""
    repo = normalize_target_repo(target_repo)
    labels = issue_labels(issue_type, repo)
    if repo != FEEDBACK_REPO or issue_type == FEATURE_ISSUE_TYPE:
        body = build_issue_body(
            version=version,
            environment=environment,
            issue_type=issue_type,
            description=description,
            logs=sanitize_logs(logs, MAX_URL_LOGS_CHARS),
            target_repo=repo,
        )
        params = {
            "title": title,
            "body": body,
        }
        if labels:
            params["labels"] = ",".join(labels)
        encoded = "&".join(
            f"{quote(k, safe='')}={quote(v, safe='')}" for k, v in params.items()
        )
        return f"{issue_new_url(repo)}?{encoded}"

    params = {
        "template": FEEDBACK_ISSUE_TEMPLATE,
        "title": title,
        "version": version,
        "environment": environment,
        "type": issue_type,
        "what-happened": description,
        "logs": sanitize_logs(logs, MAX_URL_LOGS_CHARS),
    }
    encoded = "&".join(
        f"{quote(k, safe='')}={quote(v, safe='')}" for k, v in params.items()
    )
    return f"{issue_new_url(repo)}?{encoded}"


def _safe_count(value: Any) -> int:
    """把不可信的诊断计数字段转换为非负整数。"""
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0


def format_doctor_summary(doctor: Optional[dict[str, Any]]) -> str:
    """把 doctor JSON 报告压缩成适合 Issue 和预览展示的摘要。"""
    if not isinstance(doctor, dict):
        return "未收集到 doctor 报告。"
    if not doctor.get("success"):
        return f"doctor 收集失败：{doctor.get('error') or '未知错误'}"

    report = doctor.get("report") or {}
    if not isinstance(report, dict):
        return "doctor 报告格式异常。"

    lines = [
        f"状态：{report.get('status') or 'unknown'}",
    ]
    environment = report.get("environment") or {}
    if isinstance(environment, dict):
        runtime = environment.get("runtime")
        if runtime:
            lines.append(f"运行环境：{runtime}")
    findings = report.get("findings") or []
    summary = report.get("summary") or {}
    if isinstance(summary, dict):
        advisory_count = summary.get("advisory")
        if advisory_count is None and isinstance(findings, list):
            advisory_count = sum(
                1
                for item in findings
                if isinstance(item, dict)
                and item.get("affects_report_status") is False
                and not item.get("fixed")
            )
        lines.append(
            "汇总："
            f"total={summary.get('total', 0)} "
            f"error={summary.get('error', 0)} "
            f"warn={summary.get('warn', 0)} "
            f"advisory={advisory_count or 0} "
            f"fixed={summary.get('fixed', 0)}"
        )

    if isinstance(findings, list):
        grouped: dict[tuple[str, str, str, bool], dict[str, Any]] = {}
        for item in findings:
            if not isinstance(item, dict) or item.get("severity") not in {"error", "warn"}:
                continue
            title = str(item.get("title") or item.get("id") or "未知诊断项")
            recommendation = str(item.get("recommendation") or "").strip()
            advisory = item.get("affects_report_status") is False
            key = (str(item.get("severity")), title, recommendation, advisory)
            group = grouped.setdefault(
                key,
                {
                    "count": 0,
                    "matches": 0,
                    "unique_matches": 0,
                    "sources": [],
                },
            )
            group["count"] += 1
            context = item.get("context") or {}
            if not isinstance(context, dict):
                continue
            group["matches"] += _safe_count(context.get("matches"))
            group["unique_matches"] += _safe_count(
                context.get("unique_matches") or context.get("matches") or 0
            )
            source_files = context.get("log_files") or [context.get("log_file")]
            for source_file in source_files:
                if not source_file:
                    continue
                source_name = Path(str(source_file)).name
                if source_name not in group["sources"]:
                    group["sources"].append(source_name)

        important = list(grouped.items())[:8]
        if important:
            lines.append("关键发现：")
            for (severity, title, recommendation, advisory), group in important:
                marker = f"{severity}/advisory" if advisory else severity
                line = f"- [{marker}] {title}"
                if group["count"] > 1:
                    line = f"{line}（合并 {group['count']} 项）"
                if group["sources"]:
                    line = f"{line}；来源：{', '.join(group['sources'])}"
                if group["matches"]:
                    line = f"{line}；命中：{group['matches']} 条"
                    if group["unique_matches"] < group["matches"]:
                        line = f"{line}，去重后 {group['unique_matches']} 条"
                if recommendation:
                    line = f"{line}；建议：{recommendation}"
                lines.append(line)
    return truncate("\n".join(lines), MAX_DOCTOR_SUMMARY_CHARS)


def format_log_selection(selection: Optional[dict[str, Any]]) -> str:
    """把日志筛选依据格式化为便于用户确认的摘要。"""
    if not isinstance(selection, dict):
        return "未记录日志筛选依据。"

    keywords = selection.get("keywords") or []
    keyword_text = "、".join(str(item) for item in keywords) if keywords else "未提供具体关键词"
    lines = [
        f"策略：{selection.get('strategy') or '时间窗口 + 模块噪音过滤 + 关键词块匹配'}",
        f"时间窗口：最近 {selection.get('time_window_minutes') or '?'} 分钟",
        f"窗口起点：{selection.get('window_start') or '未知'}",
        f"关键词：{keyword_text}",
        f"单文件最多保留：{selection.get('max_lines_per_file') or '?'} 行",
    ]
    warning = str(selection.get("warning") or "").strip()
    if warning:
        lines.append(f"提示：{warning}")

    matched_files = selection.get("matched_files") or []
    if not matched_files:
        lines.append("命中文件：无")
        return truncate("\n".join(lines), MAX_DOCTOR_SUMMARY_CHARS)

    lines.append("命中文件：")
    for item in matched_files[:8]:
        if not isinstance(item, dict):
            continue
        matched_keywords = item.get("matched_keywords") or []
        matched_text = "、".join(str(keyword) for keyword in matched_keywords) or "仅按时间窗口"
        lines.append(
            f"- {item.get('path') or '未知文件'}；"
            f"命中关键词：{matched_text}；"
            f"行数：{item.get('line_count') or 0}"
        )
    return truncate("\n".join(lines), MAX_DOCTOR_SUMMARY_CHARS)


def classify_failure(status_code: Optional[int], headers: Optional[dict] = None) -> str:
    """把 GitHub API HTTP 状态码映射成脚本输出的稳定失败原因。"""
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
        return "no_permission"
    if status_code == 422:
        return "invalid_payload"
    if status_code is not None and status_code >= 500:
        return "github_unavailable"
    return "api_error"


def safe_response_dict(response: Any) -> dict[str, Any]:
    """安全解析 HTTP 响应 JSON，非 dict 或解析失败时返回空 dict。"""
    try:
        data = response.json()
    except Exception:
        return {}
    if isinstance(data, dict):
        return data
    return {}


def check_content_quality(
    *,
    title: str,
    description: str,
    original_user_request: str,
    logs: Optional[str] = None,
    issue_type: str = "主程序运行问题",
) -> Optional[str]:
    """检查 Issue 内容质量，拦截测试、占位、乱码和结构缺失的提交。"""
    original_stripped = (original_user_request or "").strip()
    if not original_stripped:
        return (
            "缺少原始用户请求，无法判断本次提交是否来自真实故障。"
            "请传入触发反馈的用户原话，不能只传改写后的 Issue 草稿。"
        )

    title_body = title.strip()
    for prefix in TITLE_PREFIXES:
        if title_body.startswith(prefix):
            title_body = title_body[len(prefix):].strip()
            break
    if len(title_body) < MIN_TITLE_BODY_CHARS:
        return (
            f"标题正文太短（剔除标题前缀后只有 {len(title_body)} 字，"
            f"至少 {MIN_TITLE_BODY_CHARS} 字）。请用一句完整的话概括症状。"
        )

    desc_stripped = description.strip()
    if len(desc_stripped) < MIN_DESCRIPTION_CHARS:
        return (
            f"问题描述太短（{len(desc_stripped)} 字，至少 {MIN_DESCRIPTION_CHARS} 字）。"
            "请补充：现象 / 复现步骤 / 期望行为。"
        )

    required_signals = (
        _FEATURE_DESCRIPTION_REQUIRED_SIGNALS
        if issue_type == FEATURE_ISSUE_TYPE else _DESCRIPTION_REQUIRED_SIGNALS
    )
    missing_signals = [
        label
        for label, choices in required_signals
        if not any(choice in desc_stripped for choice in choices)
    ]
    if missing_signals:
        content_name = "功能请求" if issue_type == FEATURE_ISSUE_TYPE else "可复现 bug"
        return (
            f"问题描述缺少{content_name}所需的结构信息："
            f"{' / '.join(missing_signals)}。请补充真实现象、触发步骤和期望行为。"
        )

    haystack = "\n".join(
        part for part in (title, description, original_stripped) if part
    ).lower()
    for phrase in _QUALITY_BLOCKLIST:
        if phrase.lower() in haystack:
            return (
                f"原始请求、标题或描述命中明显占位/测试关键词「{phrase}」，"
                "已拒绝提交。如果是真实问题，请用正常的中文描述具体现象。"
            )

    match = (
        _REPEAT_GIBBERISH.search(title)
        or _REPEAT_GIBBERISH.search(description)
        or _REPEAT_GIBBERISH.search(original_stripped)
    )
    if match:
        return (
            f"标题或描述里出现疑似乱码片段「{match.group(0)[:12]}...」，"
            "请用正常文字描述问题。"
        )

    log_text = (logs or "").strip()
    if log_text:
        lowered_logs = log_text.lower()
        for phrase in _FABRICATED_LOG_PHRASES:
            if phrase.lower() in lowered_logs and len(log_text) < 200:
                return (
                    f"日志字段疑似填入了叙述性占位内容「{phrase}」，"
                    "请只提交真实日志；没有日志时留空。"
                )
    return None


def normalize_username(username: Optional[str]) -> str:
    """归一化用户名，作为脚本级提交频率限制的桶 key。"""
    return (username or "").strip().lower()


def load_submission_state() -> dict[str, Any]:
    """读取脚本持久化的短期提交状态。"""
    state_file = feedback_runtime_dir() / "submission-state.json"
    if not state_file.exists():
        return {"recent_submissions": {}, "user_submissions": {}}
    try:
        state = read_json_file(state_file)
    except Exception:
        return {"recent_submissions": {}, "user_submissions": {}}
    state.setdefault("recent_submissions", {})
    state.setdefault("user_submissions", {})
    return state


def save_submission_state(state: dict[str, Any]) -> None:
    """写回脚本持久化的短期提交状态。"""
    write_json_file(feedback_runtime_dir() / "submission-state.json", state)


def check_recent_duplicate(
    title: str,
    body: str,
    state: dict[str, Any],
    target_repo: Optional[str] = None,
) -> Optional[str]:
    """检查 60 秒内是否提交过同 title + body 的内容。"""
    now = time.time()
    recent = state.setdefault("recent_submissions", {})
    for key, ts in list(recent.items()):
        if now - float(ts or 0) > DEDUP_TTL_SECONDS:
            recent.pop(key, None)
    repo = normalize_target_repo(target_repo)
    key = hashlib.sha256(f"{repo}\x00{title}\x00{body}".encode("utf-8", errors="replace")).hexdigest()
    if key in recent:
        return key
    return None


def record_submission(
    title: str,
    body: str,
    state: dict[str, Any],
    target_repo: Optional[str] = None,
) -> None:
    """记录一次提交内容摘要，供短时间去重使用。"""
    repo = normalize_target_repo(target_repo)
    key = hashlib.sha256(f"{repo}\x00{title}\x00{body}".encode("utf-8", errors="replace")).hexdigest()
    state.setdefault("recent_submissions", {})[key] = time.time()


def evict_user_submissions_if_needed(state: dict[str, Any]) -> None:
    """限制用户提交状态桶数量，避免运行时文件无限增长。"""
    buckets = state.setdefault("user_submissions", {})
    if len(buckets) <= MAX_USER_SUBMISSIONS_BUCKETS:
        return
    excess = len(buckets) - MAX_USER_SUBMISSIONS_BUCKETS
    oldest_keys = sorted(
        buckets.items(),
        key=lambda kv: kv[1][-1] if kv[1] else 0,
    )[:excess]
    for key, _ in oldest_keys:
        buckets.pop(key, None)


def check_user_rate_limit(username: str, state: dict[str, Any]) -> Optional[str]:
    """检查单用户 30 分钟冷却和 24 小时提交配额。"""
    key = normalize_username(username)
    if not key:
        return "无法识别调用用户身份，rate limit 拒绝以防误用。"
    now = time.time()
    buckets = state.setdefault("user_submissions", {})
    timestamps = buckets.get(key, [])
    active = [float(ts) for ts in timestamps if now - float(ts or 0) < USER_DAILY_WINDOW_SECONDS]
    if active:
        buckets[key] = active
    else:
        buckets.pop(key, None)
    if active:
        since_last = now - active[-1]
        if since_last < USER_COOLDOWN_SECONDS:
            remaining = int(USER_COOLDOWN_SECONDS - since_last)
            minutes, seconds = divmod(remaining, 60)
            return (
                f"为避免给上游刷屏，同一管理员两次提交之间至少间隔 "
                f"{USER_COOLDOWN_SECONDS // 60} 分钟。请等 {minutes} 分 {seconds} 秒后再试。"
            )
    if len(active) >= USER_DAILY_QUOTA:
        recover_in = int(USER_DAILY_WINDOW_SECONDS - (now - active[0]))
        hours, remainder = divmod(recover_in, 3600)
        minutes = remainder // 60
        return (
            f"你今日已提交 {USER_DAILY_QUOTA} 个 Issue，已达 24 小时配额上限。"
            f"最早一条将在 {hours} 小时 {minutes} 分钟后过期，请到时再提。"
        )
    return None


def record_user_submission(username: str, state: dict[str, Any]) -> None:
    """记录一次用户级提交时间戳，供冷却和配额检查使用。"""
    key = normalize_username(username)
    if not key:
        return
    state.setdefault("user_submissions", {}).setdefault(key, []).append(time.time())
    evict_user_submissions_if_needed(state)


def load_diagnostics_logs(diagnostics_file: str | Path) -> tuple[str, dict[str, Any]]:
    """读取诊断文件中的日志正文并返回日志与诊断元数据。"""
    path = ensure_runtime_file(diagnostics_file)
    data = read_json_file(path)
    logs = sanitize_logs(str(data.get("logs") or ""), MAX_LOGS_CHARS)
    return logs, data


def result_payload(**fields: Any) -> str:
    """把脚本结果格式化为 Agent 容易解析的 JSON 字符串。"""
    return json.dumps(fields, ensure_ascii=False, indent=2)
