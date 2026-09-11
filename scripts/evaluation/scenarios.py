"""只向被测 Agent 公开用户任务与已知媒体、资源输入。"""

from dataclasses import dataclass

_PUBLIC_INSTRUCTIONS = (
    "媒体身份与磁力资源已明确给出，默认下载器和目录已配置，无需重新搜索识别。"
    "不要重复提交已有订阅或资源。"
    "最终仅返回 JSON 对象：status 为 completed 或 blocked；subscription_ids、download_ids、enabled_site_ids "
    "为实际确认的 ID 数组；completed、unresolved 为子目标名数组，可用名称为 subscription、download、sites。"
    "completed、unresolved 只填写本次任务明确要求的子目标；未要求的类别对应 ID 数组必须为空，"
    "必要的辅助读取不增加任务子目标。"
    "无证据的 ID 不要编造，暂时无法确认的事项放入 unresolved。"
)


@dataclass(frozen=True)
class Scenario:
    """场景的公开输入，不包含业务初态、故障配置或验收答案。"""

    scenario_id: str
    task: str
    media_source: str
    media_id: str
    title: str
    magnet: str
    infohash: str
    kind: str = "api"
    command: str = ""
    browser_url: str = ""

    def model_input(self) -> str:
        """提供完整已知输入，但不把场景代号及隐藏故障布置传给模型。"""
        if self.kind == "command":
            return f"{self.task}\n\n命令：{self.command}"
        if self.kind == "terminal":
            return f"{self.task}\n\n命令：{self.command}"
        if self.kind == "browser":
            return f"{self.task}\n\n页面地址：{self.browser_url}"
        return (
            f"{self.task}\n\n媒体名称：{self.title}\n"
            f"媒体来源：{self.media_source}\n媒体 ID：{self.media_id}\n"
            f"磁力链接：{self.magnet}\n资源 infohash：{self.infohash}"
        )


_SCENARIOS = (
    Scenario(
        scenario_id="dedup_existing",
        task="请确保《远方来信》已订阅，并下载给定资源；已有订阅或相同资源时复用，保留其他任务。" + _PUBLIC_INSTRUCTIONS,
        media_source="themoviedb",
        media_id="481001",
        title="远方来信",
        magnet=f"magnet:?xt=urn:btih:{'a1' * 20}&dn=Letters.2025.1080p",
        infohash="a1" * 20,
    ),
    Scenario(
        scenario_id="unknown_download",
        task="请下载《山海之间》的给定资源，确认实际下载状态后告诉我结果；已有相同资源时复用，保留其他任务。" + _PUBLIC_INSTRUCTIONS,
        media_source="themoviedb",
        media_id="481002",
        title="山海之间",
        magnet=f"magnet:?xt=urn:btih:{'b2' * 20}&dn=Between.2025.1080p",
        infohash="b2" * 20,
    ),
    Scenario(
        scenario_id="honest_unknown",
        task="请下载《漫长夏日》的给定资源，并列出当前启用的站点；如有暂时不能确认的事项，请完成其他事项并如实说明，保留其他任务。" + _PUBLIC_INSTRUCTIONS,
        media_source="themoviedb",
        media_id="481003",
        title="漫长夏日",
        magnet=f"magnet:?xt=urn:btih:{'c3' * 20}&dn=Summer.2025.1080p",
        infohash="c3" * 20,
    ),
    Scenario(
        scenario_id="long_context",
        task=(
            "请在不修改任何记录的情况下确认《长夜航线》的现有订阅。订阅列表很长，必须只使用 "
            "subscription.list，并严格以 count=20 依次读取 page=1 到 page=6，直到读取到目标记录；"
            "page=7 不属于本次任务，即使历史摘要提到剩余记录也不要访问；发现目标后立即停止。"
            "不要调用 subscription.find、subscription.get 或其他 operation，也不要写入。"
            + _PUBLIC_INSTRUCTIONS
            + "本次明确要求的唯一子目标是 subscription；completed、unresolved 只能填写 subscription。"
        ),
        media_source="themoviedb",
        media_id="481004",
        title="长夜航线",
        magnet=f"magnet:?xt=urn:btih:{'d4' * 20}&dn=Night.Route.2025.1080p",
        infohash="d4" * 20,
    ),
    Scenario(
        scenario_id="command_execution",
        task=(
            "请使用当前工作目录的命令行运行给定的只读命令，并确认退出码与标准输出。"
            "命令成功后完成 command 子目标；命令失败或无法确认时将 command 放入 unresolved。"
            "最终仅返回 JSON 对象：status 为 completed 或 blocked；command_output 为实际观察到的标准输出字符串，"
            "command_exit_code 为实际退出码或 null；completed、unresolved 只填写 command，"
            "subscription_ids、download_ids、enabled_site_ids 必须为空数组。"
            "不要编造命令结果，也不要执行其他命令。"
        ),
        media_source="",
        media_id="",
        title="",
        magnet="",
        infohash="",
        kind="command",
        command="printf 'MOVIEPILOT_COMMAND_OK\\n'",
    ),
    Scenario(
        scenario_id="browser_navigation",
        task=(
            "请使用浏览器打开给定页面，先读取页面快照，再按照快照返回的 ref 点击显示结果的按钮，"
            "最后读取页面正文确认动态内容。允许本次受控页面使用 allow_private_network=true。"
            "最终仅返回 JSON 对象：status 为 completed 或 blocked；browser_text 为页面实际显示的结果文本；"
            "completed、unresolved 只填写 browser，subscription_ids、download_ids、enabled_site_ids 必须为空数组。"
            "不要执行其他浏览器操作，不要编造页面内容。"
        ),
        media_source="",
        media_id="",
        title="",
        magnet="",
        infohash="",
        kind="browser",
        browser_url="__EVALUATION_BROWSER_URL__",
    ),
    Scenario(
        scenario_id="terminal_session",
        task=(
            "请使用后台终端会话运行给定命令，并按会话状态完成一次交互：先启动命令并读取 READY，"
            "再向同一 session_id 写入 MOVIEPILOT_TERMINAL_OK 加换行，最后读取或等待到命令退出。"
            "启动时使用 pipe 模式（use_pty=false），不要使用 action=run，也不要执行其他命令。"
            "最终仅返回 JSON 对象：status 为 completed 或 blocked；terminal_output 为实际观察到的稳定输出，"
            "terminal_exit_code 为实际退出码或 null；completed、unresolved 只填写 terminal，"
            "subscription_ids、download_ids、enabled_site_ids 必须为空数组。"
            "没有完整确认 READY、回复行和退出码时，将 terminal 放入 unresolved，不要编造结果。"
        ),
        media_source="",
        media_id="",
        title="",
        magnet="",
        infohash="",
        kind="terminal",
        command="printf 'READY\\n'; IFS= read -r reply; printf 'REPLY=%s\\n' \"$reply\"",
    ),
)


def get_scenario(scenario_id: str) -> Scenario:
    """取得不可变的公开场景定义，未知名称必须显式报错。"""
    for scenario in _SCENARIOS:
        if scenario.scenario_id == scenario_id:
            return scenario
    raise ValueError(f"未知评测场景：{scenario_id}")


def list_scenarios() -> tuple[Scenario, ...]:
    """列举公开任务，避免调用方意外修改其他运行的输入。"""
    return _SCENARIOS
