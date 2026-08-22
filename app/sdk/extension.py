"""扩展本体的基类。

``_PluginBase`` 沿用历史名称而不换一个干净的拼写：每个存量扩展绑定的就是这个名字，
再给同一个类起第二个名字只会让生态里同时存在两种写法，而两种写法迟早各自漂移。

该基类混着三层——冻结契约（``init_plugin``/``get_state``）、扩展点（``provides_*`` 与
``get_*`` 钩子）、内部实现（持久化与实例定位的具体做法）。原样导出并不把三层一起冻死：
它对外承诺的公开面由 ``app/sdk/_exports.py`` 的快照登记，增删都要显式改那份快照，因此
「哪些属性是扩展能依赖的」是被审查出来的，而不是随基类改动悄悄漂移。

基类的物理归属在 SDK 而不在 ``app/plugins/``：后者是扩展的安装挂载点，要能被卷整体覆盖，
容不下任何宿主源码。旧写法 ``from app.plugins import _PluginBase`` 由
``app/runtime/compat/manifest.py`` 的符号别名继续解析到这里。

``ChainRuntimeContext`` 与它三个字段的类型 ``ChainDataPorts``、
``ChainDurableEventWriter``、``ChainRuntimeConfig`` 是构造 ``PluginChian`` 时可传入的
运行上下文：不传即沿用宿主装配好的那一份，要接管调度、事件、消息、缓存、durable 事件
写入或链层配置快照中的任何一项则按这些形状给。``ChainDurableEventWriter`` 的
``transfer_result`` 收发 ``TransferHistoryWriter`` 与 ``TransferHistoryRecord``，
两者一并出口，接管该端口才写得出实现。
"""

from abc import ABCMeta, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING, Any, List, Dict, Optional, Tuple, Type, Union

from sqlalchemy.orm import DeclarativeBase

from app.application.configuration import ChainRuntimeConfig
from app.application.history import TransferHistoryRecord, TransferHistoryWriter
from app.application.messaging.message import MessageHelper
from app.application.orchestration import ChainBase
from app.application.orchestration.context import ChainRuntimeContext
from app.application.orchestration.data import ChainDataPorts
from app.application.orchestration.durable_events import ChainDurableEventWriter
from app.db.oper.plugindata import PluginDataOper
from app.db.oper.pluginconfig import PluginConfigOper
from app.db.oper.systemconfig import SystemConfigOper
from app.runtime.config import settings
from app.runtime.events import EventManager
from app.runtime.extensions.contract.declaration import (
    ActionDeclaration,
    AgentToolDeclaration,
    CommandDeclaration,
    DashboardDeclaration,
    FilterRuleDeclaration,
    FilterRuleGroupDeclaration,
    MediaSourceDeclaration,
    MetaParserDeclaration,
    ModuleDeclaration,
    ScheduleDeclaration,
    ServiceInstanceDeclaration,
)
from app.runtime.extensions.contract.instance import (
    DEFAULT_INSTANCE_ID,
    instance_key,
    normalize_instance_id,
)
from app.runtime.extensions.lifecycle.paths import plugin_instance_path
from app.schemas.message import Message
from app.schemas.notification import ChannelCapabilities
from app.schemas.types import MessageType, NotificationChannel

if TYPE_CHECKING:
    from app.db.plugin import PluginDatabaseHandle

