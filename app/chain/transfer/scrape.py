"""整理批次的元数据刮削事件聚合。"""

from typing import Any, Optional

from app.application.transfer.workflow import TransferTask, job_lock
from app.chain._contracts import TransferMixinHost
from app.chain.transfer.contract import _TransferOwnerBase
from app.domain.context import MusicInfo
from app.schemas.transfer import TransferInfo
from app.schemas.types import (
    EventType,
)
from app.schemas.workflow import FileItem


class ScrapeBatchMixin(_TransferOwnerBase):
    """聚合同批整理结果并仅在批次完成后发送刮削事件。"""

    __mixin_host_protocol__ = TransferMixinHost


    def _send_metadata_scrape_event(
            self, task: TransferTask, transferinfo: TransferInfo
    ):
        """
        发送元数据刮削事件，保持对外事件载荷兼容。
        """
        if (
                not task
                or not transferinfo
                or not transferinfo.need_scrape
                or not self._is_primary_media_file(task.fileitem, task.mediainfo)
        ):
            return

        target_diritem = transferinfo.target_diritem
        if not target_diritem:
            return

        self.eventmanager.send_event(
            EventType.MetadataScrape,
            self._build_metadata_scrape_payload(
                task=task,
                fileitem=target_diritem,
                file_list=transferinfo.file_list_new,
                overwrite=False,
            ),
        )

    @staticmethod
    def _build_metadata_scrape_payload(
            task: TransferTask,
            fileitem: FileItem,
            file_list: Optional[list[str]],
            overwrite: bool,
    ) -> dict[str, Any]:
        """构造刮削事件载荷，并为音乐批次保留逐文件身份上下文。"""
        paths = list(dict.fromkeys(file_list or []))
        payload: dict[str, Any] = {
            "meta": task.meta,
            "mediainfo": task.mediainfo,
            "fileitem": fileitem,
            "file_list": paths,
            "overwrite": overwrite,
        }
        if isinstance(task.mediainfo, MusicInfo):
            payload["file_contexts"] = [
                {
                    "path": path,
                    "meta": task.meta,
                    "mediainfo": task.mediainfo,
                }
                for path in paths
            ]
        return payload

    def _register_scrape_batch_task(self, task: TransferTask):
        """
        登记批次任务。刮削事件只在批次关闭且任务全部完成后统一发送。
        """
        if not task or not task.transfer_batch_id:
            return
        with job_lock:
            batch = self._scrape_batches.setdefault(
                task.transfer_batch_id,
                {
                    "pending": set(),
                    "targets": {},
                    "closed": False,
                },
            )
            batch["pending"].add(task.fileitem.path)

    def _close_scrape_batch(self, batch_id: Optional[str]):
        """
        标记批次不再接收新任务，并尝试发送已聚合的刮削事件。
        """
        if not batch_id:
            return
        with job_lock:
            batch = self._scrape_batches.setdefault(
                batch_id,
                {
                    "pending": set(),
                    "targets": {},
                    "closed": False,
                },
            )
            batch["closed"] = True
        self._flush_scrape_batch_if_ready(batch_id)

    def _record_scrape_target(self, task: TransferTask, transferinfo: TransferInfo):
        """
        记录批次内需要刮削的目标文件，按目标媒体根目录聚合。
        """
        if (
                not task
                or not task.transfer_batch_id
                or not transferinfo
                or not transferinfo.need_scrape
                or not self._is_primary_media_file(task.fileitem, task.mediainfo)
        ):
            return

        target_diritem = transferinfo.target_diritem
        if not target_diritem:
            return

        target_files = transferinfo.file_list_new or []
        target_key = (target_diritem.storage, target_diritem.path)
        with job_lock:
            batch = self._scrape_batches.setdefault(
                task.transfer_batch_id,
                {
                    "pending": set(),
                    "targets": {},
                    "closed": False,
                },
            )
            target = batch["targets"].setdefault(
                target_key,
                {
                    "fileitem": target_diritem,
                    "meta": task.meta,
                    "mediainfo": task.mediainfo,
                    "files": [],
                    "file_contexts": {},
                    "overwrite": False,
                },
            )
            if not target.get("meta"):
                target["meta"] = task.meta
            if not target.get("mediainfo"):
                target["mediainfo"] = task.mediainfo
            for target_file in target_files:
                if target_file and target_file not in target["files"]:
                    target["files"].append(target_file)
                if target_file and isinstance(task.mediainfo, MusicInfo):
                    target["file_contexts"][target_file] = {
                        "path": target_file,
                        "meta": task.meta,
                        "mediainfo": task.mediainfo,
                    }

    def _finish_scrape_batch_task(self, task: TransferTask):
        """
        标记批次内单个任务已结束。
        """
        if not task or not task.transfer_batch_id:
            return
        with job_lock:
            batch = self._scrape_batches.get(task.transfer_batch_id)
            if not batch:
                return
            batch["pending"].discard(task.fileitem.path)
        self._flush_scrape_batch_if_ready(task.transfer_batch_id)

    def _flush_scrape_batch_if_ready(self, batch_id: Optional[str]):
        """
        批次任务全部结束后发送聚合后的刮削事件。
        """
        if not batch_id:
            return

        with job_lock:
            batch = self._scrape_batches.get(batch_id)
            if (
                    not batch
                    or not batch.get("closed")
                    or batch.get("pending")
            ):
                return
            targets = list(batch.get("targets", {}).values())
            self._scrape_batches.pop(batch_id, None)

        for target in targets:
            fileitem = target.get("fileitem")
            if not fileitem:
                continue
            file_list = list(dict.fromkeys(target.get("files") or []))
            file_contexts = target.get("file_contexts") or {}
            payload = {
                "meta": target.get("meta"),
                "mediainfo": target.get("mediainfo"),
                "fileitem": fileitem,
                "file_list": file_list,
                "overwrite": target.get("overwrite", False),
            }
            if file_contexts:
                payload["file_contexts"] = [
                    file_contexts[path]
                    for path in file_list
                    if path in file_contexts
                ]
            self.eventmanager.send_event(
                EventType.MetadataScrape,
                payload,
            )
