import errno
import os
import uuid
from abc import ABCMeta, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, List, Dict, Optional, Tuple, Type, Union

from sqlalchemy.orm import DeclarativeBase

from app.application.messaging.message import MessageHelper
from app.application.orchestration import ChainBase
from app.db.oper.plugindata import PluginDataOper
from app.db.oper.pluginconfig import PluginConfigOper
from app.db.oper.systemconfig import SystemConfigOper
from app.foundation.paths import ensure_path_segment
from app.runtime.config import settings
from app.runtime.events import EventManager
from app.runtime.extensions.instance import DEFAULT_INSTANCE_ID
from app.runtime.log import logger
from app.schemas.message import Message
from app.schemas.notification import ChannelCapabilities
from app.schemas.types import MessageType, NotificationChannel

if TYPE_CHECKING:
    from app.db.plugin import PluginDatabaseHandle

# 当前支持的插件持久化路径用途：data 为插件业务数据，db 为插件自管理数据库
_PLUGIN_PATH_KINDS = frozenset({"data", "db"})
# 插件实例目录布局迁移完成后写入插件持久化根目录下的哨兵文件名
_INSTANCE_LAYOUT_SENTINEL_NAME = ".instance-layout-migrated"
# 迁移过程中源目录的改名中转目录名前缀，与插件持久化根目录同级
_INSTANCE_LAYOUT_STAGING_INFIX = ".migrating-"


def plugin_instance_path(plugin_id: str, instance_id: str, kind: str) -> Path:
    """返回插件某一实例、某一用途的持久化目录，目录不存在时创建。

    ``plugin_id`` 与 ``instance_id`` 各自独立校验，任一非法都会拒绝整次调用；
    校验通过后目录固定形如 ``config/plugins/<插件ID>/<实例ID>/<kind>/``。

    :param plugin_id: 插件标识
    :param instance_id: 实例标识
    :param kind: 用途分类，取值为 "data"（插件业务数据）或 "db"（插件自管理数据库）
    :return: 对应目录的绝对路径
    :raises ValueError: 标识包含路径分隔符、盘符、空字符、指向上级目录，或 kind 不受支持
    """
    if kind not in _PLUGIN_PATH_KINDS:
        raise ValueError(f"不支持的插件路径用途：{kind!r}")
    safe_plugin_id = ensure_path_segment(plugin_id, subject="插件ID")
    safe_instance_id = ensure_path_segment(instance_id, subject="插件实例ID")

    plugin_root = settings.PLUGIN_DATA_PATH / safe_plugin_id
    if safe_instance_id == DEFAULT_INSTANCE_ID:
        target = _resolve_default_instance_dir(plugin_root, kind)
    else:
        target = plugin_root / safe_instance_id / kind

    target.mkdir(parents=True, exist_ok=True)
    return target


def _resolve_default_instance_dir(plugin_root: Path, kind: str) -> Path:
    """定位默认实例下指定用途的目录，首次访问时顺带完成一次历史数据迁移。

    :param plugin_root: 插件持久化根目录（迁移前的旧数据目录）
    :param kind: 用途分类
    :return: 迁移完成时为 ``plugin_root/default/<kind>``；迁移放弃或失败时为
        仍持有历史数据的目录（不追加 kind 层级，与迁移前的旧布局一致）
    """
    sentinel = plugin_root / _INSTANCE_LAYOUT_SENTINEL_NAME
    default_dir = plugin_root / DEFAULT_INSTANCE_ID

    if sentinel.exists():
        return default_dir / kind

    staging = _find_leftover_staging(plugin_root)
    if staging is None:
        if not plugin_root.exists() or _only_contains(plugin_root, default_dir):
            _write_instance_layout_sentinel(sentinel)
            return default_dir / kind
        staging = plugin_root.parent / (
            f"{plugin_root.name}{_INSTANCE_LAYOUT_STAGING_INFIX}{uuid.uuid4().hex}"
        )

    return _migrate_to_default_instance(plugin_root, staging, sentinel, default_dir, kind)


