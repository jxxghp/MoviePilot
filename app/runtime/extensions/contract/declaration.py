"""扩展声明式注册的声明载体。

扩展经 ``provides_*`` 钩子交出的是声明而不是裸实现：声明的数据字段描述「提供了
什么」，``impl`` 字段携带「用什么提供」。这样拆分是为了让声明面在宿主换实现语言、
扩展改为独立进程时仍然成立——届时 ``impl`` 不参与序列化，其余字段原样成为握手
报文，契约校验从内省对象退化为校验声明数据。

判据见 docs/plugin-extension-architecture.md。
"""

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Dict, List, Mapping, Optional, Tuple

from app.schemas.rule import RULE_CONDITION_FIELDS


@dataclass(frozen=True, slots=True)
class ExtensionDeclaration:
    """
    扩展声明的公共部分

    「提供了什么」由各声明自身的数据字段回答：带方法表的两种声明由 ``methods`` 的键
    回答，其余声明由自己的标识回答（``job_id``、``cmd``、``action_id``、工具名）。
    公共部分只承载「用什么提供」。

    :param impl: 实现该声明的对象，进程内直接使用；跨进程时不参与传输
    """

    impl: Optional[Any] = None


@dataclass(frozen=True, slots=True)
class ServiceInstanceRequirement:
    """
    声明「本扩展点作用于哪一族服务实例」

    一个纯扩展型插件只有一个分身，用户真正配了多份的是它提供的**服务实例**：配了
    三台下载器就该有三台可选。这两个「多」不在同一根轴上，因此动作与仪表盘要指到
    某台下载器，靠的不是分身标识，而是本声明。

    宿主拿这份声明做三件事，字段形状即由这三件事决定：按 ``capability`` 找到该族
    的配置列表当作实例选择器的数据源、按 ``types``（给了的话）收窄候选、以及在用户
    选中的实例消失时按同一对坐标说清是哪一族的哪个实例不在了。

    **不含实例名**：实例名是用户在设置页自填的持久数据，声明期根本不存在——插件被
    加载时用户可能一台下载器都还没配。声明随插件版本静态发布，实例名随用户增删漂移，
    把后者钉进前者只会得到一个一改配置就失效、且插件作者无从修正的引用。选哪一台是
    用户的选择，宿主负责把选择项交给他并校验他的选择，而不是替他写死。

    ``types`` 留空表示该族任意类型都行；给出时只有类型落在其中的实例才是候选。收窄
    有实际指称：一个提供了某种下载器类型的插件，它的动作多半只对自家那种类型成立，
    不收窄就会把别家类型的实例也列进选择器，选中后要到运行时才炸。

    :param capability: 能力标签，取值须与服务族登记表中的族一致；判定见
        `app.runtime.extensions.admission.service_instance_requirement`
    :param types: 收窄到该族的哪几个类型标识，留空表示不收窄
    """

    capability: str = ""
    types: Tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AgentToolDeclaration(ExtensionDeclaration):
    """
    智能体工具声明

    ``name``/``description`` 是工具向宿主自报的标识与说明，作为声明数据独立于
    ``impl``：宿主换实现语言、扩展改为独立进程时，这两个字段随其余声明数据
    原样成为握手报文，``impl`` 不参与传输。

    :param name: 工具名，供 Agent 识别并调用
    :param description: 工具描述，供 Agent 判断何时调用该工具
    """

    name: str = ""
    description: str = ""


@dataclass(frozen=True, slots=True)
class ModuleDeclaration(ExtensionDeclaration):
    """
    模块方法表声明

    ``methods`` 是原 ``_PluginBase.get_module()`` 那张「方法名到实现」表的声明式
    版本，宿主按方法名把请求分发到其中的可调用对象。跨进程时该表退化为方法名
    清单：可调用对象本身不参与序列化，握手报文只带方法名，具体调用改由对端进程
    按同名方法自行响应。

    本声明只描述方法表。按用户配置扇出多个具名服务实例是另一回事，由
    `ServiceInstanceDeclaration` 承担——两者混在一条声明里会让「提供一批方法」与
    「提供一族可配置实例」共用同一个入口，而宿主对二者的装载路径本就不同。

    :param methods: 方法名到可调用对象的映射，跨进程时退化为方法名清单
    """

    methods: Mapping[str, Any] = MappingProxyType({})


