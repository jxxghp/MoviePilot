from fastapi import APIRouter, Depends
from app import schemas
from app.core.security import get_user_id
from app.db.models.sysconfig import SysConfig

router = APIRouter()


@router.get("/dashboard", summary="Get dashboard config", response_model=schemas.SysConfigBase)
def get_dashboard_config(user_id: int = Depends(get_user_id)):
    """
    获取仪表盘配置
    找不到用户配置时，返回默认配置
    return: default_dashboard_config
    """
    user_config = SysConfig.get_by_uid(user_id)
    if user_config:
        return user_config
    else:
        SysConfig(uid=user_id).create()
    return schemas.SysConfigBase()


@router.put("/dashboard", summary="Update dashboard config", response_model=schemas.SysConfigBase)
def update_dashboard_config(config: schemas.SysConfigBase, user_id: int = Depends(get_user_id)):
    """
    更新仪表盘配置
    """
    user_config = SysConfig.get_by_uid(user_id)
    if user_config:
        user_config.update_by_uid(user_id, **config.dict())
        return config
    else:
        SysConfig(uid=user_id, **config.dict()).create()
    return config

# Todo: 删除用户时，删除用户配置
