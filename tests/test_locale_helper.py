import ast
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from fastapi import HTTPException

from app.factory import localized_http_exception_handler
from app.helper.locale import LocaleHelper
from app.helper.progress import ProgressHelper
from app.schemas.dashboard import ScheduleInfo, ScheduleProgress
from app.schemas.response import Response


def _has_chinese(text: str) -> bool:
    """判断文本是否包含中文字符。"""
    return any("\u4e00" <= char <= "\u9fff" for char in text)


def _sample_message_expression(value: ast.AST) -> list[str]:
    """从接口 message 表达式中生成用于翻译校验的样本文本。"""
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return [value.value]
    if isinstance(value, ast.JoinedStr):
        parts = []
        has_chinese = False
        index = 0
        for item in value.values:
            if isinstance(item, ast.Constant) and isinstance(item.value, str):
                parts.append(item.value)
                has_chinese = has_chinese or _has_chinese(item.value)
            else:
                index += 1
                parts.append(f"样例{index}")
        return ["".join(parts)] if has_chinese else []
    if isinstance(value, ast.IfExp):
        return _sample_message_expression(value.body) + _sample_message_expression(value.orelse)
    if isinstance(value, ast.BoolOp):
        samples = []
        for item in value.values:
            samples.extend(_sample_message_expression(item))
        return samples
    if isinstance(value, ast.BinOp) and isinstance(value.op, ast.Add):
        left_samples = _sample_message_expression(value.left)
        right_samples = _sample_message_expression(value.right)
        if left_samples and right_samples:
            return [left + right for left in left_samples for right in right_samples]
        if left_samples:
            return [f"{left}样例" for left in left_samples]
        if right_samples:
            return [f"样例{right}" for right in right_samples]
    return []


def _sample_progress_text_expression(value: ast.AST) -> list[str]:
    """从进度 text 表达式中生成用于翻译校验的样本文本。"""
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return [value.value]
    if isinstance(value, ast.JoinedStr):
        samples = [""]
        placeholder_index = 0
        for item in value.values:
            if isinstance(item, ast.Constant) and isinstance(item.value, str):
                choices = [item.value]
            elif isinstance(item, ast.FormattedValue):
                choices = [
                    sample
                    for sample in _sample_progress_text_expression(item.value)
                    if _has_chinese(sample)
                ]
                if not choices:
                    placeholder_index += 1
                    choices = [f"样例{placeholder_index}"]
            else:
                placeholder_index += 1
                choices = [f"样例{placeholder_index}"]
            samples = [prefix + choice for prefix in samples for choice in choices]
        return [sample for sample in samples if _has_chinese(sample)]
    if isinstance(value, ast.IfExp):
        return (
            _sample_progress_text_expression(value.body)
            + _sample_progress_text_expression(value.orelse)
        )
    if isinstance(value, ast.BoolOp):
        samples = []
        for item in value.values:
            samples.extend(_sample_progress_text_expression(item))
        return samples
    if isinstance(value, ast.BinOp) and isinstance(value.op, ast.Add):
        left_samples = _sample_progress_text_expression(value.left)
        right_samples = _sample_progress_text_expression(value.right)
        if left_samples and right_samples:
            return [left + right for left in left_samples for right in right_samples]
        if left_samples:
            return [f"{left}样例" for left in left_samples]
        if right_samples:
            return [f"样例{right}" for right in right_samples]
    return []


def _is_progress_text_call(node: ast.Call) -> bool:
    """判断调用是否可能写入前端展示的进度文本。"""
    func = ast.unparse(node.func)
    return (
        func.endswith("progress_callback")
        or func.endswith(".update")
        or func.endswith(".end")
    )


def test_locale_helper_normalizes_supported_aliases():
    """语言别名应规范化为项目支持的语言标识。"""
    assert LocaleHelper.normalize_locale("en") == "en-US"
    assert LocaleHelper.normalize_locale("zh_Hant") == "zh-TW"
    assert LocaleHelper.normalize_locale("unknown") == "zh-CN"


def test_locale_helper_reads_request_headers_by_priority():
    """请求语言应优先使用显式前端语言头，再回退 Accept-Language。"""
    request = SimpleNamespace(
        headers={
            "x-moviepilot-locale": "en-US",
            "accept-language": "zh-TW,zh;q=0.9",
        }
    )

    assert LocaleHelper.get_locale_from_request(request) == "en-US"