@dataclass(frozen=True, slots=True)
class ServiceInstanceDeclaration(ExtensionDeclaration):
    """
    可配置服务实例类型声明

    服务实例与其它扩展点的区别在于「有没有」不是终点：用户在设置页里按类型新建
    具名实例，每个实例带自己的一份配置。因此声明描述的是**类型**，宿主按该类型
    下的每条用户配置构造一个实例。

    ``multi_instance`` 回答「用户能为这个类型配几份」：为 True 时该类型下有几条
    用户配置就有几个具名实例，为 False 时该类型只认一份配置。取值由声明表达而不
    由服务族推定——同一族里两种都存在，例如认证器接第三方站点单点登录时全局只有
    一份配置，接媒体服务器单点登录时需要每台服务器一份。

    该字段与「扩展本体是否分身」正交，两者回答的不是同一个问题：分身是扩展自己
    按 ``plugin_id@instance_id`` 扇出的多个行为体，各自独立运行、各自持有配置；
    ``multi_instance`` 描述的是本类型的配置列表允许有几条记录，与声明它的扩展建
    了几个分身无关。一个只建了默认分身的扩展照样可以提供多实例类型，一个建了多
    个分身的扩展提供的类型也可以只认一份配置。

    ``capability`` 是该类型属于哪一族服务的语义标签，取值须是服务族登记表
    （`app.runtime.extensions.registry.service_family`）中已登记的族，宿主自带
    下载器、媒体服务器、消息通知、存储与登录认证五族。各族共用这一条声明，
    差异只在该标签：取用链是同一条——同一张服务实例表，按「能力标签加类型
    标识」取用，形状没有区别，因此不按业务族拆成多个钩子，差异作为参数声明出来。

    登录认证族（``capability="auth"``）的每条用户配置即登录页上的一个入口：媒体服务器
    单点登录声明 ``multi_instance=True``（每台一份），第三方站点单点登录声明
    ``multi_instance=False``（一种类型一份）。登录入口列表由「该族配置加本登记」直接
    投影，不经实例构造——登录页在任何用户会话之前就要渲染，一次构造失败不能让整族入口
    消失。``impl``/``factory`` 仍按通用规则二选一，它构造的是完成认证握手的那个对象。

    构造方式二选一，宿主对该类型下的每条用户配置执行其一：

    - ``impl``：实现类，宿主按 ``impl(name=配置名, **配置内容)`` 构造，要求构造
      签名能接受关键字 ``name``
    - ``factory``：可调用对象，宿主按 ``factory(配置对象)`` 构造，配置对象即该族
      配置模型的一条记录，怎么落到实例上由扩展自行决定

    两者互斥，同时给出视为意图不明，整条声明被拒；都不给出同样被拒，宿主无从
    构造实例。二者都是进程内快路径，跨进程时均不参与序列化。

    **存储族（``capability="storage"``）的构造协议另有一套**，因此这两条路径在
    该族里的读法不同：``impl`` 是存储后端类，须继承
    ``app.modules._base.storage.StorageBase`` 并落地全部抽象方法，宿主**不**按
    ``impl(name=..., **config)`` 构造它；``type`` 同时是存储标识，按令牌取用的登记
    从这里来。构造一律走工厂——不给 ``factory`` 时宿主用默认工厂
    （`app.runtime.extensions.registry.storage.storage_instance_factory`），按实例
    归属交付后端、配置由后端自己按存储令牌懒读，扩展作者一行工厂都不用写；给了
    ``factory`` 就走扩展自己那一个，宿主只交出整条配置对象。存储的构造不经关键字
    展开，因此 ``config_schema`` 在该族里没有保留字段名。

    该服务类型的专属配置界面二选一：

    - ``config_form``：vuetify 模式，(组件树, 默认数据) 二元组
    - ``config_component``：vue 模式，本扩展联邦远程中承载该界面的组件名，
      要求扩展的 ``get_render_mode()`` 返回 ``"vue"``

    两者互斥，同时给出视为意图不明，整条声明被拒；都不给出合法，表示该类型
    没有专属界面，前端沿用内建类型的渲染方式。界面归属这条声明，不归属声明
    它的扩展。

    ``config_schema`` 与配置界面回答的不是同一个问题：界面是呈现，交给前端；契约是
    形状，宿主据此在配置写入与实例构造两处拒绝畸形配置。二者并列而不互相推导——vue
    模式下界面是扩展自带的联邦组件，宿主看不见组件树，推不出配置形状。契约描述的是
    该类型自己的配置内容，即该族配置模型 ``config`` 字段的形状；``name``/``type``/
    ``enabled`` 这类外壳字段属于服务族，不由类型描述。取值是 JSON Schema 的一个受控
    子集，判据与关键字集合见 `app.runtime.extensions.contract.config_schema`。

    :param capability: 能力标签，取值须是服务族登记表中已登记的族
    :param type: 类型标识，与该族配置模型的 ``type`` 字段取值对应，例如 qbittorrent；
        存储族里它同时是存储标识，例如 u115
    :param name: 类型展示名称
    :param icon: 类型展示图标，取值为前端可解析的图标标识；未声明时由前端按类型自行
        决定呈现，登录认证族的入口按钮即取此图标
    :param multi_instance: 用户能否为该类型配置多份，默认为 True
    :param factory: 接收单条服务配置并返回实例的可调用对象；与 ``impl`` 互斥
    :param config_form: (组件树, 默认数据) 二元组，vuetify 模式；与
        ``config_component`` 互斥
    :param config_component: 联邦远程中的组件名，vue 模式；与 ``config_form`` 互斥
    :param config_schema: 该类型配置内容的契约，JSON Schema 受控子集；未声明时宿主
        不对该类型的配置内容做形状判定
    """

    capability: str = ""
    type: str = ""
    name: str = ""
    icon: Optional[str] = None
    multi_instance: bool = True
    factory: Optional[Any] = None
    config_form: Optional[Tuple[List[Dict[str, Any]], Dict[str, Any]]] = None
    config_component: Optional[str] = None
    config_schema: Optional[Dict[str, Any]] = None


