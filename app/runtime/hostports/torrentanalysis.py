"""过滤扩展取用候选种子分析能力的端口槽位。

候选种子的判定由多个分析器共同给出：内置规则引擎与插件贡献的分析器实现同一个
能力方法，由模块分发在能力族内多播收集。过滤扩展只声明取用多播结果的最小协议，
具体分发实现由组合根注入；端口未注入时过滤扩展只运行内置分析器。
"""

from typing import Any, List, Optional, Protocol

from app.runtime.hostports.port import HostPort
from app.schemas.filter import TorrentVerdict


class TorrentAnalysisProvider(Protocol):
    """过滤扩展所需的候选种子分析能力多播。"""

    def analyze_torrent_candidates(
            self,
            rule_groups: List[str],
            torrent_list: List[Any],
            mediainfo: Optional[Any] = None,
    ) -> List[List[TorrentVerdict]]:
        """收集全部分析器对候选种子列表的判定。"""
        ...


torrent_analysis_port: HostPort[TorrentAnalysisProvider] = HostPort("torrent_analysis")
