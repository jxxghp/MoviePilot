import json
import time
from datetime import datetime
from typing import Union, Any

import tailer
from dotenv import set_key
from fastapi import APIRouter, HTTPException, Depends, Response
from fastapi.responses import StreamingResponse

from app import schemas
from app.chain.search import SearchChain
from app.chain.system import SystemChain
from app.core.config import settings, global_vars
from app.core.module import ModuleManager
from app.core.security import verify_token
from app.db.models import User
from app.db.systemconfig_oper import SystemConfigOper
from app.db.userauth import get_current_active_superuser
from app.helper.message import MessageHelper
from app.helper.progress import ProgressHelper
from app.helper.sites import SitesHelper
from app.scheduler import Scheduler
from app.schemas.types import SystemConfigKey
from app.utils.http import RequestUtils
from app.utils.system import SystemUtils
from version import APP_VERSION

router = APIRouter()


@router.get("/img/{proxy}", summary="图片代理")
def get_img(imgurl: str, proxy: bool = False) -> Any:
    """
    通过图片代理（使用代理服务器）
    """
    if not imgurl:
        return None
    if proxy:
        response = RequestUtils(ua=settings.USER_AGENT, proxies=settings.PROXY).get_res(url=imgurl)
    else:
        response = RequestUtils(ua=settings.USER_AGENT).get_res(url=imgurl)
    if response:
        return Response(content=response.content, media_type="image/jpeg")
    return None


@router.get("/env", summary="查询系统环境变量", response_model=schemas.Response)
def get_env_setting(_: User = Depends(get_current_active_superuser)):
    """
    查询系统环境变量，包括当前版本号
    """
    info = settings.dict(
        exclude={"SECRET_KEY", "SUPERUSER_PASSWORD"}
    )
    info.update({
        "VERSION": APP_VERSION,
        "AUTH_VERSION": SitesHelper().auth_version,
        "INDEXER_VERSION": SitesHelper().indexer_version,
        "FRONTEND_VERSION": SystemChain().get_frontend_version()
    })
    return schemas.Response(success=True,
                            data=info)


@router.post("/env", summary="更新系统环境变量", response_model=schemas.Response)
def set_env_setting(env: dict,
                    _: User = Depends(get_current_active_superuser)):
    """
    更新系统环境变量
    """
    for k, v in env.items():
        if k == "undefined":
            continue
        if hasattr(settings, k):
            if v == "None":
                v = None
            setattr(settings, k, v)
            if v is None:
                v = ''
            else:
                v = str(v)
            set_key(settings.CONFIG_PATH / "app.env", k, v)
    return schemas.Response(success=True)


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
            if global_vars.is_system_stopped():
                break
            detail = progress.get(process_type)
            yield 'data: %s\n\n' % json.dumps(detail)
            time.sleep(0.2)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@router.get("/setting/{key}", summary="查询系统设置", response_model=schemas.Response)
def get_setting(key: str,
                _: User = Depends(get_current_active_superuser)):
    """
    查询系统设置
    """
    if hasattr(settings, key):
        value = getattr(settings, key)
    else:
        value = SystemConfigOper().get(key)
    return schemas.Response(success=True, data={
        "value": value
    })


@router.post("/setting/{key}", summary="更新系统设置", response_model=schemas.Response)
def set_setting(key: str, value: Union[list, dict, bool, int, str] = None,
                _: User = Depends(get_current_active_superuser)):
    """
    更新系统设置
    """
    if hasattr(settings, key):
        if value == "None":
            value = None
        setattr(settings, key, value)
        if value is None:
            value = ''
        else:
            value = str(value)
        set_key(settings.CONFIG_PATH / "app.env", key, value)
    else:
        SystemConfigOper().set(key, value)
    return schemas.Response(success=True)


@router.get("/message", summary="实时消息")
def get_message(token: str, role: str = "system"):
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
            if global_vars.is_system_stopped():
                break
            detail = message.get(role)
            yield 'data: %s\n\n' % (detail or '')
            time.sleep(3)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@router.get("/logging", summary="实时日志")
def get_logging(token: str, length: int = 50, logfile: str = "moviepilot.log"):
    """
    实时获取系统日志
    length = -1 时, 返回text/plain
    否则 返回格式SSE
    """
    if not token or not verify_token(token):
        raise HTTPException(
            status_code=403,
            detail="认证失败！",
        )

    log_path = settings.LOG_PATH / logfile

    def log_generator():
        # 读取文件末尾50行，不使用tailer模块
        with open(log_path, 'r', encoding='utf-8') as f:
            for line in f.readlines()[-max(length, 50):]:
                yield 'data: %s\n\n' % line
        while True:
            if global_vars.is_system_stopped():
                break
            for t in tailer.follow(open(log_path, 'r', encoding='utf-8')):
                yield 'data: %s\n\n' % (t or '')
            time.sleep(1)

    # 根据length参数返回不同的响应
    if length == -1:
        # 返回全部日志作为文本响应
        if not log_path.exists():
            return Response(content="日志文件不存在！", media_type="text/plain")
        with open(log_path, 'r', encoding='utf-8') as file:
            text = file.read()
        # 倒序输出
        text = '\n'.join(text.split('\n')[::-1])
        return Response(content=text, media_type="text/plain")
    else:
        # 返回SSE流响应
        return StreamingResponse(log_generator(), media_type="text/event-stream")


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


@router.get("/modulelist", summary="查询已加载的模块ID列表", response_model=schemas.Response)
def modulelist(_: schemas.TokenPayload = Depends(verify_token)):
    """
    查询已加载的模块ID列表
    """
    modules = [{
        "id": k,
        "name": v.get_name(),
    } for k, v in ModuleManager().get_modules().items()]
    return schemas.Response(success=True, data={
        "modules": modules
    })


@router.get("/moduletest/{moduleid}", summary="模块可用性测试", response_model=schemas.Response)
def moduletest(moduleid: str, _: schemas.TokenPayload = Depends(verify_token)):
    """
    模块可用性测试接口
    """
    state, errmsg = ModuleManager().test(moduleid)
    return schemas.Response(success=state, message=errmsg)


@router.get("/restart", summary="重启系统", response_model=schemas.Response)
def restart_system(_: User = Depends(get_current_active_superuser)):
    """
    重启系统
    """
    if not SystemUtils.can_restart():
        return schemas.Response(success=False, message="当前运行环境不支持重启操作！")
    # 标识停止事件
    global_vars.stop_system()
    # 执行重启
    ret, msg = SystemUtils.restart()
    return schemas.Response(success=ret, message=msg)


@router.get("/reload", summary="重新加载模块", response_model=schemas.Response)
def reload_module(_: User = Depends(get_current_active_superuser)):
    """
    重新加载模块
    """
    ModuleManager().reload()
    Scheduler().init()
    return schemas.Response(success=True)


@router.get("/runscheduler", summary="运行服务", response_model=schemas.Response)
def execute_command(jobid: str,
                    _: User = Depends(get_current_active_superuser)):
    """
    执行命令
    """
    if not jobid:
        return schemas.Response(success=False, message="命令不能为空！")
    Scheduler().start(jobid)
    return schemas.Response(success=True)