@dataclass(frozen=True, slots=True)
class MetaParserDeclaration(ExtensionDeclaration):
    """
    名称解析器声明

    ``impl`` 是解析环的实现：接收一个 `MetaParseRequest`，交回本环认为成立的
    `ParsedMeta`，返回 None 即本次不认领。宿主按用户排定的顺序把各环串成管道，
    每一环拿到的是上游累积出的结果，因此下游既能补空位，也能改写上游填错的字段——
    代价是宿主为每个字段记录来源与被覆盖前的取值。

    解析环只能贡献，拿不到「继续或中断」的开关：一环抛异常只跳过这一环，整条
    链继续，内建解析保证门面永远返回可用结果。

    ``priority`` 只是该解析器初次出现在顺序表里的默认位置。实际顺序取用户排定的
    持久配置——顺序即语义，谁先跑决定谁的结果被覆盖，这种选择不能由用户看不见的
    声明值或登记先后决定。

    :param parser_id: 解析器标识，取值须形如 ``[A-Za-z0-9][A-Za-z0-9._-]{0,63}``；
        同一扩展的多个分身各声明一次即多个各自成立的解析环，宿主按实例键为其分别
        编号，因此标识只需在声明它的实例内唯一
    :param name: 解析器展示名称，供用户在顺序配置里辨认
    :param priority: 默认顺序，数值越小越靠前，仅在用户尚未排到该解析器时生效
    """

    parser_id: str = ""
    name: str = ""
    priority: int = 0


@dataclass(frozen=True, slots=True)
class MediaSourceDeclaration(ExtensionDeclaration):
    """
    媒体数据源声明

    一条声明同时回答「有这么一个来源」与「它由谁实现」：``media_source``/``name``/
    ``media_types`` 是来源自身的展示信息，宿主据此聚合来源列表；``methods`` 是识别、
    搜索、图片与 NFO 刮削的实现，形状与 `ModuleDeclaration.methods` 相同。两者合在
    一条里，是因为分开声明时各自都独立合法：只报展示信息的来源在界面上选得到、调用
    却落空，只挂实现的来源能被调用却进不了来源列表，两种残缺都要等到用户使用时才
    暴露。合成一条后契约校验能在登记时判定完整性。

    ``methods`` 里按 ``source`` 收窄的多来源契约方法（media_detail、media_credits、
    media_recommend、media_similar、person_detail、person_credits、discover、
    discover_board、match_media 及其 ``async_`` 变体）由宿主按本声明的
    ``media_source`` 自动路由：调用带的来源不是本来源时宿主直接让出，不触达实现。
    因此这些方法只需处理本来源的请求，既不必自己比对 ``source``，也不会因误返回空
    列表而把该契约下的其它来源一并拦截。其余方法名原样挂载，不做路由。

    跨进程时 ``methods`` 与 `ModuleDeclaration.methods` 一样退化为方法名清单，展示
    信息原样成为握手报文——路由所依据的 ``media_source`` 本身就是声明数据，异语言
    宿主拿到同一份报文即可做同样的路由。

    :param media_source: 规范媒体来源标识，须能被 ``MediaSource`` 解析——内置常量
        或形如 ``[a-z][a-z0-9._-]{0,63}`` 的插件扩展标识
    :param name: 数据源展示名称
    :param media_types: 支持的媒体类型；留空时由消费方按自身默认值处理
    :param methods: 方法名到可调用对象的映射，跨进程时退化为方法名清单
    """

    media_source: str = ""
    name: str = ""
    media_types: Tuple[str, ...] = ()
    methods: Mapping[str, Any] = MappingProxyType({})


@dataclass(frozen=True, slots=True)
class ActionDeclaration(ExtensionDeclaration):
    """
    工作流动作声明

    ``impl`` 是动作的实现函数：首个位置参数固定为 ``ActionContext`` 实例，返回
    ``(执行状态, 更新后的 ActionContext)`` 二元组，与既有 ``get_actions()`` 对
    实现函数的要求一致。

    ``requires_service_instance`` 声明本动作作用于哪一族服务实例。声明了它，宿主
    就在工作流编辑器里渲染实例选择器、校验用户选中的实例仍然存在，并在调用时把选中
    的实例名按关键字 ``service_instance`` 一并交给 ``impl``。省略即本动作与服务实例
    无关，调用形状一字不改。

    :param action_id: 动作标识，工作流按此标识调用该动作
    :param name: 动作展示名称
    :param kwargs: 调用该动作实现时附加传递的静态参数
    :param requires_service_instance: 本动作作用于哪一族服务实例；为 None 表示无关
    """

    action_id: str = ""
    name: str = ""
    kwargs: Mapping[str, Any] = MappingProxyType({})
    requires_service_instance: Optional[ServiceInstanceRequirement] = None


