"""沿用 Hermes skill_manage 的按动作区分参数形态，避免正文写进错误字段。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class SkillOperation(BaseModel):  # type: ignore[misc]
    """技能身份由名称选择，个人根与后台身份始终由宿主绑定。"""

    model_config = ConfigDict(extra='forbid')
    name: str = Field(pattern=r'^[a-z0-9][a-z0-9_-]{0,63}$')


class CreateSkill(SkillOperation):
    """新建完整大类技能，正文必须含可加载的 YAML 前言。"""

    action: Literal['create']
    content: str = Field(min_length=1, max_length=100_000)
    category: str | None = Field(default=None, pattern=r'^[a-z0-9][a-z0-9_-]{0,63}$')


class PatchSkill(SkillOperation):
    """局部修改必须提供旧片段；允许明确替换所有命中。"""

    action: Literal['patch']
    old_string: str = Field(min_length=1, max_length=100_000)
    new_string: str = Field(max_length=100_000)
    file_path: str | None = Field(default=None, max_length=255)
    replace_all: bool = False


class RewriteSkill(SkillOperation):
    """Hermes 兼容的完整重写形态；不能与局部修改字段混用。"""

    action: Literal['patch']
    content: str = Field(min_length=1, max_length=100_000)


class WriteSkillFile(SkillOperation):
    """写入技能支持文件；不能借此更新所有权或绕过主文档校验。"""

    action: Literal['write_file']
    file_path: str = Field(min_length=1, max_length=255)
    file_content: str = Field(max_length=100_000)


class RemoveSkillFile(SkillOperation):
    """删除精确支持文件，不能递归删除目录。"""

    action: Literal['remove_file']
    file_path: str = Field(min_length=1, max_length=255)


class DeleteSkill(SkillOperation):
    """整项删除必须独占批次；合并目标可作为追溯依据。"""

    action: Literal['delete']
    absorbed_into: str | None = Field(default=None, pattern=r'^(?:[a-z0-9][a-z0-9_-]{0,63})?$')


class ManageSkillsInput(BaseModel):  # type: ignore[misc]
    """最多二十个有序操作，任一失败回滚本次全部技能变更。"""

    model_config = ConfigDict(extra='forbid')
    operations: list[CreateSkill | PatchSkill | RewriteSkill | WriteSkillFile | RemoveSkillFile | DeleteSkill] = Field(
        min_length=1, max_length=20,
    )


class ViewSkillInput(BaseModel):  # type: ignore[misc]
    """读取具体技能或支持文件，并登记本轮实际读取的版本。"""

    model_config = ConfigDict(extra='forbid')
    name: str = Field(pattern=r'^[a-z0-9][a-z0-9_-]{0,63}$')
    file_path: str = Field(default='SKILL.md', max_length=255)


class MemoryOperation(BaseModel):  # type: ignore[misc]
    """old_text 只定位整条记忆，replace 的 content 会替换整条而非子串。"""

    model_config = ConfigDict(extra='forbid')
    action: Literal['add', 'replace', 'remove']
    content: str | None = Field(default=None, max_length=100_000)
    old_text: str | None = Field(default=None, max_length=100_000)
    new_text: str | None = Field(default=None, max_length=100_000)


class MemoryInput(BaseModel):  # type: ignore[misc]
    """沿用 Hermes 单操作及原子批次形态，pending 只读列出等待人工处理的提案。"""

    model_config = ConfigDict(extra='forbid')
    action: Literal['add', 'replace', 'remove', 'pending'] | None = None
    target: Literal['memory', 'user'] = 'memory'
    content: str | None = Field(default=None, max_length=100_000)
    old_text: str | None = Field(default=None, max_length=100_000)
    new_text: str | None = Field(default=None, max_length=100_000)
    operations: list[MemoryOperation] | None = Field(default=None, min_length=1, max_length=20)
