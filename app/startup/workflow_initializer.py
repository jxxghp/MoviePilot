from fastapi import FastAPI

from app.core.workflow import WorkFlowManager


def init_workflow(_: FastAPI):
    """
    初始化动作
    """
    WorkFlowManager()


def stop_workflow(_: FastAPI):
    """
    停止动作
    """
    WorkFlowManager().stop()
