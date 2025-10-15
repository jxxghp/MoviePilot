#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
简化的TMDB海报测试
直接测试TMDB API的地区化图片支持
"""

import requests
import json

def test_tmdb_images():
    """测试TMDB API的地区化图片支持"""
    print("开始测试TMDB API的地区化图片支持...")
    
    # TMDB API配置
    api_key = "db55323b8d3e4154498498a75642b381"
    base_url = "https://api.themoviedb.org/3"
    
    # 变形金刚2的TMDB ID
    movie_id = 8373
    
    try:
        # 测试1: 获取电影详情
        print(f"\n1. 获取电影详情 (ID: {movie_id})...")
        url = f"{base_url}/movie/{movie_id}"
        params = {
            "api_key": api_key,
            "language": "zh-CN"
        }
        
        response = requests.get(url, params=params)
        if response.status_code == 200:
            movie_data = response.json()
            print(f"电影标题: {movie_data.get('title')}")
            print(f"原始海报路径: {movie_data.get('poster_path')}")
            print(f"原始背景图路径: {movie_data.get('backdrop_path')}")
        else:
            print(f"获取电影详情失败: {response.status_code}")
            return
        
        # 测试2: 获取图片信息（包含地区化参数）
        print(f"\n2. 获取图片信息（包含地区化参数）...")
        url = f"{base_url}/movie/{movie_id}/images"
        params = {
            "api_key": api_key,
            "include_image_language": "zh-CN,zh-TW,null"
        }
        
        response = requests.get(url, params=params)
        if response.status_code == 200:
            images_data = response.json()
            posters = images_data.get("posters", [])
            backdrops = images_data.get("backdrops", [])
            
            print(f"找到 {len(posters)} 张海报")
            print(f"找到 {len(backdrops)} 张背景图")
            
            # 分析海报语言分布
            lang_count = {}
            for poster in posters:
                lang = poster.get("iso_639_1") or "unknown"
                lang_count[lang] = lang_count.get(lang, 0) + 1
            
            print("\n海报语言分布:")
            for lang, count in sorted(lang_count.items(), key=lambda x: (x[0] is None, x[0])):
                print(f"  {lang}: {count} 张")
            
            # 检查zh-CN和zh-TW海报
            zh_cn_posters = [p for p in posters if p.get("iso_639_1") == "zh-CN"]
            zh_tw_posters = [p for p in posters if p.get("iso_639_1") == "zh-TW"]
            
            print(f"\nzh-CN地区海报数量: {len(zh_cn_posters)}")
            print(f"zh-TW地区海报数量: {len(zh_tw_posters)}")
            
            if zh_cn_posters:
                print("找到zh-CN地区海报！")
                best_poster = zh_cn_posters[0]
                print(f"最佳zh-CN海报: {best_poster.get('file_path')}")
                print(f"海报URL: https://image.tmdb.org/t/p/original{best_poster.get('file_path')}")
            elif zh_tw_posters:
                print("找到zh-TW地区海报！")
                best_poster = zh_tw_posters[0]
                print(f"最佳zh-TW海报: {best_poster.get('file_path')}")
                print(f"海报URL: https://image.tmdb.org/t/p/original{best_poster.get('file_path')}")
            else:
                print("未找到中文地区海报")
                
            # 显示前5张海报的详细信息
            print("\n前5张海报详细信息:")
            for i, poster in enumerate(posters[:5]):
                lang = poster.get("iso_639_1", "unknown")
                file_path = poster.get("file_path", "")
                width = poster.get("width", 0)
                height = poster.get("height", 0)
                print(f"  海报 {i+1}: 语言={lang}, 尺寸={width}x{height}, 路径={file_path}")
                
        else:
            print(f"获取图片信息失败: {response.status_code}")
            print(f"响应内容: {response.text}")
            
    except Exception as e:
        print(f"测试失败: {str(e)}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    test_tmdb_images()