@dataclass(frozen=True, slots=True)
class ScheduleDeclaration(ExtensionDeclaration):
    """
    定时任务声明

    ``impl`` 是到点执行的实现：宿主按 ``impl(**kwargs)`` 调用它，同步函数与协程
    函数都接受。返回 ``(False, 失败原因)`` 二元组时宿主按执行失败记账，与既有
    ``get_service()`` 交出的回调完全一致。

    调度本身是**纯数据**：``trigger`` 说清是哪一类调度，``trigger_args`` 给出该类
    调度的参数。这与 ``get_service()`` 直接交出一个构造好的 ``CronTrigger`` 对象
    是两回事——触发器对象过不了进程边界，而「cron 加五段表达式」这样的数据过得去，
    异语言宿主拿到同一份报文即可自行建出等价调度。同理，表达式写错在登记那一刻
    就被判出来，不必等到该跑的那一刻。

    ``trigger`` 的取值与各自的 ``trigger_args``：

    - ``"cron"``：``crontab`` 给五段表达式（``分 时 日 月 周``），或按
      ``minute``/``hour``/``day``/``month``/``day_of_week``/``week``/``year``/
      ``second`` 逐字段给出，两种写法互斥
    - ``"interval"``：``weeks``/``days``/``hours``/``minutes``/``seconds``
    - ``"date"``：``run_date`` 给 ISO 8601 时间字符串

    ``trigger_args`` 只描述调度，不承载宿主的任务选项；它必须能 JSON 序列化往返，
    因此时间一律写成字符串而不是 ``datetime`` 对象。

    :param job_id: 任务标识，取值须形如 ``[A-Za-z0-9][A-Za-z0-9._-]{0,63}``；同一
        扩展的多个分身各声明一次即多个各自成立的任务，宿主按实例键为其分别编号，
        因此标识只需在声明它的实例内唯一
    :param name: 任务展示名称，出现在后台任务列表里
    :param trigger: 调度类型，取值为 cron、interval 或 date
    :param trigger_args: 该调度类型的参数，纯数据，须能 JSON 序列化往返
    :param kwargs: 调用实现时附加传递的静态参数
    """

    job_id: str = ""
    name: str = ""
    trigger: str = ""
    trigger_args: Mapping[str, Any] = MappingProxyType({})
    kwargs: Mapping[str, Any] = MappingProxyType({})


@dataclass(frozen=True, slots=True)
class CommandDeclaration(ExtensionDeclaration):
    """
    远程命令声明

    ``impl`` 是命令的实现函数，宿主以 ``impl(data=...)`` 调用它，``data`` 由本声明的
    ``data`` 字段与本次调用的渠道、来源、用户与参数串合并而成；不接受参数的实现按无参
    调用。声明不提供事件类型这条路径——命令要有归属、要能在登记时判定「实现可调用」，
    而广播一个事件再指望某处有监听者，宿主无从校验也无从记账。

    ``cmd`` 是用户在聊天窗口里手打的那个词，它同时是宿主命令表的键与外部渠道菜单的
    命令名，必须合命令词文法，否则登记时看不出问题，要到用户敲这条命令、或渠道菜单
    整批注册失败时才炸。

    ``overrides_builtin`` 是接管同名内建命令的意图声明。命令词不像存储后端标识那样
    指称一个共同的外部对象，声明它本身说明不了作者是想接管内建行为还是根本不知道宿主
    已有同名命令；把意图单列一个字段，两种情形才分得开：声明了即按接管处置，没声明就
    是撞车，该条插件命令作废、内建命令保持生效。

    :param cmd: 命令词，以 ``/`` 开头，须合命令词文法
    :param name: 命令展示名称，同时用作渠道菜单上的按钮文案
    :param category: 命令分类，为空表示不分类；企业微信菜单只收录带分类的命令
    :param args_description: 参数描述，供智能助手与帮助文案说明该命令接受什么参数
    :param data: 调用实现时附加传递的静态数据，与本次调用的上下文合并后交给实现
    :param show: 是否在渠道菜单与命令列表中展示
    :param overrides_builtin: 是否意在接管同名的内建命令；命令词与内建不撞时该字段无作用
    """

    cmd: str = ""
    name: str = ""
    category: Optional[str] = None
    args_description: Optional[str] = None
    data: Mapping[str, Any] = MappingProxyType({})
    show: bool = True
    overrides_builtin: bool = False


