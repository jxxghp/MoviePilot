"""用确定性模型核对隔离复盘、缓存前缀、执行白名单与生命周期。"""

import asyncio
import json
from copy import deepcopy
from typing import Any

import pytest
from langchain.agents import create_agent
from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import StructuredTool
from pydantic import Field

from app.agent.learning.review import ReviewLoop, ReviewSnapshot
from app.agent.learning.session import LearningSession
from app.agent.middleware.learning import LearningCaptureMiddleware, LearningMiddleware
from app.agent.middleware.memory import MemoryMiddleware
from app.agent.middleware.skills import SKILLS_SYSTEM_PROMPT
from app.agent.prompt import PromptManager
from app.runtime.tasks import TaskRegistry


class ReviewModel(BaseChatModel):
    """固定响应模型记录实际绑定 schema 和消息，测试不发任何网络请求。"""

    responses: list[AIMessage] = Field(default_factory=list)
    requests: list[Any] = Field(default_factory=list)
    index: int = 0

    @property
    def _llm_type(self):
        """返回测试模型类型。"""
        return 'learning-test'

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        """保持真实 LangChain 工具绑定路径，记录双方广告 schema。"""
        from langchain_core.utils.function_calling import convert_to_openai_tool
        return self.bind(tools=[convert_to_openai_tool(tool) for tool in tools], tool_choice=tool_choice, **kwargs)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        """同步假模型只返回预录制工具调用及 usage。"""
        del stop, run_manager
        self.requests.append((deepcopy(messages), deepcopy(kwargs)))
        message = self.responses[min(self.index, len(self.responses) - 1)].model_copy(deep=True)
        self.index += 1
        message.usage_metadata = dict(input_tokens=100, output_tokens=10, total_tokens=110)
        return ChatResult(generations=[ChatGeneration(message=message)])


def session(tmp_path, on_usage=lambda _usage: None):
    """为测试构造隔离目录中的已认证用户会话。"""
    return LearningSession(session_id='alice-session', skill_root=tmp_path / 'skills', memory_root=tmp_path / 'memory',
                           public_roots=(), on_usage=on_usage)


def response(name, arguments, identifier='call'):
    """生成供应商返回形态的工具调用。"""
    return AIMessage(content='', tool_calls=[dict(name=name, args=arguments, id=identifier)])


def test_review_keeps_prefix_schema_and_denies_business_tools(tmp_path):
    """广告保持一致，派发不会执行业务写；记忆成果来自新工具回执且主快照不变。"""
    async def scenario():
        """完整跑过复盘的拒绝、合法记忆写及最终响应。"""
        usage = []
        owner = session(tmp_path, usage.append)
        called = []

        async def forbidden():
            """任何误派发都会在断言中暴露。"""
            called.append(True)
            return '业务已执行'

        tool = StructuredTool.from_function(coroutine=forbidden, name='danger', description='business write')
        model = ReviewModel(responses=[response('danger', {}), response('memory', dict(action='add', content='验证过的环境事实'), 'save'), AIMessage(content='done')])
        original = [HumanMessage(content='任务'), AIMessage(content='任务已完成')]
        system = PromptManager().get_agent_prompt('webagent') + SKILLS_SYSTEM_PROMPT.format(skills_list='(No skills available yet.)')
        request = ModelRequest(model=model, messages=original[:-1], system_message=SystemMessage(content=system), tools=[*owner.tools.tools, tool])
        snapshot = ReviewSnapshot.capture(request, ModelResponse(result=original[-1:]))
        loop = ReviewLoop(snapshot, owner.tools, review_memory=True, review_skills=False, cancelled=lambda: False, on_usage=usage.append)
        result = await loop.run()
        assert not called
        assert snapshot.messages == original and len(original) == 2
        assert len(result) == 1 and result[0]['tool'] == 'memory'
        assert len(usage) == 3 and loop.consumed == 300
        sent, arguments = model.requests[0]
        assert sent[:3] == [SystemMessage(content=system), *original]
        assert 'host-started background learning review' in sent[0].content
        assert 'This review permits memory maintenance only' in sent[-1].content
        assert 'Do not create or rewrite skills unless' not in sent[0].content
        assert arguments['tools'] == snapshot.schemas
        assert (tmp_path / 'memory/MEMORY.md').read_text() == '验证过的环境事实'
        assert '不能执行' in model.requests[1][0][-1].content

    asyncio.run(scenario())


