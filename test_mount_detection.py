#!/usr/bin/env python3
"""
挂载检测功能测试脚本
"""

import sys
from pathlib import Path

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent))

from app.utils.system import SystemUtils

def test_mount_detection():
    """测试挂载检测功能"""
    print("=== 挂载检测功能测试 ===\n")
    
    # 测试路径
    test_paths = [
        "/tmp",
        "/home",
        "/mnt",
        "/media",
        "/",
    ]
    
    for path_str in test_paths:
        path = Path(path_str)
        print(f"测试路径: {path}")
        
        # 获取挂载信息
        mount_info = SystemUtils.get_mount_info(path)
        if mount_info:
            print(f"  挂载信息: {mount_info}")
            
            # 检查是否为网络挂载
            is_network = SystemUtils.is_network_mount(path)
            print(f"  是否网络挂载: {is_network}")
            
            # 获取设备标识
            device = SystemUtils.get_mount_device(path)
            print(f"  设备标识: {device}")
        else:
            print(f"  不是挂载点")
        
        print()
    
    # 测试同一挂载检测
    print("=== 同一挂载检测测试 ===")
    test_pairs = [
        ("/tmp", "/tmp/test"),
        ("/home", "/home/user"),
        ("/mnt", "/mnt/data"),
    ]
    
    for src, dest in test_pairs:
        src_path = Path(src)
        dest_path = Path(dest)
        is_same = SystemUtils.is_same_mount(src_path, dest_path)
        print(f"{src} 和 {dest} 是否同一挂载: {is_same}")
    
    print("\n=== 测试完成 ===")

if __name__ == "__main__":
    test_mount_detection()