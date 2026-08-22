"""服务实例族对实例形状的必填契约。

必填集定在**服务发现把实例交出去之后、族级取用链上无保护直调的那几个方法**上，
不定在业务方法上。声明 ``capability="downloader"`` 不会让宿主把下载派给它——
``add_torrent``、``send_msg`` 这些是模块内部约定，宿主的分发按方法名走模块清单，
而扩展声明的服务实例类型不进模块清单。真要接管下载得另外用 ``provides_modules()``
挂上 ``download`` 并在实现里自取实例。把业务方法写进必填集，拒的是宿主根本不会
发起的调用。

族级取用链上的直调则是另一回事：下载器与媒体服务器的十分钟重连回路直调
``is_inactive``/``reconnect``，消息通知的连通性测试直调 ``get_state``，三处都没有
保护，缺席即在用户看不见的背景回路里抛异常。这些方法在登记时判定在不在。

未登记的族没有必填集：族是登记出来的，扩展能带进宿主答不出形状的新族。这同时是
契约演进的活口——宿主将来新增可选契约方法，只要不进本表，已发布的声明当场仍然合规。

判定只看「在不在、可不可调用」，不追问实现是不是空桩：判空桩要读源码，动态生成与
编译分发的实现会被误判，而必填集是硬拒，误拒的代价高于放过一个空桩。

存储族不在本表：它的形状已由 `app.runtime.extensions.admission.storage`
按 ``StorageBase`` 的继承与抽象方法判定，两处判同一件事必然漂移。登录认证族同样
不在本表：该族零内建类型，登录握手走 ``unicast("user_authenticate")`` 的模块分发
而不是实例方法，宿主对实例一个方法都不调，空地板上不该硬编一个必填名字。
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Any, Mapping, Optional, Tuple

from app.schemas.types import ModuleType

# 各族取用链上必须在场的实例方法。键为能力标签，值为方法名元组；不在表中的族不做形状判定
SERVICE_INSTANCE_REQUIRED_METHODS: Mapping[str, Tuple[str, ...]] = MappingProxyType({
    # 十分钟重连回路直调，见 app/modules/_base/downloader.py 的 scheduler_job()
    ModuleType.Downloader.value: ("is_inactive", "reconnect"),
    # 十分钟重连回路直调，见 app/modules/_base/mediaserver.py 的 scheduler_job()
    ModuleType.MediaServer.value: ("is_inactive", "reconnect"),
    # 连通性测试的默认实现直调，见 app/modules/_base/notification.py 的 _test_connection()
    ModuleType.Notification.value: ("get_state",),
})


def service_instance_shape_violation(capability: Any, impl: Any) -> Optional[str]:
    """
    校验实现类是否带齐本族取用链上必须在场的方法

    ``factory`` 路径不校验形状：宿主拿不到工厂产出的类型，真调工厂又会连上外部服务，
    而登记期明令不构造实例。这是诚实的限制而不是遗漏，走该路径的声明由扩展自己保证
    产出物的形状。

    必填集之外的方法一律不判，缺席即视为该实例不提供这项能力，扩展不必为此写空桩。

    :param capability: 声明的能力标签
    :param impl: 声明携带的实现类；为 None 表示走 factory 路径
    :return: 违反契约的描述；实现形状成立时为 None
    """
    required = SERVICE_INSTANCE_REQUIRED_METHODS.get(capability)
    if not required or impl is None:
        return None
    missing = [name for name in required if not callable(getattr(impl, name, None))]
    if not missing:
        return None
    return (
        f"{impl!r} 缺少 {capability} 族取用链上必须在场的方法：{missing}，"
        f"该族必填 {list(required)}"
    )
