"""智能体工具族扩展实现工具时要继承与调用的东西。

智能体工具的声明契约按 ``impl`` 的继承关系判定：不派生自 ``MoviePilotTool`` 的实现类
一律拒绝登记，因此这个基类不是可选的便利品，而是该族声明成立的前提。基类同时提供工具
自身跑不掉的两样东西——``run_blocking`` 把阻塞型调用放进受控线程池，``send_message``
把工具中途的消息回到发起这次对话的渠道。

``StreamingHandler`` 是 ``set_stream_handler`` 收下的流式缓冲合同，工具重载该方法时按它标注
参数。它是结构化协议而不是基类，实现方不必继承，方法在场即满足。

``ToolTag`` 是工具向宿主自报能力域的词汇。标签不是装饰：只读子代理按 ``ToolTag.Read``
筛选可用工具，不带该标签的工具在只读场景里一次都不会被选中；工具筛选中间件也按标签把
同一能力域的工具归组。不带标签的工具照常登记，代价是它只出现在全量工具列表里。

本模块使 ``app.sdk`` 依赖 ``app.agent``，该边记在 ``tests/test_architecture_dependencies.py``
的依赖负债表 ``("sdk", "agent")``：基类现居 ``app.agent.tools.base``，而校验它的
``app.runtime`` 层依赖矩阵禁止反向引用 ``app.agent``，宿主因此靠
``configure_agent_tool_base`` 在启动期注入基类。清偿方向与存储族相同——把工具基类迁出
``app.agent``，SDK 与校验层即可直接 import，运行期注入随之撤除。
"""

from app.agent.tools.base import MoviePilotTool
from app.agent.tools.base import _StreamingHandlerProtocol as StreamingHandler
from app.agent.tools.tags import ToolTag


__all__ = ["MoviePilotTool", "StreamingHandler", "ToolTag"]
