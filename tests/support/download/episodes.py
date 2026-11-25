"""
下载器既有任务的文件选择与启停状态模拟；不注册测试用例。
"""

from types import SimpleNamespace

from app.modules.qbittorrent import QbittorrentModule
from app.modules.transmission import TransmissionModule


def existing_task(module, server, torrent_hash, names, *, wanted=(), completed=(), stopped=False, tags=(), error=False):
    """
    让真实模块把本次添加解释为已有任务，并用可变状态模拟下载器的文件选择、启停与标签。

    文件写入、启动和标签读写都会改变模拟状态，使模块写入后的回读确认能看到真实效果。

    :param module: 当前参数化的下载器模块
    :param server: 被替换的外部下载器客户端
    :param torrent_hash: 已有任务的哈希
    :param names: 种子内文件的相对路径
    :param wanted: 当前已选中的文件索引
    :param completed: 当前已下载完成的文件索引
    :param stopped: 任务当前是否处于暂停状态
    :param tags: 任务当前的标签
    :param error: 下载器是否报告任务出错（qB missingFiles、TR 本地错误；rT 没有错误状态）
    :return: 文件状态（索引到 wanted/completed）与任务状态（stopped/tags）两个可变字典
    """
    files = {index: {"wanted": index in wanted, "completed": index in completed} for index in range(len(names))}
    task = {"stopped": stopped, "tags": list(tags)}

    def read_files(*_args, **_kwargs):
        """
        按各客户端的文件对象协议投影当前模拟状态。
        """
        if isinstance(module, TransmissionModule):
            return [
                SimpleNamespace(id=index, name=name, selected=files[index]["wanted"],
                                completed=42 if files[index]["completed"] else 0, size=42)
                for index, name in enumerate(names)
            ]
        scale = 1 if isinstance(module, QbittorrentModule) else 100
        return [
            {"id": index, "name": name, "priority": 1 if files[index]["wanted"] else 0,
             "progress": scale if files[index]["completed"] else 0}
            for index, name in enumerate(names)
        ]

    def write_files(*args, **kwargs):
        """
        记录被设为下载或不下载的文件。
        """
        file_ids = kwargs["file_ids"] if "file_ids" in kwargs else args[1]
        for file_id in file_ids:
            files[file_id]["wanted"] = kwargs.get("priority", 1) > 0
        return True

    def start(*_args, **_kwargs):
        """
        启动任务后状态变为运行。
        """
        task["stopped"] = False
        return True

    def stop(*_args, **_kwargs):
        """
        暂停任务。
        """
        task["stopped"] = True
        return True

    def add_tags(*_args, ids=None, tags=(), **_kwargs):
        """
        qB/rT 追加标签（rT 客户端在内部合并 d.custom1）。
        """
        task["tags"] += [tag for tag in tags if tag not in task["tags"]]
        return True

    def remove_tags(*_args, ids=None, tag=(), **_kwargs):
        """
        qB/rT 移除指定标签。
        """
        removed = [tag] if isinstance(tag, str) else list(tag)
        task["tags"] = [item for item in task["tags"] if item not in removed]
        return True

    def replace_labels(*_args, ids=None, labels=(), **_kwargs):
        """
        Transmission 整体覆盖标签。
        """
        task["tags"] = list(labels)
        return True

    def read_torrents(*_args, **_kwargs):
        """
        返回带当前启停状态与标签的已有任务。
        """
        if isinstance(module, TransmissionModule):
            torrent = SimpleNamespace(hashString=torrent_hash, name="Show", labels=list(task["tags"]),
                                      status="stopped" if task["stopped"] else "downloading", error=3 if error else 0)
        elif isinstance(module, QbittorrentModule):
            state = "missingFiles" if error else "pausedDL" if task["stopped"] else "downloading"
            torrent = {"hash": torrent_hash, "name": "Show", "tags": ",".join(task["tags"]), "state": state}
        else:
            torrent = {"hash": torrent_hash, "name": "Show", "tags": ",".join(task["tags"]),
                       "state": 0 if task["stopped"] else 1, "is_active": 0 if task["stopped"] else 1, "complete": 0}
        return [torrent], False

    server.add_torrent.return_value = (False, []) if isinstance(module, QbittorrentModule) else False
    server.add_torrent_with_status.return_value = (None, False)
    server.get_torrents.side_effect = read_torrents
    server.get_files.side_effect = read_files
    server.set_files.side_effect = write_files
    server.start_torrents.side_effect = start
    server.torrents_set_force_start.side_effect = start
    server.resume_torrents.side_effect = start
    server.stop_torrents.side_effect = stop
    server.set_torrents_tag.side_effect = add_tags
    server.remove_torrents_tag.side_effect = remove_tags
    server.replace_torrent_labels.side_effect = replace_labels
    server.update_priorities.return_value = True
    return files, task
