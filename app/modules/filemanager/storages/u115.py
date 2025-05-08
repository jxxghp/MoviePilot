import base64
import hashlib
import io
import json
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from queue import Queue, Empty
from typing import List, Dict, Optional, Tuple, Union

import oss2
import requests
from oss2.models import PartInfo
from tqdm import tqdm

from app import schemas
from app.core.config import settings
from app.log import logger
from app.modules.filemanager import StorageBase
from app.schemas.types import StorageSchema
from app.utils.singleton import Singleton
from app.utils.string import StringUtils

lock = threading.Lock()


class NoCheckInException(Exception):
    pass


class U115Pan(StorageBase, metaclass=Singleton):
    """
    115相关操作
    """

    # 存储类型
    schema = StorageSchema.U115

    # 支持的整理方式
    transtype = {
        "move": "移动",
        "copy": "复制"
    }

    # 验证参数
    _auth_state = {}

    # 上传进度值
    _last_progress = 0

    # 基础url
    base_url = "https://proapi.115.com"

    # CID和路径缓存
    _id_cache: Dict[str, str] = {}

    # 最大线程数
    MAX_WORKERS = 10
    # 最大分片大小(1GB)
    MAX_PART_SIZE = 1024 * 1024 * 1024

    def __init__(self):
        super().__init__()
        self.session = requests.Session()
        self._init_session()

    def _init_session(self):
        """
        初始化带速率限制的会话
        """
        self.session.headers.update({
            "User-Agent": "W115Storage/2.0",
            "Accept-Encoding": "gzip, deflate",
            "Content-Type": "application/x-www-form-urlencoded"
        })

    def _check_session(self):
        """
        检查会话是否过期
        """
        if not self.access_token:
            raise NoCheckInException("【115】请先扫码登录！")

    @property
    def access_token(self) -> Optional[str]:
        """
        访问token
        """
        with lock:
            tokens = self.get_conf()
            refresh_token = tokens.get("refresh_token")
            if not refresh_token:
                return None
            expires_in = tokens.get("expires_in", 0)
            refresh_time = tokens.get("refresh_time", 0)
            if expires_in and refresh_time + expires_in < int(time.time()):
                tokens = self.__refresh_access_token(refresh_token)
                if tokens:
                    self.set_config({
                        "refresh_time": int(time.time()),
                        **tokens
                    })
            access_token = tokens.get("access_token")
            if access_token:
                self.session.headers.update({"Authorization": f"Bearer {access_token}"})
            return access_token

    def generate_qrcode(self) -> Tuple[dict, str]:
        """
        实现PKCE规范的设备授权二维码生成
        """
        # 生成PKCE参数
        code_verifier = secrets.token_urlsafe(96)[:128]
        code_challenge = base64.b64encode(
            hashlib.sha256(code_verifier.encode("utf-8")).digest()
        ).decode("utf-8")
        # 请求设备码
        resp = self.session.post(
            "https://passportapi.115.com/open/authDeviceCode",
            data={
                "client_id": settings.U115_APP_ID,
                "code_challenge": code_challenge,
                "code_challenge_method": "sha256"
            }
        )
        if resp is None:
            return {}, "网络错误"
        result = resp.json()
        if result.get("code") != 0:
            return {}, result.get("message")
        # 持久化验证参数
        self._auth_state = {
            "code_verifier": code_verifier,
            "uid": result["data"]["uid"],
            "time": result["data"]["time"],
            "sign": result["data"]["sign"]
        }

        # 生成二维码内容
        return {
            "codeContent": result['data']['qrcode']
        }, ""

    def check_login(self) -> Optional[Tuple[dict, str]]:
        """
        改进的带PKCE校验的登录状态检查
        """
        if not self._auth_state:
            return {}, "生成二维码失败"
        try:
            resp = self.session.get(
                "https://qrcodeapi.115.com/get/status/",
                params={
                    "uid": self._auth_state["uid"],
                    "time": self._auth_state["time"],
                    "sign": self._auth_state["sign"]
                }
            )
            if resp is None:
                return {}, "网络错误"
            result = resp.json()
            if result.get("code") != 0 or not result.get("data"):
                return {}, result.get("message")
            if result["data"]["status"] == 2:
                tokens = self.__get_access_token()
                self.set_config({
                    "refresh_time": int(time.time()),
                    **tokens
                })
            return {"status": result["data"]["status"], "tip": result["data"]["msg"]}, ""
        except Exception as e:
            return {}, str(e)

    def __get_access_token(self) -> dict:
        """
        确认登录后，获取相关token
        """
        if not self._auth_state:
            raise Exception("【115】请先生成二维码")
        resp = self.session.post(
            "https://passportapi.115.com/open/deviceCodeToToken",
            data={
                "uid": self._auth_state["uid"],
                "code_verifier": self._auth_state["code_verifier"]
            }
        )
        if resp is None:
            raise Exception("获取 access_token 失败")
        result = resp.json()
        if result.get("code") != 0:
            raise Exception(result.get("message"))
        return result["data"]

    def __refresh_access_token(self, refresh_token: str) -> Optional[dict]:
        """
        刷新access_token
        """
        resp = self.session.post(
            "https://passportapi.115.com/open/refreshToken",
            data={
                "refresh_token": refresh_token
            }
        )
        if resp is None:
            logger.error(f"【115】刷新 access_token 失败：refresh_token={refresh_token}")
            return None
        result = resp.json()
        if result.get("code") != 0:
            logger.warn(f"【115】刷新 access_token 失败：{result.get('code')} - {result.get('message')}！")
        return result.get("data")

    def _request_api(self, method: str, endpoint: str,
                     result_key: Optional[str] = None, **kwargs) -> Optional[Union[dict, list]]:
        """
        带错误处理和速率限制的API请求
        """
        # 检查会话
        self._check_session()

        resp = self.session.request(
            method, f"{self.base_url}{endpoint}",
            **kwargs
        )
        if resp is None:
            logger.warn(f"【115】{method} 请求 {endpoint} 失败！")
            return None

        # 处理速率限制
        if resp.status_code == 429:
            reset_time = int(resp.headers.get("X-RateLimit-Reset", 60))
            time.sleep(reset_time + 5)
            return self._request_api(method, endpoint, result_key, **kwargs)

        # 处理请求错误
        resp.raise_for_status()

        # 返回数据
        ret_data = resp.json()
        if ret_data.get("code") != 0:
            logger.warn(f"【115】{method} 请求 {endpoint} 出错：{ret_data.get('message')}！")

        if result_key:
            return ret_data.get(result_key)
        return ret_data

    def _path_to_id(self, path: str) -> str:
        """
        路径转FID（带缓存机制）
        """
        # 根目录
        if path == "/":
            return '0'
        if len(path) > 1 and path.endswith("/"):
            path = path[:-1]
        # 检查缓存
        if path in self._id_cache:
            return self._id_cache[path]
        # 逐级查找缓存
        current_id = 0
        parent_path = "/"
        for p in Path(path).parents:
            if str(p) in self._id_cache:
                parent_path = str(p)
                current_id = self._id_cache[parent_path]
                break
        # 计算相对路径
        rel_path = Path(path).relative_to(parent_path)
        for part in Path(rel_path).parts:
            offset = 0
            find_part = False
            while True:
                resp = self._request_api(
                    "GET",
                    "/open/ufile/files",
                    "data",
                    params={"cid": current_id, "limit": 1000, "offset": offset, "cur": True, "show_dir": 1}
                )
                if not resp:
                    break
                for item in resp:
                    if item["fn"] == part:
                        current_id = item["fid"]
                        find_part = True
                        break
                if find_part:
                    break
                if len(resp) < 1000:
                    break
                offset += len(resp)
            if not find_part:
                raise FileNotFoundError(f"【115】{path} 不存在")
        if not current_id:
            raise FileNotFoundError(f"【115】{path} 不存在")
        # 缓存路径
        self._id_cache[path] = str(current_id)
        return str(current_id)

    @staticmethod
    def _calc_sha1(filepath: Path, size: Optional[int] = None) -> str:
        """
        计算文件SHA1（符合115规范）
        size: 前多少字节
        """
        sha1 = hashlib.sha1()
        with open(filepath, 'rb') as f:
            if size:
                chunk = f.read(size)
                sha1.update(chunk)
            else:
                while chunk := f.read(8192):
                    sha1.update(chunk)
        return sha1.hexdigest()

    def init_storage(self):
        pass

    def list(self, fileitem: schemas.FileItem) -> List[schemas.FileItem]:
        """
        目录遍历实现
        """

        if fileitem.type == "file":
            item = self.detail(fileitem)
            if item:
                return [item]
            return []
        if fileitem.path == "/":
            cid = '0'
        else:
            cid = fileitem.fileid
            if not cid:
                cid = self._path_to_id(fileitem.path)

        items = []
        offset = 0

        while True:
            resp = self._request_api(
                "GET",
                "/open/ufile/files",
                "data",
                params={"cid": int(cid), "limit": 1000, "offset": offset, "cur": True, "show_dir": 1}
            )
            if resp is None:
                raise FileNotFoundError(f"【115】{fileitem.path} 检索出错！")
            if not resp:
                break
            for item in resp:
                # 更新缓存
                path = f"{fileitem.path}{item['fn']}"
                self._id_cache[path] = str(item["fid"])

                file_path = path + ("/" if item["fc"] == "0" else "")
                items.append(schemas.FileItem(
                    storage=self.schema.value,
                    fileid=str(item["fid"]),
                    parent_fileid=cid,
                    name=item["fn"],
                    basename=Path(item["fn"]).stem,
                    extension=item["ico"] if item["fc"] == "1" else None,
                    type="dir" if item["fc"] == "0" else "file",
                    path=file_path,
                    size=item["fs"] if item["fc"] == "1" else None,
                    modify_time=item["upt"],
                    pickcode=item["pc"]
                ))

            if len(resp) < 1000:
                break
            offset += len(resp)

        return items

    def create_folder(self, parent_item: schemas.FileItem, name: str) -> Optional[schemas.FileItem]:
        """
        创建目录
        """
        new_path = Path(parent_item.path) / name
        resp = self._request_api(
            "POST",
            "/open/folder/add",
            data={
                "pid": int(parent_item.fileid),
                "file_name": name
            }
        )
        if not resp:
            return None
        if not resp.get("state"):
            if resp.get("code") == 20004:
                # 目录已存在
                return self.get_item(new_path)
            logger.warn(f"【115】创建目录失败: {resp.get('error')}")
            return None
        # 缓存新目录
        self._id_cache[str(new_path)] = str(resp["data"]["file_id"])
        return schemas.FileItem(
            storage=self.schema.value,
            fileid=str(resp["data"]["file_id"]),
            path=str(new_path) + "/",
            name=name,
            basename=name,
            type="dir",
            modify_time=int(time.time())
        )

    def _calc_parts(self, file_size: int) -> Tuple[int, int]:
        """
        计算最优分片大小和线程数
        """
        # 根据文件大小计算合适的分片数
        if file_size <= 100 * 1024 * 1024:  # 小于100MB
            return 10 * 1024 * 1024, min(3, self.MAX_WORKERS)  # 10MB分片
        elif file_size <= 1024 * 1024 * 1024:  # 小于1GB
            return 100 * 1024 * 1024, min(5, self.MAX_WORKERS)  # 100MB分片
        else:
            # 文件较大,使用较大分片
            part_size = min(file_size // self.MAX_WORKERS, self.MAX_PART_SIZE)
            return part_size, min(file_size // part_size + 1, self.MAX_WORKERS)

    @staticmethod
    def _upload_part(bucket: oss2.Bucket, object_name: str, upload_id: str,
                     part_number: int, part_data: bytes, progress_queue: Queue) -> PartInfo:
        """
        上传单个分片
        """
        try:
            result = bucket.upload_part(object_name, upload_id, part_number, part_data)
            part_info = PartInfo(part_number, result.etag)
            # 将上传进度放入队列
            progress_queue.put(len(part_data))
            logger.info(f"【115】分片 {part_number} 上传完成")
            return part_info
        except Exception as e:
            logger.error(f"【115】分片 {part_number} 上传失败: {str(e)}")
            raise

    @staticmethod
    def _log_progress(desc: str, total: int) -> tqdm:
        """
        创建一个可以输出到日志的进度条
        """

        class TqdmToLogger(io.StringIO):
            def write(s, buf): # noqa
                buf = buf.strip('\r\n\t ')
                if buf:
                    logger.info(buf)

        return tqdm(
            total=total,
            unit='B',
            unit_scale=True,
            desc=desc,
            file=TqdmToLogger(),
            mininterval=1.0,
            maxinterval=5.0,
            miniters=1
        )

    def upload(self, target_dir: schemas.FileItem, local_path: Path,
               new_name: Optional[str] = None) -> Optional[schemas.FileItem]:
        """
        实现带秒传、断点续传和多线程并发上传
        """

        def encode_callback(cb: dict):
            return oss2.utils.b64encode_as_string(json.dumps(cb).strip())

        target_name = new_name or local_path.name
        target_path = Path(target_dir.path) / target_name
        # 计算文件特征值
        file_size = local_path.stat().st_size
        file_sha1 = self._calc_sha1(local_path)
        file_preid = self._calc_sha1(local_path, 128 * 1024 * 1024)

        # 获取目标目录CID
        target_cid = target_dir.fileid
        target_param = f"U_1_{target_cid}"

        # Step 1: 初始化上传
        init_data = {
            "file_name": target_name,
            "file_size": file_size,
            "target": target_param,
            "fileid": file_sha1,
            "preid": file_preid
        }
        init_resp = self._request_api(
            "POST",
            "/open/upload/init",
            data=init_data
        )
        if not init_resp:
            return None
        if not init_resp.get("state"):
            logger.warn(f"【115】初始化上传失败: {init_resp.get('error')}")
            return None

        # 结果
        init_result = init_resp.get("data")
        logger.debug(f"【115】上传 Step 1 初始化结果: {init_result}")
        # 回调信息
        bucket_name = init_result.get("bucket")
        object_name = init_result.get("object")
        callback = init_result.get("callback")
        # 二次认证信息
        sign_check = init_result.get("sign_check")
        pick_code = init_result.get("pick_code")
        sign_key = init_result.get("sign_key")

        # Step 2: 处理二次认证
        if init_result.get("code") in [700, 701] and sign_check:
            sign_checks = sign_check.split("-")
            start = int(sign_checks[0])
            end = int(sign_checks[1])
            with open(local_path, "rb") as f:
                f.seek(start)
                chunk = f.read(end - start + 1)
                sign_val = hashlib.sha1(chunk).hexdigest().upper()
            init_data.update({
                "pick_code": pick_code,
                "sign_key": sign_key,
                "sign_val": sign_val
            })
            init_resp = self._request_api(
                "POST",
                "/open/upload/init",
                data=init_data
            )
            if not init_resp:
                return None
            init_result = init_resp.get("data")
            logger.debug(f"【115】上传 Step 2 二次认证结果: {init_result}")
            if not pick_code:
                pick_code = init_result.get("pick_code")
            if not bucket_name:
                bucket_name = init_result.get("bucket")
            if not object_name:
                object_name = init_result.get("object")
            if not callback:
                callback = init_result.get("callback")

        # Step 3: 秒传
        if init_result.get("status") == 2:
            logger.info(f"【115】{target_name} 秒传成功")
            return self.get_item(target_path)

        # Step 4: 获取上传凭证
        token_resp = self._request_api(
            "GET",
            "/open/upload/get_token",
            "data"
        )
        if not token_resp:
            logger.warn("【115】获取上传凭证失败")
            return None
        logger.debug(f"【115】上传 Step 4 获取上传凭证结果: {token_resp}")

        endpoint = token_resp.get("endpoint")
        AccessKeyId = token_resp.get("AccessKeyId")
        AccessKeySecret = token_resp.get("AccessKeySecret")
        SecurityToken = token_resp.get("SecurityToken")

        # Step 5: 断点续传
        resume_resp = self._request_api(
            "POST",
            "/open/upload/resume",
            "data",
            data={
                "file_size": file_size,
                "target": target_param,
                "fileid": file_sha1,
                "pick_code": pick_code
            }
        )
        if resume_resp:
            logger.debug(f"【115】上传 Step 5 断点续传结果: {resume_resp}")
            if resume_resp.get("callback"):
                callback = resume_resp["callback"]

        # Step 6: 多线程分片上传
        auth = oss2.StsAuth(
            access_key_id=AccessKeyId,
            access_key_secret=AccessKeySecret,
            security_token=SecurityToken
        )
        bucket = oss2.Bucket(auth, endpoint, bucket_name)  # noqa

        # 计算分片大小和线程数
        part_size, workers = self._calc_parts(file_size)
        logger.info(f"【115】开始上传: {local_path} -> {target_path}，"
                    f"分片大小：{StringUtils.str_filesize(part_size)}，线程数：{workers}")

        # 初始化进度条
        progress_bar = self._log_progress(f"【115】{target_name} 上传进度", file_size)

        # 初始化分片上传
        upload_id = bucket.init_multipart_upload(object_name,
                                                 params={
                                                     "encoding-type": "url",
                                                     "sequential": ""
                                                 }).upload_id

        # 创建进度队列
        progress_queue = Queue()

        # 创建线程池
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = []
            parts = []

            # 提交上传任务
            with open(local_path, 'rb') as fileobj:
                part_number = 1
                offset = 0
                while offset < file_size:
                    size = min(part_size, file_size - offset)
                    fileobj.seek(offset)
                    part_data = fileobj.read(size)
                    future = pool.submit(
                        self._upload_part,
                        bucket,
                        object_name,
                        upload_id,
                        part_number,
                        part_data,
                        progress_queue
                    )
                    futures.append(future)
                    offset += size
                    part_number += 1

            # 更新进度条
            while len(parts) < len(futures):
                try:
                    uploaded = progress_queue.get(timeout=1)
                    progress_bar.update(uploaded)
                except Empty:
                    pass

            # 等待所有任务完成
            for future in as_completed(futures):
                try:
                    part_info = future.result()
                    parts.append(part_info)
                except Exception as e:
                    logger.error(f"【115】分片上传失败: {str(e)}")
                    progress_bar.close()
                    return None

        # 按分片号排序
        parts.sort(key=lambda x: x.part_number)

        # 完成上传
        headers = {
            'X-oss-callback': encode_callback(callback["callback"]),
            'x-oss-callback-var': encode_callback(callback["callback_var"]),
            'x-oss-forbid-overwrite': 'false'
        }

        try:
            result = bucket.complete_multipart_upload(object_name, upload_id, parts,
                                                      headers=headers)
            if result.status == 200:
                logger.debug(f"【115】上传 Step 6 回调结果：{result.resp.response.json()}")
                logger.info(f"【115】{target_name} 上传成功")
            else:
                logger.warn(f"【115】{target_name} 上传失败，错误码: {result.status}")
                return None
        except oss2.exceptions.OssError as e:
            if e.code == "FileAlreadyExists":
                logger.warn(f"【115】{target_name} 已存在")
            else:
                logger.error(f"【115】{target_name} 上传失败: {e.status}, 错误码: {e.code}, 详情: {e.message}")
                return None
        finally:
            progress_bar.close()

        # 返回结果
        return self.get_item(target_path)

    def download(self, fileitem: schemas.FileItem, path: Path = None) -> Optional[Path]:
        """
        带限速处理的下载
        """
        detail = self.get_item(Path(fileitem.path))
        if not detail:
            return None
        download_info = self._request_api(
            "POST",
            "/open/ufile/downurl",
            "data",
            data={
                "pick_code": detail.pickcode
            }
        )
        if not download_info:
            return None
        download_url = list(download_info.values())[0].get("url", {}).get("url")
        local_path = path or settings.TEMP_PATH / fileitem.name
        with self.session.get(download_url, stream=True) as r:
            r.raise_for_status()
            with open(local_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)
        return local_path

    def check(self) -> bool:
        return self.access_token is not None

    def delete(self, fileitem: schemas.FileItem) -> bool:
        """
        删除文件/目录
        """
        try:
            self._request_api(
                "POST",
                "/open/ufile/delete",
                data={
                    "file_ids": int(fileitem.fileid)
                }
            )
            return True
        except requests.exceptions.HTTPError:
            return False

    def rename(self, fileitem: schemas.FileItem, name: str) -> bool:
        """
        重命名文件/目录
        """
        resp = self._request_api(
            "POST",
            "/open/ufile/update",
            data={
                "file_id": int(fileitem.fileid),
                "file_name": name
            }
        )
        if not resp:
            return False
        if resp["state"]:
            if fileitem.path in self._id_cache:
                del self._id_cache[fileitem.path]
                for key in list(self._id_cache.keys()):
                    if key.startswith(fileitem.path):
                        del self._id_cache[key]
            new_path = Path(fileitem.path).parent / name
            self._id_cache[str(new_path)] = fileitem.fileid
            return True
        return False

    def get_item(self, path: Path) -> Optional[schemas.FileItem]:
        """
        获取指定路径的文件/目录项
        """
        try:
            file_id = self._path_to_id(str(path))
            if not file_id:
                return None
            resp = self._request_api(
                "GET",
                "/open/folder/get_info",
                "data",
                params={
                    "file_id": int(file_id)
                }
            )
            if not resp:
                return None
            return schemas.FileItem(
                storage=self.schema.value,
                fileid=str(resp["file_id"]),
                path=str(path) + ("/" if resp["file_category"] == "0" else ""),
                type="file" if resp["file_category"] == "1" else "dir",
                name=resp["file_name"],
                basename=Path(resp["file_name"]).stem,
                extension=Path(resp["file_name"]).suffix[1:] if resp["file_category"] == "1" else None,
                pickcode=resp["pick_code"],
                size=StringUtils.num_filesize(resp['size']) if resp["file_category"] == "1" else None,
                modify_time=resp["utime"]
            )
        except Exception as e:
            logger.debug(f"【115】获取文件信息失败: {str(e)}")
            return None

    def get_folder(self, path: Path) -> Optional[schemas.FileItem]:
        """
        获取指定路径的文件夹，如不存在则创建
        """

        def __find_dir(_fileitem: schemas.FileItem, _name: str) -> Optional[schemas.FileItem]:
            """
            查找下级目录中匹配名称的目录
            """
            for sub_folder in self.list(_fileitem):
                if sub_folder.type != "dir":
                    continue
                if sub_folder.name == _name:
                    return sub_folder
            return None

        # 是否已存在
        folder = self.get_item(path)
        if folder:
            return folder
        # 逐级查找和创建目录
        fileitem = schemas.FileItem(storage=self.schema.value, path="/")
        for part in path.parts[1:]:
            dir_file = __find_dir(fileitem, part)
            if dir_file:
                fileitem = dir_file
            else:
                dir_file = self.create_folder(fileitem, part)
                if not dir_file:
                    logger.warn(f"【115】创建目录 {fileitem.path}{part} 失败！")
                    return None
                fileitem = dir_file
        return fileitem

    def detail(self, fileitem: schemas.FileItem) -> Optional[schemas.FileItem]:
        """
        获取文件/目录详细信息
        """
        return self.get_item(Path(fileitem.path))

    def copy(self, fileitem: schemas.FileItem, path: Path, new_name: str) -> bool:
        """
        企业级复制实现（支持目录递归复制）
        """
        src_fid = fileitem.fileid
        dest_cid = self._path_to_id(str(path))

        resp = self._request_api(
            "POST",
            "/open/ufile/copy",
            data={
                "file_id": int(src_fid),
                "pid": int(dest_cid)
            }
        )
        if not resp:
            return False
        if resp["state"]:
            new_path = Path(path) / fileitem.name
            new_item = self.get_item(new_path)
            self.rename(new_item, new_name)
            # 更新缓存
            del self._id_cache[fileitem.path]
            rename_new_path = Path(path) / new_name
            self._id_cache[str(rename_new_path)] = new_item.fileid
            return True
        return False

    def move(self, fileitem: schemas.FileItem, path: Path, new_name: str) -> bool:
        """
        原子性移动操作实现
        """
        src_fid = fileitem.fileid
        dest_cid = self._path_to_id(str(path))

        resp = self._request_api(
            "POST",
            "/open/ufile/move",
            data={
                "file_ids": int(src_fid),
                "to_cid": int(dest_cid)
            }
        )
        if not resp:
            return False
        if resp["state"]:
            new_path = Path(path) / fileitem.name
            new_file = self.get_item(new_path)
            self.rename(new_file, new_name)
            # 更新缓存
            del self._id_cache[fileitem.path]
            rename_new_path = Path(path) / new_name
            self._id_cache[str(rename_new_path)] = src_fid
            return True
        return False

    def link(self, fileitem: schemas.FileItem, target_file: Path) -> bool:
        pass

    def softlink(self, fileitem: schemas.FileItem, target_file: Path) -> bool:
        pass

    def usage(self) -> Optional[schemas.StorageUsage]:
        """
        获取带有企业级配额信息的存储使用情况
        """
        try:
            resp = self._request_api(
                "GET",
                "/open/user/info",
                "data"
            )
            if not resp:
                return None
            space = resp["rt_space_info"]
            return schemas.StorageUsage(
                total=space["all_total"]["size"],
                available=space["all_remain"]["size"]
            )
        except NoCheckInException:
            return None
