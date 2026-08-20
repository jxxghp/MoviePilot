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
from app.runtime.extensions.declaration import (
    ActionDeclaration,
    AgentToolDeclaration,
    AuthProviderDeclaration,
    DashboardDeclaration,
    MediaSourceDeclaration,
    ModuleDeclaration,
    ServiceInstanceDeclaration,
    StorageDeclaration,
)
from app.runtime.extensions.instance import (
    DEFAULT_INSTANCE_ID,
    instance_key,
    normalize_instance_id,
)
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
    # 插件标识，缺省取插件主类名
    plugin_id: Optional[str] = None
    # 运行实例标识，同一插件类的多个实例各自持有独立的配置、数据与数据目录
    instance_id: str = DEFAULT_INSTANCE_ID

    def __init__(self, plugin_id: Optional[str] = None, instance_id: Optional[str] = None):
        """
        初始化插件运行实例
        :param plugin_id: 插件标识，为空时取插件主类名
        :param instance_id: 实例标识，为空时取默认实例
        """
        # 插件标识
        self.plugin_id = plugin_id or self.__class__.__name__
        # 实例标识
        self.instance_id = normalize_instance_id(instance_id)
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

    def _target_instance(self, plugin_id: Optional[str]) -> Tuple[str, str]:
        """
        定位一次持久化访问的目标插件与实例
        :param plugin_id: 调用方指定的插件标识，为空时取当前插件
        :return: `(插件标识, 实例标识)`；访问其他插件时取该插件的默认实例
        """
        current = self.plugin_id or self.__class__.__name__
        if not plugin_id or plugin_id == current:
            return current, self.instance_id or DEFAULT_INSTANCE_ID
        return plugin_id, DEFAULT_INSTANCE_ID

    def get_instance_key(self) -> str:
        """
        获取本实例在宿主内的稳定标识
        :return: 实例键，默认实例即裸插件标识
        """
        plugin_id, instance_id = self._target_instance(None)
        return instance_key(plugin_id, instance_id)

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

    def provides_dashboards(self) -> Optional[List[DashboardDeclaration]]:
        """
        声明本插件提供的仪表盘

        声明的是「有哪些仪表盘、长什么样」；「当前该显示什么数据」仍由带参数的
        get_dashboard(key, **kwargs) 在每次请求时实时取用，两者不是一回事。

        返回示例：
        [DashboardDeclaration(
            key="dashboard1",                    # 仪表盘 key，在插件实例范围内
                                                  # 唯一；单仪表盘插件可留空
            name="仪表盘1",                       # 展示名称
            config_form=([...], {...}),          # 该仪表盘的初始界面（vuetify
                                                  # 模式），形状与 get_form() 相同；
                                                  # 与 config_component 互斥，可选
        )]

        vue 模式改用 `config_component="Dashboard1"`——本插件联邦远程中承载该
        仪表盘的组件名，要求 `get_render_mode()` 返回 "vue"；与 `config_form`
        二选一，同时给出视为意图不明，整条声明被拒。

        也可直接返回描述字典（不包 `DashboardDeclaration`），字典形态复用
        get_dashboard_meta() 的 key/name 字段，兼容早期写法；此时无法声明专属
        配置界面。

        :return: `DashboardDeclaration` 列表；插件不提供仪表盘时无需实现
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

    def provides_auth_providers(self) -> Optional[List[AuthProviderDeclaration]]:
        """
        声明本插件提供的登录认证入口

        返回示例：
        [AuthProviderDeclaration(
            id="oidc",                           # 提供方标识，缺省回落为
                                                  # plugin:<实例键>
            name="OIDC 登录",                     # 展示名称，缺省回落为插件展示名
            icon="mdi-openid",
            capabilities=["oidc"],               # 承诺提供的能力方法名
            config_form=([...], {...}),          # 该提供方的专属配置界面（vuetify
                                                  # 模式），形状与 get_form() 相同；
                                                  # 与 config_component 互斥，可选
        )]

        vue 模式改用 `config_component="OidcProviderConfig"`——本插件联邦远程中承载
        该界面的组件名，要求 `get_render_mode()` 返回 "vue"；与 `config_form` 二选一，
        同时给出视为意图不明，整条声明被拒。登录入口本身在 vue 模式下固定渲染为
        `AuthPage`，由宿主联邦机制原样注入，不受本字段影响；该配置界面归属这条
        声明，不归属插件本身。

        也可直接返回字段字典本身（不包 `AuthProviderDeclaration`），宿主按字典内容
        取用展示字段，兼容早期写法，此时无法声明专属配置界面。

        :return: `AuthProviderDeclaration` 列表；插件不提供登录认证入口时无需实现
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

    def provides_modules(self) -> Optional[List[ModuleDeclaration]]:
        """
        声明本插件提供的模块方法表

        返回示例：
        [ModuleDeclaration(
            methods={                            # 方法名到实现的映射，即 get_module()
                "id1": self.xxx1,                 # 那张表的声明式版本
                "id2": self.xxx2,
            },
            capabilities=["id1", "id2"],         # 承诺提供的能力方法名
            service_config="Downloaders",        # 归属的服务配置族，取值须是
                                                  # SystemConfigKey 的成员值；不归属
                                                  # 任何服务族时不填
        )]

        也可直接返回方法表字典本身（不包 `ModuleDeclaration`），宿主按字典内容
        取用方法表，兼容早期写法；此时不能声明 service_config。

        多来源契约（media_detail、media_credits、media_recommend、media_similar、
        person_detail、person_credits、discover、discover_board、match_media，
        及其 async_ 变体）由多个数据源共用同一方法名，按调用方传入的 source 参数区分
        来源，声明式登记与 get_module() 遵循同一规则：非本插件负责的 source 必须
        返回 None 让出，返回空列表会被判定为已认领而短路。

        同一实例的方法名若被本钩子与 get_module() 同时挂载，声明式登记优先生效。

        :return: `ModuleDeclaration` 列表；插件不提供模块方法表时无需实现
        """
        pass

    def get_channel_capabilities(self) -> Optional[List[ChannelCapabilities]]:
        """
        声明本插件承载的消息渠道能力

        :return: `ChannelCapabilities` 列表；插件不作为消息渠道时无需实现
        """
        pass

    def provides_channel_capabilities(self) -> Optional[List[ChannelCapabilities]]:
        """
        声明本插件承载的消息渠道能力

        返回示例：
        [ChannelCapabilities(
            channel="my_channel",                # 渠道标识，开放取值，不要求登记于
                                                  # NotificationChannel 枚举
            capabilities={ChannelCapability.MARKDOWN, ChannelCapability.IMAGES},
            max_message_length=4000,
        )]

        返回值形状与 `get_channel_capabilities()` 相同，区别在于本钩子经契约
        校验：渠道标识非空、能力集合须是 `ChannelCapability` 成员的集合，不合
        契约的声明会被拒绝登记，不留到调用时才失败。

        :return: `ChannelCapabilities` 列表；插件不作为消息渠道时无需实现
        """
        pass

    def provides_storages(self) -> Optional[List[StorageDeclaration]]:
        """
        声明本插件提供的存储后端

        返回示例：
        [StorageDeclaration(
            schema="u115",                      # 存储标识，同一标识重复登记以最新一次
                                                  # 为准，与内建标识相同即构成覆盖
            capabilities=["list", "upload"],     # 承诺提供的能力方法名
            impl=U115Storage,                    # 存储后端实现类，须继承
                                                  # app.modules._base.storage.StorageBase
                                                  # 并落地全部抽象方法；不合契约的声明
                                                  # 会被拒绝登记，不留到调用时才失败
            config_form=([...], {...}),          # 该存储类型的专属配置界面（vuetify
                                                  # 模式），形状与 get_form() 相同；
                                                  # 与 config_component 互斥，可选
        )]

        vue 模式改用 `config_component="U115StorageConfig"`——本插件联邦远程中承载
        该界面的组件名，要求 `get_render_mode()` 返回 "vue"；与 `config_form` 二选一，
        同时给出视为意图不明，整条声明被拒。界面归属这条声明，不归属本插件本身。

        也可直接返回实现类本身（不包 `StorageDeclaration`），宿主按类自身的 schema
        属性取用标识，兼容早期写法。

        :return: `StorageDeclaration` 列表；插件不作为存储提供方时无需实现
        """
        pass

    def provides_service_instances(self) -> Optional[List[ServiceInstanceDeclaration]]:
        """
        声明本插件提供的可配置服务实例类型

        返回示例：
        [ServiceInstanceDeclaration(
            capability="downloader",             # 能力标签，可选值为 downloader、
                                                  # mediaserver、notification
            type="my_downloader",                # 类型标识，与该族配置模型的 type
                                                  # 字段取值对应；与内建类型同名即构成
                                                  # 覆盖，用户为该类型配置的实例改由
                                                  # 本插件的实现承担
            name="我的下载器",                    # 类型展示名称
            impl=MyDownloader,                   # 实例实现类，宿主对该类型下的每条
                                                  # 用户配置调用 impl(name=配置名,
                                                  # **配置内容) 构造实例；构造签名不
                                                  # 接受关键字 name 的声明会被拒绝
            config_form=([...], {...}),          # 该类型的专属配置界面（vuetify
                                                  # 模式），形状与 get_form() 相同；
                                                  # 与 config_component 互斥，可选
        )]

        实现类的构造形状不便迁就宿主时改用 `factory=my_factory`——宿主对每条用户配置
        调用 `factory(配置对象)`，配置对象即该族配置模型的一条记录，怎么落到实例上由
        插件自己决定。`factory` 与 `impl` 二选一，同时给出或都不给出的声明被拒。

        vue 模式改用 `config_component="MyDownloaderConfig"`——本插件联邦远程中承载
        该界面的组件名，要求 `get_render_mode()` 返回 "vue"；与 `config_form` 二选一，
        同时给出视为意图不明，整条声明被拒。界面归属这条声明，不归属本插件本身。

        下载器、媒体服务器与消息通知共用本钩子，差异只在 `capability`：三者的取用
        方式相同，都是按配置扇出 N 个具名实例。用户未为该类型配置任何实例时，声明
        照常登记，只是没有实例产出。

        :return: `ServiceInstanceDeclaration` 列表；插件不提供服务实例类型时无需实现
        """
        pass

    def test(self) -> Optional[Tuple[bool, str]]:
        """
        检测插件依赖的外部服务是否可连通

        :return: `(是否可连通, 失败原因)`；插件不提供自检时无需实现
        """
        pass

    def get_media_source(self) -> List[Dict[str, Any]]:
        """
        注册插件提供的媒体数据源。

        返回的每项至少包含 ``name``、``media_source`` 和 ``media_types``；实际的
        搜索、识别、图片和 NFO 刮削实现通过 ``get_module`` 暴露对应方法。
        """
        pass

    def provides_media_sources(self) -> Optional[List[MediaSourceDeclaration]]:
        """
        声明本插件提供的媒体数据源

        返回示例：
        [MediaSourceDeclaration(
            media_source="acme.video",           # 规范媒体来源标识，须能被
                                                  # MediaSource 解析——内置常量
                                                  # 或形如 [a-z][a-z0-9._-]{0,63}
                                                  # 的插件扩展标识
            name="Acme Video",                   # 数据源展示名称
            media_types=["电影", "电视剧"],       # 支持的媒体类型，可选
        )]

        识别、搜索、图片与 NFO 刮削的实际实现仍通过 provides_modules()/get_module()
        按契约方法名分发，本声明只承载数据源自身的展示信息。

        也可直接返回描述字典（不包 `MediaSourceDeclaration`），字典形态复用
        get_media_source() 每项的字段名，兼容早期写法。

        :return: `MediaSourceDeclaration` 列表；插件不提供媒体数据源时无需实现
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

    def provides_actions(self) -> Optional[List[ActionDeclaration]]:
        """
        声明本插件提供的工作流动作

        返回示例：
        [ActionDeclaration(
            action_id="my_action",               # 动作标识，工作流按此标识调用
            name="我的动作",                      # 动作展示名称
            impl=self.xxx,                       # 动作实现函数，首个位置参数固定
                                                  # 为 ActionContext 实例，返回
                                                  # (执行状态, 更新后的 ActionContext)
                                                  # 二元组
            kwargs={},                           # 需要附加传递的静态参数，可选
        )]

        也可直接返回描述字典（不包 `ActionDeclaration`），字典形态复用
        get_actions() 每项的字段名（action_id/name/func/kwargs），兼容早期写法。

        :return: `ActionDeclaration` 列表；插件不提供工作流动作时无需实现
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

    def provides_agent_tools(self) -> Optional[List[AgentToolDeclaration]]:
        """
        声明本插件提供的智能体工具

        返回示例：
        [AgentToolDeclaration(
            name="my_tool",                      # 工具名，供 Agent 识别并调用
            description="工具功能说明",           # 工具描述，供 Agent 判断何时调用
            capabilities=["my_tool"],            # 承诺提供的能力方法名
            impl=MyTool,                         # 工具实现类，须继承
                                                  # app.agent.tools.base.MoviePilotTool
                                                  # 并实现异步的 run 方法；不合契约的声明
                                                  # 会被拒绝登记，不留到调用时才失败
        )]

        也可直接返回实现类本身（不包 `AgentToolDeclaration`），宿主按类自身的 name、
        description 字段取用标识，兼容早期写法。

        :return: `AgentToolDeclaration` 列表；插件不提供智能体工具时无需实现
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
        target_id, instance_id = self._target_instance(plugin_id)
        PluginConfigOper().upsert(target_id, instance_id, {"config_data": config})
        return True

    def get_config(self, plugin_id: Optional[str] = None) -> Any:
        """
        获取配置信息
        :param plugin_id: 插件ID
        """
        target_id, instance_id = self._target_instance(plugin_id)
        row = PluginConfigOper().get(target_id, instance_id)
        return row.config_data if row else None

    def get_data_path(self, plugin_id: Optional[str] = None) -> Path:
        """
        获取插件数据保存目录
        :param plugin_id: 插件ID，为空时取当前插件
        :return: 插件数据目录，不存在时创建
        :raises ValueError: 插件ID包含路径分隔符、盘符，或指向数据根目录之外
        """
        target_id, instance_id = self._target_instance(plugin_id)
        return plugin_instance_path(target_id, instance_id, "data")

    def declare_plugin_models(
        self,
        base: Type[DeclarativeBase],
        plugin_id: Optional[str] = None,
        instance_id: Optional[str] = None,
    ) -> None:
        """
        声明本插件在其专属数据库中使用的 ORM 模型集合。

        ``base`` 须由 ``app.db.plugin.plugin_declarative_base`` 产出，插件模型继承
        它定义；其 ``metadata`` 上注册的全部表会在插件数据库建立时一并建表。插件
        同时声明了迁移目录时本声明被忽略，改走 alembic。
        :param base: 插件专属声明式基类
        :param plugin_id: 插件ID，为空时取当前插件
        :param instance_id: 插件实例标识，为空时取当前实例
        """
        from app.db.plugin import declare_models
        target_id, target_instance = self._target_instance(plugin_id)
        declare_models(target_id, instance_id or target_instance, base)

    def declare_plugin_migrations(
        self,
        directory: Union[str, Path],
        plugin_id: Optional[str] = None,
        instance_id: Optional[str] = None,
    ) -> None:
        """
        声明本插件的 Alembic 迁移脚本目录，声明后插件数据库改走 alembic upgrade
        建库，不再按 ``declare_plugin_models`` 声明的模型建表。
        :param directory: 迁移脚本目录，须符合 Alembic script_location 布局
        :param plugin_id: 插件ID，为空时取当前插件
        :param instance_id: 插件实例标识，为空时取当前实例
        """
        from app.db.plugin import declare_migrations
        target_id, target_instance = self._target_instance(plugin_id)
        declare_migrations(target_id, instance_id or target_instance, Path(directory))

    def get_plugin_database(
        self,
        plugin_id: Optional[str] = None,
        instance_id: Optional[str] = None,
    ) -> "PluginDatabaseHandle":
        """
        获取本插件指定实例的数据库句柄，用于取会话读写插件自有表。

        句柄持有插件专属的引擎与会话工厂；容器不存在时按需建立，不要求提前调用
        ``declare_plugin_models``。
        :param plugin_id: 插件ID，为空时取当前插件
        :param instance_id: 插件实例标识，为空时取当前实例
        :return: 插件实例的数据库句柄
        """
        from app.db.plugin import get_database
        target_id, target_instance = self._target_instance(plugin_id)
        return get_database(target_id, instance_id or target_instance)

    def save_data(self, key: str, value: Any, plugin_id: Optional[str] = None):
        """
        保存插件数据
        :param key: 数据key
        :param value: 数据值
        :param plugin_id: 插件ID
        """
        target_id, instance_id = self._target_instance(plugin_id)
        self.plugindata.save(target_id, key, value, instance_id)

    async def async_save_data(
        self, key: str, value: Any, plugin_id: Optional[str] = None
    ) -> None:
        """
        异步保存插件数据

        :param key: 数据键
        :param value: 数据值
        :param plugin_id: 插件ID
        """
        target_id, instance_id = self._target_instance(plugin_id)
        await self.plugindata.async_save(target_id, key, value, instance_id)

    def get_data(self, key: Optional[str] = None, plugin_id: Optional[str] = None) -> Any:
        """
        获取插件数据
        :param key: 数据key
        :param plugin_id: plugin_id
        """
        target_id, instance_id = self._target_instance(plugin_id)
        return self.plugindata.get_data(target_id, key, instance_id)

    async def async_get_data(
        self, key: Optional[str] = None, plugin_id: Optional[str] = None
    ) -> Any:
        """
        异步获取插件数据

        :param key: 数据键
        :param plugin_id: 插件ID
        :return: 指定键的数据值或插件的全部数据
        """
        target_id, instance_id = self._target_instance(plugin_id)
        return await self.plugindata.async_get_data(target_id, key, instance_id)

    def del_data(self, key: str, plugin_id: Optional[str] = None) -> Any:
        """
        删除插件数据
        :param key: 数据key
        :param plugin_id: plugin_id
        """
        target_id, instance_id = self._target_instance(plugin_id)
        return self.plugindata.del_data(target_id, key, instance_id)

    def post_message(self, channel: NotificationChannel = None, mtype: MessageType = None, title: Optional[str] = None,
                     text: Optional[str] = None, image: Optional[str] = None, link: Optional[str] = None,
                     userid: Optional[str] = None, username: Optional[str] = None,
                     **kwargs):
        """
        发送消息
        """
        if not link:
            link = settings.MP_DOMAIN(
                f"#/plugins?tab=installed&id={self.get_instance_key()}"
            )
        self.chain.post_message(Message(
            channel=channel, mtype=mtype, title=title, text=text,
            image=image, link=link, userid=userid, username=username, **kwargs
        ))

    def close(self):
        pass
