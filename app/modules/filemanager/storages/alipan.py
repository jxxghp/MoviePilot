import base64
import hashlib
import json
import time
import uuid
from pathlib import Path
from typing import Optional, Tuple, List

from requests import Response

from app import schemas
from app.core.config import settings
from app.log import logger
from app.modules.filemanager.storages import StorageBase
from app.schemas.types import StorageSchema
from app.utils.http import RequestUtils
from app.utils.string import StringUtils
from app.utils.system import SystemUtils


class AliPan(StorageBase):
    """
    阿里云相关操作
    """

    # 存储类型
    schema = StorageSchema.Alipan

    # 支持的整理方式
    transtype = {
        "move": "移动"
    }

    _X_SIGNATURE = ('f4b7bed5d8524a04051bd2da876dd79afe922b8205226d65855d02b267422adb1'
                    'e0d8a816b021eaf5c36d101892180f79df655c5712b348c2a540ca136e6b22001')

    _X_PUBLIC_KEY = ('04d9d2319e0480c840efeeb75751b86d0db0c5b9e72c6260a1d846958adceaf9d'
                     'ee789cab7472741d23aafc1a9c591f72e7ee77578656e6c8588098dea1488ac2a')

    # 生成二维码
    qrcode_url = ("https://passport.aliyundrive.com/newlogin/qrcode/generate.do?"
                  "appName=aliyun_drive&fromSite=52&appEntrance=web&isMobile=false"
                  "&lang=zh_CN&returnUrl=&bizParams=&_bx-v=2.0.31")
    # 二维码登录确认
    check_url = "https://passport.aliyundrive.com/newlogin/qrcode/query.do?appName=aliyun_drive&fromSite=52&_bx-v=2.0.31"
    # 更新访问令牌
    update_accessstoken_url = "https://auth.aliyundrive.com/v2/account/token"
    # 创建会话
    create_session_url = "https://api.aliyundrive.com/users/v1/users/device/create_session"
    # 用户信息
    user_info_url = "https://user.aliyundrive.com/v2/user/get"
    # 浏览文件
    list_file_url = "https://api.aliyundrive.com/adrive/v3/file/list"
    # 创建目录或文件
    create_folder_file_url = "https://api.aliyundrive.com/adrive/v2/file/createWithFolders"
    # 文件详情
    file_detail_url = "https://api.aliyundrive.com/v2/file/get"
    # 删除文件
    delete_file_url = " https://api.aliyundrive.com/v2/recyclebin/trash"
    # 文件重命名
    rename_file_url = "https://api.aliyundrive.com/v3/file/update"
    # 获取下载链接
    download_url = "https://api.aliyundrive.com/v2/file/get_download_url"
    # 移动文件
    move_file_url = "https://api.aliyundrive.com/v2/file/move"
    # 上传文件完成
    upload_file_complete_url = "https://api.aliyundrive.com/v2/file/complete"
    # 查询存储详情
    storage_info_url = "https://api.aliyundrive.com/adrive/v1/user/driveCapacityDetails"
    # 播放地址
    play_info_url = 'https://api.aliyundrive.com/v2/file/get_video_preview_play_info'

    def __handle_error(self, res: Response, apiname: str, action: bool = True):
        """
        统一处理和打印错误信息
        """
        if res is None:
            logger.warn("无法连接到阿里云盘！")
            return
        try:
            result = res.json()
        except Exception as err:
            logger.error(f"解析阿里云盘返回数据失败：{str(err)}")
            return
        code = result.get("code")
        message = result.get("message")
        display_message = result.get("display_message")
        if code or message:
            logger.warn(f"Aliyun {apiname}失败：{code} - {display_message or message}")
            if action:
                if code == "DeviceSessionSignatureInvalid":
                    logger.warn("设备已失效，正在重新建立会话...")
                    self.__create_session(self.__get_headers(self.__auth_params))
                if code == "UserDeviceOffline":
                    logger.warn("设备已离线，尝试重新登录，如仍报错请检查阿里云盘绑定设备数量是否超限！")
                    self.__create_session(self.__get_headers(self.__auth_params))
                if code == "AccessTokenInvalid":
                    logger.warn("访问令牌已失效，正在刷新令牌...")
                    self.__update_accesstoken(self.__auth_params, self.__auth_params.get("refreshToken"))
        else:
            logger.info(f"Aliyun {apiname}成功")

    @property
    def __auth_params(self):
        """
        获取阿里云盘认证参数并初始化参数格式
        """
        conf = self.get_config()
        return conf.config if conf else {}

    def __update_params(self, params: dict):
        """
        设置阿里云盘认证参数
        """
        current_params = self.__auth_params
        current_params.update(params)
        self.set_config(current_params)

    def __clear_params(self):
        """
        清除阿里云盘认证参数
        """
        self.set_config({})

    def generate_qrcode(self) -> Optional[Tuple[dict, str]]:
        """
        生成二维码
        """
        res = RequestUtils(timeout=10).get_res(self.qrcode_url)
        if res:
            data = res.json().get("content", {}).get("data")
            return {
                "codeContent": data.get("codeContent"),
                "ck": data.get("ck"),
                "t": data.get("t")
            }, ""
        elif res is not None:
            self.__handle_error(res, "生成二维码")
            return {}, f"请求阿里云盘二维码失败：{res.status_code} - {res.reason}"
        return {}, f"请求阿里云盘二维码失败：无法连接！"

    def check_login(self, ck: str, t: str) -> Optional[Tuple[dict, str]]:
        """
        二维码登录确认
        """
        params = {
            "t": t,
            "ck": ck,
            "appName": "aliyun_drive",
            "appEntrance": "web",
            "isMobile": "false",
            "lang": "zh_CN",
            "returnUrl": "",
            "fromSite": "52",
            "bizParams": "",
            "navlanguage": "zh-CN",
            "navPlatform": "MacIntel",
        }

        body = "&".join([f"{key}={value}" for key, value in params.items()])

        status = {
            "NEW": "请用阿里云盘 App 扫码",
            "SCANED": "请在手机上确认",
            "EXPIRED": "二维码已过期",
            "CANCELED": "已取消",
            "CONFIRMED": "已确认",
        }

        headers = {
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        }

        res = RequestUtils(headers=headers, timeout=5).post_res(self.check_url, data=body)
        if res:
            data = res.json().get("content", {}).get("data") or {}
            qrCodeStatus = data.get("qrCodeStatus")
            data["tip"] = status.get(qrCodeStatus) or "未知"
            if data.get("bizExt"):
                try:
                    bizExt = json.loads(base64.b64decode(data["bizExt"]).decode('GBK'))
                    pds_login_result = bizExt.get("pds_login_result")
                    if pds_login_result:
                        data.pop('bizExt')
                        data.update({
                            'userId': pds_login_result.get('userId'),
                            'expiresIn': pds_login_result.get('expiresIn'),
                            'nickName': pds_login_result.get('nickName'),
                            'avatar': pds_login_result.get('avatar'),
                            'tokenType': pds_login_result.get('tokenType'),
                            "refreshToken": pds_login_result.get('refreshToken'),
                            "accessToken": pds_login_result.get('accessToken'),
                            "defaultDriveId": pds_login_result.get('defaultDriveId'),
                            "updateTime": time.time(),
                        })
                        self.__update_params(data)
                        self.user_info()
                except Exception as e:
                    return {}, f"bizExt 解码失败：{str(e)}"
            return data, ""
        elif res is not None:
            self.__handle_error(res, "登录确认")
            return {}, f"阿里云盘登录确认失败：{res.status_code} - {res.reason}"
        return {}, "阿里云盘登录确认失败：无法连接！"

    def __update_accesstoken(self, params: dict, refresh_token: str) -> bool:
        """
        更新阿里云盘访问令牌
        """
        headers = self.__get_headers(params)
        res = RequestUtils(headers=headers, timeout=10).post_res(
            self.update_accessstoken_url, json={
                "refresh_token": refresh_token,
                "grant_type": "refresh_token"
            })
        if res:
            data = res.json()
            code = data.get("code")
            if code in ["RefreshTokenExpired", "InvalidParameter.RefreshToken"]:
                logger.warn("刷新令牌已过期，请重新登录！")
                self.__clear_params()
                return False
            self.__update_params({
                "accessToken": data.get('access_token'),
                "expiresIn": data.get('expires_in'),
                "updateTime": time.time()
            })
            logger.info(f"阿里云盘访问令牌已更新，accessToken={data.get('access_token')}")
            return True
        else:
            self.__handle_error(res, "更新令牌", action=False)
        return False

    def __create_session(self, headers: dict):
        """
        创建会话
        """

        def __os_name():
            """
            获取操作系统名称
            """
            if SystemUtils.is_windows():
                return 'Windows 操作系统'
            elif SystemUtils.is_macos():
                return 'MacOS 操作系统'
            else:
                return '类 Unix 操作系统'

        res = RequestUtils(headers=headers, timeout=5).post_res(self.create_session_url, json={
            'deviceName': 'MoviePilot',
            'modelName': __os_name(),
            'pubKey': self._X_PUBLIC_KEY,
        })
        self.__handle_error(res, "创建会话", action=False)

    @property
    def __access_params(self) -> Optional[dict]:
        """
        获取阿里云盘访问参数，如果超时则更新后返回
        """
        params = self.__auth_params
        if not params:
            logger.warn("阿里云盘访问令牌不存在，请先扫码登录！")
            return None
        expires_in = params.get("expiresIn")
        update_time = params.get("updateTime")
        refresh_token = params.get("refreshToken")
        if not expires_in or not update_time or not refresh_token:
            logger.warn("阿里云盘访问令牌参数错误，请重新扫码登录！")
            self.__clear_params()
            return None
        # 是否需要更新设备信息
        update_device = False
        # 判断访问令牌是否过期
        if (time.time() - update_time) >= expires_in:
            logger.info("阿里云盘访问令牌已过期，正在更新...")
            if not self.__update_accesstoken(params, refresh_token):
                # 更新失败
                return None
            update_device = True
        # 生成设备ID
        x_device_id = params.get("x_device_id")
        if not x_device_id:
            x_device_id = uuid.uuid4().hex
            params['x_device_id'] = x_device_id
            self.__update_params({"x_device_id": x_device_id})
            update_device = True
        # 更新设备信息重新创建会话
        if update_device:
            self.__create_session(self.__get_headers(params))
        return params

    def __get_headers(self, params: dict):
        """
        获取请求头
        """
        if not params:
            return {}
        return {
            "Authorization": f"Bearer {params.get('accessToken')}",
            "Content-Type": "application/json;charset=UTF-8",
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://www.alipan.com/",
            "User-Agent": settings.USER_AGENT,
            "X-Canary": "client=web,app=adrive,version=v4.9.0",
            "x-device-id": params.get('x_device_id'),
            "x-signature": self._X_SIGNATURE
        }

    def check(self) -> bool:
        """
        检查存储是否可用
        """
        params = self.__access_params
        if not params:
            return False
        return True if self.list(schemas.FileItem(
            fileid="root",
            drive_id=params.get("resourceDriveId")
        )) else False

    def user_info(self) -> dict:
        """
        获取用户信息（drive_id等）
        """
        params = self.__access_params
        if not params:
            return {}
        headers = self.__get_headers(params)
        res = RequestUtils(headers=headers, timeout=10).post_res(self.user_info_url)
        if res:
            result = res.json()
            self.__update_params({
                "resourceDriveId": result.get("resource_drive_id"),
                "backDriveId": result.get("backup_drive_id")
            })
            return result
        else:
            self.__handle_error(res, "获取用户信息")
        return {}

    def list(self, fileitem: schemas.FileItem = None) -> List[schemas.FileItem]:
        """
        浏览文件
        limit 返回文件数量，默认 50，最大 100
        order_by created_at/updated_at/name/size
        parent_file_id 根目录为root
        type 	all | file | folder
        """
        params = self.__access_params
        if not params:
            return []
        # 请求头
        headers = self.__get_headers(params)
        # 根目录处理
        if not fileitem or not fileitem.drive_id:
            return [
                schemas.FileItem(
                    storage=self.schema.value,
                    fileid=fileitem.fileid,
                    drive_id=params.get("resourceDriveId"),
                    parent_fileid="root",
                    type="dir",
                    path="/资源库/",
                    name="资源库",
                    basename="资源库"
                ),
                schemas.FileItem(
                    storage=self.schema.value,
                    fileid=fileitem.fileid,
                    drive_id=params.get("backDriveId"),
                    parent_fileid="root",
                    type="dir",
                    path="/备份盘/",
                    name="备份盘",
                    basename="备份盘"
                )
            ]
        # 如果本身是文件
        if fileitem.type == "file":
            return [fileitem]
        # 返回数据
        ret_items = []
        # 分页获取
        next_marker = None
        while True:
            if not fileitem.parent_fileid or fileitem.parent_fileid == "/":
                parent_file_id = "root"
            else:
                parent_file_id = fileitem.fileid
            res = RequestUtils(headers=headers, timeout=10).post_res(self.list_file_url, json={
                "drive_id": fileitem.drive_id,
                "parent_file_id": parent_file_id,
                "marker": next_marker
            }, params={
                'jsonmask': ('next_marker,items(name,file_id,drive_id,type,size,created_at,updated_at,'
                             'category,file_extension,parent_file_id,mime_type,starred,thumbnail,url,'
                             'streams_info,content_hash,user_tags,user_meta,trashed,video_media_metadata,'
                             'video_preview_metadata,sync_meta,sync_device_flag,sync_flag,punish_flag')
            })
            if res:
                result = res.json()
                items = result.get("items")
                if not items:
                    break
                # 合并数据
                ret_items.extend(items)
                next_marker = result.get("next_marker")
                if not next_marker:
                    # 没有下一页
                    break
            else:
                self.__handle_error(res, "浏览文件")
                break
        return [schemas.FileItem(
            storage=self.schema.value,
            fileid=fileinfo.get("file_id"),
            parent_fileid=fileinfo.get("parent_file_id"),
            type="dir" if fileinfo.get("type") == "folder" else "file",
            path=f"{fileitem.path}{fileinfo.get('name')}" + ("/" if fileinfo.get("type") == "folder" else ""),
            name=fileinfo.get("name"),
            basename=Path(fileinfo.get("name")).stem,
            size=fileinfo.get("size"),
            extension=fileinfo.get("file_extension"),
            modify_time=StringUtils.str_to_timestamp(fileinfo.get("updated_at")),
            thumbnail=fileinfo.get("thumbnail"),
            drive_id=fileinfo.get("drive_id"),
        ) for fileinfo in ret_items]

    def create_folder(self, fileitem: schemas.FileItem, name: str) -> Optional[schemas.FileItem]:
        """
        创建目录
        """
        params = self.__access_params
        if not params:
            return None
        headers = self.__get_headers(params)
        res = RequestUtils(headers=headers, timeout=10).post_res(self.create_folder_file_url, json={
            "drive_id": fileitem.drive_id,
            "parent_file_id": fileitem.parent_fileid,
            "name": name,
            "check_name_mode": "refuse",
            "type": "folder"
        })
        if res:
            """
            {
                "parent_file_id": "root",
                "type": "folder",
                "file_id": "6673f2c8a88344741bd64ad192d7512b92087719",
                "domain_id": "bj29",
                "drive_id": "39146740",
                "file_name": "test",
                "encrypt_mode": "none"
            }
            """
            result = res.json()
            return schemas.FileItem(
                storage=self.schema.value,
                fileid=result.get("file_id"),
                drive_id=result.get("drive_id"),
                parent_fileid=result.get("parent_file_id"),
                type=result.get("type"),
                name=result.get("file_name"),
                path=f"{fileitem.path}{result.get('file_name')}",
            )
        else:
            self.__handle_error(res, "创建目录")
        return None

    def get_folder(self, path: Path) -> Optional[schemas.FileItem]:
        """
        根据文件路程获取目录，不存在则创建
        """

        def __find_dir(_fileitem: schemas.FileItem, _name: str) -> Optional[schemas.FileItem]:
            """
            查找下级目录中匹配名称的目录
            """
            for sub_file in self.list(_fileitem):
                if sub_file.type != "dir":
                    continue
                if sub_file.name == _name:
                    return sub_file
            return None

        # 逐级查找和创建目录
        fileitem = schemas.FileItem(fileid="root")
        for part in path.parts:
            if part == "/":
                continue
            dir_file = __find_dir(fileitem, part)
            if dir_file:
                fileitem = dir_file
            else:
                dir_file = self.create_folder(dir_file, part)
                if not dir_file:
                    logger.warn(f"{self.schema.value}创建目录 {fileitem.path}{part} 失败！")
                    return None
                fileitem = dir_file
        return fileitem

    def get_item(self, path: Path) -> Optional[schemas.FileItem]:
        """
        获取文件或目录，不存在返回None
        """

        def __find_item(_fileitem: schemas.FileItem, _name: str) -> Optional[schemas.FileItem]:
            """
            查找下级目录中匹配名称的目录或文件
            """
            for sub_file in self.list(_fileitem):
                if sub_file.name == _name:
                    return sub_file
            return None

        # 逐级查找和创建目录
        fileitem = schemas.FileItem(fileid="root")
        for part in path.parts:
            if part == "/":
                continue
            item = __find_item(fileitem, part)
            if not item:
                return None
            fileitem = item
        return fileitem

    def delete(self, fileitem: schemas.FileItem) -> bool:
        """
        删除文件
        """
        params = self.__access_params
        if not params:
            return False
        headers = self.__get_headers(params)
        res = RequestUtils(headers=headers, timeout=10).post_res(self.delete_file_url, json={
            "drive_id": fileitem.drive_id,
            "file_id": fileitem.fileid
        })
        if res:
            return True
        else:
            self.__handle_error(res, "删除文件")
        return False

    def detail(self, fileitem: schemas.FileItem) -> Optional[schemas.FileItem]:
        """
        获取文件详情
        """
        params = self.__access_params
        if not params:
            return None
        headers = self.__get_headers(params)
        res = RequestUtils(headers=headers, timeout=10).post_res(self.file_detail_url, json={
            "drive_id": fileitem.drive_id,
            "file_id": fileitem.fileid
        })
        if res:
            result = res.json()
            return schemas.FileItem(
                storage=self.schema.value,
                fileid=result.get("file_id"),
                drive_id=result.get("drive_id"),
                parent_fileid=result.get("parent_file_id"),
                type="file",
                name=result.get("name"),
                size=result.get("size"),
                extension=result.get("file_extension"),
                modify_time=StringUtils.str_to_timestamp(result.get("updated_at")),
                thumbnail=result.get("thumbnail"),
                path=f"{fileitem.path}{result.get('name')}",
                url=result.get("download_url") or result.get("url")
            )
        else:
            self.__handle_error(res, "获取文件详情")
        return None

    def rename(self, fileitem: schemas.FileItem, name: str) -> bool:
        """
        重命名文件
        """
        params = self.__access_params
        if not params:
            return False
        headers = self.__get_headers(params)
        res = RequestUtils(headers=headers, timeout=10).post_res(self.rename_file_url, json={
            "drive_id": fileitem.drive_id,
            "file_id": fileitem.fileid,
            "name": name,
            "check_name_mode": "refuse"
        })
        if res:
            return True
        else:
            self.__handle_error(res, "重命名文件")
        return False

    def download(self, fileitem: schemas.FileItem) -> Optional[Path]:
        """
        下载文件，保存到本地
        """
        params = self.__access_params
        if not params:
            return None
        headers = self.__get_headers(params)

        def __get_play_url():
            """
            获取播放地址
            """
            play_res = RequestUtils(headers=headers, timeout=10).post_res(self.play_info_url, json={
                "drive_id": fileitem.drive_id,
                "file_id": fileitem.fileid
            })
            if play_res:
                play_dict = {}
                play_info = play_res.json()
                if play_info.get('video_preview_play_info'):
                    for i in play_info['video_preview_play_info'].get('live_transcoding_task_list') or []:
                        if i.get('url'):
                            try:
                                play_dict[i['template_id']] = i['url']
                            except KeyError:
                                pass
                if play_dict:
                    return list(play_dict.values())[-1]
            return None

        # 先获取文件详情
        fileinfo = self.detail(fileitem)
        if not fileinfo:
            logger.warn(f"{fileitem.path} 文件不存在")
            return None

        # 文件下载链接
        download_url = None
        if fileinfo.url:
            # 使用文件详情中的链接
            download_url = fileinfo.url
        else:
            # 查询文件下载链接
            res = RequestUtils(headers=headers, timeout=10).post_res(self.download_url, json={
                "drive_id": fileitem.drive_id,
                "file_id": fileitem.fileid,
                "file_name": fileitem.name,
            })
            if res:
                result = res.json()
                download_url = result.get("url") or result.get("internal_url")

        if not download_url:
            # 查询播放链接
            download_url = __get_play_url()

        if not download_url:
            logger.warn(f"{fileitem.path} 未获取到下载链接")
            return None
        # 下载文件到本地
        res = RequestUtils(headers={
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": "https://www.alipan.com/",
            "Sec-Fetch-Dest": "iframe",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "cross-site",
            "User-Agent": settings.USER_AGENT
        }).get_res(download_url)
        if res:
            path = settings.TEMP_PATH / fileitem.name
            with path.open("wb") as f:
                f.write(res.content)
            return path
        else:
            self.__handle_error(res, "获取下载链接")
        return None

    def upload(self, fileitem: schemas.FileItem, path: Path) -> Optional[schemas.FileItem]:
        """
        上传文件，并标记完成
        """

        __UPLOAD_CHUNK_SIZE: int = 10485760  # 10 MB

        def __sha1(_path: Path):
            """
            计算文件sha1，用于快传
            """
            _sha1 = hashlib.sha1()
            with open(_path, 'rb') as f:
                while True:
                    data = f.read(8192)
                    if not data:
                        break
                    _sha1.update(data)
            return _sha1.hexdigest()

        params = self.__access_params
        if not params:
            return None
        headers = self.__get_headers(params)

        # 计算sha1
        sha1 = __sha1(path)
        res = RequestUtils(headers=headers, timeout=10).post_res(self.create_folder_file_url, json={
            "drive_id": fileitem.drive_id,
            "parent_file_id": fileitem.parent_fileid,
            "name": path.name,
            "check_name_mode": "refuse",
            "create_scene": "file_upload",
            "type": "file",
            "content_hash": sha1,
            "content_hash_name": "sha1",
            "part_info_list": [
                {
                    "part_number": 1
                }
            ],
            "size": path.stat().st_size
        })
        if not res:
            self.__handle_error(res, "创建文件")
            return None
        # 获取上传请求结果
        result = res.json()
        if result.get("exist") or result.get("rapid_upload"):
            # 已存在
            logger.info(f"文件 {result.get('file_name')} 已存在或已秒传完成，无需上传")
            return schemas.FileItem(
                storage=self.schema.value,
                drive_id=result.get("drive_id"),
                fileid=result.get("file_id"),
                parent_fileid=result.get("parent_file_id"),
                type="file",
                name=result.get("file_name"),
                path=f"{fileitem.path}{result.get('file_name')}"
            )
        # 上传文件
        file_id = result.get("file_id")
        upload_id = result.get("upload_id")
        part_info_list = result.get("part_info_list")
        if part_info_list:
            # 上传地址
            upload_url = part_info_list[0].get("upload_url")
            # 上传文件
            res = RequestUtils(headers={
                "Content-Type": "",
                "User-Agent": settings.USER_AGENT,
                "Referer": "https://www.alipan.com/",
                "Accept": "*/*",
            }).put_res(upload_url, data=path.read_bytes())
            if not res:
                self.__handle_error(res, "上传文件")
                return None
            # 标记文件上传完毕
            res = RequestUtils(headers=headers, timeout=10).post_res(self.upload_file_complete_url, json={
                "drive_id": fileitem.drive_id,
                "file_id": file_id,
                "upload_id": upload_id
            })
            if not res:
                self.__handle_error(res, "标记上传状态")
                return None
            result = res.json()
            return schemas.FileItem(
                storage=self.schema.value,
                fileid=result.get("file_id"),
                drive_id=result.get("drive_id"),
                parent_fileid=result.get("parent_file_id"),
                type="file",
                name=result.get("name"),
                path=f"{fileitem.path}{result.get('name')}",
            )
        else:
            logger.warn("阿里云盘上传文件失败：无法获取上传地址！")
        return None

    def move(self, fileitem: schemas.FileItem, target: schemas.FileItem) -> bool:
        """
        移动文件
        """
        params = self.__access_params
        if not params:
            return False
        headers = self.__get_headers(params)
        res = RequestUtils(headers=headers, timeout=10).post_res(self.move_file_url, json={
            "drive_id": fileitem.drive_id,
            "file_id": fileitem.fileid,
            "to_parent_file_id": target.fileid,
            "check_name_mode": "refuse"
        })
        if res:
            return True
        else:
            self.__handle_error(res, "移动文件")
        return False

    def copy(self, fileitm: schemas.FileItem, target_file: Path) -> bool:
        """
        复制文件
        """
        pass

    def link(self, fileitm: schemas.FileItem, target_file: Path) -> bool:
        """
        硬链接文件
        """
        pass

    def softlink(self, fileitm: schemas.FileItem, target_file: Path) -> bool:
        """
        软链接文件
        """
        pass

    def usage(self) -> Optional[schemas.StorageUsage]:
        """
        存储使用情况
        """
        params = self.__access_params
        if not params:
            return None
        headers = self.__get_headers(params)
        res = RequestUtils(headers=headers, timeout=10).post_res(self.storage_info_url, json={})
        if res:
            result = res.json()
            return schemas.StorageUsage(
                total=result.get("drive_total_size"),
                available=result.get("drive_total_size") - result.get("drive_used_size")
            )
        else:
            self.__handle_error(res, "查询存储详情")
        return None