def test_locale_helper_reads_query_locale_before_headers():
    """SSE 请求可通过查询参数显式指定前端语言。"""
    request = SimpleNamespace(
        query_params={"locale": "zh-TW"},
        headers={
            "x-moviepilot-locale": "en-US",
            "accept-language": "en-US",
        },
    )

    assert LocaleHelper.get_locale_from_request(request) == "zh-TW"


def test_locale_helper_parses_accept_language_quality():
    """Accept-Language 应按 q 权重选择支持的语言。"""
    request = SimpleNamespace(
        headers={
            "accept-language": "fr-FR, en-US;q=0.8, zh-TW;q=0.9",
        }
    )

    assert LocaleHelper.get_locale_from_request(request) == "zh-TW"


def test_locale_helper_translates_module_name_and_keeps_missing_text():
    """翻译存在时返回目标语言，缺失时返回默认文本。"""
    assert LocaleHelper.translate(
        "system.modules.DoubanModule.name",
        locale="en-US",
        default="豆瓣",
    ) == "Douban"
    assert LocaleHelper.translate_text("模块不支持测试", locale="en-US") == "Module does not support testing"
    assert LocaleHelper.translate_text(
        "requirements.txt 文件下载失败", locale="en-US"
    ) == "Failed to download requirements.txt"
    assert LocaleHelper.translate_text("未收录的中文错误", locale="en-US") == "未收录的中文错误"


def test_locale_helper_translates_dynamic_message_patterns():
    """动态消息应保留变量并翻译模板文本。"""
    assert LocaleHelper.translate_text(
        "无法连接Qbittorrent下载器：默认",
        locale="en-US",
    ) == "Unable to connect to qBittorrent downloader: 默认"
    assert LocaleHelper.translate_text(
        "无法连接 api.themoviedb.org，错误码：403",
        locale="en-US",
    ) == "Unable to connect to api.themoviedb.org, error code: 403"
    assert LocaleHelper.translate_text(
        "飞书 工作 未就绪",
        locale="zh-TW",
    ) == "飛書 工作 尚未就緒"


def test_locale_helper_translates_common_backend_response_messages():
    """常见后端链路消息应能生成英文展示文本。"""
    samples = {
        "QQ Bot 默认 未就绪": "QQ Bot 默认 is not ready",
        "微信 ClawBot 工作 未就绪：未登录": "WeChat ClawBot 工作 is not ready: 未登录",
        "无法打开网站！": "Unable to open the site!",
        "错误：403 Forbidden": "Error: 403 Forbidden",
        "站点【https://example.com】不存在": "Site [https://example.com] does not exist",
        "下载字幕文件失败，状态码：404 Not Found": "Failed to download subtitle file, status code: 404 Not Found",
        "未获取到第 2 季的总集数": "Unable to get the total episode count for season 2",
        "电影.mkv 已在整理队列中": "电影.mkv is already in the organization queue",
        "未识别到媒体信息，类型：电视剧，id：123": "Unable to recognize media information, type: 电视剧, id: 123",
        "默认 的下载目录 /downloads 不存在": "Download directory for 默认 does not exist: /downloads",
        "不支持 Local 到 Alist 的文件整理": "File organization from Local to Alist is not supported",
        "文件 /tmp/a.mkv 不存在": "File /tmp/a.mkv does not exist",
        "添加种子任务失败：种子无效": "Failed to add torrent task: 种子无效",
        "检查授权状态失败: timeout": "Failed to check authorization status: timeout",
        "未找到名为 工作 的微信 ClawBot 通知配置": (
            "No WeChat ClawBot notification configuration named 工作 was found"
        ),
        "整理记录不存在: 1, 2": "Organization record does not exist: 1, 2",
        "插件要求 MoviePilot 版本 >=2.14.0，当前版本 2.13.0 不满足，已拒绝安装": (
            "The plugin requires MoviePilot version >=2.14.0, but current version 2.13.0 "
            "does not satisfy it. Installation was rejected"
        ),
        "已安排一次性 release 升级并重启": "Scheduled one-shot release upgrade and restart",
        "已将微信 ClawBot 登录缓存从 old 迁移到 new": (
            "Migrated WeChat ClawBot login cache from old to new"
        ),
        "搜索完成，共 3 个资源": "Search completed, 3 resources",
        "正在搜索关键字，已完成 1 / 6 个请求 ...": (
            "Searching 关键字, completed 1/6 requests ..."
        ),
        "正在搜索字幕关键字，已完成 1 / 6 个请求 ...": (
            "Searching subtitles 关键字, completed 1/6 requests ..."
        ),
        "未开启任何支持字幕搜索的有效站点，无法搜索字幕": (
            "No valid subtitle-search site is enabled, unable to search subtitles"
        ),
        "用户名或密码错误": "Incorrect username or password",
        "工具 'query_schedulers' 未找到": "Tool 'query_schedulers' was not found",
        "插件 test 不存在或未加载": "Plugin test does not exist or is not loaded",
        "站点 1 不存在": "Site 1 does not exist",
        "智能助手未启用，请先在系统设置中开启。": (
            "The assistant is not enabled. Enable it in system settings first."
        ),
        "智能助手执行失败: timeout": "Assistant execution failed: timeout",
    }

    for message, expected in samples.items():
        assert LocaleHelper.translate_text(message, locale="en-US") == expected


