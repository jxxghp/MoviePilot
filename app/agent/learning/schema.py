"""
skill_manage 按动作区分参数形态，避免正文写进错误字段。
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class SkillOperation(BaseModel):
    """
    技能身份由名称选择，个人根与后台身份始终由宿主绑定。
    """

    model_config = ConfigDict(extra='forbid')
    name: str = Field(pattern=r'^[a-z0-9][a-z0-9_-]{0,63}$', description="技能名称，须以小写字母或数字开头，后续仅允许小写字母、数字、下划线或连字符，最多 64 字符")


class CreateSkill(SkillOperation):
    """
    新建完整大类技能，正文必须含可加载的 YAML 前言。
    """

    action: Literal['create'] = Field(description="固定为 create，表示新建技能")
    content: str = Field(min_length=1, max_length=100_000, description="完整 SKILL.md 正文，须包含可加载的 YAML 前言")
    category: str | None = Field(default=None, pattern=r'^[a-z0-9][a-z0-9_-]{0,63}$', description="可选的大类目录名称，遵循技能名称约束；None 表示未指定分类")


class PatchSkill(SkillOperation):
    """
    局部修改必须提供旧片段；允许明确替换所有命中。
    """

    action: Literal['patch'] = Field(description="固定为 patch，表示局部修改技能文件")
    old_string: str = Field(min_length=1, max_length=100_000, description="需要定位并替换的非空旧片段")
    new_string: str = Field(max_length=100_000, description="替换后的新片段；空字符串表示移除匹配片段")
    file_path: str | None = Field(default=None, max_length=255, description="技能目录内的目标文件相对路径；None 表示主技能文档")
    replace_all: bool = Field(default=False, description="是否替换全部命中，默认 False；多处匹配时须明确选择")


class RewriteSkill(SkillOperation):
    """
    完整重写形态；不能与局部修改字段混用。
    """

    action: Literal['patch'] = Field(description="固定为 patch，结合 content 表示完整重写")
    content: str = Field(min_length=1, max_length=100_000, description="替换原技能文档的完整非空正文")


class WriteSkillFile(SkillOperation):
    """
    写入技能支持文件；不能借此更新所有权或绕过主文档校验。
    """

    action: Literal['write_file'] = Field(description="固定为 write_file，表示写入技能支持文件")
    file_path: str = Field(min_length=1, max_length=255, description="技能目录内的目标文件相对路径，不得越过技能边界")
    file_content: str = Field(max_length=100_000, description="支持文件的完整文本内容；允许空字符串")


class RemoveSkillFile(SkillOperation):
    """
    删除精确支持文件，不能递归删除目录。
    """

    action: Literal['remove_file'] = Field(description="固定为 remove_file，表示删除单个支持文件")
    file_path: str = Field(min_length=1, max_length=255, description="需要删除的精确支持文件相对路径，不能指向目录")


class DeleteSkill(SkillOperation):
    """
    整项删除必须独占批次；合并目标可作为追溯依据。
    """

    action: Literal['delete'] = Field(description="固定为 delete，删除整个个人技能；此操作必须独占批次")
    absorbed_into: str | None = Field(default=None, pattern=r'^(?:[a-z0-9][a-z0-9_-]{0,63})?$', description="合并后承接内容的技能名称，作为追溯依据；未合并时可为 None 或空字符串")


class ManageSkillsInput(BaseModel):
    """
    最多二十个有序操作，任一失败回滚本次全部技能变更。
    """

    model_config = ConfigDict(extra='forbid')
    operations: list[CreateSkill | PatchSkill | RewriteSkill | WriteSkillFile | RemoveSkillFile | DeleteSkill] = Field(
        min_length=1, max_length=20, description="按顺序执行的 1 至 20 个技能操作；任一操作失败时回滚本次全部变更")


class ViewSkillInput(BaseModel):
    """
    读取具体技能或支持文件，并登记本轮实际读取的版本。
    """

    model_config = ConfigDict(extra='forbid')
    name: str = Field(pattern=r'^[a-z0-9][a-z0-9_-]{0,63}$', description="当前宿主技能范围内的技能名称")
    file_path: str = Field(default='SKILL.md', max_length=255, description="待读取文件在技能目录内的相对路径，默认 SKILL.md")


class MemoryOperation(BaseModel):
    """
    old_text 只定位整条记忆，replace 的 content 会替换整条而非子串。
    """

    model_config = ConfigDict(extra='forbid')
    action: Literal['add', 'replace', 'remove'] = Field(description="记忆动作：add 新增、replace 整条替换、remove 删除")
    content: str | None = Field(default=None, max_length=100_000, description="新增或整条替换后的记忆内容；不需要内容的操作可省略")
    old_text: str | None = Field(default=None, max_length=100_000, description="用于定位整条已有记忆的原文本；不是局部子串替换规则")
    new_text: str | None = Field(default=None, max_length=100_000, description="替换操作兼容的新内容字段；未提供时为 None")


class MemoryInput(BaseModel):
    """
    支持单操作及原子批次形态，pending 只读列出等待人工处理的提案。
    """

    model_config = ConfigDict(extra='forbid')
    action: Literal['add', 'replace', 'remove', 'pending'] | None = Field(default=None, description="单操作动作：add、replace、remove 或只读的 pending；使用 operations 批次时可省略")
    target: Literal['memory', 'user'] = Field(default='memory', description="目标记忆：memory 表示 MEMORY.md，user 表示当前用户的 USER.md，默认 memory")
    content: str | None = Field(default=None, max_length=100_000, description="单操作新增或整条替换后的记忆内容；可省略")
    old_text: str | None = Field(default=None, max_length=100_000, description="用于定位要替换或移除的整条记忆；可省略")
    new_text: str | None = Field(default=None, max_length=100_000, description="单操作兼容的新内容字段；未提供时为 None")
    operations: list[MemoryOperation] | None = Field(default=None, min_length=1, max_length=20, description="可选的 1 至 20 个原子记忆操作；None 表示使用单操作字段")
