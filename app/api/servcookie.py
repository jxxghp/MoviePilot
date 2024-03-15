import gzip
import json
import os
from typing import Annotated, Any, Callable, Dict

from fastapi import APIRouter, Depends, HTTPException, Path, Request, Response
from fastapi.responses import PlainTextResponse
from fastapi.routing import APIRoute

from app import schemas
from app.core.config import settings
from app.utils.common import get_decrypted_cookie_data


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
    return "Hello World! API ROOT = /cookiecloud"


@cookie_router.post("/", response_class=PlainTextResponse)
def post_root():
    return "Hello World! API ROOT = /cookiecloud"


@cookie_router.post("/update")
async def update_cookie(req: schemas.CookieData):
    file_path = os.path.join(settings.COOKIE_PATH,
                             os.path.basename(req.uuid) + ".json")
    content = json.dumps({"encrypted": req.encrypted})
    with open(file_path, encoding="utf-8", mode="w") as file:
        file.write(content)
    read_content = None
    with open(file_path, encoding="utf-8", mode="r") as file:
        read_content = file.read()
    if (read_content == content):
        return {"action": "done"}
    else:
        return {"action": "error"}


def load_encrypt_data(uuid: str) -> Dict[str, Any]:
    file_path = os.path.join(settings.COOKIE_PATH,
                             os.path.basename(uuid) + ".json")

    # 检查文件是否存在
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="Item not found")

    # 读取文件
    with open(file_path, encoding="utf-8", mode="r") as file:
        read_content = file.read()
    data = json.loads(read_content)
    return data


@cookie_router.get("/get/{uuid}")
async def get_cookie(
        uuid: Annotated[str, Path(min_length=5, pattern="^[a-zA-Z0-9]+$")]):
    return load_encrypt_data(uuid)


@cookie_router.post("/get/{uuid}")
async def post_cookie(
        uuid: Annotated[str, Path(min_length=5, pattern="^[a-zA-Z0-9]+$")],
        request: schemas.CookiePassword):
    data = load_encrypt_data(uuid)
    return get_decrypted_cookie_data(uuid, request.password, data["encrypted"])