def test_response_auto_fills_message_i18n_from_locale_context():
    """通用 Response 应根据请求语言上下文自动补充多语言消息。"""
    token = LocaleHelper.set_current_locale("en-US")
    try:
        response = Response(success=False, message="模块不支持测试")
    finally:
        LocaleHelper.reset_current_locale(token)

    assert response.message == "模块不支持测试"
    assert response.message_i18n == "Module does not support testing"


def test_http_exception_handler_adds_detail_i18n_from_locale_context():
    """HTTPException 响应应补充多语言 detail 字段。"""
    token = LocaleHelper.set_current_locale("en-US")
    try:
        response = asyncio.run(
            localized_http_exception_handler(
                None,
                HTTPException(status_code=401, detail="用户名或密码错误"),
            )
        )
    finally:
        LocaleHelper.reset_current_locale(token)

    payload = json.loads(response.body)
    assert payload["detail"] == "用户名或密码错误"
    assert payload["detail_i18n"] == "Incorrect username or password"


def test_progress_helper_get_adds_i18n_fields_without_mutating_cache():
    """通用进度字典应返回展示字段翻译，同时保留缓存中的原始中文。"""
    progress = ProgressHelper("__test_i18n_progress")
    progress.start()
    progress.update(
        text="开始同步媒体服务器，共 2 个 ...",
        data={"error": "后台服务不存在"},
    )

    detail = progress.get(locale="en-US")
    assert detail is not None
    assert detail["text"] == "开始同步媒体服务器，共 2 个 ..."
    assert detail["text_i18n"] == "Starting media server sync, 2 servers ..."
    assert detail["data"]["error"] == "后台服务不存在"
    assert detail["data"]["error_i18n"] == "Background service does not exist"

    detail["data"]["error_i18n"] = "mutated"
    second_detail = progress.get(locale="en-US")
    assert second_detail is not None
    assert second_detail["data"]["error_i18n"] == "Background service does not exist"


def test_schedule_info_auto_fills_i18n_display_fields():
    """后台服务数据应补充前端展示所需的多语言字段。"""
    token = LocaleHelper.set_current_locale("en-US")
    try:
        schedule = ScheduleInfo(
            id="mediaserver_sync",
            name="同步媒体服务器",
            provider="[系统]",
            status="等待",
            next_run="2小时3分钟",
            progress_text="开始同步媒体服务器，共 2 个 ...",
            progress_detail=ScheduleProgress(
                id="mediaserver_sync",
                name="同步媒体服务器",
                provider="[系统]",
                text="媒体服务器 Emby 无可同步媒体库",
                error="后台服务不存在",
            ),
        )
    finally:
        LocaleHelper.reset_current_locale(token)

    assert schedule.name_i18n == "Sync Media Servers"
    assert schedule.provider_i18n == "[System]"
    assert schedule.status_i18n == "Waiting"
    assert schedule.next_run_i18n == "2h 3m"
    assert schedule.progress_text_i18n == "Starting media server sync, 2 servers ..."
    assert schedule.progress_detail.text_i18n == "Media server Emby has no libraries to sync"
    assert schedule.progress_detail.error_i18n == "Background service does not exist"