@dataclass(frozen=True, slots=True)
class FilterRuleDeclaration(ExtensionDeclaration):
    """
    筛选规则声明

    规则是纯数据：五个条件字段的形状与用户自定义规则 `CustomRule` 完全相同，宿主把
    声明投影成同一形状后并入运行期规则集，因此规则引擎（含 Rust 快路）分辨不出一条
    规则来自内建、插件还是用户。本声明不携带 ``impl``——判定逻辑仍由宿主的规则引擎
    执行，扩展只提供参数。

    ``rule_id`` 会作为原子进入规则串的语法，必须合规则ID文法，否则用户把它写进规则组
    时才会解析失败；契约校验在登记时即拒绝不合文法的标识。

    五个条件字段至少要给出一个：一条不带任何条件的规则对每颗种子都判定通过，等同于
    没有这条规则，声明它多半是笔误而不是意图。

    :param rule_id: 规则标识，作为原子出现在规则串中，须合规则ID文法
    :param name: 规则展示名称
    :param include: 包含项正则
    :param exclude: 排除项正则
    :param size_range: 大小范围（MB），形如 ``1024-4096``、``>1024``、``<4096``
    :param seeders: 最少做种人数
    :param publish_time: 发布时间（分钟），形如 ``60`` 或 ``60-1440``
    """

    rule_id: str = ""
    name: str = ""
    include: Optional[str] = None
    exclude: Optional[str] = None
    size_range: Optional[str] = None
    seeders: Optional[str] = None
    publish_time: Optional[str] = None


@dataclass(frozen=True, slots=True)
class FilterRuleGroupDeclaration(ExtensionDeclaration):
    """
    筛选规则组声明

    规则组把规则标识按布尔表达式与优先级组合成一套可整体引用的筛选方案，用户在搜索、
    订阅、洗版与默认规则四个场景里按 ``name`` 引用它。``rule_string`` 的书写顺序即
    优先级：``>`` 分隔的层级从高到低，同层内用 ``&``/``|``/``!`` 组合规则标识。

    ``name`` 既是标识也是展示名——四个场景保存的就是组名，两者不是可以分开的东西。

    :param name: 规则组名称，用户在四个场景里按此名称引用
    :param rule_string: 规则串，形如 ``CNSUB & 4K & !BLU > CNSUB & 1080P``
    :param media_type: 适用媒体类型，为空表示全部；取值为「电影」或「电视剧」
    :param category: 适用媒体类别，为空表示全部；取值为二级分类名
    """

    name: str = ""
    rule_string: str = ""
    media_type: Optional[str] = None
    category: Optional[str] = None


@dataclass(frozen=True, slots=True)
class DashboardDeclaration(ExtensionDeclaration):
    """
    仪表盘声明

    声明的是「有哪些仪表盘、长什么样」；「当前该显示什么数据」仍由带参数的
    ``get_dashboard(key, **kwargs)`` 在每次请求时实时取用，两者不是一回事。

    该仪表盘的专属界面二选一，与 ``ServiceInstanceDeclaration.config_form``/
    ``config_component`` 同一套语义：``config_form`` 是 vuetify 模式下的
    （组件树, 默认数据）二元组，``config_component`` 是 vue 模式下本扩展联邦
    远程中承载该仪表盘的组件名，要求扩展的 ``get_render_mode()`` 返回 ``"vue"``。
    两者互斥，同时给出视为意图不明，整条声明被拒；都不给出合法，表示该仪表盘
    没有随声明附带的初始界面。

    ``requires_service_instance`` 声明本仪表盘展示哪一族服务实例的数据。声明了它，
    宿主就在仪表盘元信息里带上该族的坐标供前端渲染实例选择器，并在取数时把选中的
    实例名解析出来交给 ``get_dashboard``。省略即本仪表盘与服务实例无关，取数形状
    一字不改。

    :param key: 仪表盘 key，在插件实例范围内唯一；单仪表盘插件可留空，代表
        插件的默认仪表盘
    :param name: 仪表盘展示名称
    :param config_form: (组件树, 默认数据) 二元组，vuetify 模式；与
        ``config_component`` 互斥
    :param config_component: 联邦远程中的组件名，vue 模式；与 ``config_form`` 互斥
    :param requires_service_instance: 本仪表盘展示哪一族服务实例的数据；为 None 表示无关
    """

    key: str = ""
    name: str = ""
    config_form: Optional[Tuple[List[Dict[str, Any]], Dict[str, Any]]] = None
    config_component: Optional[str] = None
    requires_service_instance: Optional[ServiceInstanceRequirement] = None


def declaration_config_form(
    declaration: Any,
) -> Optional[Tuple[List[Dict[str, Any]], Dict[str, Any]]]:
    """
    读取声明自带的配置界面

    :param declaration: 扩展声明
    :return: (组件树, 默认数据) 二元组；声明未带配置界面时为 None
    """
    return getattr(declaration, "config_form", None)


def declaration_config_schema(declaration: Any) -> Any:
    """
    读取声明自带的配置契约

    按原值返回而不做形状归一：取值合法性由契约校验判定，此处先归一会把错误取值悄悄
    变成一个合法答案。

    :param declaration: 扩展声明
    :return: config_schema 字段的原始值；字段缺失时为 None
    """
    return _declared_field(declaration, "config_schema")


def declaration_config_component(declaration: Any) -> Optional[str]:
    """
    读取声明自带的 vue 模式配置界面组件名

    :param declaration: 扩展声明
    :return: 组件名；未声明或为空白时为 None
    """
    return _declared_text(declaration, "config_component")


