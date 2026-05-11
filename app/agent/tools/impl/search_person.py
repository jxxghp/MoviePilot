"""搜索人物工具"""

import json
from typing import Optional, Type

from pydantic import BaseModel, Field

from app.agent.tools.base import MoviePilotTool
from app.chain.media import MediaChain
from app.log import logger


class SearchPersonInput(BaseModel):
    """搜索人物工具的输入参数模型"""
    explanation: str = Field(..., description="Clear explanation of why this tool is being used in the current context")
    name: str = Field(..., description="The name of the person to search for (e.g., 'Tom Hanks', '周杰伦')")


class SearchPersonTool(MoviePilotTool):
    name: str = "search_person"
    description: str = "Search for person information including actors, directors, etc. Supports searching by name. Returns detailed person information from TMDB, Douban, or Bangumi database."
    args_schema: Type[BaseModel] = SearchPersonInput

    def get_tool_message(self, **kwargs) -> Optional[str]:
        """根据搜索参数生成友好的提示消息"""
        name = kwargs.get("name", "")
        return f"搜索人物: {name}"

    async def run(self, name: str, **kwargs) -> str:
        logger.info(f"执行工具: {self.name}, 参数: name={name}")

        try:
            media_chain = MediaChain()
            # 使用 MediaChain.async_search_persons 方法搜索人物
            persons = await media_chain.async_search_persons(name=name)

            if persons:
                # 人物搜索结果只返回前 30 条，避免 biography/别名等字段挤占上下文。
                total_count = len(persons)
                limited_persons = persons[:30]
                # 精简字段，只保留关键信息
                simplified_results = []
                for person in limited_persons:
                    simplified = {
                        "name": person.name,
                        "id": person.id,
                        "source": person.source,
                        "profile_path": person.profile_path,
                        "original_name": person.original_name,
                        "known_for_department": person.known_for_department,
                        "popularity": person.popularity,
                        "biography": person.biography[:200] + "..." if person.biography and len(person.biography) > 200 else person.biography,
                        "birthday": person.birthday,
                        "deathday": person.deathday,
                        "place_of_birth": person.place_of_birth,
                        "gender": person.gender,
                        "imdb_id": person.imdb_id,
                        "also_known_as": person.also_known_as[:5] if person.also_known_as else [],  # 限制别名数量
                    }
                    # 添加豆瓣特有字段
                    if person.source == "douban":
                        simplified["url"] = person.url
                        simplified["avatar"] = person.avatar
                        simplified["latin_name"] = person.latin_name
                        simplified["roles"] = person.roles[:5] if person.roles else []  # 限制角色数量
                    # 添加Bangumi特有字段
                    if person.source == "bangumi":
                        simplified["career"] = person.career
                        simplified["relation"] = person.relation
                    
                    simplified_results.append(simplified)
                
                result_json = json.dumps(simplified_results, ensure_ascii=False, indent=2)
                # 如果结果被裁剪，添加提示信息
                if total_count > len(limited_persons):
                    return f"注意：搜索结果共找到 {total_count} 条，为节省上下文空间，仅显示前 {len(limited_persons)} 条结果。\n\n{result_json}"
                return result_json
            else:
                return f"未找到相关人物信息: {name}"
        except Exception as e:
            error_message = f"搜索人物失败: {str(e)}"
            logger.error(f"搜索人物失败: {e}", exc_info=True)
            return error_message
