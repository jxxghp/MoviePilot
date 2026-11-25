"""
整理附属文件、历史空仓储与剧集元数据构造；不注册测试用例。
"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from app.chain.transfer.facade import TransferChain
from app.domain.meta.metabase import MetaBase
from app.schemas.file import FileItem
from app.schemas.types import MediaType
from tests.support.transfer.jobs import make_transfer_chain as make_base_transfer_chain


class FakeMeta(MetaBase):
    """
    构造整理链路所需的最小剧集元数据。

    继承 MetaBase 而非鸭子类型：TransferTask.meta 已标注为 MetaBase，pydantic 会做
    isinstance 校验。name 是 MetaBase 上的 property，用类级属性遮蔽；episode_list
    在下方以 property 覆盖，不必再遮蔽。

    :ivar name: 测试剧集的固定名称，遮蔽只读的基类属性
    """

    name = None

    def __init__(self, episode: int):
        """
        初始化附属文件归属测试使用的固定季集信息。
        """
        super().__init__(title=f"Test Show S01E{episode:02d}")
        self.name = "Test Show"
        self.title = f"Test Show S01E{episode:02d}"
        self.year = "2026"
        self.type = MediaType.TV
        self.begin_season = 1
        self.end_season = None
        self.total_season = 1
        self.begin_episode = episode
        self.end_episode = None
        self.total_episode = 1
        self.part = None

    @property
    def episode_list(self) -> list[int]:
        """
        返回当前文件覆盖的集数列表。
        """
        return [self.begin_episode]


def make_transfer_chain() -> TransferChain:
    """
    构造不启动后台线程的整理链实例。
    """
    chain = make_base_transfer_chain()
    chain._TransferChain__ensure_recovery_scheduler = MagicMock()
    return chain


def bind_empty_history_repositories(chain: TransferChain) -> None:
    """
    为整理链绑定不命中任何记录的测试历史仓储。
    """
    chain.transfer_history_repository = SimpleNamespace(
        get_by_src=lambda src, storage=None: None
    )
    chain.download_history_repository = SimpleNamespace(
        get_by_hash=lambda download_hash: None,
        get_file_by_fullpath=lambda fullpath: None,
        get_files_by_savepath=lambda savepath: [],
        get_by_path=lambda path: None,
    )


def make_fileitem(path: str) -> FileItem:
    """
    根据路径构造文件项。
    """
    file_path = Path(path)
    return FileItem(
        storage="local",
        path=file_path.as_posix(),
        type="file",
        name=file_path.name,
        basename=file_path.stem,
        extension=file_path.suffix.lstrip("."),
        size=1024,
    )
