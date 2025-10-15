#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
测试TMDB海报获取功能
测试变形金刚2的海报获取是否正常
"""

import sys
import os
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from app.modules.themoviedb.tmdbapi import TmdbApi
from app.core.config import settings

def test_transformers_poster():
    """测试变形金刚2的海报获取"""
    print("开始测试变形金刚2的海报获取...")
    
    # 创建TMDB API实例
    tmdb_api = TmdbApi()
    
    # 变形金刚2的TMDB ID
    transformers_id = 8373
    
    try:
        # 获取电影详情
        print(f"获取电影详情 (ID: {transformers_id})...")
        movie_info = tmdb_api.get_info("movie", transformers_id)
        
        if movie_info:
            print(f"电影标题: {movie_info.get('title')}")
            print(f"原始海报路径: {movie_info.get('poster_path')}")
            print(f"原始背景图路径: {movie_info.get('backdrop_path')}")
            
            # 获取图片信息
            print("\n获取图片信息...")
            images = tmdb_api.get_movie_images(transformers_id)
            
            if images:
                posters = images.get("posters", [])
                print(f"找到 {len(posters)} 张海报")
                
                # 显示前5张海报的语言信息
                for i, poster in enumerate(posters[:5]):
                    lang = poster.get("iso_639_1", "unknown")
                    file_path = poster.get("file_path", "")
                    print(f"  海报 {i+1}: 语言={lang}, 路径={file_path}")
                
                # 检查是否有zh-CN地区的海报
                zh_cn_posters = [p for p in posters if p.get("iso_639_1") == "zh-CN"]
                zh_tw_posters = [p for p in posters if p.get("iso_639_1") == "zh-TW"]
                
                print(f"\nzh-CN地区海报数量: {len(zh_cn_posters)}")
                print(f"zh-TW地区海报数量: {len(zh_tw_posters)}")
                
                if zh_cn_posters:
                    print("找到zh-CN地区海报！")
                    best_poster = zh_cn_posters[0]
                    print(f"最佳zh-CN海报: {best_poster.get('file_path')}")
                elif zh_tw_posters:
                    print("找到zh-TW地区海报！")
                    best_poster = zh_tw_posters[0]
                    print(f"最佳zh-TW海报: {best_poster.get('file_path')}")
                else:
                    print("未找到中文地区海报")
            else:
                print("未获取到图片信息")
        else:
            print("未获取到电影信息")
            
    except Exception as e:
        print(f"测试失败: {str(e)}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    test_transformers_poster()