def _only_contains(directory: Path, only_entry: Path) -> bool:
    """判断目录是否为空，或仅含 ``only_entry`` 这一个子项。

    :param directory: 待判断的目录，调用方需确保其存在
    :param only_entry: 允许存在的唯一子项
    :return: 目录为空或仅含 only_entry 时为 True
    """
    return all(entry == only_entry for entry in directory.iterdir())


def _find_leftover_staging(plugin_root: Path) -> Optional[Path]:
    """在插件目录的同级查找上次迁移中断遗留的改名中转目录。

    :param plugin_root: 插件持久化根目录
    :return: 遗留的中转目录，不存在时为 None
    """
    parent = plugin_root.parent
    if not parent.is_dir():
        return None
    prefix = f"{plugin_root.name}{_INSTANCE_LAYOUT_STAGING_INFIX}"
    candidates = sorted(
        entry
        for entry in parent.iterdir()
        if entry.is_dir() and entry.name.startswith(prefix)
    )
    return candidates[0] if candidates else None


def _migrate_to_default_instance(
    plugin_root: Path,
    staging: Path,
    sentinel: Path,
    default_dir: Path,
    kind: str,
) -> Path:
    """执行或续做一次迁移的改名步骤，成功后写入哨兵。

    续做时按 ``staging``、``default_dir`` 是否已存在判断上次改名到了哪一步，跳过
    已完成的部分。改名的落地目标是 ``default_dir/kind`` 而非 ``default_dir`` 本身，
    使旧布局下直接位于插件目录的文件迁移后仍位于调用方按 kind 取到的目录下。

    :param plugin_root: 插件持久化根目录
    :param staging: 改名中转目录
    :param sentinel: 迁移完成后写入的哨兵文件
    :param default_dir: 默认实例目录
    :param kind: 用途分类
    :return: 改名成功时为 default_dir/kind；改名失败时为仍持有数据的目录（staging
        或 plugin_root）
    """
    kind_dir = default_dir / kind
    try:
        if not staging.exists():
            os.rename(plugin_root, staging)
        if not default_dir.exists():
            default_dir.mkdir(parents=True)
        if staging.exists():
            os.rename(staging, kind_dir)
    except OSError as error:
        if getattr(error, "errno", None) == errno.EXDEV:
            logger.warning(
                f"插件持久化目录跨设备无法原子改名，放弃本次迁移：{plugin_root} - {error}"
            )
        else:
            logger.error(f"插件持久化目录迁移失败：{plugin_root} - {error}")
        return staging if staging.exists() else plugin_root

    try:
        _write_instance_layout_sentinel(sentinel)
    except OSError as error:
        logger.warning(f"迁移哨兵写入失败，下次访问将重试：{sentinel} - {error}")
    return kind_dir


def _write_instance_layout_sentinel(sentinel: Path) -> None:
    """写入迁移完成哨兵文件，内容为完成时刻的 UTC 时间戳。

    :param sentinel: 哨兵文件路径
    """
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_text(datetime.now(timezone.utc).isoformat(), encoding="utf-8")


class PluginChian(ChainBase):
    """
    插件处理链
    """
    pass


