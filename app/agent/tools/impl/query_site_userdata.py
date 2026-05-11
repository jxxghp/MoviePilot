"""查询站点用户数据工具"""

import json
from typing import Optional, Type

from pydantic import BaseModel, Field

from app.agent.tools.base import MoviePilotTool
from app.db import AsyncSessionFactory
from app.db.models.site import Site
from app.db.models.siteuserdata import SiteUserData
from app.log import logger

SITE_USERDATA_DETAIL_PREVIEW_LIMIT = 10


def _preview_list(value, limit: int = SITE_USERDATA_DETAIL_PREVIEW_LIMIT) -> tuple[list, int, bool]:
    """返回列表字段预览，避免做种明细或未读消息一次性撑大工具结果。"""
    items = list(value) if isinstance(value, (list, tuple)) else []
    return items[:limit], len(items), len(items) > limit


class QuerySiteUserdataInput(BaseModel):
    """查询站点用户数据工具的输入参数模型"""

    explanation: str = Field(
        ...,
        description="Clear explanation of why this tool is being used in the current context",
    )
    site_id: int = Field(
        ...,
        description="The ID of the site to query user data for (can be obtained from query_sites tool)",
    )
    workdate: Optional[str] = Field(
        None,
        description="Work date to query (optional, format: 'YYYY-MM-DD', if not specified returns latest data)",
    )


class QuerySiteUserdataTool(MoviePilotTool):
    name: str = "query_site_userdata"
    description: str = "Query user data for a specific site including username, user level, upload/download statistics, seeding information, bonus points, and other account details. Supports querying data for a specific date or latest data."
    require_admin: bool = True
    args_schema: Type[BaseModel] = QuerySiteUserdataInput

    def get_tool_message(self, **kwargs) -> Optional[str]:
        """根据查询参数生成友好的提示消息"""
        site_id = kwargs.get("site_id")
        workdate = kwargs.get("workdate")

        message = f"查询站点 #{site_id} 的用户数据"
        if workdate:
            message += f" (日期: {workdate})"
        else:
            message += " (最新数据)"

        return message

    async def run(self, site_id: int, workdate: Optional[str] = None, **kwargs) -> str:
        logger.info(
            f"执行工具: {self.name}, 参数: site_id={site_id}, workdate={workdate}"
        )

        try:
            # 获取数据库会话
            async with AsyncSessionFactory() as db:
                # 获取站点
                site = await Site.async_get(db, site_id)
                if not site:
                    return json.dumps(
                        {"success": False, "message": f"站点不存在: {site_id}"},
                        ensure_ascii=False,
                    )

                # 获取站点用户数据
                user_data_list = await SiteUserData.async_get_by_domain(
                    db, domain=site.domain, workdate=workdate
                )

                if not user_data_list:
                    return json.dumps(
                        {
                            "success": False,
                            "message": f"站点 {site.name} ({site.domain}) 暂无用户数据",
                            "site_id": site_id,
                            "site_name": site.name,
                            "site_domain": site.domain,
                            "workdate": workdate,
                        },
                        ensure_ascii=False,
                    )

                # 格式化用户数据
                result = {
                    "success": True,
                    "site_id": site_id,
                    "site_name": site.name,
                    "site_domain": site.domain,
                    "workdate": workdate,
                    "data_count": len(user_data_list),
                    "user_data": [],
                }

                for user_data in user_data_list:
                    # 格式化上传/下载量（转换为可读格式）
                    upload_gb = user_data.upload / (1024**3) if user_data.upload else 0
                    download_gb = (
                        user_data.download / (1024**3) if user_data.download else 0
                    )
                    seeding_size_gb = (
                        user_data.seeding_size / (1024**3)
                        if user_data.seeding_size
                        else 0
                    )
                    leeching_size_gb = (
                        user_data.leeching_size / (1024**3)
                        if user_data.leeching_size
                        else 0
                    )

                    seeding_preview, seeding_count, seeding_truncated = _preview_list(
                        user_data.seeding_info
                    )
                    unread_preview, unread_count, unread_truncated = _preview_list(
                        user_data.message_unread_contents
                    )

                    user_data_dict = {
                        "domain": user_data.domain,
                        "name": user_data.name,
                        "username": user_data.username,
                        "userid": user_data.userid,
                        "user_level": user_data.user_level,
                        "join_at": user_data.join_at,
                        "bonus": user_data.bonus,
                        "upload": user_data.upload,
                        "upload_gb": round(upload_gb, 2),
                        "download": user_data.download,
                        "download_gb": round(download_gb, 2),
                        "ratio": round(user_data.ratio, 2) if user_data.ratio else 0,
                        "seeding": int(user_data.seeding) if user_data.seeding else 0,
                        "leeching": int(user_data.leeching)
                        if user_data.leeching
                        else 0,
                        "seeding_size": user_data.seeding_size,
                        "seeding_size_gb": round(seeding_size_gb, 2),
                        "leeching_size": user_data.leeching_size,
                        "leeching_size_gb": round(leeching_size_gb, 2),
                        "seeding_info_count": seeding_count,
                        "seeding_info": seeding_preview,
                        "seeding_info_truncated": seeding_truncated,
                        "message_unread": user_data.message_unread,
                        "message_unread_contents_count": unread_count,
                        "message_unread_contents": unread_preview,
                        "message_unread_contents_truncated": unread_truncated,
                        "err_msg": user_data.err_msg,
                        "updated_day": user_data.updated_day,
                        "updated_time": user_data.updated_time,
                    }
                    result["user_data"].append(user_data_dict)

                # 如果有多条数据，只返回最新的（按更新时间排序）
                if len(result["user_data"]) > 1:
                    result["user_data"].sort(
                        key=lambda x: (
                            x.get("updated_day", ""),
                            x.get("updated_time", ""),
                        ),
                        reverse=True,
                    )
                    result["message"] = (
                        f"找到 {len(result['user_data'])} 条数据，显示最新的一条"
                    )
                    result["user_data"] = [result["user_data"][0]]

                return json.dumps(result, ensure_ascii=False, indent=2)

        except Exception as e:
            error_message = f"查询站点用户数据失败: {str(e)}"
            logger.error(f"查询站点用户数据失败: {e}", exc_info=True)
            return json.dumps(
                {"success": False, "message": error_message, "site_id": site_id},
                ensure_ascii=False,
            )