def test_scheduler_progress_patterns_translate_dynamic_texts():
    """定时任务进度动态模板应翻译固定词并保留业务变量。"""
    assert (
        LocaleHelper.translate_text("同步媒体服务器 开始执行 ...", locale="en-US")
        == "Starting Sync Media Servers ..."
    )
    assert (
        LocaleHelper.translate_text("同步媒体服务器 执行完成", locale="en-US")
        == "Sync Media Servers completed"
    )
    assert (
        LocaleHelper.translate_text("同步媒体服务器 执行失败", locale="zh-TW")
        == "同步媒體伺服器 執行失敗"
    )
    assert (
        LocaleHelper.translate_text("工作流动作（1/3）动作A 执行完成", locale="en-US")
        == "Workflow action (1/3) 动作A completed"
    )


def test_scheduler_progress_texts_have_english_translations():
    """定时任务相关进度文案应有英文翻译，避免前端切英文后回退中文。"""
    untranslated = []
    progress_paths = [
        Path("app/scheduler.py"),
        Path("app/chain/torrents.py"),
        Path("app/chain/mediaserver.py"),
        Path("app/chain/site.py"),
        Path("app/chain/recommend.py"),
        Path("app/chain/transfer.py"),
        Path("app/chain/subscribe.py"),
        Path("app/chain/workflow.py"),
    ]
    for path in progress_paths:
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not _is_progress_text_call(node):
                continue
            for keyword in node.keywords:
                if keyword.arg != "text":
                    continue
                for message in _sample_progress_text_expression(keyword.value):
                    translated = LocaleHelper.translate_text(message, locale="en-US")
                    if translated == message:
                        untranslated.append(f"{path}:{keyword.value.lineno}:{message}")

    assert untranslated == []


def test_api_endpoint_literal_messages_have_english_translations():
    """接口直接返回的中文 message 应有英文翻译，避免前端切英文后回退中文。"""
    untranslated = []
    for path in Path("app/api/endpoints").glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for keyword in node.keywords:
                if keyword.arg != "message":
                    continue
                value = keyword.value
                if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
                    continue
                message = value.value
                if not any("\u4e00" <= char <= "\u9fff" for char in message):
                    continue
                translated = LocaleHelper.translate_text(message, locale="en-US")
                if translated == message:
                    untranslated.append(f"{path}:{value.lineno}:{message}")

    assert untranslated == []


def test_api_endpoint_dynamic_messages_have_english_translations():
    """接口动态拼接的中文 message 应由模板翻译覆盖。"""
    untranslated = []
    for path in Path("app/api/endpoints").glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for keyword in node.keywords:
                if keyword.arg != "message":
                    continue
                value = keyword.value
                if not isinstance(value, ast.JoinedStr):
                    continue
                parts = []
                has_chinese = False
                index = 0
                for item in value.values:
                    if isinstance(item, ast.Constant) and isinstance(item.value, str):
                        parts.append(item.value)
                        has_chinese = has_chinese or _has_chinese(item.value)
                    else:
                        index += 1
                        parts.append(f"样例{index}")
                if not has_chinese:
                    continue
                message = "".join(parts)
                translated = LocaleHelper.translate_text(message, locale="en-US")
                if translated == message:
                    untranslated.append(f"{path}:{value.lineno}:{message}")

    assert untranslated == []


def test_api_endpoint_message_expressions_have_english_translations():
    """接口 message 表达式中的中文分支应有英文翻译。"""
    untranslated = []
    for path in Path("app/api/endpoints").glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if ast.unparse(node.func) not in {"schemas.Response", "Response"}:
                continue
            for keyword in node.keywords:
                if keyword.arg != "message":
                    continue
                for message in _sample_message_expression(keyword.value):
                    if not _has_chinese(message):
                        continue
                    translated = LocaleHelper.translate_text(message, locale="en-US")
                    if translated == message:
                        untranslated.append(f"{path}:{keyword.value.lineno}:{message}")

    assert untranslated == []


def test_api_endpoint_http_exception_details_have_english_translations():
    """HTTPException 的中文 detail 应有英文翻译。"""
    untranslated = []
    for path in Path("app/api/endpoints").glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not ast.unparse(node.func).endswith("HTTPException"):
                continue
            for keyword in node.keywords:
                if keyword.arg != "detail":
                    continue
                for message in _sample_message_expression(keyword.value):
                    if not _has_chinese(message):
                        continue
                    translated = LocaleHelper.translate_text(message, locale="en-US")
                    if translated == message:
                        untranslated.append(f"{path}:{keyword.value.lineno}:{message}")

    assert untranslated == []
