#!/usr/bin/env python3
"""
合并重复站点脚本

用于处理在实现域名别名功能之前已经存在的重复站点记录。
"""

import sys
import os

# 添加项目根目录到Python路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db.site_oper import SiteOper
from app.core.config import settings


def main():
    """主函数"""
    print("=== MoviePilot 重复站点合并工具 ===")
    print()
    
    # 初始化站点操作器
    site_oper = SiteOper()
    
    # 检测重复站点
    print("正在检测重复站点...")
    duplicates = site_oper.find_duplicate_sites()
    
    if not duplicates:
        print("✅ 未发现重复站点，无需合并。")
        return
    
    print(f"🔍 发现 {len(duplicates)} 组重复站点：")
    print()
    
    # 显示重复站点信息
    for group_key, sites in duplicates.items():
        print(f"📍 站点组 [{group_key}]:")
        for i, site in enumerate(sites, 1):
            status = "✅ 活跃" if site['is_active'] else "❌ 禁用"
            print(f"  {i}. {site['domain']} - {site['name']} ({status})")
            print(f"     成功: {site['success_count']}, 失败: {site['fail_count']}")
            print(f"     更新时间: {site['updated_at'] or '未知'}")
        print()
    
    # 询问用户是否继续
    while True:
        choice = input("是否继续合并重复站点？(y/n): ").lower().strip()
        if choice in ['y', 'yes', '是']:
            break
        elif choice in ['n', 'no', '否']:
            print("操作已取消。")
            return
        else:
            print("请输入 y 或 n")
    
    print()
    print("开始合并重复站点...")
    
    # 执行合并
    success, message = site_oper.merge_duplicate_sites()
    
    if success:
        print(f"✅ {message}")
    else:
        print(f"❌ {message}")
    
    print()
    print("=== 合并完成 ===")


if __name__ == "__main__":
    main()
