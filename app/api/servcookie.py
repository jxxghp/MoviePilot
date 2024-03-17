import gzip
import json
from hashlib import md5
from typing import Annotated, Callable
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Request, Response
from fastapi.responses import PlainTextResponse
from fastapi.routing import APIRoute

from app import schemas
from app.core.config import settings
from app.log import logger
from app.utils.common import decrypt


class GzipRequest(Request):

    async def body(self) -> bytes:
        if not hasattr(self, "_body"):
            body = await super().body()
            if "gzip" in self.headers.getlist("Content-Encoding"):
                body = gzip.decompress(body)
            self._body = body
        return self._body


class GzipRoute(APIRoute):

    def get_route_handler(self) -> Callable:
        original_route_handler = super().get_route_handler()

        async def custom_route_handler(request: Request) -> Response:
            request = GzipRequest(request.scope, request.receive)
            return await original_route_handler(request)

        return custom_route_handler


async def verify_server_enabled():
    """
    校验CookieCloud服务路由是否打开
    """
    if not settings.COOKIECLOUD_ENABLE_LOCAL:
        raise HTTPException(status_code=400, detail="本地CookieCloud服务器未启用")
    return True


cookie_router = APIRouter(route_class=GzipRoute,
                          tags=['servcookie'],
                          dependencies=[Depends(verify_server_enabled)])


@cookie_router.get("/", response_class=PlainTextResponse)
def get_root():
    return "Hello MoviePilot! COOKIECLOUD API ROOT = /cookiecloud"


@cookie_router.post("/", response_class=PlainTextResponse)
def post_root():
    return "Hello MoviePilot! COOKIECLOUD API ROOT = /cookiecloud"


@cookie_router.post("/update")
async def update_cookie(req: schemas.CookieData):
    """
    上传Cookie数据
    """
    file_path = settings.COOKIE_PATH / f"{req.uuid}.json"
    content = json.dumps({"encrypted": req.encrypted})
    with open(file_path, encoding="utf-8", mode="w") as file:
        file.write(content)
    with open(file_path, encoding="utf-8", mode="r") as file:
        read_content = file.read()
    if read_content == content:
        return {"action": "done"}
    else:
        return {"action": "error"}


def load_encrypt_data(uuid: str) -> Dict[str, Any]:
    """
    加载本地加密原始数据
    """
    file_path = settings.COOKIE_PATH / f"{uuid}.json"

    # 检查文件是否存在
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Item not found")

    # 读取文件
    with open(file_path, encoding="utf-8", mode="r") as file:
        read_content = file.read()
    data = json.loads(read_content.encode("utf-8"))
    return data


def get_decrypted_cookie_data(uuid: str, password: str,
                              encrypted: str) -> Optional[Dict[str, Any]]:
    """
    加载本地加密数据并解密为Cookie
    """
    key_md5 = md5()
    key_md5.update((uuid + '-' + password).encode('utf-8'))
    aes_key = (key_md5.hexdigest()[:16]).encode('utf-8')

    if encrypted:
        try:
            decrypted_data = decrypt(encrypted, aes_key).decode('utf-8')
            decrypted_data = json.loads(decrypted_data)
            if 'cookie_data' in decrypted_data:
                return decrypted_data
        except Exception as e:
            logger.error(f"解密Cookie数据失败：{str(e)}")
            return None
    else:
        return None


@cookie_router.get("/get/{uuid}")
async def get_cookie(
        uuid: Annotated[str, Path(min_length=5, pattern="^[a-zA-Z0-9]+$")]):
    """
    GET 下载加密数据
    """
    return load_encrypt_data(uuid)


@cookie_router.post("/get/{uuid}")
async def post_cookie(
        uuid: Annotated[str, Path(min_length=5, pattern="^[a-zA-Z0-9]+$")],
        request: schemas.CookiePassword):
    """
    POST 下载加密数据
    """
    data = load_encrypt_data(uuid)
    return get_decrypted_cookie_data(uuid, request.password, data["encrypted"])
