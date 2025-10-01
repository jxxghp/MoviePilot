"""
电影资源管理工具集
封装电影搜索、订阅、下载等功能为智能体工具
"""
from typing import Dict, Any, List, Optional
from datetime import datetime
import json
from agent_core import Tool


# 模拟数据存储
class MovieDatabase:
    """电影数据库（模拟）"""
    
    def __init__(self):
        self.movies = [
            {
                "id": "1",
                "title": "盗梦空间",
                "title_en": "Inception",
                "year": 2010,
                "director": "克里斯托弗·诺兰",
                "rating": 9.3,
                "genres": ["科幻", "悬疑", "冒险"],
                "description": "一部关于梦境与现实的科幻悬疑片",
                "available_quality": ["1080p", "4K"],
                "size_gb": {"1080p": 2.5, "4K": 8.0}
            },
            {
                "id": "2",
                "title": "肖申克的救赎",
                "title_en": "The Shawshank Redemption",
                "year": 1994,
                "director": "弗兰克·德拉邦特",
                "rating": 9.7,
                "genres": ["剧情", "犯罪"],
                "description": "一部关于希望与自由的经典电影",
                "available_quality": ["720p", "1080p", "4K"],
                "size_gb": {"720p": 1.5, "1080p": 2.8, "4K": 9.0}
            },
            {
                "id": "3",
                "title": "星际穿越",
                "title_en": "Interstellar",
                "year": 2014,
                "director": "克里斯托弗·诺兰",
                "rating": 9.3,
                "genres": ["科幻", "冒险", "剧情"],
                "description": "关于人类寻找新家园的太空史诗",
                "available_quality": ["1080p", "4K"],
                "size_gb": {"1080p": 3.2, "4K": 10.5}
            },
            {
                "id": "4",
                "title": "教父",
                "title_en": "The Godfather",
                "year": 1972,
                "director": "弗朗西斯·福特·科波拉",
                "rating": 9.3,
                "genres": ["剧情", "犯罪"],
                "description": "经典黑帮电影，讲述柯里昂家族的故事",
                "available_quality": ["1080p", "4K"],
                "size_gb": {"1080p": 2.6, "4K": 8.5}
            },
            {
                "id": "5",
                "title": "阿甘正传",
                "title_en": "Forrest Gump",
                "year": 1994,
                "director": "罗伯特·泽米吉斯",
                "rating": 9.5,
                "genres": ["剧情", "爱情"],
                "description": "一个智商只有75的男人的传奇人生",
                "available_quality": ["720p", "1080p"],
                "size_gb": {"720p": 1.8, "1080p": 3.0}
            }
        ]
        self.subscriptions = []
        self.downloads = []
    
    def search_movies(self, 
                     keyword: Optional[str] = None,
                     year: Optional[int] = None,
                     genre: Optional[str] = None,
                     min_rating: Optional[float] = None) -> List[Dict[str, Any]]:
        """搜索电影"""
        results = self.movies.copy()
        
        if keyword:
            keyword_lower = keyword.lower()
            results = [
                m for m in results 
                if keyword_lower in m["title"].lower() 
                or keyword_lower in m["title_en"].lower()
                or keyword_lower in m["director"].lower()
            ]
        
        if year:
            results = [m for m in results if m["year"] == year]
        
        if genre:
            results = [m for m in results if genre in m["genres"]]
        
        if min_rating:
            results = [m for m in results if m["rating"] >= min_rating]
        
        return results
    
    def get_movie_by_id(self, movie_id: str) -> Optional[Dict[str, Any]]:
        """根据 ID 获取电影"""
        for movie in self.movies:
            if movie["id"] == movie_id:
                return movie
        return None
    
    def add_subscription(self, movie_id: str, quality: str = "1080p") -> Dict[str, Any]:
        """添加订阅"""
        movie = self.get_movie_by_id(movie_id)
        if not movie:
            return {"success": False, "error": "电影不存在"}
        
        # 检查是否已订阅
        for sub in self.subscriptions:
            if sub["movie_id"] == movie_id:
                return {"success": False, "error": "已经订阅过该电影"}
        
        subscription = {
            "id": f"sub_{len(self.subscriptions) + 1}",
            "movie_id": movie_id,
            "movie_title": movie["title"],
            "quality": quality,
            "subscribed_at": datetime.now().isoformat(),
            "status": "active"
        }
        self.subscriptions.append(subscription)
        return {"success": True, "subscription": subscription}
    
    def remove_subscription(self, movie_id: str) -> Dict[str, Any]:
        """取消订阅"""
        for i, sub in enumerate(self.subscriptions):
            if sub["movie_id"] == movie_id:
                removed = self.subscriptions.pop(i)
                return {"success": True, "removed": removed}
        return {"success": False, "error": "未找到该订阅"}
    
    def get_subscriptions(self) -> List[Dict[str, Any]]:
        """获取所有订阅"""
        return self.subscriptions
    
    def add_download(self, movie_id: str, quality: str = "1080p") -> Dict[str, Any]:
        """添加下载任务"""
        movie = self.get_movie_by_id(movie_id)
        if not movie:
            return {"success": False, "error": "电影不存在"}
        
        if quality not in movie["available_quality"]:
            return {
                "success": False, 
                "error": f"该电影不支持 {quality} 画质，可用画质: {movie['available_quality']}"
            }
        
        download = {
            "id": f"dl_{len(self.downloads) + 1}",
            "movie_id": movie_id,
            "movie_title": movie["title"],
            "quality": quality,
            "size_gb": movie["size_gb"][quality],
            "status": "queued",
            "progress": 0,
            "created_at": datetime.now().isoformat(),
            "started_at": None,
            "completed_at": None
        }
        self.downloads.append(download)
        
        # 模拟自动开始下载
        download["status"] = "downloading"
        download["started_at"] = datetime.now().isoformat()
        download["progress"] = 5
        
        return {"success": True, "download": download}
    
    def get_downloads(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        """获取下载列表"""
        if status:
            return [d for d in self.downloads if d["status"] == status]
        return self.downloads
    
    def get_download_status(self, download_id: str) -> Optional[Dict[str, Any]]:
        """获取下载状态"""
        for download in self.downloads:
            if download["id"] == download_id:
                # 模拟进度更新
                if download["status"] == "downloading" and download["progress"] < 100:
                    download["progress"] = min(100, download["progress"] + 15)
                    if download["progress"] >= 100:
                        download["status"] = "completed"
                        download["completed_at"] = datetime.now().isoformat()
                return download
        return None
    
    def cancel_download(self, download_id: str) -> Dict[str, Any]:
        """取消下载"""
        for download in self.downloads:
            if download["id"] == download_id:
                if download["status"] in ["queued", "downloading"]:
                    download["status"] = "cancelled"
                    return {"success": True, "download": download}
                else:
                    return {"success": False, "error": f"无法取消状态为 {download['status']} 的下载"}
        return {"success": False, "error": "未找到该下载任务"}


# 全局数据库实例
db = MovieDatabase()


# 工具函数定义
def search_movies(keyword: str = "", 
                 year: int = None,
                 genre: str = "",
                 min_rating: float = 0.0) -> Dict[str, Any]:
    """
    搜索电影资源
    
    Args:
        keyword: 搜索关键词（电影名称、导演等）
        year: 上映年份
        genre: 电影类型（如：科幻、剧情、犯罪等）
        min_rating: 最低评分
    
    Returns:
        搜索结果
    """
    try:
        results = db.search_movies(
            keyword=keyword if keyword else None,
            year=year,
            genre=genre if genre else None,
            min_rating=min_rating if min_rating > 0 else None
        )
        
        return {
            "success": True,
            "count": len(results),
            "movies": results
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


def get_movie_details(movie_id: str) -> Dict[str, Any]:
    """
    获取电影详细信息
    
    Args:
        movie_id: 电影 ID
    
    Returns:
        电影详细信息
    """
    try:
        movie = db.get_movie_by_id(movie_id)
        if movie:
            return {
                "success": True,
                "movie": movie
            }
        else:
            return {
                "success": False,
                "error": "未找到该电影"
            }
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


def subscribe_movie(movie_id: str, quality: str = "1080p") -> Dict[str, Any]:
    """
    订阅电影（自动监控更新）
    
    Args:
        movie_id: 电影 ID
        quality: 期望的画质（720p, 1080p, 4K）
    
    Returns:
        订阅结果
    """
    try:
        result = db.add_subscription(movie_id, quality)
        return result
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


def unsubscribe_movie(movie_id: str) -> Dict[str, Any]:
    """
    取消订阅电影
    
    Args:
        movie_id: 电影 ID
    
    Returns:
        取消订阅结果
    """
    try:
        result = db.remove_subscription(movie_id)
        return result
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


def list_subscriptions() -> Dict[str, Any]:
    """
    列出所有订阅
    
    Returns:
        订阅列表
    """
    try:
        subscriptions = db.get_subscriptions()
        return {
            "success": True,
            "count": len(subscriptions),
            "subscriptions": subscriptions
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


def download_movie(movie_id: str, quality: str = "1080p") -> Dict[str, Any]:
    """
    下载电影
    
    Args:
        movie_id: 电影 ID
        quality: 画质（720p, 1080p, 4K）
    
    Returns:
        下载任务信息
    """
    try:
        result = db.add_download(movie_id, quality)
        return result
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


def list_downloads(status: str = "") -> Dict[str, Any]:
    """
    列出下载任务
    
    Args:
        status: 筛选状态（queued, downloading, completed, cancelled, failed）
    
    Returns:
        下载任务列表
    """
    try:
        downloads = db.get_downloads(status if status else None)
        return {
            "success": True,
            "count": len(downloads),
            "downloads": downloads
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


def check_download_status(download_id: str) -> Dict[str, Any]:
    """
    查看下载进度
    
    Args:
        download_id: 下载任务 ID
    
    Returns:
        下载状态信息
    """
    try:
        download = db.get_download_status(download_id)
        if download:
            return {
                "success": True,
                "download": download
            }
        else:
            return {
                "success": False,
                "error": "未找到该下载任务"
            }
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


def cancel_download(download_id: str) -> Dict[str, Any]:
    """
    取消下载任务
    
    Args:
        download_id: 下载任务 ID
    
    Returns:
        取消结果
    """
    try:
        result = db.cancel_download(download_id)
        return result
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


# 创建工具列表
def create_movie_tools() -> List[Tool]:
    """创建电影管理工具集"""
    
    tools = [
        Tool(
            name="search_movies",
            description="搜索电影资源。支持按关键词、年份、类型、评分等条件搜索。",
            parameters={
                "type": "object",
                "properties": {
                    "keyword": {
                        "type": "string",
                        "description": "搜索关键词，可以是电影名称、导演名称等"
                    },
                    "year": {
                        "type": "integer",
                        "description": "电影上映年份"
                    },
                    "genre": {
                        "type": "string",
                        "description": "电影类型，如：科幻、剧情、犯罪、悬疑等"
                    },
                    "min_rating": {
                        "type": "number",
                        "description": "最低评分（0-10）"
                    }
                }
            },
            function=search_movies
        ),
        Tool(
            name="get_movie_details",
            description="获取电影的详细信息，包括简介、评分、可用画质等。",
            parameters={
                "type": "object",
                "properties": {
                    "movie_id": {
                        "type": "string",
                        "description": "电影的唯一标识 ID"
                    }
                },
                "required": ["movie_id"]
            },
            function=get_movie_details
        ),
        Tool(
            name="subscribe_movie",
            description="订阅电影，系统会自动监控该电影的更新和资源。",
            parameters={
                "type": "object",
                "properties": {
                    "movie_id": {
                        "type": "string",
                        "description": "电影的唯一标识 ID"
                    },
                    "quality": {
                        "type": "string",
                        "description": "期望的画质，可选值：720p, 1080p, 4K",
                        "enum": ["720p", "1080p", "4K"],
                        "default": "1080p"
                    }
                },
                "required": ["movie_id"]
            },
            function=subscribe_movie
        ),
        Tool(
            name="unsubscribe_movie",
            description="取消电影订阅。",
            parameters={
                "type": "object",
                "properties": {
                    "movie_id": {
                        "type": "string",
                        "description": "电影的唯一标识 ID"
                    }
                },
                "required": ["movie_id"]
            },
            function=unsubscribe_movie
        ),
        Tool(
            name="list_subscriptions",
            description="列出所有已订阅的电影。",
            parameters={
                "type": "object",
                "properties": {}
            },
            function=list_subscriptions
        ),
        Tool(
            name="download_movie",
            description="下载电影到本地。创建下载任务并开始下载。",
            parameters={
                "type": "object",
                "properties": {
                    "movie_id": {
                        "type": "string",
                        "description": "电影的唯一标识 ID"
                    },
                    "quality": {
                        "type": "string",
                        "description": "下载画质，可选值：720p, 1080p, 4K",
                        "enum": ["720p", "1080p", "4K"],
                        "default": "1080p"
                    }
                },
                "required": ["movie_id"]
            },
            function=download_movie
        ),
        Tool(
            name="list_downloads",
            description="列出所有下载任务。可以按状态筛选。",
            parameters={
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "description": "下载状态筛选，可选值：queued（队列中）, downloading（下载中）, completed（已完成）, cancelled（已取消）, failed（失败）"
                    }
                }
            },
            function=list_downloads
        ),
        Tool(
            name="check_download_status",
            description="查看特定下载任务的进度和状态。",
            parameters={
                "type": "object",
                "properties": {
                    "download_id": {
                        "type": "string",
                        "description": "下载任务的唯一标识 ID"
                    }
                },
                "required": ["download_id"]
            },
            function=check_download_status
        ),
        Tool(
            name="cancel_download",
            description="取消正在进行的下载任务。",
            parameters={
                "type": "object",
                "properties": {
                    "download_id": {
                        "type": "string",
                        "description": "下载任务的唯一标识 ID"
                    }
                },
                "required": ["download_id"]
            },
            function=cancel_download
        )
    ]
    
    return tools
