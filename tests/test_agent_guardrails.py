"""真实 LangChain 图中验证循环停止、并行顺序、轮询和原始工具结果。"""

import asyncio
import json
from types import SimpleNamespace

import pytest
from langchain.agents import create_agent as _create_agent
from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import StructuredTool
from langgraph.checkpoint.memory import MemorySaver

from app.agent.guardrails.completion import degenerate_final, trailing_continue_intent
from app.agent.guardrails.controller import ToolCallGuardrailConfig
from app.agent.middleware.guardrails import GUARDRAIL_KEY, TURN_STATUS_KEY, ToolGuardrailsMiddleware
from app.agent.middleware.output import ToolOutputMiddleware
from app.agent.middleware.policy import AgentPolicyMiddleware
from app.agent.orchestrator import MoviePilotAgent
from app.agent.policy.contracts import AuthSource, PrincipalType, ToolOrigin, ToolPolicyContext
from app.agent.tools.base import format_tool_result_for_agent
from app.agent.tools.result import TOOL_OBSERVATION_MARKER, messages_for_persistence


def create_agent(**kwargs):
    """使用生产的策略节点组合，保持循环检测不新增模型前后图节点。"""
    middleware = kwargs['middleware']
    guard = next(item for item in middleware if isinstance(item, ToolGuardrailsMiddleware))
    policy = next((item for item in middleware if isinstance(item, AgentPolicyMiddleware)), None)
    if policy is None:
        middleware.insert(0, AgentPolicyMiddleware(context=guard.context, guardrails=guard))
    else:
        policy.guardrails = guard
    return _create_agent(**kwargs)


class GuardModel(FakeMessagesListChatModel):
    """固定工具调用模型，保证测试不依赖真实服务或模型概率。"""

    def bind_tools(self, _tools, **_kwargs):
        """使用真实 ToolNode，模型仅返回预定消息。"""
        return self


def context(*, unattended=True):
    """只有宿主入口身份决定是否默认强制停止。"""
    return ToolPolicyContext(session_id='guard', user_id='alice', origin=ToolOrigin.AGENT_INTERACTIVE,
                             principal_type=PrincipalType.HUMAN,
                             auth_source=AuthSource.CHANNEL if unattended else AuthSource.WEB_SESSION,
                             agent_context={'is_admin': False})


def call(index, *names, arguments=None):
    """构造唯一调用编号但参数相同的模型工具批次。"""
    return AIMessage(content='', tool_calls=[dict(name=name, args=arguments or {}, id=f'{index}-{name}') for name in names])


@pytest.mark.parametrize('unattended, expected', [(True, 5), (False, 7)])
def test_successful_replay_stops_only_unattended_and_resets_next_turn(unattended, expected):
    """成功但无进展的普通工具也被检测，前台只提示，新轮次不继承停止。"""
    async def scenario():
        """实际执行工具，检查第六次是否被省掉，而非只测试检测器状态。"""
        executed = []

        async def probe():
            """稳定输出模拟没有取得进展的探测。"""
            executed.append(True)
            return '相同结果'

        middleware = ToolGuardrailsMiddleware(context(unattended=unattended))
        model = GuardModel(responses=[*[call(i, 'probe') for i in range(7)], AIMessage(content='结束')])
        graph = create_agent(model=model, tools=[StructuredTool.from_function(coroutine=probe, name='probe')], middleware=[middleware])
        result = await graph.ainvoke({'messages': [HumanMessage(content='执行任务')]})
        assert len(executed) == expected
        assert bool(result['messages'][-1].additional_kwargs.get(GUARDRAIL_KEY)) is unattended
        if unattended:
            assert result['messages'][-1].additional_kwargs[TURN_STATUS_KEY] == 'blocked'
            assert '尚未完成' in result['messages'][-1].content
        assert not middleware._runs
        assert not any(name.startswith('ToolGuardrailsMiddleware.') for name in graph.get_graph().nodes)
        model.responses = [*[call(i + 20, 'probe') for i in range(7)], AIMessage(content='再次结束')]
        model.i = 0
        executed.clear()
        again = await graph.ainvoke({'messages': [HumanMessage(content='重新核验')]})
        assert len(executed) == expected
        assert bool(again['messages'][-1].additional_kwargs.get(GUARDRAIL_KEY)) is unattended
        assert not middleware._runs

    asyncio.run(scenario())