def declaration_agent_tool_identity(declaration: Any) -> Tuple[Optional[str], Optional[str]]:
    """
    读取声明自报的工具名与描述

    :param declaration: 智能体工具声明，或插件直接交出的实现类
    :return: (工具名, 工具描述) 二元组；对应字段缺失、非字符串或为空白时该位为 None
    """
    return _declared_text(declaration, "name"), _declared_text(declaration, "description")


def _declared_text(declaration: Any, field: str) -> Optional[str]:
    """
    读取声明对象上的非空字符串字段

    :param declaration: 声明对象
    :param field: 字段名
    :return: 去除首尾空白后的字符串；字段缺失、非字符串或全为空白时为 None
    """
    value = getattr(declaration, field, None)
    return value.strip() if isinstance(value, str) and value.strip() else None


def declaration_methods(declaration: Any) -> Optional[Mapping[str, Any]]:
    """
    读取声明的方法表

    兼容插件直接交出方法表字典而不包 `ModuleDeclaration` 的写法：此时方法表
    即声明本身。

    :param declaration: `ModuleDeclaration` 实例，或插件直接交出的方法表字典
    :return: 方法名到可调用对象的映射；取不到时为 None
    """
    if isinstance(declaration, Mapping):
        return declaration
    methods = getattr(declaration, "methods", None)
    return methods if isinstance(methods, Mapping) else None


def declaration_impl(declaration: Any) -> Optional[Any]:
    """
    读取声明携带的实现对象

    兼容扩展直接交出实现类而非声明对象的写法：此时实现即声明本身。

    :param declaration: 扩展声明或实现对象
    :return: 实现对象；取不到时为 None
    """
    if declaration is None:
        return None
    impl = getattr(declaration, "impl", None)
    return impl if impl is not None else declaration


def _declared_field(declaration: Any, field: str) -> Any:
    """
    读取声明字段的原始值，兼容属性对象与映射两种载体

    媒体数据源、工作流动作与仪表盘的兼容旧写法是插件直接交出描述字典而非
    声明对象，字典没有属性访问，须按载体类型分别取值。

    :param declaration: 声明对象，或插件直接交出的描述字典
    :param field: 字段名
    :return: 字段原始值；字段缺失时为 None
    """
    if isinstance(declaration, Mapping):
        return declaration.get(field)
    return getattr(declaration, field, None)


def _declared_field_text(declaration: Any, field: str) -> Optional[str]:
    """
    读取声明字段的非空字符串值，兼容属性对象与映射两种载体

    :param declaration: 声明对象，或插件直接交出的描述字典
    :param field: 字段名
    :return: 去除首尾空白后的字符串；字段缺失、非字符串或全为空白时为 None
    """
    value = _declared_field(declaration, field)
    return value.strip() if isinstance(value, str) and value.strip() else None


def declaration_media_source_identity(declaration: Any) -> Tuple[Optional[str], Optional[str]]:
    """
    读取声明自报的数据源标识与展示名称

    :param declaration: `MediaSourceDeclaration` 实例，或插件直接交出的描述字典
    :return: (数据源标识, 展示名称) 二元组；对应字段缺失、非字符串或为空白时该位为 None
    """
    return (
        _declared_field_text(declaration, "media_source"),
        _declared_field_text(declaration, "name"),
    )


def declaration_media_source_methods(declaration: Any) -> Any:
    """
    读取媒体数据源声明携带的方法表

    按原值返回而不做形状归一：取值合法性由契约校验判定，此处先归一会把错误取值悄悄
    变成一个合法答案。字典形态的声明按 ``methods`` 键取值，不套用 `declaration_methods`
    的「字典即方法表」回落——媒体数据源的字典还承载展示字段，整份字典不是方法表。

    :param declaration: `MediaSourceDeclaration` 实例，或插件直接交出的描述字典
    :return: methods 字段的原始值；字段缺失时为 None
    """
    return _declared_field(declaration, "methods")


def declaration_media_types(declaration: Any) -> Optional[Tuple[Any, ...]]:
    """
    读取声明自报的支持媒体类型

    :param declaration: `MediaSourceDeclaration` 实例，或插件直接交出的描述字典
    :return: 媒体类型序列转换成的元组；字段缺失或不是序列时为 None
    """
    value = _declared_field(declaration, "media_types")
    return tuple(value) if isinstance(value, (list, tuple)) else None


def declaration_meta_parser_identity(declaration: Any) -> Tuple[Optional[str], Optional[str]]:
    """
    读取声明自报的解析器标识与展示名称

    :param declaration: `MetaParserDeclaration` 实例
    :return: (解析器标识, 展示名称) 二元组；对应字段缺失、非字符串或为空白时该位为 None
    """
    return (
        _declared_field_text(declaration, "parser_id"),
        _declared_field_text(declaration, "name"),
    )


