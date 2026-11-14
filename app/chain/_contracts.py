"""
Chain 混入类 对宿主能力的静态契约。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from app.application.classification.execution import ClassificationExecutionPort
    from app.application.history import TransferHistoryRepository
    from app.application.transfer.execution import TransferExecutionRepository


class ChainRuntimeMixinHost(Protocol):
    """
    识别与消息混入类共用的 Chain 运行能力。

    :ivar runtime_config: 当前处理链使用的运行配置快照
    :ivar eventmanager: 发送插件链式事件和业务事实事件的管理器
    :ivar messageoper: 保存通知历史的持久化能力
    :ivar messagequeue: 异步投递通知的消息队列
    :ivar classification_service: 统一媒体分类服务，None 时兼容未装配服务的旧调用方
    """

    runtime_config: Any
    eventmanager: Any
    messageoper: Any
    messagequeue: Any
    classification_service: ClassificationExecutionPort | None

    def run_module(self, method: str, **kwargs: Any) -> Any:
        """
        调用同步模块能力。

        :param method: 需要分发的模块方法名称
        :param kwargs: 交给模块方法的命名参数，保留调度器支持的控制参数
        :return: 按模块方法契约聚合的调用结果；没有有效结果时可为 None
        """
        ...

    def run_module_strict(self, method: str, **kwargs: Any) -> Any:
        """
        同步调用模块能力，并向调用方传播能力提供方失败。

        :param method: 需要分发的模块方法名称
        :param kwargs: 交给模块方法的命名参数，保留调度器支持的控制参数
        :return: 按模块方法契约聚合的调用结果；没有有效结果时可为 None
        """
        ...

    async def async_run_module(self, method: str, **kwargs: Any) -> Any:
        """
        调用异步模块能力。

        :param method: 需要分发的模块方法名称
        :param kwargs: 交给模块方法的命名参数，保留调度器支持的控制参数
        :return: 按模块方法契约聚合的调用结果；没有有效结果时可为 None
        """
        ...


class MusicSubscribeMixinHost(Protocol):
    """
    音乐订阅混入类对 SubscribeChain 的最小要求。
    """

    @classmethod
    def _music_media_chain(cls) -> Any:
        """
        构造媒体识别链。

        :return: 本次音乐订阅使用的媒体识别链实例
        """
        ...

    def _music_download_chain(self) -> Any:
        """
        构造下载链。

        :return: 本次音乐订阅使用的下载链实例
        """
        ...

    def _music_search_chain(self) -> Any:
        """
        构造搜索链。

        :return: 本次音乐订阅使用的资源搜索链实例
        """
        ...

    def _music_site_keywords(self, mediainfo: Any) -> list[str]:
        """
        构造音乐站点搜索关键字。

        :param mediainfo: 已识别的音乐订阅目标
        :return: 按检索优先级排列的站点搜索关键词
        """
        ...

    def _matches_music_resource(self, mediainfo: Any, *texts: Any) -> bool:
        """
        判断站点资源文本是否匹配音乐目标。

        :param mediainfo: 需要匹配的音乐目标，包含曲名、专辑和艺人线索
        :param texts: 候选资源标题、副标题等文本证据
        :return: 候选文本满足音乐目标匹配规则时为 True
        """
        ...

    def get_sub_sites(self, subscribe: Any) -> list[int]:
        """
        按订阅范围与系统配置选择检索站点。

        :param subscribe: 包含站点限制的订阅快照
        :return: 结合订阅与系统默认范围确定的站点标识列表
        """
        ...

    def get_params(self, subscribe: Any) -> Any:
        """
        提取订阅资源筛选条件。

        :param subscribe: 包含质量、关键词和音频限制的订阅快照
        :return: 合并订阅与系统默认值并去除 None 项后的过滤参数
        """
        ...

    def filter_torrents(self, *args: Any, **kwargs: Any) -> Any:
        """
        按订阅筛选条件过滤资源候选。

        :param args: 按宿主签名传递的过滤规则组、资源列表和媒体信息
        :param kwargs: 按宿主签名提供的过滤参数，可命名传入规则组、资源列表及媒体信息
        :return: 通过规则筛选并附加优先级的资源列表
        """
        ...

    def check_and_handle_existing_media(self, *args: Any, **kwargs: Any) -> Any:
        """
        检查媒体缺失范围并处理已完成订阅。

        :param args: 订阅快照、解析元数据、媒体信息与媒体键的位置参数
        :param kwargs: 同一缺失检查调用的命名参数
        :return: 是否已经全部下载或入库，以及按媒体键和季号组织的缺失范围
        """
        ...

    def finish_subscribe_or_not(self, *args: Any, **kwargs: Any) -> Any:
        """
        根据缺集和下载结果更新订阅完成状态。

        :param args: 订阅、解析元数据和媒体信息等宿主完成判定参数
        :param kwargs: 下载结果、剩余范围和是否强制完成等命名参数
        :return: 不返回业务数据；按判定更新订阅状态并触发完成副作用
        """
        ...

    def get_subscribe_source_keyword(self, subscribe: Any) -> str:
        """
        生成写入下载来源的订阅标识。

        :param subscribe: 需要标记为下载来源的订阅快照
        :return: 带 Subscribe 前缀和序列化订阅身份的来源文本
        """
        ...

    def _process_search_subscription(self, *args: Any, **kwargs: Any) -> Any:
        """
        交给订阅搜索组件处理一次订阅搜索或匹配任务。

        :param args: 订阅搜索组件的定位参数
        :param kwargs: 搜索执行上下文及可选控制参数
        :return: 搜索组件返回的任务结果
        """
        ...

    def _SubscribeChain__candidate_contract_changed(
        self,
        prepared: Any,
        current: Any,
    ) -> bool:
        """
        判断准备候选期间订阅身份或过滤合同是否已经变化。

        :param prepared: 开始准备候选时读取的订阅快照
        :param current: 准备后重新读取的当前订阅快照
        :return: 媒体身份或过滤条件已变化、旧候选不能继续使用时为 True
        """
        ...


class InteractionMixinHost(Protocol):
    """
    交互委托混入类对业务 Chain 的最小要求。

    :ivar _interaction_handler_type: 当前业务使用的交互处理器类型，提供渠道回调解析能力
    """

    _interaction_handler_type: type

    def _interaction_handler(self) -> Any:
        """
        构造业务交互处理器。

        :return: 绑定当前业务能力的交互处理器实例
        """
        ...


class TransferMixinHost(ChainRuntimeMixinHost, Protocol):
    """
    整理辅助混入类对 TransferChain 的最小要求。

    :ivar transfer_history_repository: 查询和记录文件整理历史的仓库
    :ivar transfer_execution_repository: 维护整理任务持久执行状态和租约的仓库
    """

    @classmethod
    def _transfer_media_chain(cls) -> Any:
        """
        构造媒体识别链。

        :return: 本次整理协作使用的媒体识别链实例
        """
        ...

    @classmethod
    def _transfer_storage_chain(cls) -> Any:
        """
        构造存储链。

        :return: 本次整理协作使用的存储访问链实例
        """
        ...

    @classmethod
    def _transfer_subscribe_chain(cls) -> Any:
        """
        构造订阅链。

        :return: 本次整理协作使用的订阅链实例
        """
        ...

    def post_message(self, *args: Any, **kwargs: Any) -> Any:
        """
        按消息路由发送通知。

        :param args: 按宿主顺序传入的消息及媒体、种子和整理上下文
        :param kwargs: 渲染或覆盖通知内容的命名参数
        :return: 不返回业务数据；按路由发送或排队投递通知
        """
        ...

    async def async_post_message(self, *args: Any, **kwargs: Any) -> Any:
        """
        异步按消息路由发送通知。

        :param args: 按宿主顺序传入的消息及媒体、种子和整理上下文
        :param kwargs: 渲染或覆盖通知内容的命名参数
        :return: 不返回业务数据；按路由发送或排队投递通知
        """
        ...

    def obtain_images(self, *args: Any, **kwargs: Any) -> Any:
        """
        通过模块调度补齐媒体图片。

        :param args: 需要补充图片的媒体信息位置参数
        :param kwargs: 按宿主签名提供的媒体信息命名参数
        :return: 补充后的媒体信息，未取得有效结果时为 None
        """
        ...

    def do_transfer(self, *args: Any, **kwargs: Any) -> Any:
        """
        按已识别媒体与目标目录执行文件整理。

        :param args: 按公开整理签名传入的源文件和媒体上下文等位置参数
        :param kwargs: 目标目录、整理模式、预览、后台执行和刮削等选项
        :return: 是否接纳或执行成功，以及结果说明或预览数据字典
        """
        ...
    transfer_history_repository: TransferHistoryRepository
    transfer_execution_repository: TransferExecutionRepository