def test_parallel_two_call_cycles_follow_emission_order():
    """工具完成次序交错时仍按模型声明顺序检测 A/B 循环。"""
    async def scenario():
        """A 在偶数轮较慢，B 在奇数轮较慢，结果始终相同。"""
        counts = {'a': 0, 'b': 0}

        async def a():
            """返回稳定 A 结果，并交替让出事件循环。"""
            counts['a'] += 1
            if counts['a'] % 2:
                await asyncio.sleep(.002)
            return 'A'

        async def b():
            """返回稳定 B 结果，与 A 的完成顺序交替。"""
            counts['b'] += 1
            if not counts['b'] % 2:
                await asyncio.sleep(.002)
            return 'B'

        middleware = ToolGuardrailsMiddleware(context())
        graph = create_agent(model=GuardModel(responses=[call(i, 'a', 'b') for i in range(8)]),
                             tools=[StructuredTool.from_function(coroutine=a), StructuredTool.from_function(coroutine=b)],
                             middleware=[middleware])
        result = await graph.ainvoke({'messages': [HumanMessage(content='比较')]})
        assert counts == {'a': 5, 'b': 5}
        assert result['messages'][-1].additional_kwargs[GUARDRAIL_KEY]['code'] == 'identical_cycle_halt'

    asyncio.run(scenario())


def test_polling_is_exempt_but_still_executes_and_deduplicates_fresh_results():
    """正常等待不会被当成循环，每次仍执行；较长重复回执仅压缩上下文表示。"""
    async def scenario():
        """等待七次之后得到完成结果，状态变化必须使正文重新完整出现。"""
        executed = []

        async def execute_command(action: str):
            """模拟真实终端等待协议，不启动任何进程。"""
            executed.append(action)
            return json.dumps(dict(execution_outcome='pending' if len(executed) < 7 else 'succeeded', output='x' * 1000))

        middleware = ToolGuardrailsMiddleware(context())
        graph = create_agent(model=GuardModel(responses=[*[call(i, 'execute_command', arguments={'action': 'wait'}) for i in range(7)], AIMessage(content='结束')]),
                             tools=[StructuredTool.from_function(coroutine=execute_command)], middleware=[middleware])
        result = await graph.ainvoke({'messages': [HumanMessage(content='等待')]})
        outputs = [message for message in result['messages'] if isinstance(message, ToolMessage)]
        assert len(executed) == 7 and middleware.controller.halt_decision is None
        assert 'byte-identical' in outputs[1].content and 'pending' in outputs[1].content
        assert json.loads(outputs[-1].content)['execution_outcome'] == 'succeeded'

    asyncio.run(scenario())


def test_raw_result_changes_survive_identical_truncated_previews():
    """只在被截掉中部发生的真实结果变化也必须清除重复判断。"""
    async def scenario():
        """截断原文记录器与循环观察器组合，确认没有把相同预览当完整结果。"""
        count = 0

        async def read_file():
            """生成中段变化的长文件。"""
            nonlocal count
            count += 1
            return format_tool_result_for_agent(json.dumps({'content': 'a' * 80000 + str(count) + 'b' * 80000}), tool_name='read_file')

        policy = context()
        guard = ToolGuardrailsMiddleware(policy)
        graph = create_agent(model=GuardModel(responses=[*[call(i, 'read_file') for i in range(7)], AIMessage(content='完成')]),
                             tools=[StructuredTool.from_function(coroutine=read_file)], middleware=[ToolOutputMiddleware(policy), guard])
        result = await graph.ainvoke({'messages': [HumanMessage(content='连续读取')]}, {'configurable': {'thread_id': 'guard'}})
        assert count == 7 and guard.controller.halt_decision is None
        previews = [json.loads(message.content)['content_preview'] for message in result['messages'] if isinstance(message, ToolMessage)]
        assert len(set(previews)) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize('tool_name', ['web_search', 'search_web'])
def test_evicted_reference_uses_fresh_full_result_and_cap_blocks_before_execution(tool_name):
    """压缩后不产生悬空引用；总调用上限在实际调用之前执行。"""
    async def scenario():
        """直接使用真实 ToolCallRequest，隔离验证存根恢复和调用预算。"""
        guard = ToolGuardrailsMiddleware(context(unattended=False))
        guard.controller.config = ToolCallGuardrailConfig(loop_caps=SimpleNamespace(max_web_searches=2, max_subagents=50))
        executed = []

        async def handler(request):
            """计数实际执行，第三次必须无法到达这里。"""
            executed.append(True)
            return ToolMessage(content='x' * 700, tool_call_id=request.tool_call['id'], name='web_search', id=request.tool_call['id'])

        outputs = []
        for index in range(3):
            request = ToolCallRequest(tool_call=call(index, tool_name).tool_calls[0], tool=None, state={}, runtime=None)
            message = await guard.awrap_tool_call(request, handler)
            result = await guard.prepare_model({'messages': [call(index, tool_name), message]}, None)
            outputs.append(result['messages'][-1])
        assert len(executed) == 2
        assert outputs[1].content.startswith('x' * 700)
        assert outputs[-1].additional_kwargs[GUARDRAIL_KEY]['code'] == 'loop_web_search_cap'

    asyncio.run(scenario())


