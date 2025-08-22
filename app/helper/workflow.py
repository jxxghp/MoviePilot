import json
from typing import List, Tuple, Optional

from app.core.cache import cached
from app.core.config import settings
from app.db.models import Workflow
from app.db.workflow_oper import WorkflowOper
from app.log import logger
from app.utils.http import RequestUtils, AsyncRequestUtils
from app.utils.singleton import WeakSingleton
from app.utils.system import SystemUtils


class WorkflowHelper(metaclass=WeakSingleton):
    """
    工作流分享等
    """

    _workflow_share = f"{settings.MP_SERVER_HOST}/workflow/share"

    _workflow_shares = f"{settings.MP_SERVER_HOST}/workflow/shares"

    _workflow_fork = f"{settings.MP_SERVER_HOST}/workflow/fork/%s"

    _shares_cache_region = "workflow_share"

    _share_user_id = None

    def __init__(self):
        self.get_user_uuid()

    @staticmethod
    def _check_workflow_share_enabled() -> Tuple[bool, str]:
        """
        检查工作流分享功能是否开启
        """
        if not settings.WORKFLOW_STATISTIC_SHARE:
            return False, "当前没有开启工作流数据共享功能"
        return True, ""

    @staticmethod
    def _validate_workflow(workflow: Workflow) -> Tuple[bool, str]:
        """
        验证工作流是否可以分享
        """
        if not workflow:
            return False, "工作流不存在"

        if not workflow.actions or not workflow.flows:
            return False, "请分享有动作和流程的工作流"

        return True, ""

    @staticmethod
    def _prepare_workflow_data(workflow: Workflow) -> dict:
        """
        准备工作流分享数据
        """
        workflow_dict = workflow.to_dict()
        workflow_dict.pop("id", None)
        workflow_dict.pop("context", None)
        workflow_dict['actions'] = json.dumps(workflow_dict['actions'] or [])
        workflow_dict['flows'] = json.dumps(workflow_dict['flows'] or [])
        return workflow_dict

    def _build_share_payload(self, share_title: str, share_comment: str,
                             share_user: str, workflow_dict: dict) -> dict:
        """
        构建分享请求载荷
        """
        return {
            "share_title": share_title,
            "share_comment": share_comment,
            "share_user": share_user,
            "share_uid": self._share_user_id,
            **workflow_dict
        }

    def _handle_response(self, res, clear_cache: bool = True) -> Tuple[bool, str]:
        """
        处理HTTP响应
        """
        if res is None:
            return False, "连接MoviePilot服务器失败"

        # 检查响应状态
        success = True if res.status_code == 200 else False

        if success:
            # 清除缓存
            if clear_cache:
                self.get_shares.cache_clear()
                self.async_get_shares.cache_clear()
            return True, ""
        else:
            try:
                error_msg = res.json().get("message", "未知错误")
            except (json.JSONDecodeError, ValueError) as e:
                logger.error(f"工作流响应JSON解析失败: {e}")
                error_msg = f"响应解析失败: {res.text[:100]}..."
            return False, error_msg

    @staticmethod
    def _handle_list_response(res) -> List[dict]:
        """
        处理返回List的HTTP响应
        """
        if res and res.status_code == 200:
            try:
                return res.json()
            except (json.JSONDecodeError, ValueError) as e:
                logger.error(f"工作流列表响应JSON解析失败: {e}")
                return []
        return []

    def workflow_share(self, workflow_id: int,
                       share_title: str, share_comment: str, share_user: str) -> Tuple[bool, str]:
        """
        分享工作流
        """
        # 检查功能是否开启
        enabled, message = self._check_workflow_share_enabled()
        if not enabled:
            return False, message

        # 获取工作流信息
        workflow = WorkflowOper().get(workflow_id)

        # 验证工作流
        valid, message = self._validate_workflow(workflow)
        if not valid:
            return False, message

        # 准备数据
        workflow_dict = self._prepare_workflow_data(workflow)
        payload = self._build_share_payload(share_title, share_comment, share_user, workflow_dict)

        # 发送分享请求
        res = RequestUtils(proxies=settings.PROXY or {},
                           content_type="application/json",
                           timeout=10).post(self._workflow_share, json=payload)

        return self._handle_response(res)

    async def async_workflow_share(self, workflow_id: int,
                                   share_title: str, share_comment: str, share_user: str) -> Tuple[bool, str]:
        """
        异步分享工作流
        """
        # 检查功能是否开启
        enabled, message = self._check_workflow_share_enabled()
        if not enabled:
            return False, message

        # 获取工作流信息
        workflow = await WorkflowOper().async_get(workflow_id)

        # 验证工作流
        valid, message = self._validate_workflow(workflow)
        if not valid:
            return False, message

        # 准备数据
        workflow_dict = self._prepare_workflow_data(workflow)
        payload = self._build_share_payload(share_title, share_comment, share_user, workflow_dict)

        # 发送分享请求
        res = await AsyncRequestUtils(proxies=settings.PROXY or {},
                                      content_type="application/json",
                                      timeout=10).post(self._workflow_share, json=payload)

        return self._handle_response(res)

    def share_delete(self, share_id: int) -> Tuple[bool, str]:
        """
        删除分享
        """
        # 检查功能是否开启
        enabled, message = self._check_workflow_share_enabled()
        if not enabled:
            return False, message

        res = RequestUtils(proxies=settings.PROXY or {},
                           timeout=5).delete_res(f"{self._workflow_share}/{share_id}",
                                                 params={"share_uid": self._share_user_id})

        return self._handle_response(res)

    async def async_share_delete(self, share_id: int) -> Tuple[bool, str]:
        """
        异步删除分享
        """
        # 检查功能是否开启
        enabled, message = self._check_workflow_share_enabled()
        if not enabled:
            return False, message

        res = await AsyncRequestUtils(proxies=settings.PROXY or {},
                                      timeout=5).delete_res(f"{self._workflow_share}/{share_id}",
                                                            params={"share_uid": self._share_user_id})

        return self._handle_response(res)

    def workflow_fork(self, share_id: int) -> Tuple[bool, str]:
        """
        复用分享的工作流
        """
        # 检查功能是否开启
        enabled, message = self._check_workflow_share_enabled()
        if not enabled:
            return False, message

        res = RequestUtils(proxies=settings.PROXY or {}, timeout=5, headers={
            "Content-Type": "application/json"
        }).get_res(self._workflow_fork % share_id)

        return self._handle_response(res, clear_cache=False)

    async def async_workflow_fork(self, share_id: int) -> Tuple[bool, str]:
        """
        异步复用分享的工作流
        """
        # 检查功能是否开启
        enabled, message = self._check_workflow_share_enabled()
        if not enabled:
            return False, message

        res = await AsyncRequestUtils(proxies=settings.PROXY or {},
                                      timeout=5,
                                      headers={
                                          "Content-Type": "application/json"
                                      }).get_res(self._workflow_fork % share_id)

        return self._handle_response(res, clear_cache=False)

    @cached(region=_shares_cache_region, maxsize=1, skip_empty=True)
    def get_shares(self, name: Optional[str] = None, page: Optional[int] = 1, count: Optional[int] = 30) -> List[dict]:
        """
        获取工作流分享数据
        """
        enabled, _ = self._check_workflow_share_enabled()
        if not enabled:
            return []

        res = RequestUtils(proxies=settings.PROXY or {}, timeout=15).get_res(self._workflow_shares, params={
            "name": name,
            "page": page,
            "count": count
        })
        return self._handle_list_response(res)

    @cached(region=_shares_cache_region, maxsize=1, skip_empty=True)
    async def async_get_shares(self, name: Optional[str] = None, page: Optional[int] = 1, count: Optional[int] = 30) -> \
            List[dict]:
        """
        异步获取工作流分享数据
        """
        enabled, _ = self._check_workflow_share_enabled()
        if not enabled:
            return []

        res = await AsyncRequestUtils(proxies=settings.PROXY or {}, timeout=15).get_res(self._workflow_shares, params={
            "name": name,
            "page": page,
            "count": count
        })
        return self._handle_list_response(res)

    def get_user_uuid(self) -> str:
        """
        获取用户uuid
        """
        if not self._share_user_id:
            self._share_user_id = SystemUtils.generate_user_unique_id()
            logger.info(f"当前用户UUID: {self._share_user_id}")
        return self._share_user_id or ""
