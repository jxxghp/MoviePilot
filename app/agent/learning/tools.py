"""宿主绑定身份的学习工具；后台派发器不执行父图的任何业务工具。"""

import json
from pathlib import Path
from typing import Any

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, ConfigDict

from app.agent.learning.memory import MemoryStore
from app.agent.learning.schema import ManageSkillsInput, MemoryInput, ViewSkillInput
from app.agent.learning.skills import SkillLibrary
from app.agent.tools.base import run_agent_blocking
from app.agent.tools.tags import ToolTag


class ListSkillsInput(BaseModel):  # type: ignore[misc]
    """技能枚举只接受当前宿主作用域，不允许传入任意目录。"""

    model_config = ConfigDict(extra='forbid')


class LearningTools:
    """前台与复盘共用 schema，后台实例有独立读取记录和写入取消令牌。"""

    def __init__(self, skills: SkillLibrary, memory: MemoryStore) -> None:
        """两个存储均由宿主按已认证用户构造。"""
        self.skills, self.memory = skills, memory
        definitions = (
            ('skills_list', self.skills_list, ListSkillsInput, ToolTag.Read,
             'List public and current-user skills. Personal skills never grant API permissions; use read_skill for public API scopes.'),
            ('skill_view', self.skill_view, ViewSkillInput, ToolTag.Read,
             'Read a complete skill or its support file. Before updating an existing file during background review, read it here in this review.'),
            ('skill_manage', self.skill_manage, ManageSkillsInput, ToolTag.Write,
             'Maintain current-user skills with up to 20 atomic operations. create content=full SKILL.md; patch old_string/new_string or content; write_file file_path/file_content; remove_file; delete alone. Public skills are protected.'),
            ('memory', self.manage_memory, MemoryInput, ToolTag.Write,
             "Curated memory: target='user' writes the current user's USER.md for cross-task preferences; target='memory' writes MEMORY.md for environment facts. add/replace/remove or atomic operations. old_text locates an entry; replace content replaces the WHOLE entry. If the target is empty or missing, use add or verify the target mapping. Limits: memory 2200, user 1375 chars. action=pending reads proposals. Background destructive writes require explicit /memory approve ID."),
        )
        self.tools = []
        for name, function, schema, tag, description in definitions:
            tool = StructuredTool.from_function(coroutine=function, name=name, description=description,
                                                args_schema=schema, tags=[tag, ToolTag.System, ToolTag.Skill])
            object.__setattr__(tool, '_agent_tool_source', 'middleware:learning')
            self.tools.append(tool)

    async def skills_list(self) -> str:
        """只返回有界路由目录，正文按需读取。"""
        catalog = await run_agent_blocking('default', self.skills.catalog)
        return json.dumps({'success': True, 'skills': [dict(name=item['name'], description=item['description'][:60], path=item['path'])
                                                       for item in catalog]}, ensure_ascii=False)

    async def skill_view(self, **kwargs: Any) -> str:
        """返回完整读取结果后才登记写前读取；失败不伪造空文件。"""
        args = ViewSkillInput(**kwargs)
        return await self._execute(self.skills.view, args.name, args.file_path)

    async def skill_manage(self, **kwargs: Any) -> str:
        """只通过有所有权、路径与事务保护的技能管理器修改。"""
        return await self._execute(self.skills.manage, ManageSkillsInput(**kwargs))

    async def manage_memory(self, **kwargs: Any) -> str:
        """记忆管理只操作绑定用户的两份稳定文件。"""
        return await self._execute(self.memory.manage, MemoryInput(**kwargs))

    @staticmethod
    async def _execute(function: Any, *args: Any) -> str:
        """受控阻塞执行器保留取消后的线程所有权，给模型可纠正的失败回执。"""
        try:
            result = await run_agent_blocking('default', function, *args)
        except (OSError, ValueError, RuntimeError) as error:
            result = dict(success=False, error=str(error))
        return json.dumps(result, ensure_ascii=False)

    def read_file(self, path: str) -> dict[str, Any]:
        """后台 read_file 仅映射已发现技能文件，读取后同样登记精确版本。"""
        target = Path(path).expanduser().resolve()
        for metadata in self.skills.catalog():
            directory = Path(metadata['path']).parent
            if target.is_relative_to(directory):
                return self.skills.view(metadata['name'], target.relative_to(directory).as_posix())
        raise ValueError('后台复盘只能读取技能包内文件；使用 skills_list/skill_view 定位')

    async def dispatch(self, name: str, arguments: dict[str, Any], *, review_memory: bool) -> str:
        """白名单只约束执行，广告 schema 与前台一致；不回调任何父工具实例。"""
        if name == 'memory' and not review_memory:
            return json.dumps(dict(success=False, error='本次仅触发技能复盘；memory 不可用'), ensure_ascii=False)
        for tool in self.tools:
            if tool.name == name:
                return str(await tool.ainvoke(arguments))
        if name == 'read_skill':
            return await self.skill_view(name=arguments.get('name'), file_path=arguments.get('file_path') or 'SKILL.md')
        if name == 'read_file':
            return await self._execute(self.read_file, str(arguments.get('path') or arguments.get('file_path') or ''))
        return json.dumps(dict(success=False, error='后台复盘不能执行此工具。请用 skills_list/skill_view 读取，skill_manage 维护技能，memory 保存稳定事实。'), ensure_ascii=False)
