import json
import time
from datetime import datetime
from typing import Union

import tailer
from fastapi import APIRouter, HTTPException, Depends
from fastapi.responses import StreamingResponse

from app import schemas
from app.chain.search import SearchChain
from app.core.config import settings
from app.core.security import verify_token
from app.db.systemconfig_oper import SystemConfigOper
from app.helper.message import MessageHelper
from app.helper.progress import ProgressHelper
from app.helper.sites import SitesHelper
from app.scheduler import Scheduler
from app.schemas.types import SystemConfigKey
from app.utils.http import RequestUtils
from app.utils.system import SystemUtils
from version import APP_VERSION

router = APIRouter()


@router.get("/env", summary="查询系统环境变量", response_model=schemas.Response)
def get_env_setting(_: schemas.TokenPayload = Depends(verify_token)):
    """
    查询系统环境变量，包括当前版本号
    """
    info = settings.dict(
        exclude={"SECRET_KEY", "SUPERUSER_PASSWORD", "API_TOKEN"}
    )
    info.update({
        "VERSION": APP_VERSION,
        "AUTH_VERSION": SitesHelper().auth_version,
        "INDEXER_VERSION": SitesHelper().indexer_version,
    })
    return schemas.Response(success=True,
                            data=info)


@router.get("/progress/{process_type}", summary="实时进度")
def get_progress(process_type: str, token: str):
    """
    实时获取处理进度，返回格式为SSE
    """
    if not token or not verify_token(token):
        raise HTTPException(
            status_code=403,
            detail="认证失败！",
        )

    progress = ProgressHelper()

    def event_generator():
        while True:
            detail = progress.get(process_type)
            yield 'data: %s\n\n' % json.dumps(detail)
            time.sleep(0.2)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@router.get("/setting/{key}", summary="查询系统设置", response_model=schemas.Response)
def get_setting(key: str,
                _: schemas.TokenPayload = Depends(verify_token)):
    """
    查询系统设置
    """
    return schemas.Response(success=True, data={
        "value": SystemConfigOper().get(key)
    })


@router.post("/setting/{key}", summary="更新系统设置", response_model=schemas.Response)
def set_setting(key: str, value: Union[list, dict, str, int] = None,
                _: schemas.TokenPayload = Depends(verify_token)):
    """
    更新系统设置
    """
    SystemConfigOper().set(key, value)
    return schemas.Response(success=True)


@router.get("/message", summary="实时消息")
def get_message(token: str):
    """
    实时获取系统消息，返回格式为SSE
    """
    if not token or not verify_token(token):
        raise HTTPException(
            status_code=403,
            detail="认证失败！",
        )

    message = MessageHelper()

    def event_generator():
        while True:
            detail = message.get()
            yield 'data: %s\n\n' % (detail or '')
            time.sleep(3)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@router.get("/logging", summary="实时日志")
def get_logging(token: str):
    """
    实时获取系统日志，返回格式为SSE
    """
    if not token or not verify_token(token):
        raise HTTPException(
            status_code=403,
            detail="认证失败！",
        )

    def log_generator():
        log_path = settings.LOG_PATH / 'moviepilot.log'
        # 读取文件末尾50行，不使用tailer模块
        with open(log_path, 'r', encoding='utf-8') as f:
            for line in f.readlines()[-50:]:
                yield 'data: %s\n\n' % line
        while True:
            for text in tailer.follow(open(log_path, 'r', encoding='utf-8')):
                yield 'data: %s\n\n' % (text or '')
            time.sleep(1)

    return StreamingResponse(log_generator(), media_type="text/event-stream")


@router.get("/nettest", summary="测试网络连通性")
def nettest(url: str,
            proxy: bool,
            _: schemas.TokenPayload = Depends(verify_token)):
    """
    测试网络连通性
    """
    # 记录开始的毫秒数
    start_time = datetime.now()
    url = url.replace("{TMDBAPIKEY}", settings.TMDB_API_KEY)
    result = RequestUtils(proxies=settings.PROXY if proxy else None,
                          ua=settings.USER_AGENT).get_res(url)
    # 计时结束的毫秒数
    end_time = datetime.now()
    # 计算相关秒数
    if result and result.status_code == 200:
        return schemas.Response(success=True, data={
            "time": round((end_time - start_time).microseconds / 1000)
        })
    elif result:
        return schemas.Response(success=False, message=f"错误码：{result.status_code}", data={
            "time": round((end_time - start_time).microseconds / 1000)
        })
    else:
        return schemas.Response(success=False, message="网络连接失败！")


@router.get("/versions", summary="查询Github所有Release版本", response_model=schemas.Response)
def latest_version(_: schemas.TokenPayload = Depends(verify_token)):
    """
    查询Github所有Release版本
    """
    version_res = RequestUtils(proxies=settings.PROXY, headers=settings.GITHUB_HEADERS).get_res(
        f"https://api.github.com/repos/jxxghp/MoviePilot/releases")
    if version_res:
        ver_json = version_res.json()
        if ver_json:
            return schemas.Response(success=True, data=ver_json)
    return schemas.Response(success=False)


@router.get("/ruletest", summary="优先级规则测试", response_model=schemas.Response)
def ruletest(title: str,
             subtitle: str = None,
             ruletype: str = None,
             _: schemas.TokenPayload = Depends(verify_token)):
    """
    过滤规则测试，规则类型 1-订阅，2-洗版，3-搜索
    """
    torrent = schemas.TorrentInfo(
        title=title,
        description=subtitle,
    )
    if ruletype == "2":
        rule_string = SystemConfigOper().get(SystemConfigKey.BestVersionFilterRules)
    elif ruletype == "3":
        rule_string = SystemConfigOper().get(SystemConfigKey.SearchFilterRules)
    else:
        rule_string = SystemConfigOper().get(SystemConfigKey.SubscribeFilterRules)
    if not rule_string:
        return schemas.Response(success=False, message="优先级规则未设置！")

    # 过滤
    result = SearchChain().filter_torrents(rule_string=rule_string,
                                           torrent_list=[torrent])
    if not result:
        return schemas.Response(success=False, message="不符合优先级规则！")
    return schemas.Response(success=True, data={
        "priority": 100 - result[0].pri_order + 1
    })


@router.get("/restart", summary="重启系统", response_model=schemas.Response)
def restart_system(_: schemas.TokenPayload = Depends(verify_token)):
    """
    重启系统
    """
    if not SystemUtils.can_restart():
        return schemas.Response(success=False, message="当前运行环境不支持重启操作！")
    # 执行重启
    ret, msg = SystemUtils.restart()
    return schemas.Response(success=ret, message=msg)


@router.get("/runscheduler", summary="运行服务", response_model=schemas.Response)
def execute_command(jobid: str,
                    _: schemas.TokenPayload = Depends(verify_token)):
    """
    执行命令
    """
    if not jobid:
        return schemas.Response(success=False, message="命令不能为空！")
    Scheduler().start(jobid)
    return schemas.Response(success=True)