def test_moviepilot_file_edit_restarts_failed_terminal_experiment():
    """宿主 edit_file 名称映射到 patch 修改类别，真实修改后重试不能累积成原样失败循环。"""
    async def scenario():
        """七次失败之间每次有不同修复，最终正常返回，不在第五次失败处截断。"""
        attempts = []

        async def execute_command(command: str):
            """模拟未通过的验证命令。"""
            attempts.append(command)
            return json.dumps({'execution_outcome': 'failed', 'exit_code': 1})

        async def edit_file(content: str):
            """模拟已经落盘的不同修复。"""
            attempts.append(content)
            return json.dumps({'success': True})

        responses = []
        for index in range(7):
            responses.extend([call(index, 'execute_command', arguments={'command': 'pytest'}),
                              call(index, 'edit_file', arguments={'content': str(index)})])
        graph = create_agent(model=GuardModel(responses=[*responses, AIMessage(content='已完成诊断')]),
                             tools=[StructuredTool.from_function(coroutine=execute_command), StructuredTool.from_function(coroutine=edit_file)],
                             middleware=[ToolGuardrailsMiddleware(context())])
        result = await graph.ainvoke({'messages': [HumanMessage(content='修复并验证')]}, {'recursion_limit': 200})
        assert len(attempts) == 14 and result['messages'][-1].content == '已完成诊断'

    asyncio.run(scenario())


def test_parallel_graph_runs_do_not_share_counters_or_pending_results():
    """子代理可共享图中间件，但两个真实 thread_id 各执行五次后独立停止。"""
    async def scenario():
        """同一中间件及相同工具调用 ID 用于两个并行运行，验证作用域来自宿主。"""
        count = 0

        async def probe():
            """交错执行，制造两个图同时等待的窗口。"""
            nonlocal count
            count += 1
            await asyncio.sleep(.001)
            return 'same'

        guard = ToolGuardrailsMiddleware(context())
        tool = StructuredTool.from_function(coroutine=probe)
        graphs = [create_agent(model=GuardModel(responses=[call(i, 'probe') for i in range(8)]),
                               tools=[tool], middleware=[guard]) for _ in range(2)]
        results = await asyncio.gather(*[graph.ainvoke({'messages': [HumanMessage(content='任务')]},
                                                      {'configurable': {'thread_id': str(index)}})
                                        for index, graph in enumerate(graphs)])
        assert count == 10
        assert all(result['messages'][-1].additional_kwargs[TURN_STATUS_KEY] == 'blocked' for result in results)
        assert not guard._runs

    asyncio.run(scenario())


def test_outer_policy_exception_receipts_remain_unknown_when_halted():
    """检测也覆盖策略层转换的异常，超时可能已经发生的副作用不能写成成功。"""
    async def scenario():
        """工具在循环中抛出超时，外层真实策略生成未知回执。"""
        attempts = []

        async def probe():
            """不访问外部服务，仅模拟结果未确认。"""
            attempts.append(True)
            raise TimeoutError('unconfirmed')

        policy = context()
        graph = create_agent(model=GuardModel(responses=[call(i, 'probe') for i in range(8)]),
                             tools=[StructuredTool.from_function(coroutine=probe)],
                             middleware=[AgentPolicyMiddleware(context=policy), ToolGuardrailsMiddleware(policy)])
        result = await graph.ainvoke({'messages': [HumanMessage(content='执行')]})
        assert len(attempts) == 5
        assert result['messages'][-1].additional_kwargs[TURN_STATUS_KEY] == 'unknown'

    asyncio.run(scenario())


