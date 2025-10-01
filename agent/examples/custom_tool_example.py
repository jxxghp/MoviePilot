"""
自定义工具示例 - 演示如何扩展新工具
"""
import asyncio
from typing import Dict, Any, List
from agent.base import BaseTool, ToolParameter, ToolResult
from agent import MovieAgent


class RecommendMovieTool(BaseTool):
    """推荐电影工具 - 基于用户偏好推荐电影"""
    
    @property
    def name(self) -> str:
        return "recommend_movie"
    
    @property
    def description(self) -> str:
        return "根据用户的偏好和历史记录推荐电影"
    
    @property
    def parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter(
                name="genre",
                type="string",
                description="用户偏好的电影类型",
                required=False,
                enum=["动作", "喜剧", "爱情", "科幻", "恐怖", "悬疑", "剧情", "动画"]
            ),
            ToolParameter(
                name="min_rating",
                type="number",
                description="最低评分要求（0-10）",
                required=False
            ),
            ToolParameter(
                name="year_range",
                type="string",
                description="年份范围，格式如 '2020-2024'",
                required=False
            ),
            ToolParameter(
                name="limit",
                type="integer",
                description="推荐数量，默认5部",
                required=False
            )
        ]
    
    async def execute(self, **kwargs) -> Dict[str, Any]:
        """执行推荐"""
        genre = kwargs.get("genre")
        min_rating = kwargs.get("min_rating", 7.0)
        year_range = kwargs.get("year_range")
        limit = kwargs.get("limit", 5)
        
        # TODO: 接入实际的推荐算法
        # 这里可以调用推荐系统、分析用户历史等
        
        # 模拟推荐结果
        recommendations = [
            {
                "id": "movie_001",
                "title": "星际穿越",
                "year": 2014,
                "genre": "科幻",
                "rating": 9.3,
                "reason": "基于您对科幻类型的偏好"
            },
            {
                "id": "movie_002",
                "title": "盗梦空间",
                "year": 2010,
                "genre": "科幻",
                "rating": 9.2,
                "reason": "高分科幻经典，强烈推荐"
            }
        ]
        
        result = ToolResult(
            success=True,
            message=f"为您推荐了 {len(recommendations)} 部电影",
            data={
                "recommendations": recommendations,
                "filter": {
                    "genre": genre,
                    "min_rating": min_rating,
                    "year_range": year_range,
                    "limit": limit
                }
            }
        )
        
        return result.dict()


class AnalyzeMovieTool(BaseTool):
    """电影分析工具 - 分析电影的各项指标"""
    
    @property
    def name(self) -> str:
        return "analyze_movie"
    
    @property
    def description(self) -> str:
        return "分析电影的各项数据，包括票房、评分趋势、用户评论等"
    
    @property
    def parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter(
                name="movie_id",
                type="string",
                description="电影ID",
                required=True
            ),
            ToolParameter(
                name="analysis_type",
                type="string",
                description="分析类型",
                required=False,
                enum=["评分", "票房", "评论", "综合"]
            )
        ]
    
    async def execute(self, **kwargs) -> Dict[str, Any]:
        """执行分析"""
        movie_id = kwargs.get("movie_id")
        analysis_type = kwargs.get("analysis_type", "综合")
        
        # TODO: 接入实际的数据分析服务
        
        # 模拟分析结果
        analysis = {
            "movie_id": movie_id,
            "analysis_type": analysis_type,
            "rating_trend": {
                "current": 8.5,
                "trend": "上升",
                "change": "+0.3"
            },
            "box_office": {
                "total": "15.6亿",
                "rank": 5
            },
            "sentiment": {
                "positive": 78,
                "neutral": 15,
                "negative": 7
            }
        }
        
        result = ToolResult(
            success=True,
            message=f"电影分析完成",
            data=analysis
        )
        
        return result.dict()


class CompareMoviesTool(BaseTool):
    """电影对比工具 - 对比多部电影"""
    
    @property
    def name(self) -> str:
        return "compare_movies"
    
    @property
    def description(self) -> str:
        return "对比多部电影的各项指标，帮助用户做出选择"
    
    @property
    def parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter(
                name="movie_ids",
                type="array",
                description="要对比的电影ID列表（JSON数组格式）",
                required=True
            ),
            ToolParameter(
                name="compare_fields",
                type="string",
                description="对比维度，多个用逗号分隔",
                required=False
            )
        ]
    
    async def execute(self, **kwargs) -> Dict[str, Any]:
        """执行对比"""
        movie_ids = kwargs.get("movie_ids", [])
        compare_fields = kwargs.get("compare_fields", "评分,票房,口碑")
        
        # TODO: 接入实际的对比服务
        
        # 模拟对比结果
        comparison = {
            "movies": movie_ids,
            "fields": compare_fields.split(","),
            "results": [
                {
                    "movie_id": movie_ids[0] if movie_ids else "movie_001",
                    "rating": 8.5,
                    "box_office": "10亿",
                    "sentiment": "积极"
                }
            ]
        }
        
        result = ToolResult(
            success=True,
            message=f"已对比 {len(movie_ids)} 部电影",
            data=comparison
        )
        
        return result.dict()


async def demo_custom_tools():
    """演示自定义工具的使用"""
    
    print("=" * 60)
    print("自定义工具示例")
    print("=" * 60)
    
    # 创建智能体
    agent = MovieAgent(
        api_key="your-api-key",
        model="gpt-4-turbo-preview"
    )
    
    # 添加自定义工具
    agent.add_tool(RecommendMovieTool())
    agent.add_tool(AnalyzeMovieTool())
    agent.add_tool(CompareMoviesTool())
    
    print("\n已添加自定义工具:")
    print("  - recommend_movie: 推荐电影")
    print("  - analyze_movie: 分析电影")
    print("  - compare_movies: 对比电影")
    
    # 测试对话
    test_messages = [
        "根据我的口味推荐几部高分科幻电影",
        "分析一下《星际穿越》这部电影",
        "对比一下《星际穿越》和《盗梦空间》",
    ]
    
    for message in test_messages:
        print(f"\n\n用户: {message}")
        print("-" * 60)
        print("智能体: ", end="", flush=True)
        
        try:
            async for chunk in agent.chat_stream(message):
                print(chunk, end="", flush=True)
            print()
        except Exception as e:
            print(f"\n错误: {e}")
    
    print("\n" + "=" * 60)


async def demo_tool_execution():
    """演示工具的直接执行"""
    
    print("\n\n" + "=" * 60)
    print("工具直接执行示例")
    print("=" * 60)
    
    # 创建工具实例
    recommend_tool = RecommendMovieTool()
    analyze_tool = AnalyzeMovieTool()
    
    # 1. 推荐工具
    print("\n1. 推荐电影工具")
    print("-" * 60)
    result = await recommend_tool.execute(
        genre="科幻",
        min_rating=8.0,
        limit=3
    )
    print(f"结果: {result}")
    
    # 2. 分析工具
    print("\n2. 分析电影工具")
    print("-" * 60)
    result = await analyze_tool.execute(
        movie_id="movie_001",
        analysis_type="综合"
    )
    print(f"结果: {result}")
    
    print("\n" + "=" * 60)


if __name__ == "__main__":
    # 演示自定义工具
    asyncio.run(demo_custom_tools())
    
    # 演示工具直接执行
    asyncio.run(demo_tool_execution())