class _PluginBase(metaclass=ABCMeta):
    """
    插件模块基类，通过继续该类实现插件功能
    除内置属性外，还有以下方法可以扩展或调用：
    - stop_service() 停止插件服务
    - get_config() 获取配置信息
    - update_config() 更新配置信息
    - init_plugin() 生效配置信息
    - get_data_path() 获取插件数据保存目录
    """
    # 插件名称
    plugin_name: Optional[str] = ""
    # 插件描述
    plugin_desc: Optional[str] = ""
    # 插件顺序
    plugin_order: Optional[int] = 9999
    # 是否为插件分身
    is_clone: bool = False

    def __init__(self):
        # 插件数据
        self.plugindata = PluginDataOper()
        # 处理链
        self.chain = PluginChian()
        # 系统配置
        self.systemconfig = SystemConfigOper()
        # 系统消息
        self.systemmessage = MessageHelper()
        # 事件管理器
        self.eventmanager = EventManager()

    @abstractmethod
    def init_plugin(self, config: dict = None):
        """
        生效配置信息
        :param config: 配置信息字典
        """
        pass

    def get_name(self) -> str:
        """
        获取插件名称
        :return: 插件名称
        """
        return self.plugin_name

    @abstractmethod
    def get_state(self) -> bool:
        """
        获取插件运行状态
        """
        pass

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """
        注册插件远程命令
        [{
            "cmd": "/xx",
            "event": EventType.xx,
            "desc": "名称",
            "category": "分类，需要注册到Wechat时必须有分类",
            "data": {}
        }]
        """
        pass

    @staticmethod
    def get_render_mode() -> Tuple[str, Optional[str]]:
        """
        获取插件渲染模式
        :return: 1、渲染模式，支持：vue/vuetify，默认vuetify；2、vue模式下编译后文件的相对路径，默认为`dist/asserts`，vuetify模式下为None
        """
        return "vuetify", None

    @abstractmethod
    def get_api(self) -> List[Dict[str, Any]]:
        """
        注册插件API
        [{
            "path": "/xx",
            "endpoint": self.xxx,
            "methods": ["GET", "POST"],
            "auth: "apikey",  # 鉴权类型：apikey/bear
            "summary": "API名称",
            "description": "API说明"
        }]
        """
        pass

    @abstractmethod
    def get_form(self) -> Tuple[Optional[List[dict]], Dict[str, Any]]:
        """
        拼装插件配置页面，插件配置页面使用Vuetify组件拼装，参考：https://vuetifyjs.com/
        :return: 1、页面配置（vuetify模式）或 None（vue模式）；2、默认数据结构
        """
        pass

    @abstractmethod
    def get_page(self) -> Optional[List[dict]]:
        """
        拼装插件详情页面，需要返回页面配置，同时附带数据
        插件详情页面使用Vuetify组件拼装，参考：https://vuetifyjs.com/
        :return: 页面配置（vuetify模式）或 None（vue模式）
        """
        pass

    def get_service(self) -> List[Dict[str, Any]]:
        """
        注册插件公共服务
        [{
            "id": "服务ID",
            "name": "服务名称",
            "trigger": "触发器：cron/interval/date/CronTrigger.from_crontab()",
            "func": self.xxx,
            "kwargs": {} # 定时器参数
        }]
        """
        pass

    def get_dashboard(self, key: str, **kwargs) -> Optional[Tuple[Dict[str, Any], Dict[str, Any], Optional[List[dict]]]]:
        """
        获取插件仪表盘页面，需要返回：1、仪表板col配置字典；2、全局配置（布局、自动刷新等）；3、仪表板页面元素配置含数据json（vuetify）或 None（vue模式）
        1、col配置参考：
        {
            "cols": 12, "md": 6
        }
        2、全局配置参考：
        {
            "refresh": 10, // 自动刷新时间，单位秒
            "border": True, // 是否显示边框，默认True，为False时取消组件边框和边距，由插件自行控制
            "title": "组件标题", // 组件标题，如有将显示该标题，否则显示插件名称
            "subtitle": "组件子标题", // 组件子标题，缺省时不展示子标题
        }
        3、vuetify模式页面配置使用Vuetify组件拼装，参考：https://vuetifyjs.com/；vue模式为None

        kwargs参数可获取的值：1、user_agent：浏览器UA

        :param key: 仪表盘key，根据指定的key返回相应的仪表盘数据，缺省时返回一个固定的仪表盘数据（兼容旧版）
        """
        pass

    def get_dashboard_meta(self) -> Optional[List[Dict[str, str]]]:
        """
        获取插件仪表盘元信息
        返回示例：
            [{
                "key": "dashboard1", // 仪表盘的key，在当前插件范围唯一
                "name": "仪表盘1" // 仪表盘的名称
            }, {
                "key": "dashboard2",
                "name": "仪表盘2"
            }]
        """
        pass

    def get_auth_providers(self) -> List[Dict[str, Any]]:
        """
        声明插件提供的登录认证入口。

        返回示例：
        [{
            "id": "oidc",
            "name": "OIDC 登录",
            "icon": "mdi-openid",
            "component": "AuthPage",
            "enabled": True
        }]
        """
        pass

    def get_module(self) -> Dict[str, Any]:
        """
        获取插件模块声明，用于胁持系统模块实现（方法名：方法实现）
        {
            "id1": self.xxx1,
            "id2": self.xxx2,
        }

        多来源契约（media_detail、media_credits、media_recommend、media_similar、
        person_detail、person_credits、discover、discover_board、match_media，
        及其 async_ 变体）由多个数据源共用同一方法名，按调用方传入的 source 参数区分
        来源。挂载这些方法名时，非本插件负责的 source 必须返回 None 让出；返回空列表
        会被判定为已认领而短路，因此非本来源也不能返回空列表，否则会拦截该契约下的
        全部数据源，而不只是插件本意接管的那一个
        """
        pass

    def get_channel_capabilities(self) -> Optional[List[ChannelCapabilities]]:
        """
        声明本插件承载的消息渠道能力

        :return: `ChannelCapabilities` 列表；插件不作为消息渠道时无需实现
        """
        pass

    def test(self) -> Optional[Tuple[bool, str]]:
        """
        检测插件依赖的外部服务是否可连通

        :return: `(是否可连通, 失败原因)`；插件不提供自检时无需实现
        """
        pass

    def get_actions(self) -> List[Dict[str, Any]]:
        """
        获取插件工作流动作
        [{
            "id": "动作ID",
            "name": "动作名称",
            "func": self.xxx,
            "kwargs": {} # 需要附加传递的参数
        }]

        对实现函数的要求：
        1、函数的第一个参数固定为 ActionContent 实例，如需要传递额外参数，在kwargs中定义
        2、函数的返回：执行状态 True / False，更新后的 ActionContent 实例
        """
        pass

    def get_agent_tools(self) -> List[Type]:
        """
        获取插件智能体工具
        返回工具类列表，每个工具类必须继承自 MoviePilotTool
        [ToolClass1, ToolClass2, ...]

        对工具类的要求：
        1、工具类必须继承自 app.agent.tools.base.MoviePilotTool
        2、工具类需要实现 run 方法（异步方法）
        3、工具类需要定义 name 和 description 属性
        4、工具类可以定义 args_schema 来指定输入参数模型
        """
        pass

    @abstractmethod
    def stop_service(self):
        """
        停止插件
        """
        pass

    def update_config(self, config: dict, plugin_id: Optional[str] = None) -> bool:
        """
        更新配置信息
        :param config: 配置信息字典
        :param plugin_id: 插件ID
        """
        if not plugin_id:
            plugin_id = self.__class__.__name__
        PluginConfigOper().upsert(plugin_id, DEFAULT_INSTANCE_ID, {"config_data": config})
        return True

    def get_config(self, plugin_id: Optional[str] = None) -> Any:
        """
        获取配置信息
        :param plugin_id: 插件ID
        """
        if not plugin_id:
            plugin_id = self.__class__.__name__
        row = PluginConfigOper().get(plugin_id, DEFAULT_INSTANCE_ID)
        return row.config_data if row else None

    def get_data_path(self, plugin_id: Optional[str] = None) -> Path:
        """
        获取插件数据保存目录
        :param plugin_id: 插件ID，为空时取当前插件
        :return: 插件数据目录，不存在时创建
        :raises ValueError: 插件ID包含路径分隔符、盘符，或指向数据根目录之外
        """
        if not plugin_id:
            plugin_id = self.__class__.__name__
        return plugin_instance_path(plugin_id, DEFAULT_INSTANCE_ID, "data")

    def declare_plugin_models(
        self,
        base: Type[DeclarativeBase],
        plugin_id: Optional[str] = None,
        instance_id: str = DEFAULT_INSTANCE_ID,
    ) -> None:
        """
        声明本插件在其专属数据库中使用的 ORM 模型集合。

        ``base`` 须由 ``app.db.plugin.plugin_declarative_base`` 产出，插件模型继承
        它定义；其 ``metadata`` 上注册的全部表会在插件数据库建立时一并建表。插件
        同时声明了迁移目录时本声明被忽略，改走 alembic。
        :param base: 插件专属声明式基类
        :param plugin_id: 插件ID，为空时取当前插件
        :param instance_id: 插件实例标识，本批固定取默认实例
        """
        from app.db.plugin import declare_models
        if not plugin_id:
            plugin_id = self.__class__.__name__
        declare_models(plugin_id, instance_id, base)

    def declare_plugin_migrations(
        self,
        directory: Union[str, Path],
        plugin_id: Optional[str] = None,
        instance_id: str = DEFAULT_INSTANCE_ID,
    ) -> None:
        """
        声明本插件的 Alembic 迁移脚本目录，声明后插件数据库改走 alembic upgrade
        建库，不再按 ``declare_plugin_models`` 声明的模型建表。
        :param directory: 迁移脚本目录，须符合 Alembic script_location 布局
        :param plugin_id: 插件ID，为空时取当前插件
        :param instance_id: 插件实例标识，本批固定取默认实例
        """
        from app.db.plugin import declare_migrations
        if not plugin_id:
            plugin_id = self.__class__.__name__
        declare_migrations(plugin_id, instance_id, Path(directory))

    def get_plugin_database(
        self,
        plugin_id: Optional[str] = None,
        instance_id: str = DEFAULT_INSTANCE_ID,
    ) -> "PluginDatabaseHandle":
        """
        获取本插件指定实例的数据库句柄，用于取会话读写插件自有表。

        句柄持有插件专属的引擎与会话工厂；容器不存在时按需建立，不要求提前调用
        ``declare_plugin_models``。
        :param plugin_id: 插件ID，为空时取当前插件
        :param instance_id: 插件实例标识，本批固定取默认实例
        :return: 插件实例的数据库句柄
        """
        from app.db.plugin import get_database
        if not plugin_id:
            plugin_id = self.__class__.__name__
        return get_database(plugin_id, instance_id)

    def save_data(self, key: str, value: Any, plugin_id: Optional[str] = None):
        """
        保存插件数据
        :param key: 数据key
        :param value: 数据值
        :param plugin_id: 插件ID
        """
        if not plugin_id:
            plugin_id = self.__class__.__name__
        self.plugindata.save(plugin_id, key, value)

    async def async_save_data(
        self, key: str, value: Any, plugin_id: Optional[str] = None
    ) -> None:
        """
        异步保存插件数据

        :param key: 数据键
        :param value: 数据值
        :param plugin_id: 插件ID
        """
        if not plugin_id:
            plugin_id = self.__class__.__name__
        await self.plugindata.async_save(plugin_id, key, value)

    def get_data(self, key: Optional[str] = None, plugin_id: Optional[str] = None) -> Any:
        """
        获取插件数据
        :param key: 数据key
        :param plugin_id: plugin_id
        """
        if not plugin_id:
            plugin_id = self.__class__.__name__
        return self.plugindata.get_data(plugin_id, key)

    async def async_get_data(
        self, key: Optional[str] = None, plugin_id: Optional[str] = None
    ) -> Any:
        """
        异步获取插件数据

        :param key: 数据键
        :param plugin_id: 插件ID
        :return: 指定键的数据值或插件的全部数据
        """
        if not plugin_id:
            plugin_id = self.__class__.__name__
        return await self.plugindata.async_get_data(plugin_id, key)

    def del_data(self, key: str, plugin_id: Optional[str] = None) -> Any:
        """
        删除插件数据
        :param key: 数据key
        :param plugin_id: plugin_id
        """
        if not plugin_id:
            plugin_id = self.__class__.__name__
        return self.plugindata.del_data(plugin_id, key)

    def post_message(self, channel: NotificationChannel = None, mtype: MessageType = None, title: Optional[str] = None,
                     text: Optional[str] = None, image: Optional[str] = None, link: Optional[str] = None,
                     userid: Optional[str] = None, username: Optional[str] = None,
                     **kwargs):
        """
        发送消息
        """
        if not link:
            link = settings.MP_DOMAIN(f"#/plugins?tab=installed&id={self.__class__.__name__}")
        self.chain.post_message(Message(
            channel=channel, mtype=mtype, title=title, text=text,
            image=image, link=link, userid=userid, username=username, **kwargs
        ))

    def close(self):
        pass
