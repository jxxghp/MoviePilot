"""远程命令词的文法。

命令词是用户在聊天窗口里手打的那个词，它同时要过三道关：宿主的命令分发、外部渠道的
菜单注册、渠道把菜单点击回传时的精确查表。三道关各自的失败点都不在登记现场，因此文法
收在这里，供扩展契约校验与宿主自检共用一份。

每一条限制都对应一处确定的失败点：

- **必须以 ``/`` 开头**。消息网关只把以 ``/`` 开头的文本转成命令事件，不带斜杠的命令词
  永远等不到分发；Telegram 注册菜单时按 ``cmd[1:]`` 直接削掉首字符，不带斜杠的命令词
  会被削掉一个有效字符，注册成一个谁也打不出来的名字。
- **不能含空白**。分发链路按 ``str.split()[0]`` 取出命令词后做精确查表，含空白的命令词
  取出来的只是它的前半截，与登记的键永远对不上。
- **只能是小写字母、数字与下划线，长度 1 到 32**。Telegram 的 ``set_my_commands`` 是一次
  批量调用，命令名不合该形状会让整批请求失败——一条坏命令连带打掉全部命令的菜单；
  Slack 与 Discord 则把命令名小写化后注册，含大写字母的命令词从菜单回传时对不上精确
  查表的键。
- **不能落在智能助手前缀之下**。消息网关判定命令之前先判智能助手前缀，落在该前缀下的
  文本整条交给智能助手，命令永远不会被执行。
"""

import re
from typing import Any, List, Optional

from pydantic import BaseModel, Field

# 智能助手前缀。以此开头的消息由消息网关整条交给智能助手处理，不再走命令分发
AI_COMMAND_PREFIX = "/ai"

# 命令词的合法形状：前导斜杠加 1 到 32 个小写字母、数字或下划线。取值范围是三处渠道
# 菜单要求的交集，其中 Telegram 的要求最严且失败方式最重（整批注册失败）
_COMMAND_WORD_RE = re.compile(r"^/[a-z0-9_]{1,32}$")

# 命令词文法的说明文案，供各处校验给出一致的违约描述
COMMAND_WORD_GRAMMAR_HINT = (
    "命令词须以 / 开头，其后为 1 到 32 个小写字母、数字或下划线"
    "（例如 /clear_cache），不能含大写字母、连字符、空格或其它字符"
)


def is_valid_command_word(cmd: Any) -> bool:
    """
    判断命令词能否走通分发链路与渠道菜单注册

    :param cmd: 待判定的命令词
    :return: 合文法时为 True；非字符串、为空或含非法字符时为 False
    """
    return isinstance(cmd, str) and bool(_COMMAND_WORD_RE.match(cmd))


def command_word_violation(cmd: Any) -> Optional[str]:
    """
    校验命令词是否满足分发链路与渠道菜单的形状要求

    :param cmd: 待校验的命令词
    :return: 违反文法的描述；命令词合法时为 None
    """
    if not isinstance(cmd, str) or not cmd.strip():
        return "命令词缺失或为空"
    if not is_valid_command_word(cmd):
        return f"命令词 {cmd!r} 不合文法：{COMMAND_WORD_GRAMMAR_HINT}"
    if cmd.startswith(AI_COMMAND_PREFIX):
        return (
            f"命令词 {cmd!r} 落在智能助手前缀 {AI_COMMAND_PREFIX!r} 之下，"
            f"消息网关会把这类消息整条交给智能助手，该命令永远不会被分发执行"
        )
    return None


class CommandLayer(BaseModel):
    """一个命令词的一处来源。"""

    # 来源层：builtin 内建、plugin 插件、other 单独注册
    layer: str
    # 插件实例键，非插件层时为空
    owner: Optional[str] = None
    # 插件标识，非插件层时为空
    extension_id: Optional[str] = None
    # 插件实例标识，非插件层时为空
    instance_id: Optional[str] = None
    # 该来源为这条命令给出的展示名称
    description: Optional[str] = None
    # 该来源为这条命令给出的分类
    category: Optional[str] = None


class CommandConflict(BaseModel):
    """一个命令词被多个插件同时声明而使插件声明整体失效的呈现。

    命令词不指称任何共同对象，两个插件的同名命令做的并不是同一件事，宿主无从裁决该把
    它交给谁，因此双方一并失效。用户据此知道该让哪一方改命令词或停用谁。
    """

    # 声明该命令词的插件标识，已排序
    plugins: List[str] = Field(default_factory=list)
    # 与 plugins 一一对应的插件实例键
    owners: List[str] = Field(default_factory=list)


class CommandOrigin(BaseModel):
    """一个命令词在运行期命令表中的来源分层。"""

    # 命令词
    cmd: str
    # 该命令词当前是否能被敲出来
    effective: bool = False
    # 交出当前生效定义的那一层
    source: Optional[CommandLayer] = None
    # 被上层压住、当前不生效的下层来源，按内建到插件的次序排列
    shadowed: List[CommandLayer] = Field(default_factory=list)
    # 与内建命令同名却未声明接管意图，因而被拒的插件声明
    declined: List[CommandLayer] = Field(default_factory=list)
    # 该命令词的插件声明因跨插件同名而整体失效时的详情
    conflict: Optional[CommandConflict] = None