@pytest.mark.parametrize('review_memory,review_skills,scope', [
    (True, False, 'memory maintenance only'),
    (False, True, 'personal skill maintenance only'),
    (True, True, 'memory and personal skill maintenance'),
])
def test_review_without_new_evidence_is_a_noop(tmp_path, review_memory, review_skills, scope):
    """三种复盘均允许无成果结束，传入的提示词明确维护范围且不要求凑学习记录。"""
    async def scenario():
        """检查实际发送的复盘指令及无工具写入的终态。"""
        owner = session(tmp_path)
        model = ReviewModel(responses=[AIMessage(content='Nothing to save.')])
        snapshot = ReviewSnapshot.capture(
            ModelRequest(model=model, messages=[], tools=owner.tools.tools),
            ModelResponse(result=[AIMessage(content='常规查询已完成')]),
        )
        loop = ReviewLoop(snapshot, owner.tools, review_memory=review_memory,
                          review_skills=review_skills, cancelled=lambda: False, on_usage=lambda _: None)

        assert await loop.run() == []
        assert len(model.requests) == 1
        prompt = model.requests[0][0][-1].content
        assert scope in prompt
        assert 'There is no update quota' in prompt
        assert 'A no-op is a successful review' in prompt
        assert 'most sessions produce at least one skill update' not in prompt
        assert not list(tmp_path.rglob('*.md'))

    asyncio.run(scenario())


def test_real_graph_capture_is_final_and_does_not_share_state(tmp_path):
    """真实 LangChain 图的返回快照包含动态 system 和最终回复，复盘改动不回写图。"""
    async def scenario():
        """构造真实图并验证实例解耦。"""
        owner = session(tmp_path)
        model = ReviewModel(responses=[AIMessage(content='完成')])
        graph = create_agent(model=model, middleware=[LearningMiddleware(owner), LearningCaptureMiddleware(owner)], system_prompt='基础')
        result = await graph.ainvoke({'messages': [HumanMessage(content='请求')]})
        assert owner.snapshot.messages[-1].content == '完成'
        assert '<personal_skills>' in owner.snapshot.system.text
        owner.snapshot.messages[-1].content = '仅复盘副本'
        assert result['messages'][-1].content == '完成'
        assert owner.snapshot.model is not model

    asyncio.run(scenario())