def declaration_meta_parser_priority(declaration: Any) -> Any:
    """
    读取声明自报的默认顺序取值

    按原值返回而不归一为整数：取值合法性由契约校验判定，此处先归一会把非整数的
    错误取值悄悄变成一个合法答案。

    :param declaration: `MetaParserDeclaration` 实例
    :return: priority 字段的原始值；字段缺失时为 None
    """
    return _declared_field(declaration, "priority")


def declaration_action_identity(declaration: Any) -> Tuple[Optional[str], Optional[str]]:
    """
    读取声明自报的动作标识与展示名称

    :param declaration: `ActionDeclaration` 实例，或插件直接交出的描述字典
    :return: (动作标识, 展示名称) 二元组；对应字段缺失、非字符串或为空白时该位为 None
    """
    return (
        _declared_field_text(declaration, "action_id"),
        _declared_field_text(declaration, "name"),
    )


def declaration_action_impl(declaration: Any) -> Any:
    """
    读取声明的动作实现函数

    兼容插件直接交出描述字典而不包 `ActionDeclaration` 的写法：字典形态复用
    ``get_actions()`` 返回项的 ``func`` 字段存放实现函数，与工作流实际消费的
    字段一致。

    :param declaration: `ActionDeclaration` 实例，或插件直接交出的描述字典
    :return: 实现函数；取不到时为 None
    """
    if isinstance(declaration, Mapping):
        return declaration.get("func")
    return getattr(declaration, "impl", None)


def declaration_action_kwargs(declaration: Any) -> Any:
    """
    读取声明自带的动作附加参数原始值

    :param declaration: `ActionDeclaration` 实例，或插件直接交出的描述字典
    :return: kwargs 字段的原始值；字段缺失时为 None
    """
    return _declared_field(declaration, "kwargs")


def declaration_service_instance_requirement(declaration: Any) -> Any:
    """
    读取声明自报的服务实例作用对象原始值

    按原值返回而不做形状归一：取值合法性由契约校验判定，此处先归一会把错误取值悄悄
    变成一个合法答案。字段缺失与显式给出 None 都答 None，两者对宿主是同一件事——本
    声明与服务实例无关。

    :param declaration: 扩展声明，或插件直接交出的描述字典
    :return: requires_service_instance 字段的原始值；字段缺失时为 None
    """
    return _declared_field(declaration, "requires_service_instance")


def declaration_schedule_identity(declaration: Any) -> Tuple[Optional[str], Optional[str]]:
    """
    读取声明自报的任务标识与展示名称

    :param declaration: `ScheduleDeclaration` 实例
    :return: (任务标识, 展示名称) 二元组；对应字段缺失、非字符串或为空白时该位为 None
    """
    return (
        _declared_field_text(declaration, "job_id"),
        _declared_field_text(declaration, "name"),
    )


def declaration_schedule_trigger(declaration: Any) -> Tuple[Any, Any]:
    """
    读取声明自报的调度类型与调度参数

    按原值返回而不做归一：取值合法性由契约校验判定，此处先归一会把错误取值悄悄
    变成一个合法答案。

    :param declaration: `ScheduleDeclaration` 实例
    :return: (调度类型, 调度参数) 二元组的原始值；对应字段缺失时该位为 None
    """
    return (
        _declared_field(declaration, "trigger"),
        _declared_field(declaration, "trigger_args"),
    )


def declaration_schedule_kwargs(declaration: Any) -> Any:
    """
    读取声明自带的实现调用参数原始值

    :param declaration: `ScheduleDeclaration` 实例
    :return: kwargs 字段的原始值；字段缺失时为 None
    """
    return _declared_field(declaration, "kwargs")


