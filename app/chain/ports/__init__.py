"""按业务域划分的模块能力端口客户端。

每个客户端只持有一个模块调度器，暴露本域的能力端口方法；
使用方按需组合所需的域，不必获得全部能力端口。
"""

from app.chain.ports.dispatch import (
    CapabilityDispatch,
    CapabilityPorts,
    ModuleCapabilityDispatch,
    ModuleErrorReporter,
)
from app.chain.ports.download import DownloadPorts
from app.chain.ports.library import LibraryPorts
from app.chain.ports.metadata import MetadataPorts
from app.chain.ports.parsing import ParsingPorts
from app.chain.ports.search import SearchPorts
from app.chain.ports.system import SystemPorts
from app.chain.ports.transfer import TransferPorts

__all__ = [
    "CapabilityDispatch",
    "CapabilityPorts",
    "DownloadPorts",
    "LibraryPorts",
    "MetadataPorts",
    "ModuleCapabilityDispatch",
    "ModuleErrorReporter",
    "ParsingPorts",
    "SearchPorts",
    "SystemPorts",
    "TransferPorts",
]