@pytest.mark.parametrize('text', ['Let me now inspect the file.', '接下来我会检查文件。'])
def test_announced_action_gets_only_two_nudges_and_not_fake_user_history(text):
    """英文和中文尾部动作使用同一有界继续机制，模型始终不行动时明确部分完成。"""
    async def scenario():
        """始终只返回下一步计划，确保不会无限追加模型调用。"""
        async def probe():
            """不会实际被调用的可用工具。"""
            raise AssertionError('unexpected execution')

        graph = create_agent(model=GuardModel(responses=[AIMessage(content=text, id=f'ack-{index}') for index in range(3)]),
                             tools=[StructuredTool.from_function(coroutine=probe)], middleware=[ToolGuardrailsMiddleware(context())])
        result = await graph.ainvoke({'messages': [HumanMessage(content='检查文件')]})
        messages = result['messages']
        assert sum(bool(message.additional_kwargs.get(TOOL_OBSERVATION_MARKER)) for message in messages) == 2
        assert messages[-1].additional_kwargs[TURN_STATUS_KEY] == 'partial'
        assert len([message for message in messages if isinstance(message, AIMessage)]) == 3
        assert sum(isinstance(message, HumanMessage) for message in messages_for_persistence(messages)) == 1

    asyncio.run(scenario())


def test_continuation_recovers_to_actual_work_and_normal_answer():
    """一次纠偏后实际调用工具并给出答案，不把正常完成回复继续追问。"""
    async def scenario():
        """真实图走过承诺、继续提示、工具执行及最终答案四种节点。"""
        attempts = []

        async def probe():
            """记录已实际执行的核验。"""
            attempts.append(True)
            return 'observed'

        graph = create_agent(model=GuardModel(responses=[AIMessage(content='Let me now check the file.'), call(1, 'probe'), AIMessage(content='已核验。')]),
                             tools=[StructuredTool.from_function(coroutine=probe)], middleware=[ToolGuardrailsMiddleware(context())])
        result = await graph.ainvoke({'messages': [HumanMessage(content='检查文件')]})
        assert attempts == [True] and result['messages'][-1].content == '已核验。'
        assert GUARDRAIL_KEY not in result['messages'][-1].additional_kwargs

    asyncio.run(scenario())


def test_short_answers_and_nonterminal_plans_do_not_trigger_continuation():
    """保留尾部动作的窄匹配边界，数字、正常中文短答、已交付内容和礼貌提议都不触发。"""
    for text in ('42', 'SQLite', 'report.csv', '你好。', 'Done.', ':8080', 'Let me now check. The answer is 42.', "I'll help if needed."):
        assert not trailing_continue_intent(text)
        assert not degenerate_final(text, '正常中文问题')
    assert not trailing_continue_intent('x' * 400 + 'Let me now check.')
    assert not degenerate_final('是', '是否完成')
    assert degenerate_final('?warming up', 'Check the service')


def test_stream_delivers_host_generated_guardrail_stop():
    """没有模型 token 的停止说明也会进入真实流式回调，不能只存到 checkpoint。"""
    async def scenario():
        """执行五轮无进展后由宿主结束，通过 MoviePilot 流式入口读取说明。"""
        async def probe():
            """返回稳定读取结果。"""
            return 'same'

        graph = create_agent(model=GuardModel(responses=[call(index, 'probe') for index in range(8)]),
                             tools=[StructuredTool.from_function(coroutine=probe)], middleware=[ToolGuardrailsMiddleware(context())],
                             checkpointer=MemorySaver())
        tokens = []
        await MoviePilotAgent._stream_agent_tokens(graph, {'messages': [HumanMessage(content='任务')]},
                                                  {'configurable': {'thread_id': 'stream-guard'}}, tokens.append)
        assert ''.join(tokens).count('本轮未完成：') == 1
        assert '任务尚未完成' in ''.join(tokens)

    asyncio.run(scenario())


def test_duplicate_preview_references_latest_live_output_after_eviction():
    """重复工具仍执行，超过八项输出缓存容量后存根仍引用最新可读正文。"""
    async def scenario():
        """执行十二次相同长结果，检查最后引用仍在宿主缓存中。"""
        calls = []

        async def read_file():
            """读取相同长文本以触发存根而非跳过真实读取。"""
            calls.append(True)
            return format_tool_result_for_agent('x' * 100000, tool_name='read_file')

        policy = context(unattended=False)
        output = ToolOutputMiddleware(policy)
        graph = create_agent(model=GuardModel(responses=[*[call(index, 'read_file') for index in range(12)], AIMessage(content='已读')]),
                             tools=[StructuredTool.from_function(coroutine=read_file)], middleware=[output, ToolGuardrailsMiddleware(policy)])
        result = await graph.ainvoke({'messages': [HumanMessage(content='读取')]}, {'configurable': {'thread_id': 'eviction'}})
        last = next(message for message in reversed(result['messages']) if isinstance(message, ToolMessage))
        assert len(calls) == 12
        assert 'byte-identical' in last.content
        assert any(identifier in last.content for identifier in output._results), (last.content, list(output._results))

    asyncio.run(scenario())
