import random
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session

from app import schemas
from app.chain.tmdb import TmdbChain
from app.chain.user import UserChain
from app.core import security
from app.core.config import settings
from app.core.security import get_password_hash
from app.db import get_db
from app.db.models.user import User
from app.log import logger
from app.utils.http import RequestUtils

router = APIRouter()


@router.post("/access-token", summary="获取token", response_model=schemas.Token)
async def login_access_token(
        db: Session = Depends(get_db), form_data: OAuth2PasswordRequestForm = Depends()
) -> Any:
    """
    获取认证Token
    """
    # 检查数据库
    user = User.authenticate(
        db=db,
        name=form_data.username,
        password=form_data.password
    )
    if not user:
        # 请求协助认证
        logger.warn("登录用户本地不匹配，尝试辅助认证 ...")
        token = UserChain(db).user_authenticate(form_data.username, form_data.password)
        if not token:
            raise HTTPException(status_code=401, detail="用户名或密码不正确")
        else:
            logger.info(f"辅助认证成功，用户信息: {token}")
            # 加入用户信息表
            user = User.get_by_name(db=db, name=form_data.username)
            if not user:
                logger.info(f"用户不存在，创建用户: {form_data.username}")
                user = User(name=form_data.username, is_active=True,
                            is_superuser=False, hashed_password=get_password_hash(token))
                user.create(db)
    elif not user.is_active:
        raise HTTPException(status_code=403, detail="用户未启用")
    access_token_expires = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    return schemas.Token(
        access_token=security.create_access_token(
            user.id, expires_delta=access_token_expires
        ),
        token_type="bearer",
    )


@router.get("/bing", summary="Bing每日壁纸", response_model=schemas.Response)
def bing_wallpaper() -> Any:
    """
    获取Bing每日壁纸
    """
    url = "https://cn.bing.com/HPImageArchive.aspx?format=js&idx=0&n=1"
    try:
        resp = RequestUtils(timeout=5).get_res(url)
    except Exception as err:
        print(str(err))
        return schemas.Response(success=False)
    if resp and resp.status_code == 200:
        try:
            result = resp.json()
            if isinstance(result, dict):
                for image in result.get('images') or []:
                    return schemas.Response(success=False,
                                            message=f"https://cn.bing.com{image.get('url')}" if 'url' in image else '')
        except Exception as err:
            print(str(err))
    return schemas.Response(success=False)


@router.get("/tmdb", summary="TMDB电影海报", response_model=schemas.Response)
def tmdb_wallpaper(db: Session = Depends(get_db)) -> Any:
    """
    获取TMDB电影海报
    """
    infos = TmdbChain(db).tmdb_trending()
    if infos:
        # 随机一个电影
        while True:
            info = random.choice(infos)
            if info and info.get("backdrop_path"):
                return schemas.Response(
                    success=True,
                    message=f"https://image.tmdb.org/t/p/original{info.get('backdrop_path')}"
                )
    return schemas.Response(success=False)
