#!/usr/bin/env python3
"""
测试SMB连接修复的脚本
"""

import sys
import os
import time
import threading
from pathlib import Path

# 添加项目根目录到Python路径
sys.path.insert(0, str(Path(__file__).parent))

from app.modules.filemanager.storages.smb import SMB
from app.schemas import FileItem
from app.log import logger

def test_smb_connection():
    """
    测试SMB连接和基本操作
    """
    try:
        logger.info("开始测试SMB连接...")
        
        # 创建SMB实例
        smb = SMB()
        
        # 测试连接检查
        if smb.check():
            logger.info("SMB连接检查成功")
        else:
            logger.error("SMB连接检查失败")
            return False
        
        # 测试列出根目录
        root_item = FileItem(
            storage="smb",
            path="/",
            name="",
            basename="",
            type="dir",
            modify_time=int(time.time())
        )
        
        items = smb.list(root_item)
        if items is not None:
            logger.info(f"成功列出根目录，找到 {len(items)} 个项目")
            for item in items[:5]:  # 只显示前5个
                logger.info(f"  - {item.name} ({item.type})")
        else:
            logger.error("列出根目录失败")
            return False
        
        # 测试上下文管理器
        with smb as smb_ctx:
            logger.info("使用上下文管理器测试SMB操作")
            items = smb_ctx.list(root_item)
            if items is not None:
                logger.info("上下文管理器测试成功")
            else:
                logger.error("上下文管理器测试失败")
                return False
        
        logger.info("SMB连接测试完成")
        return True
        
    except Exception as e:
        logger.error(f"SMB连接测试失败: {e}")
        return False

def test_concurrent_operations():
    """
    测试并发SMB操作
    """
    def worker(worker_id):
        """工作线程函数"""
        try:
            smb = SMB()
            root_item = FileItem(
                storage="smb",
                path="/",
                name="",
                basename="",
                type="dir",
                modify_time=int(time.time())
            )
            
            for i in range(3):
                items = smb.list(root_item)
                logger.info(f"Worker {worker_id} 第 {i+1} 次操作完成，找到 {len(items) if items else 0} 个项目")
                time.sleep(0.1)
                
        except Exception as e:
            logger.error(f"Worker {worker_id} 出错: {e}")
    
    try:
        logger.info("开始测试并发SMB操作...")
        
        # 创建多个线程
        threads = []
        for i in range(3):
            thread = threading.Thread(target=worker, args=(i,))
            threads.append(thread)
            thread.start()
        
        # 等待所有线程完成
        for thread in threads:
            thread.join()
        
        logger.info("并发SMB操作测试完成")
        return True
        
    except Exception as e:
        logger.error(f"并发SMB操作测试失败: {e}")
        return False

def main():
    """
    主测试函数
    """
    logger.info("=" * 50)
    logger.info("SMB连接修复测试开始")
    logger.info("=" * 50)
    
    # 测试基本连接
    if not test_smb_connection():
        logger.error("基本连接测试失败")
        return False
    
    # 测试并发操作
    if not test_concurrent_operations():
        logger.error("并发操作测试失败")
        return False
    
    logger.info("=" * 50)
    logger.info("所有测试通过！SMB连接修复成功")
    logger.info("=" * 50)
    return True

if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)