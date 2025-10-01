# ⚡ 快速开始指南

5 分钟快速体验电影智能助手！

## 🚀 最快开始（3步）

### 步骤 1: 安装依赖

```bash
pip3 install -r requirements.txt
```

### 步骤 2: 运行测试

```bash
python3 test_agent.py
```

看到所有测试通过 ✓

### 步骤 3: 启动服务

```bash
python3 api_server.py
```

访问演示界面: http://localhost:5000/demo

## 💬 开始对话

在演示界面中尝试这些对话：

```
1. "帮我找一些科幻电影"
2. "搜索评分最高的电影"
3. "告诉我盗梦空间的详细信息"
4. "订阅这部电影"
5. "查看我的订阅列表"
6. "下载星际穿越，要1080p的"
7. "查看下载进度"
```

## 🎯 三种使用方式

### 方式 1: 交互式命令行

```bash
python3 test_agent.py interactive
```

直接在终端与智能助手对话

### 方式 2: Python API

```python
from agent_core import Agent
from movie_tools import create_movie_tools

agent = Agent(name="MovieAgent")
agent.register_tools(create_movie_tools())

response = agent.chat("帮我搜索科幻电影")
print(response)
```

### 方式 3: REST API

```bash
curl -X POST http://localhost:5000/api/message \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "user123",
    "message": "搜索科幻电影"
  }'
```

## 🔧 配置 LLM（可选）

如果要使用 GPT-4 等 LLM：

1. 复制配置文件：
```bash
cp .env.example .env
```

2. 编辑 .env：
```bash
LLM_PROVIDER=openai
LLM_API_KEY=your-api-key-here
LLM_MODEL=gpt-4
```

3. 重启服务

## 📚 更多资源

- 完整文档: [README.md](README.md)
- 架构设计: [ARCHITECTURE.md](ARCHITECTURE.md)
- 部署指南: [DEPLOYMENT.md](DEPLOYMENT.md)
- 项目总结: [PROJECT_SUMMARY.md](PROJECT_SUMMARY.md)

## 🎯 下一步

1. 查看 `example_usage.py` 了解更多使用场景
2. 查看 `client_example.py` 学习如何集成到你的应用
3. 阅读 `ARCHITECTURE.md` 了解系统设计
4. 参考 `DEPLOYMENT.md` 部署到生产环境

## ❓ 常见问题

**Q: 需要 API Key 吗？**
A: 不需要！系统可以在没有 LLM 的情况下运行（使用规则匹配）

**Q: 支持哪些 LLM？**
A: OpenAI GPT、Azure OpenAI、以及兼容 OpenAI API 的模型

**Q: 可以修改电影数据吗？**
A: 可以！修改 `movie_tools.py` 中的 `MovieDatabase` 类

**Q: 如何添加新功能？**
A: 实现函数 → 创建 Tool → 注册到 Agent

**Q: 支持生产部署吗？**
A: 完全支持！查看 DEPLOYMENT.md

## 🆘 遇到问题？

1. 查看日志输出
2. 运行测试: `python3 test_agent.py test`
3. 查看文档
4. 提交 Issue

---

**开始享受智能化的电影管理体验吧！** 🎬✨
