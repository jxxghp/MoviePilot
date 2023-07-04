import queue

from app.utils.singleton import Singleton


class MessageHelper(metaclass=Singleton):
    """
    消息队列管理器
    """
    def __init__(self):
        self.queue = queue.Queue()

    def put(self, message: str):
        self.queue.put(message)

    def get(self):
        return self.queue.get(block=False)
