"""存储族扩展实现存储后端时要继承与调用的东西。

存储类型的声明契约按 ``impl`` 的 MRO 判定：不派生自 ``StorageBase`` 的实现类一律拒绝
登记，因此这个基类不是可选的便利品，而是该族声明成立的前提。

``StorageInstanceSingleton`` 是可选的元类，按 ``(后端类, 实例名)`` 复用后端对象。不用它
时每次取用都新建一个对象并重新登录，整理一批文件即是一串登录往返；用它则同一实例共用
一份连接与限流状态，不同实例各自一份。

``transfer_process`` 是上传下载进度回到宿主进度条的唯一入口，形状为「传一个路径，拿一个
接收百分比的回调」。
"""

from app.modules._base.storage import (
    StorageBase,
    StorageInstanceSingleton,
    transfer_process,
)


__all__ = ["StorageBase", "StorageInstanceSingleton", "transfer_process"]
