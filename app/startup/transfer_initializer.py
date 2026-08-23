from app.application.orchestration.transfer import TransferChain


def replay_pending_transfers():
    """
    回放上次进程退出时仍未整理完的文件。

    整理队列是纯内存的，挂载挂死后的人工重启、版本升级、OOM、宿主重启都会让
    队列连同「这些文件还没整理」这个事实一起蒸发；而已稳定落地的文件不会再产生
    任何监控事件，也不会有新的补偿扫描起点，结果就是永久漏件。
    回放本身在后台线程执行，不阻塞启动流程。
    """
    TransferChain().replay_pending()


async def stop_transfer_runtime(timeout_seconds: float = 30.0) -> bool:
    """关闭已存在的整理后台 owner，且不在关停阶段创建新的整理链实例。

    :param timeout_seconds: worker 与 pending 回放共享的最大等待秒数
    :return: 没有已创建实例或所有整理后台 owner 均已收敛时返回 True
    """
    transfer_chain = TransferChain.get_existing_instance()
    if transfer_chain is None:
        return True
    return await transfer_chain.close(timeout_seconds=timeout_seconds)
