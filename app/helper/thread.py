# 兼容垫片：ThreadHelper 已迁移至 app.core.thread。此处保留 re-export 以兼容旧导入路径。
from app.core.thread import ThreadHelper

__all__ = ["ThreadHelper"]
