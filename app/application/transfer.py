"""
整理任务：整理链的进程内工作项。

TransferTask 此前住在 app/schemas/transfer.py，但它不是出网的 DTO——meta 装的是领域侧
的 MetaBase 子类，mediainfo 装的是领域侧的 MediaInfo / MusicInfo，都带行为而非纯数据。
放在 app.schemas 的代价是它没法命名自己真正装的类型：app.schemas 一旦 import 领域类型，
app.schemas -> app.schemas.transfer -> app.domain.* -> app.schemas.types -> app.schemas
就闭环，仓库自己的 test_migrated_modules_are_not_in_import_cycles 会红（已实测）。于是
两个字段只能标成 Optional[Any]，把「这里到底能放什么」这件事整个交给了口头约定。

搬到应用层就没有这个约束：app.application 允许依赖 app.domain 与 app.schemas，两个
字段因此能标出真实类型。它面向前端的投影仍是 app/schemas/transfer.py 里的
TransferJob / TransferJobTask，那两个用 app.schemas 的同名 DTO——一个是工作项，一个是
视图，分开表达之后两边都不必再迁就对方。
"""
from pathlib import Path
from typing import Callable, List, Optional, Union

from pydantic import BaseModel, ConfigDict

from app.domain.context import MediaInfo, MusicInfo
from app.domain.meta.metabase import MetaBase
from app.schemas.file import FileItem
from app.schemas.history import DownloadHistory
from app.schemas.media import OptionalMediaIdentityMixin
from app.schemas.system import TransferDirectoryConf
from app.schemas.tmdb import TmdbEpisode
from app.schemas.transfer import TransferInfo
from app.schemas.types import MediaSource, MediaType


class TransferTask(OptionalMediaIdentityMixin, BaseModel):
    """
    文件整理任务。
    """

    # MetaBase 与 MediaInfo / MusicInfo 都是普通类而非 BaseModel，pydantic 需要显式放行
    model_config = ConfigDict(arbitrary_types_allowed=True)

    fileitem: FileItem
    meta: Optional[MetaBase] = None
    mediainfo: Optional[Union[MusicInfo, MediaInfo]] = None
    media_source: Optional[MediaSource] = None
    media_id: Optional[str] = None
    mtype: Optional[MediaType] = None
    target_directory: Optional[TransferDirectoryConf] = None
    target_storage: Optional[str] = None
    target_path: Optional[Path] = None
    transfer_type: Optional[str] = None
    scrape: Optional[bool] = False
    library_type_folder: Optional[bool] = False
    library_category_folder: Optional[bool] = False
    episodes_info: Optional[List[TmdbEpisode]] = None
    username: Optional[str] = None
    downloader: Optional[str] = None
    download_hash: Optional[str] = None
    download_history: Optional[DownloadHistory] = None
    transfer_batch_id: Optional[str] = None
    manual: Optional[bool] = False
    background: Optional[bool] = True
    preview: Optional[bool] = False

    def to_dict(self):
        """
        返回字典。

        meta 与 mediainfo 用 to_dict() 而非 model_dump()：它们是领域对象，没有
        model_dump。此前这里写的是 model_dump()，仓内无人调用才一直没炸——字段类型
        标成 Any 时，这种错配静态检查也看不出来。
        """
        dicts = vars(self).copy()
        dicts["fileitem"] = self.fileitem.model_dump() if self.fileitem else None
        dicts["meta"] = self.meta.to_dict() if self.meta else None
        dicts["mediainfo"] = self.mediainfo.to_dict() if self.mediainfo else None
        dicts["target_directory"] = self.target_directory.model_dump() if self.target_directory else None
        return dicts


class TransferQueue(BaseModel):
    """
    异步整理队列信息。

    和 TransferTask 一起从 app/schemas 搬来：它装着一个 TransferTask 和一个回调函数，
    回调根本不可序列化，因此从来就不是 DTO，只是恰好和视图模型住在同一个文件里。
    """
    # 任务信息
    task: Optional[TransferTask] = None
    # 回调函数
    callback: Optional[Callable] = None
    # 整理结果
    result: Optional[TransferInfo] = None