def test_delivered_graph_review_is_consumed_by_next_user_session(tmp_path, monkeypatch):
    """真实工具迭代触发隔离复盘，落盘成果由下一会话读取且不会泄漏给另一用户。"""
    async def scenario():
        """只脚本化模型决定；图、计数、复盘、存储与下一轮加载都使用实际实现。"""
        registry = TaskRegistry()
        monkeypatch.setattr('app.agent.learning.session.get_task_registry', lambda: registry)
        owner = session(tmp_path / 'alice')
        fact = '订阅记录应按媒体来源与媒体编号核对。'
        document = '---\nname: subscription-check\ndescription: 核对订阅记录\n---\n读取全部分页，按媒体来源和编号核对。\n'
        observed = []

        async def inspect_subscription(index: int) -> str:
            """返回隔离业务快照，十次真实工具回合不能直接伪造计数。"""
            observed.append(index)
            return json.dumps({'success': True, 'media_source': 'themoviedb', 'media_id': str(index)})

        model = ReviewModel(responses=[
            *[response('inspect_subscription', {'index': index}, f'inspect-{index}') for index in range(10)],
            AIMessage(content='已核对十条记录。'),
            response('memory', {'action': 'add', 'content': fact}, 'save-memory'),
            response('skill_manage', {'operations': [{'action': 'create', 'name': 'subscription-check',
                                                      'content': document}]}, 'save-skill'),
            AIMessage(content='复盘完成。'),
        ])
        messages = [HumanMessage(content=f'请核对记录 {index}', id=f'user-{index}') for index in range(10)]
        await owner.begin(messages)
        graph = create_agent(model=model, tools=[StructuredTool.from_function(coroutine=inspect_subscription)],
                             middleware=[LearningMiddleware(owner), LearningCaptureMiddleware(owner)])
        result = await graph.ainvoke({'messages': messages}, {'recursion_limit': 100})
        before = deepcopy(result['messages'])
        assert observed == list(range(10)) and owner.iterations == 10 and owner.memory_due
        owner.finish(delivered=True)
        assert owner.run and owner.run.task
        await owner.run.task
        assert result['messages'] == before
        assert [action['tool'] for action in owner.last_actions] == ['memory', 'skill_manage']
        assert (owner.memory_root / 'MEMORY.md').read_text() == fact
        assert (owner.skill_root / 'subscription-check' / 'SKILL.md').read_text() == document

        for username in ('alice', 'bob'):
            following = session(tmp_path / username)
            responses = ([response('skill_view', {'name': 'subscription-check'}, 'load-skill')]
                         if username == 'alice' else [])
            next_model = ReviewModel(responses=[*responses, AIMessage(content='下一任务')])
            next_graph = create_agent(model=next_model, middleware=[
                MemoryMiddleware(memory_dir=str(tmp_path / 'public'), user_memory_dir=str(following.memory_root),
                                 store=following.tools.memory),
                LearningMiddleware(following), LearningCaptureMiddleware(following),
            ])
            await next_graph.ainvoke({'messages': [HumanMessage(content='再次核对订阅')]})
            sent = '\n'.join(message.text for request, _kwargs in next_model.requests for message in request)
            assert (fact in sent) == (username == 'alice')
            assert ('subscription-check' in sent) == (username == 'alice')
            assert ('读取全部分页，按媒体来源和编号核对。' in sent) == (username == 'alice')
        assert await registry.shutdown()

    asyncio.run(scenario())


def test_budget_iteration_limit_and_skill_only_memory_gate(tmp_path):
    """重复拒绝也消耗迭代预算，技能复盘不能借广告中的 memory 自动改记忆。"""
    async def scenario():
        """使用每次都发 memory 的假模型触达两种独立预算。"""
        owner = session(tmp_path)
        model = ReviewModel(responses=[response('memory', dict(action='add', content='不应写入'))])
        snapshot = ReviewSnapshot.capture(ModelRequest(model=model, messages=[], tools=owner.tools.tools), ModelResponse(result=[AIMessage(content='任务结束')]))
        loop = ReviewLoop(snapshot, owner.tools, review_memory=False, review_skills=True, cancelled=lambda: False, on_usage=lambda _: None)
        assert await loop.run() == []
        assert len(model.requests) == 16
        assert not (tmp_path / 'memory/MEMORY.md').exists()
        loop = ReviewLoop(snapshot, owner.tools, review_memory=False, review_skills=True, cancelled=lambda: False, on_usage=lambda _: None)
        loop.budget = 150
        await loop.run()
        assert loop.consumed == 200
        assert len(model.requests) == 18

    asyncio.run(scenario())


def test_cancel_before_start_clears_owner_and_no_unrequested_review(tmp_path, monkeypatch):
    """阈值、完成状态和取消前封门都由宿主判定，取消未启动任务不留下 owner。"""
    async def scenario():
        """使用真实 TaskRegistry 观察取消与回调清理。"""
        owner = session(tmp_path)
        registry = TaskRegistry()
        monkeypatch.setattr('app.agent.learning.session.get_task_registry', lambda: registry)
        model = ReviewModel(responses=[AIMessage(content='Nothing to save')])
        snapshot = ReviewSnapshot.capture(ModelRequest(model=model, messages=[], tools=owner.tools.tools), ModelResponse(result=[AIMessage(content='完成')]))
        owner.snapshot = snapshot
        owner.finish(delivered=True)
        assert owner.run is None
        owner.iterations = 10
        owner.finish(delivered=False)
        assert owner.run is None
        owner.finish(delivered=True)
        run = owner.run
        assert run is not None and len(registry.records) == 1
        await owner.wait_cancelled()
        await asyncio.sleep(0)
        assert run.cancelled.is_set() and owner.run is None
        assert not model.requests
        assert await registry.shutdown()

    asyncio.run(scenario())


