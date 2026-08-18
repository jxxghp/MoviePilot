"""按业务域划分的模块能力端口客户端。

每个客户端只持有一个模块调度器，暴露本域的能力端口方法；
使用方按需组合所需的域，不必获得全部能力端口。
"""

from app.application.orchestration.ports.dispatch import (
    CapabilityDispatch,
    CapabilityPorts,
    ModuleCapabilityDispatch,
    ModuleErrorReporter,
)
from app.application.orchestration.ports.download import DownloadPorts
from app.application.orchestration.ports.library import LibraryPorts
from app.application.orchestration.ports.metadata import MetadataPorts
from app.application.orchestration.ports.parsing import ParsingPorts
from app.application.orchestration.ports.search import SearchPorts
from app.application.orchestration.ports.system import SystemPorts
from app.application.orchestration.ports.transfer import TransferPorts

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
