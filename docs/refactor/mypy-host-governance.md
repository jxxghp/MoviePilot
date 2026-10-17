# v3 宿主 Mypy 逐文件治理记录

每个已交付条目对应一个独立提交：生产文件错误清零后才加入 `mypy.ini` 严格清单，并仅固化全宿主基线中真实下降的错误。完整扫描采用 Linux/Python 3.14 合同；行为与兼容测试按文件影响范围选择。

## 已交付

| 生产文件 | 消除的错误 | 合同依据 |
| --- | --- | --- |
| `app/api/endpoints/anilist.py` | `attr-defined` 1 | `MediaInfo` 改从定义模块 `app.schemas.context` 导入；运行时对象身份不变。 |
| `app/api/endpoints/douban.py` | `attr-defined` 1 | 豆瓣响应模型改从同一定义模块导入，保留原模型对象。 |

## 需单独设计或外部条件

| 文件 | 阻碍 |
| --- | --- |
| `app/agent/skills/metadata.py`、`app/agent/runtime.py` | PyYAML 缺类型桩；需按依赖与锁文件流程处理，不能局部抑制 `import-untyped`。 |
| `app/agent/middleware/config.py`、`app/agent/middleware/patching.py`、`app/agent/middleware/usage.py` | LangChain 基类被当前导入策略视为 `Any`；调整定向导入策略后须重新审查全宿主诊断和实际状态类型。 |
| `app/agent/tools/manager.py` | 动态工厂加载器的返回契约未表达类能力，需联动加载器定义。 |
| `app/agent/tools/impl/mcp.py` | 消息属性 setter 的 `str` 签名与实际允许 `None` 的调用合同不一致，需联动基类。 |
| `app/modules/indexer/parser/sunnypt.py` | 外部资料和消息中的畸形日期值处理缺明确合同，收窄为字符串可能改变既有容错。 |
| `app/workflow/actions/filter_torrents.py` | `TorrentHelper` 构造器标注属于另一生产文件，且可空上下文需先确定行为合同。 |
