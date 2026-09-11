"""只向被测 Agent 公开用户任务与已知媒体、资源输入。"""

from dataclasses import dataclass
from typing import Optional

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
    terminal_use_pty: Optional[bool] = None
    steering_message: str = ""
    steering_plan: tuple[tuple[int, str], ...] = ()

    def steering_schedule(self) -> tuple[tuple[int, str], ...]:
        """返回按业务工具回执次数触发的补充消息计划。"""
        if self.steering_plan:
            return self.steering_plan
        return ((1, self.steering_message),) if self.steering_message else ()

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
        scenario_id="subagent_parallel_status",
        task=(
            "请核对《并行巡检》的现有订阅和当前启用站点。必须把两个相互独立的只读检查分别交给通用子代理并行执行："
            "一个检查该媒体的订阅，另一个检查当前启用站点；主 Agent 等待两个子任务完成后再综合结果。"
            "主 Agent 不要直接调用这两个业务 API，子代理不得写入、删除或改变任何记录。"
            + _PUBLIC_INSTRUCTIONS
            + "本次明确要求的子目标是 subscription 和 sites；completed、unresolved 只能填写这两个名称。"
        ),
        media_source="themoviedb",
        media_id="481005",
        title="并行巡检",
        magnet=f"magnet:?xt=urn:btih:{'e5' * 20}&dn=Parallel.Check.2025.1080p",
        infohash="e5" * 20,
    ),
    Scenario(
        scenario_id="subagent_cancel_recovery",
        task=(
            "请先启动一个只读通用子代理任务，任务描述必须是‘执行一次 subscription.list 只读巡检；请求会保持等待，"
            "等待主 Agent 取消，不执行任何写操作’，"
            "使用 subagent_task 的 action=start 并记下返回的 task_id。随后立即使用 action=cancel 只取消这个 task_id，"
            "确认取消请求已经收口；取消子代理后，主 Agent 再读取一次当前启用站点并据此完成任务。"
            "主 Agent 不要直接调用 subscription API，子代理不得写入、删除或改变任何记录。"
            + _PUBLIC_INSTRUCTIONS
            + "本次明确要求的唯一子目标是 sites；completed、unresolved 只能填写 sites。"
        ),
        media_source="themoviedb",
        media_id="481006",
        title="取消恢复巡检",
        magnet=f"magnet:?xt=urn:btih:{'f6' * 20}&dn=Cancel.Recovery.2025.1080p",
        infohash="f6" * 20,
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
        scenario_id="steering_long_context",
        task=(
            "请在不修改任何记录的情况下确认《长夜航线》的现有订阅。订阅列表很长，必须只使用 "
            "subscription.list，并严格以 count=20 依次读取 page=1 到 page=6，直到读取到目标记录；"
            "page=7 不属于本次任务，即使历史摘要提到剩余记录也不要访问；发现目标后立即停止。"
            "不要调用 subscription.find、subscription.get 或其他 operation，也不要写入。"
            "运行期间可能收到一条补充要求；补充要求属于本次任务，应用后继续遵守原来的分页边界。"
            + _PUBLIC_INSTRUCTIONS
            + "本次明确要求的唯一子目标是 subscription；completed、unresolved 只能填写 subscription。"
        ),
        media_source="themoviedb",
        media_id="481004",
        title="长夜航线",
        magnet=f"magnet:?xt=urn:btih:{'d4' * 20}&dn=Night.Route.2025.1080p",
        infohash="d4" * 20,
        steering_message=(
            "补充要求：继续按原分页任务读取后续页面；不要改变原任务范围，发现目标后立即停止。"
            "完成后仍严格遵守原任务的最终输出约束，仅返回 JSON 对象，不要输出解释性文本。"
            "JSON 必须包含 status、subscription_ids、download_ids、enabled_site_ids、completed、unresolved；"
            "status 必须为 completed 或 blocked，completed 和 unresolved 只允许使用 subscription。"
        ),
    ),
    Scenario(
        scenario_id="steering_multi_message",
        task=(
            "请在不修改任何记录的情况下确认《长夜航线》的现有订阅。订阅列表很长，必须只使用 "
            "subscription.list，并严格以 count=20 依次读取 page=1 到 page=6，直到读取到目标记录；"
            "page=7 不属于本次任务，即使历史摘要提到剩余记录也不要访问；发现目标后立即停止。"
            "不要调用 subscription.find、subscription.get 或其他 operation，也不要写入。"
            "运行期间可能收到两条补充要求；每条都属于本次任务，应用后继续遵守原来的分页边界。"
            + _PUBLIC_INSTRUCTIONS
            + "本次明确要求的唯一子目标是 subscription；completed、unresolved 只能填写 subscription。"
        ),
        media_source="themoviedb",
        media_id="481004",
        title="长夜航线",
        magnet=f"magnet:?xt=urn:btih:{'d4' * 20}&dn=Night.Route.2025.1080p",
        infohash="d4" * 20,
        steering_plan=(
            (
                1,
                "第一条补充要求：继续按原分页任务读取后续页面；不要改变原任务范围，发现目标后立即停止。"
                "完成后仍严格遵守原任务的最终输出约束，仅返回 JSON 对象，不要输出解释性文本。"
                "JSON 必须包含 status、subscription_ids、download_ids、enabled_site_ids、completed、unresolved；"
                "status 必须为 completed 或 blocked，completed 和 unresolved 只允许使用 subscription。",
            ),
            (
                3,
                "第二条补充要求：保留已经确认的页码和目标身份，不要重复读取已完成页面，也不要访问 page=7。"
                "继续完成剩余分页并在有证据时立即停止；最终仍只返回符合原任务约束的 JSON 对象。",
            ),
        ),
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
            "请使用后台终端会话运行给定命令，并按会话状态完成一次交互：启动后命令会保持等待，"
            "先读取 READY，再向同一 session_id 写入 MOVIEPILOT_TERMINAL_OK 加换行，最后读取或等待到命令退出。"
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
        command=(
            "printf 'READY\\n'; IFS= read -r reply; "
            "if [ -z \"$reply\" ]; then sleep 30; "
            "else printf 'REPLY=%s\\n' \"$reply\"; fi"
        ),
        terminal_use_pty=False,
    ),
    Scenario(
        scenario_id="terminal_pty_session",
        task=(
            "请使用后台终端会话运行给定命令，并按会话状态完成一次 PTY 交互：启动时使用 PTY，"
            "先读取 READY，再向同一 session_id 写入 MOVIEPILOT_TERMINAL_OK 加换行，最后读取或等待到命令退出。"
            "不要使用 action=run，也不要执行其他命令。"
            "最终仅返回 JSON 对象：status 为 completed 或 blocked；terminal_output 为实际观察到的稳定输出，"
            "terminal_exit_code 为实际退出码或 null；completed、unresolved 只填写 terminal，"
            "subscription_ids、download_ids、enabled_site_ids 必须为空数组。"
            "如果写入后进程句柄已结束，优先采用同一命令 completed 回执中的真实 exit_code；"
            "只有没有完整确认 READY、回复行和退出码时，才将 terminal 放入 unresolved，不要编造结果。"
        ),
        media_source="",
        media_id="",
        title="",
        magnet="",
        infohash="",
        kind="terminal",
        # 回复后保留足够窗口，让模型能读取 completed 回执中的真实退出码。
        command="sleep 1; printf 'READY\\n'; IFS= read -r reply; printf 'REPLY=%s\\n' \"$reply\"; sleep 30",
        terminal_use_pty=True,
    ),
    Scenario(
        scenario_id="subagent_terminal_share",
        task=(
            "请启动后台终端会话运行给定命令，启动时使用 pipe 模式（use_pty=false），并记下 action=start 返回的 session_id。"
            "随后必须把对这个同一 session_id 的只读读取任务交给通用子代理；使用 task 或 subagent_task 时，"
            "在该任务条目中明确传入 terminal_sessions=[{session_id, actions:[read]}]。"
            "子代理只能使用 action=read 读取父终端，不能启动命令、写入输入、发送信号或终止会话。"
            "确认子代理从真实回执读到 SHARED_READY 后，主 Agent 再用同一个 session_id 读取到 SHARED_DONE 并确认退出码 0。"
            "不要执行其他命令。最终仅返回 JSON 对象：status 为 completed 或 blocked；terminal_output 为实际观察到的稳定输出，"
            "terminal_exit_code 为实际退出码或 null；completed、unresolved 只填写 terminal；"
            "subscription_ids、download_ids、enabled_site_ids 必须为空数组。"
            "没有同时确认子代理读取证据、SHARED_DONE 和退出码时，将 terminal 放入 unresolved，不要编造结果。"
        ),
        media_source="",
        media_id="",
        title="",
        magnet="",
        infohash="",
        kind="terminal",
        command="printf 'SHARED_READY\\n'; sleep 1; printf 'SHARED_DONE\\n'",
        terminal_use_pty=False,
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
