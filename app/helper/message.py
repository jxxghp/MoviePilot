import queue


class MessageHelper:
    """
    消息队列管理器
    """
    def __init__(self):
        self.queue = queue.Queue()

    def put(self, message: str):
        self.queue.put(message)

    def get(self):
        return self.queue.get(block=True)