def declaration_service_instance_identity(
    declaration: Any,
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    读取服务实例声明自报的能力标签、类型标识与展示名称

    :param declaration: `ServiceInstanceDeclaration` 实例
    :return: (能力标签, 类型标识, 展示名称) 三元组；对应字段缺失、非字符串或
        为空白时该位为 None
    """
    return (
        _declared_field_text(declaration, "capability"),
        _declared_field_text(declaration, "type"),
        _declared_field_text(declaration, "name"),
    )


def declaration_service_instance_icon(declaration: Any) -> Optional[str]:
    """
    读取服务实例声明自报的类型展示图标

    :param declaration: `ServiceInstanceDeclaration` 实例
    :return: 图标标识；字段缺失、非字符串或为空白时为 None
    """
    return _declared_field_text(declaration, "icon")


def declaration_service_instance_multi_instance(declaration: Any) -> Any:
    """
    读取服务实例声明自报的实例数取值

    按原值返回而不归一为布尔：取值合法性由契约校验判定，此处先归一会把非布尔的
    错误取值悄悄变成一个合法答案，校验就再也看不见它。

    :param declaration: `ServiceInstanceDeclaration` 实例
    :return: multi_instance 字段的原始值；字段缺失时为 None
    """
    return _declared_field(declaration, "multi_instance")


def declaration_service_instance_constructor(
    declaration: Any,
) -> Tuple[Optional[Any], Optional[Any]]:
    """
    读取服务实例声明的两条构造路径

    服务实例类型无法从裸实现推出能力标签与类型标识，因此两个字段都按原值读取，
    不套用 `declaration_impl` 的「实现即声明」回落。

    :param declaration: `ServiceInstanceDeclaration` 实例
    :return: (实现类, 实例工厂) 二元组；对应字段缺失时该位为 None
    """
    return (
        _declared_field(declaration, "impl"),
        _declared_field(declaration, "factory"),
    )


def declaration_command_identity(
    declaration: Any,
) -> Tuple[Optional[str], Optional[str]]:
    """
    读取命令声明自报的命令词与展示名称

    :param declaration: `CommandDeclaration` 实例，或插件直接交出的描述字典
    :return: (命令词, 展示名称) 二元组；对应字段缺失、非字符串或为空白时该位为 None
    """
    return (
        _declared_field_text(declaration, "cmd"),
        _declared_field_text(declaration, "name"),
    )


def declaration_command_presentation(
    declaration: Any,
) -> Tuple[Optional[str], Optional[str]]:
    """
    读取命令声明的分类与参数描述原始值

    :param declaration: `CommandDeclaration` 实例，或插件直接交出的描述字典
    :return: (分类, 参数描述) 二元组；对应字段缺失时该位为 None
    """
    return (
        _declared_field(declaration, "category"),
        _declared_field(declaration, "args_description"),
    )


def declaration_command_data(declaration: Any) -> Any:
    """
    读取命令声明附加传递的静态数据原始值

    :param declaration: `CommandDeclaration` 实例，或插件直接交出的描述字典
    :return: data 字段的原始值；字段缺失时为 None
    """
    return _declared_field(declaration, "data")


def declaration_command_show(declaration: Any) -> Any:
    """
    读取命令声明的菜单展示开关原始值

    :param declaration: `CommandDeclaration` 实例，或插件直接交出的描述字典
    :return: show 字段的原始值；字段缺失时为 None
    """
    return _declared_field(declaration, "show")


def declaration_command_override(declaration: Any) -> Any:
    """
    读取命令声明接管同名内建命令的意图原始值

    :param declaration: `CommandDeclaration` 实例，或插件直接交出的描述字典
    :return: overrides_builtin 字段的原始值；字段缺失时为 None
    """
    return _declared_field(declaration, "overrides_builtin")


def declaration_filter_rule_identity(
    declaration: Any,
) -> Tuple[Optional[str], Optional[str]]:
    """
    读取筛选规则声明自报的规则标识与展示名称

    :param declaration: `FilterRuleDeclaration` 实例，或插件直接交出的描述字典
    :return: (规则标识, 展示名称) 二元组；对应字段缺失、非字符串或为空白时该位为 None
    """
    return (
        _declared_field_text(declaration, "rule_id"),
        _declared_field_text(declaration, "name"),
    )


def declaration_filter_rule_conditions(declaration: Any) -> Dict[str, Any]:
    """
    读取筛选规则声明的全部匹配条件字段原始值

    按原值返回而不做归一：取值合法性由契约校验判定，此处先归一会把非字符串的错误
    取值悄悄变成一个合法答案，校验就再也看不见它。

    :param declaration: `FilterRuleDeclaration` 实例，或插件直接交出的描述字典
    :return: 条件字段名到原始值的字典，字段缺失时该项为 None
    """
    return {field: _declared_field(declaration, field) for field in RULE_CONDITION_FIELDS}


def declaration_filter_rule_group_identity(
    declaration: Any,
) -> Tuple[Optional[str], Optional[str]]:
    """
    读取筛选规则组声明自报的组名与规则串

    :param declaration: `FilterRuleGroupDeclaration` 实例，或插件直接交出的描述字典
    :return: (组名, 规则串) 二元组；对应字段缺失、非字符串或为空白时该位为 None
    """
    return (
        _declared_field_text(declaration, "name"),
        _declared_field_text(declaration, "rule_string"),
    )


def declaration_filter_rule_group_scope(
    declaration: Any,
) -> Tuple[Optional[str], Optional[str]]:
    """
    读取筛选规则组声明的适用范围

    :param declaration: `FilterRuleGroupDeclaration` 实例，或插件直接交出的描述字典
    :return: (适用媒体类型, 适用媒体类别) 二元组；对应字段缺失时该位为 None
    """
    return (
        _declared_field(declaration, "media_type"),
        _declared_field(declaration, "category"),
    )


def declaration_dashboard_identity(declaration: Any) -> Tuple[Optional[str], Optional[str]]:
    """
    读取声明自报的仪表盘 key 与展示名称

    key 保留原始字符串（含空字符串），空字符串代表插件的默认仪表盘，与
    ``name`` 的「非空才有效」语义不同，不能共用同一条读取规则。

    :param declaration: `DashboardDeclaration` 实例，或插件直接交出的描述字典
    :return: (仪表盘 key, 展示名称) 二元组；key 非字符串时为 None，name 为空白时为 None
    """
    key = _declared_field(declaration, "key")
    return (key if isinstance(key, str) else None), _declared_field_text(declaration, "name")