__all__ = [
    "ChainDataPorts",
    "ChainDurableEventWriter",
    "ChainRuntimeConfig",
    "ChainRuntimeContext",
    "PluginChian",
    "TransferHistoryRecord",
    "TransferHistoryWriter",
    "_PluginBase",
]


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

        本钩子只报描述字典，命令词不经文法校验：不合文法的命令词在登记时看不出问题，
        要到用户敲这条命令、或渠道菜单整批注册失败时才暴露。替代它的
        ``provides_commands()`` 在登记时即校验命令词并把实现收进声明。
        """
        pass

    def provides_commands(self) -> Optional[List[CommandDeclaration]]:
        """
        声明本插件提供的远程命令

        返回示例：
        [CommandDeclaration(
            cmd="/acme_sync",                    # 命令词，须以 / 开头，其后为 1 到 32 个
                                                  # 小写字母、数字或下划线；含大写字母、
                                                  # 连字符或空格的命令词会被拒绝登记
            name="同步 Acme 网盘",                # 展示名称，同时是渠道菜单上的按钮文案
            category="管理",                      # 命令分类；企业微信菜单只收录带分类的命令
            args_description="可选，指定目录路径",  # 参数描述，供智能助手与帮助文案说明用法
            data={"scope": "all"},               # 附加静态数据，调用时与上下文合并
            show=True,                           # 是否在渠道菜单与命令列表中展示
            overrides_builtin=False,             # 是否意在接管同名的内建命令
            impl=self.remote_sync,               # 命令实现
        )]

        宿主以 ``impl(data=...)`` 调用实现，``data`` 由声明的 ``data`` 与本次调用的
        ``channel``、``source``、``user``、``arg_str`` 合并而成；不接受参数的实现按无参调用。
        本钩子不提供「发一个事件」这条路径：命令要有归属、要能在登记时判定实现可调用，
        而广播事件再指望某处有监听者，宿主既校验不了也记不了账。

        命令词是扩展级标识——用户在聊天窗口里手打的就是它，命令表按它建键，渠道菜单里
        也是它，敲它时不带任何实例限定符。因此同一插件的多个实例声明同一命令词只登记
        一次（默认实例优先），各实例声明不同命令词互不影响。同一命令词被**不同插件**
        声明时双方一并失效并告警：两个插件的同名命令做的并不是同一件事，宿主无从裁决
        该把它交给谁，按加载顺序取其一会让同一个词的行为随插件加载顺序变化。

        声明一个**内建已有**的命令词要用 ``overrides_builtin=True`` 说明意在接管，声明了
        才由本插件接手、内建命令被压住；没声明即按撞车处置，该条插件命令作废、内建命令
        保持生效并告警一次。命令词自己带不出「我要替换内建行为」这个意思，接管与撞车从
        命令词分辨不出来，因此要求把意图说出来；静默接管会让用户敲一个自以为是内建的
        命令却执行了别的东西。

        同一实例内命令词唯一，重复声明的后一条会被拒绝；与 ``get_command()`` 同时声明
        同一命令词时本钩子生效。``get_command()`` 报不出接管意图，其条目与内建同名时
        恒按撞车处置。

        :return: `CommandDeclaration` 列表；插件不提供远程命令时无需实现
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

        本钩子交出的是活的触发器对象与方法对象，两者都过不了进程边界，且调度表达式
        写错要等到该任务本该触发的那一刻才失败。替代它的 ``provides_schedules()`` 把
        调度写成纯数据，登记时即可判定表达式是否成立。
        """
        pass

    def provides_schedules(self) -> Optional[List[ScheduleDeclaration]]:
        """
        声明本插件提供的定时任务

        返回示例：
        [ScheduleDeclaration(
            job_id="sync",                       # 任务标识，须形如
                                                  # [A-Za-z0-9][A-Za-z0-9._-]{0,63}；
                                                  # 只需在本插件实例内唯一
            name="定时同步",                      # 展示名称，出现在后台任务列表里
            trigger="cron",                      # 调度类型，取值为 cron、interval
                                                  # 或 date
            trigger_args={"crontab": self._cron},
                                                 # 该调度类型的参数，纯数据
            impl=self.sync,                      # 到点执行的实现，同步函数与协程
                                                  # 函数都可以
            kwargs={},                           # 调用实现时附加传递的静态参数，可选
        )]

        三种调度类型的 `trigger_args`：

        - cron：`{"crontab": "0 1 * * *"}` 给五段表达式（分 时 日 月 周），或按
          `{"hour": 1, "minute": 0}` 逐字段给出，两种写法互斥
        - interval：`{"hours": 6}`、`{"minutes": 30}` 等
        - date：`{"run_date": "2026-07-19 20:30:00"}`，时间写成 ISO 8601 字符串

        调度参数只描述调度、不承载宿主的任务选项，且必须能 JSON 序列化往返——跨进程
        时它原样成为握手报文，触发器对象与 `datetime` 这类只在进程内成立的形状过不去。
        表达式建不出触发器、实现不可调用或接不住声明的 kwargs、任务标识缺失或在本实例
        内重复，都会让整条声明被拒绝登记，一条坏声明只跳过它自己。

        同一实例的同一任务标识若同时由 `get_service()` 挂载，以本钩子为准。

        :return: `ScheduleDeclaration` 列表；插件不提供定时任务时无需实现
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

        本仪表盘展示某一族服务实例的数据时，加
        `requires_service_instance=ServiceInstanceRequirement(capability="downloader")`。
        宿主据此在仪表盘元信息里带上该族坐标供前端渲染实例选择器，并在取数时把用户
        选中的实例名按关键字 `service_instance` 交给 get_dashboard()——实现的签名接得住
        （显式声明该形参或带 **kwargs）才会收到。不声明即与服务实例无关，取数形状不变。

        也可直接返回描述字典（不包 `DashboardDeclaration`），字典形态复用
        get_dashboard_meta() 的 key/name 字段，兼容早期写法；此时无法声明专属
        配置界面。

        :return: `DashboardDeclaration` 列表；插件不提供仪表盘时无需实现
        """
        pass

    def get_auth_providers(self) -> List[Dict[str, Any]]:
        """
        声明插件提供的登录认证入口。

        本钩子是分身级写法：一个分身一个登录入口，接第二台服务器要再建一个分身。
        已进入废弃期，改用 `provides_service_instances()` 声明 capability="auth" 的
        登录入口类型，由用户在登录认证设置里配置几份就有几个入口。

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

    def provides_modules(self) -> Optional[List[ModuleDeclaration]]:
        """
        声明本插件提供的模块方法表

        返回示例：
        [ModuleDeclaration(
            methods={                            # 方法名到实现的映射，即 get_module()
                "id1": self.xxx1,                 # 那张表的声明式版本
                "id2": self.xxx2,
            },
        )]

        也可直接返回方法表字典本身（不包 `ModuleDeclaration`），宿主按字典内容
        取用方法表，兼容早期写法。

        本插件提供的能力面即 `methods` 的键，宿主已知，不必再声明一遍。

        按用户配置扇出多个具名服务实例（下载器、媒体服务器、消息通知与存储）由
        `provides_service_instances()` 承担，不在本钩子的方法表里声明。

        多来源契约（media_detail、media_credits、media_recommend、media_similar、
        person_detail、person_credits、discover、discover_board、match_media，
        及其 async_ 变体）由多个数据源共用同一方法名，按调用方传入的 source 参数区分
        来源，本钩子与 get_module() 遵循同一规则：非本插件负责的 source 必须返回 None
        让出，返回空列表会被判定为已认领而短路。本钩子挂载这些方法名适用于接管一个
        已存在的来源；提供**新**数据源请改用 `provides_media_sources()` 把展示信息与
        实现一并声明，宿主据此自动按 source 路由，无需实现自行让出，来源也才会出现在
        来源列表里供用户选择。

        同一实例的方法名若被多条来源同时挂载，优先级从低到高为 get_module()、本钩子、
        `provides_media_sources()`，高优先级的实现生效。

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

    def provides_service_instances(self) -> Optional[List[ServiceInstanceDeclaration]]:
        """
        声明本插件提供的可配置服务实例类型

        返回示例：
        [ServiceInstanceDeclaration(
            capability="downloader",             # 能力标签，宿主自带 downloader、
                                                  # mediaserver、notification、storage、
                                                  # auth 五族；本次运行的现有取值由
                                                  # app.sdk.service_instances 的
                                                  # service_capabilities() 答出
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
            config_schema={                      # 该类型配置内容的契约，宿主据此
                "type": "object",                 # 在配置写入与实例构造两处拒绝
                "properties": {                   # 畸形配置并说明原因
                    "host": {"type": "string", "title": "服务器地址"},
                    "port": {"type": "integer", "minimum": 1, "maximum": 65535},
                },
                "required": ["host"],
                "additionalProperties": False,
            },
        )]

        `config_schema` 与配置界面并列而不互相推导：界面是呈现，契约是形状。取值是
        JSON Schema 的一个受控子集——支持 string/integer/number/boolean/array/object
        六种类型与 title、description、default、enum、minimum、maximum、minLength、
        maxLength、pattern、items、minItems、maxItems、properties、required、
        additionalProperties 这些关键字，子集之外的关键字（`$ref`、`allOf` 等）会让
        整条声明被拒，因为宿主评估不了它们，悄悄忽略等于给出一份拦不住东西的契约。

        契约描述的是本类型自己的配置内容，即该族配置模型 `config` 字段的形状；`name`、
        `type`、`enabled` 这类外壳字段属于服务族，不由类型描述。走 `impl` 路径时契约
        不得声明名为 `name` 的字段——实例名由宿主填入，同名会让构造得到两个 `name`。

        暂不声明契约的类型照常登记，只是宿主不判定其配置形状，并在启动时提示一次。

        实现类的构造形状不便迁就宿主时改用 `factory=my_factory`——宿主对每条用户配置
        调用 `factory(配置对象)`，配置对象即该族配置模型的一条记录，怎么落到实例上由
        插件自己决定。`factory` 与 `impl` 二选一，同时给出或都不给出的声明被拒。

        vue 模式改用 `config_component="MyDownloaderConfig"`——本插件联邦远程中承载
        该界面的组件名，要求 `get_render_mode()` 返回 "vue"；与 `config_form` 二选一，
        同时给出视为意图不明，整条声明被拒。界面归属这条声明，不归属本插件本身。

        下载器、媒体服务器、消息通知、存储与登录认证共用本钩子，差异只在 `capability`：
        各族的取用方式相同，都是按配置扇出 N 个具名实例。用户未为该类型配置任何实例时，
        声明照常登记，只是没有实例产出。

        `impl` 交出的实现类须带齐本族取用链上宿主无保护直调的方法，缺一个整条声明被拒：

        - `capability="downloader"`：`is_inactive`、`reconnect`（十分钟重连回路直调）
        - `capability="mediaserver"`：`is_inactive`、`reconnect`（同上）
        - `capability="notification"`：`get_state`（连通性测试直调）

        这份名单在 app.sdk.service_instances 有出口：按族继承 DownloaderInstance、
        MediaServerInstance、NotificationInstance 三个协议即可让类型检查器先一步指出
        漏写的方法，继承与否不影响登记——形状按方法名判，不按 MRO 判；不继承任何协议、
        只把方法写齐的实现照常通过。名单本身由同处的 service_instance_required_methods()
        答出，能力标签的现有取值由 service_capabilities() 答出。

        名单之外的方法**一个都不必写空桩**：缺席即表示本实例不提供那项能力，宿主据此
        让开；写一个返回 None 的空桩反而是声称提供却什么都不做。存储族的形状另按
        `StorageBase` 的继承与抽象方法判定，登录认证族的握手走模块分发而不是实例方法，
        两族都不在这份名单里。走 `factory` 路径时宿主拿不到工厂产出的类型，不判形状，
        实现形状由插件自己保证。

        声明存储类型改 `capability="storage"`，`type` 即存储标识（与内建标识相同即构成
        覆盖），`impl` 给存储后端类——须继承 app.sdk.storage.StorageBase 并落地全部
        抽象方法：

        [ServiceInstanceDeclaration(
            capability="storage",
            type="u115",                         # 存储标识，同时是类型标识
            name="115网盘",
            impl=U115Storage,                    # 存储后端类，不由宿主按关键字展开构造
            multi_instance=True,
            config_schema={...},
        )]

        存储的构造协议与三族不同，**但不用自己写工厂**：不给 `factory` 时宿主用默认
        工厂，按实例归属构造后端（`后端类(storage_instance=实例名)`），配置由后端自己
        按存储令牌懒读，因此存储配置支持运行期改写后重连。要自己接管构造就给 `factory`，
        宿主把整条配置对象交给它；该族里 `factory` 是可选项而不是 `impl` 的替代项——
        `impl` 还要用来回答「令牌指的实体是谁」。

        声明登录入口类型改 `capability="auth"`，`type` 是入口类型标识，用户配几份就有
        几个登录入口——接第二台服务器不再需要建插件分身：

        [ServiceInstanceDeclaration(
            capability="auth",
            type="emby_sso",                     # 入口类型标识
            name="Emby 单点登录",                 # 类型展示名称，用于设置页
            icon="mdi-emby",                     # 入口图标，登录页按钮取它
            impl=EmbySsoEntry,                   # 完成认证握手的实现类
            multi_instance=True,                 # 每台服务器一份配置
            config_schema={...},
        )]

        登录页上那个按钮的名称取**实例名**而不是类型名，用户接两台服务器时才分辨得出
        点的是哪一台。每个入口另有一个身份绑定标识，即写进第三方身份绑定表 `provider`
        列的取值：用户不填时宿主按 `类型@实例名` 派生，填了就用填的那个。插件在认证
        握手成功后调用 `create_plugin_auth_ticket_for_identity(provider_id=入口标识, ...)`
        时原样回传登录页交来的入口标识即可，不要自行拼接——宿主不改写这个取值，它一变
        就是另一个身份命名空间。第三方站点单点登录通常「一种类型一份配置」，声明
        `multi_instance=False`。

        该类型只该被配一份时加上 `multi_instance=False`——例如一个全局唯一的接入点。
        此时用户若配了多份，宿主按该族的默认调用目标裁决：有显式默认就用它，没有默认
        或默认已停用则整个类型不产出实例并报错列出候选，绝不替用户挑一份。该字段与本
        插件建了几个实例无关：插件实例是插件自己的分身，`multi_instance` 描述的是本
        类型的配置列表允许有几条记录。缺省为 True，即按配置扇出多个实例。

        :return: `ServiceInstanceDeclaration` 列表；插件不提供服务实例类型时无需实现
        """
        pass

    def provides_meta_parsers(self) -> Optional[List[MetaParserDeclaration]]:
        """
        声明本插件提供的名称解析器

        返回示例：
        [MetaParserDeclaration(
            parser_id="llm",                     # 解析器标识，须形如
                                                  # [A-Za-z0-9][A-Za-z0-9._-]{0,63}；
                                                  # 只需在本插件实例内唯一
            name="大模型识别",                    # 展示名称，供用户在顺序配置里辨认
            priority=100,                        # 默认顺序，数值越小越靠前，仅在
                                                  # 用户尚未排到该解析器时生效
            impl=self.parse,                     # 解析环实现，接收一个
                                                  # MetaParseRequest，交回本环认为
                                                  # 成立的 ParsedMeta；不认领返回 None
        )]

        解析环拿到的 `request.parsed` 是内建识别与上游各环累积出的结果，因此既可以
        只补空位，也可以改写上游填错的字段——覆盖会被宿主记进字段级溯源，用户能查到
        某个字段由谁填、原值是什么。要把上游填错的字段清空，把字段名列进
        `ParsedMeta.clears`；单靠 None 表达的是「本环对该字段无话可说」。

        名称按 `cn_name`/`en_name` 两个字段表达，`MetaBase.name` 是二者的派生属性，
        不是可声明的字段。

        实现必须是同步函数：识别是同步链路，协程实现会被拒绝登记。一环抛异常只跳过
        这一环，整条链继续，内建识别保证门面永远返回可用结果。

        执行顺序取用户排定的持久配置，声明的 priority 只是默认初始位置；用户也可以
        在该配置里单独关掉某一环。

        :return: `MetaParserDeclaration` 列表；插件不提供名称解析器时无需实现
        """
        pass

    def provides_filter_rules(self) -> Optional[List[FilterRuleDeclaration]]:
        """
        声明本插件提供的筛选规则

        返回示例：
        [FilterRuleDeclaration(
            rule_id="ACMEWEB",                   # 规则标识，会作为原子进入规则串
                                                  # 语法，只能由字母和数字组成且必须
                                                  # 以字母开头或形如「数字+字母」开头
                                                  # （BLU、4K、1080P），不合文法的
                                                  # 标识会被拒绝登记
            name="Acme 官组 WEB-DL",              # 规则展示名称
            include=r"Acme.*WEB-?DL",            # 包含项正则
            exclude=r"HDTV",                     # 排除项正则
            size_range="1024-8192",              # 大小范围（MB）
            seeders="5",                         # 最少做种人数
            publish_time="60-1440",              # 发布时间（分钟）
        )]

        五个条件字段的形状与用户自定义规则完全相同，至少要给出一个；正则须能编译、
        数值区间须能转换，不合契约的声明会被拒绝登记，不留到逐条匹配时才失败。

        本钩子只提供规则**数据**，判定逻辑仍由宿主的规则引擎执行——插件不提供可执行
        的判定谓词。筛选是每颗种子每条规则的热路径且已经加速化，让插件提供判定函数
        会让每颗种子都跨回插件代码。

        规则标识的优先级为「内建 < 插件 < 用户」：与内建标识相同即覆盖内建定义，
        用户自定义的同名规则则永远优先于插件。同一标识被**不同插件**声明时双方一并
        失效并告警——规则是数据不是实现，宿主无从裁决哪一份语义为准，按加载顺序取
        其一会让筛选行为随插件加载顺序变化。

        :return: `FilterRuleDeclaration` 列表；插件不提供筛选规则时无需实现
        """
        pass

    def provides_filter_rule_groups(self) -> Optional[List[FilterRuleGroupDeclaration]]:
        """
        声明本插件提供的筛选规则组

        返回示例：
        [FilterRuleGroupDeclaration(
            name="Acme 高码率优先",               # 规则组名称，用户在搜索、订阅、洗版
                                                  # 与默认规则四个场景里按此名称引用
            rule_string="ACMEWEB & 4K > ACMEWEB & 1080P",
                                                 # 规则串，> 分隔的层级即优先级从高到低，
                                                  # 同层内用 &、|、! 组合规则标识
            media_type="电影",                    # 适用媒体类型，为空表示全部
            category=None,                       # 适用媒体类别，为空表示全部
        )]

        规则串会被校验能否解析——括号配对、优先级层级非空、每个原子都合规则ID文法；
        不合契约的声明会被拒绝登记。规则串引用的标识是否存在不在校验范围内：规则组
        可以引用内建规则、用户自定义规则，或另一个插件提供的规则，登记本条声明时
        它们未必都已就位。

        组名的冲突处置与规则标识相同：用户自定义的同名规则组优先，不同插件声明同一
        组名时双方一并失效并告警。

        :return: `FilterRuleGroupDeclaration` 列表；插件不提供筛选规则组时无需实现
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

        本钩子只报展示信息，实现另写在 ``get_module`` 里，两处各自独立合法，写漏一处
        要到用户使用时才暴露。替代它的 ``provides_media_sources()`` 把两者合成一条声明，
        登记时即可判定完整性。
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
            methods={                            # 本来源的识别、搜索、图片与 NFO
                "media_detail": self.detail,      # 刮削实现，形状与 provides_modules()
                "discover": self.discover,        # 的方法表相同；缺了它整条声明被拒
            },
        )]

        展示信息与实现必须写在同一条声明里，缺任一半都会被拒绝登记：只报展示信息的
        来源会出现在来源列表里、用户选中后却无实现可调用；只挂实现的来源能被调用却
        进不了来源列表，用户在界面上选不到它。两种残缺各自都是合法的半条声明，只有
        合成一条契约校验才拦得住，否则要到用户使用时才暴露。

        methods 里按 source 收窄的多来源契约方法（media_detail、media_credits、
        media_recommend、media_similar、person_detail、person_credits、discover、
        discover_board、match_media 及其 async_ 变体）由宿主按本条声明的 media_source
        自动路由：调用带的来源不是本来源时宿主直接让出，实现不会被触达。因此这些方法
        只需处理本来源的请求，不必自己比对 source，也不会因误返回空列表而把该契约下
        的其它来源一并拦掉。其余方法名原样挂载，不做路由。

        本来源的能力面（识别、搜索、详情、推荐、发现、刮削）由宿主按 methods 里的方法
        名推导，无需也无从另行声明：只挂 discover 的来源不会出现在元数据识别源的选项
        里，用户因此选不到一个选中后无人应答的来源。反过来，不支持的方法不要补空桩，
        写了桩就等于认领这个能力面，误返回空列表还会短路掉同一契约下的其它来源。

        同一插件实例可以声明多个数据源，各来源的同名契约方法互不覆盖，宿主按来源分别
        路由，能力面也各按自己的方法表推导。接管一个已存在的来源（而不是提供新来源）
        仍走 provides_modules()——那种写法没有新来源要进列表，也需要实现自己按 source
        认领。

        也可直接返回描述字典（不包 `MediaSourceDeclaration`），字典按 media_source、
        name、media_types、methods 四个键取值，完整性要求与声明对象相同。

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
            requires_service_instance=ServiceInstanceRequirement(
                capability="downloader",         # 本动作作用于哪一族服务实例，可选
                types=("qbittorrent",),          # 收窄到该族的哪几个类型，留空即不收窄
            ),
        )]

        声明了 requires_service_instance，宿主就在工作流编辑器里渲染实例选择器、校验
        用户选中的实例仍然存在，并在调用时把实例名按关键字 ``service_instance`` 一并
        交给实现；实现的签名因此要能接收这个关键字。不声明即本动作与服务实例无关，
        调用形状不变。声明里不写实例名——那是用户在设置页自填的持久数据，声明期不存在。

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
        1、工具类必须继承自 app.sdk.agent.MoviePilotTool
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
            impl=MyTool,                         # 工具实现类，须继承
                                                  # app.sdk.agent.MoviePilotTool
                                                  # 并实现异步的 run 方法；不合契约的声明
                                                  # 会被拒绝登记，不留到调用时才失败
        )]

        工具标签取 app.sdk.agent.ToolTag：只读子代理按 ToolTag.Read 筛选可用工具，
        不带该标签的工具在只读场景里一次都不会被选中。

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
