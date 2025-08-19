#!/usr/bin/env python3
"""
测试缓存重构的脚本
"""

import sys
import os

# 添加项目根目录到Python路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.helper.redis_helper import redis_helper, cached
from app.helper.cache_manager import (
    CacheManager, 
    TmdbCacheManager, 
    DoubanCacheManager, 
    TorrentsCacheManager
)
from app.core.config import settings

def test_redis_helper():
    """测试RedisHelper基本功能"""
    print("=== 测试RedisHelper基本功能 ===")
    
    # 测试设置和获取缓存
    redis_helper.set("test_key", "test_value", ttl=60, region="test")
    value = redis_helper.get("test_key", region="test")
    print(f"Redis缓存测试: {value}")
    
    # 测试缓存装饰器
    @cached(region="test_decorator", ttl=60)
    def test_function(x, y):
        return x + y
    
    result1 = test_function(1, 2)
    result2 = test_function(1, 2)  # 应该从缓存获取
    print(f"装饰器缓存测试: {result1}, {result2}")
    
    # 清理测试缓存
    redis_helper.clear(region="test")
    redis_helper.clear(region="test_decorator")

def test_cache_manager():
    """测试CacheManager基本功能"""
    print("\n=== 测试CacheManager基本功能 ===")
    
    # 创建测试缓存管理器
    test_cache = CacheManager("test_cache", region="test_manager")
    
    # 测试设置和获取
    test_cache.set("key1", "value1", ttl=60)
    value = test_cache.get("key1")
    print(f"CacheManager测试: {value}")
    
    # 测试存在性检查
    exists = test_cache.exists("key1")
    print(f"缓存存在性检查: {exists}")
    
    # 清理测试缓存
    test_cache.clear()

def test_tmdb_cache_manager():
    """测试TMDB缓存管理器"""
    print("\n=== 测试TMDB缓存管理器 ===")
    
    # 创建模拟的meta对象
    class MockMeta:
        def __init__(self, name, year, type_val="电影"):
            self.name = name
            self.year = year
            self.type = type_val
            self.tmdbid = None
            self.begin_season = None
    
    meta = MockMeta("测试电影", "2023")
    
    # 测试TMDB缓存
    tmdb_cache = TmdbCacheManager()
    
    # 模拟缓存数据
    cache_data = {
        "id": 12345,
        "type": "电影",
        "year": "2023",
        "title": "测试电影",
        "poster_path": "/test/poster.jpg"
    }
    
    tmdb_cache.set_by_meta(meta, cache_data)
    retrieved_data = tmdb_cache.get_by_meta(meta)
    print(f"TMDB缓存测试: {retrieved_data.get('title')}")

def test_torrents_cache_manager():
    """测试种子缓存管理器"""
    print("\n=== 测试种子缓存管理器 ===")
    
    # 创建种子缓存管理器
    torrents_cache = TorrentsCacheManager("spider")
    
    # 模拟种子数据
    torrents_data = {
        "site1": [
            {"title": "测试种子1", "size": "1GB"},
            {"title": "测试种子2", "size": "2GB"}
        ],
        "site2": [
            {"title": "测试种子3", "size": "3GB"}
        ]
    }
    
    # 测试设置和获取种子缓存
    torrents_cache.set_torrents(torrents_data)
    retrieved_torrents = torrents_cache.get_torrents()
    print(f"种子缓存测试: 站点数量={len(retrieved_torrents)}")
    for site, torrents in retrieved_torrents.items():
        print(f"  {site}: {len(torrents)}个种子")

def main():
    """主测试函数"""
    print("开始测试缓存重构...")
    
    try:
        # 测试RedisHelper
        test_redis_helper()
        
        # 测试CacheManager
        test_cache_manager()
        
        # 测试TMDB缓存管理器
        test_tmdb_cache_manager()
        
        # 测试种子缓存管理器
        test_torrents_cache_manager()
        
        print("\n=== 所有测试完成 ===")
        print("缓存重构测试成功！")
        
    except Exception as e:
        print(f"测试失败: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    main()