def test_memory_interval_resume_counter_and_native_tools_skip(tmp_path):
    """恢复轮次按历史用户数初始化；技能维护重置工具计数，原生工具不冒充可控派发。"""
    async def scenario():
        """核对记忆和技能复盘的默认触发计数边界。"""
        owner = session(tmp_path)
        await owner.begin([HumanMessage(content=str(index)) for index in range(10)])
        assert owner.memory_due
        model = ReviewModel(responses=[AIMessage(content='done')])
        request = ModelRequest(model=model, messages=[], tools=owner.tools.tools)
        owner.iterations = 9
        owner.observe(request, ModelResponse(result=[response('skills_list', {})]))
        assert owner.iterations == 10
        owner.observe(request, ModelResponse(result=[response('skill_manage', {})]))
        assert owner.iterations == 0
        assert ReviewSnapshot.capture(request.override(tools=[{'type': 'web_search_preview'}]), ModelResponse(result=[AIMessage(content='done')])) is None
        await owner.begin([HumanMessage(content='下一轮')])
        assert not owner.memory_due

    asyncio.run(scenario())


def test_control_commands_are_host_only_and_reject_quoted_requests(tmp_path):
    """确认入口不在工具 schema 中；只有完整用户命令可以应用指定提案。"""
    async def scenario():
        """模拟后台提出删除及前台用户的拒绝/确认过程。"""
        from app.agent.learning.memory import MemoryStore
        from app.agent.learning.schema import MemoryInput
        owner = session(tmp_path)
        owner.tools.memory.manage(MemoryInput(action='add', content='旧规则'))
        proposal = MemoryStore(owner.memory_root, background=True).manage(MemoryInput(action='replace', old_text='旧规则', content='新规则'))
        command = f"/memory approve {proposal['pending_id']}"
        assert await owner.command('模型建议：' + command) is None
        assert (owner.memory_root / 'MEMORY.md').read_text() == '旧规则'
        assert not any(tool.name in {'approve', 'resolve'} for tool in owner.tools.tools)
        assert json.loads(await owner.command(command))['approved']
        assert (owner.memory_root / 'MEMORY.md').read_text() == '新规则'

    asyncio.run(scenario())


def test_review_cancellation_fences_running_model_and_preserves_parent(tmp_path, monkeypatch):
    """模型请求在运行时取消，任务退出后不会发生学习写入或覆盖主快照。"""
    async def scenario():
        """借真实 Registry 发起可取消模型等待，观察终态与存储。"""
        registry = TaskRegistry()
        monkeypatch.setattr('app.agent.learning.session.get_task_registry', lambda: registry)
        started = asyncio.Event()
        owner = session(tmp_path)
        model = ReviewModel(responses=[AIMessage(content='done')])
        owner.snapshot = ReviewSnapshot.capture(ModelRequest(model=model, messages=[], tools=owner.tools.tools), ModelResponse(result=[AIMessage(content='主回复')]))
        before = deepcopy(owner.snapshot.messages)

        async def block(_self):
            """等待取消前不产生副作用。"""
            started.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(ReviewLoop, 'run', block)
        owner.memory_due = True
        owner.finish(delivered=True)
        await asyncio.wait_for(started.wait(), timeout=1)
        assert await owner.wait_cancelled()
        await asyncio.sleep(0)
        assert owner.run is None and owner.snapshot.messages == before
        assert not (owner.memory_root / 'MEMORY.md').exists()
        assert await registry.shutdown()

    asyncio.run(scenario())
