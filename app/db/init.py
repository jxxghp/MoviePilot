from pathlib import Path
from typing import Optional, Tuple, Dict

from alembic.command import upgrade, downgrade
from alembic.config import Config
from sqlalchemy import inspect

from app.core.config import settings
from app.db import Engine, Base
from app.log import logger
from app.utils.version import VersionUtils


def init_db():
    """
    初始化数据库
    """
    # 全量建表
    Base.metadata.create_all(bind=Engine)


def update_db():
    """
    更新数据库

    自动化识别是否需要升级或降级数据库
    """
    db_location = settings.CONFIG_PATH / 'user.db'
    script_location = settings.ROOT_PATH / 'database'
    u_or_d = None
    try:
        # 全部迁移脚本版本
        script_vers_dict = __get_alembic_script_versions(script_location=script_location / 'versions')
        # 最接近当前后端版本的迁移脚本的两个版本号
        script_num_ver, script_hash_ver = __get_update_db_version(script_vers=script_vers_dict)
        alembic_cfg = Config()
        alembic_cfg.set_main_option('script_location', str(script_location))
        alembic_cfg.set_main_option('sqlalchemy.url', f"sqlite:///{db_location}")

        # 动态识别升级还是降级
        u_or_d = upgrade_db_or_downgrade_db(script_num_ver=script_num_ver,
                                            script_hash_ver=script_hash_ver,
                                            script_vers=script_vers_dict)
        if u_or_d == 'upgrade':
            upgrade(alembic_cfg, script_hash_ver)
            logger.debug('数据库升级成功')
            pass
        elif u_or_d == 'downgrade':
            downgrade(alembic_cfg, script_hash_ver)
            logger.debug('数据库降级成功')
            pass
        elif u_or_d == 'no_need':
            logger.debug('数据库已经是当前支持或最新版本，无需更新')
        else:
            raise ValueError("没有找到可处理的版本")
    except Exception as e:
        if not u_or_d:
            logger.error(f'数据库迁移失败：{str(e)}')
        else:
            logger.error(f'数据库{ "升级" if u_or_d == "upgrade" else "降级"}失败：{str(e)}')


def upgrade_db_or_downgrade_db(script_num_ver: str, script_hash_ver: str, script_vers: dict) -> str:
    """
    升级还是降级数据库

    :param script_num_ver: 最接近当前app_version 的脚本的 数字 版本号
    :param script_hash_ver: 最接近当前app_version 的脚本的 hash 版本号
    :param script_vers: 全部迁移脚本版本
    :return: upgrade / downgrade / no_need
    :raise: Exception
    """
    try:
        db_hash_ver = __get_alembic_version_table_value()
        # 反转字典，数字版本号与hash版本号 应该都是唯一的？
        script_vers = {v: k for k, v in script_vers.items()}
        db_num_ver = script_vers.get(db_hash_ver)
        if not db_num_ver:
            raise ValueError('数据库中的 hash 版本号在不在当前的迁移脚本中，无法进行升降级操作！')
        if not db_hash_ver:
            return 'upgrade'
        # hash 版本号相同时，则无需再进行版本号比较；解决已经迭代多个版本，但未需要迁移数据库的问题
        if db_hash_ver == script_hash_ver:
            return 'no_need'
        # 检查脚本数字版本号 >= 当前的系统版本号，>= 则为升级，< 则为降级
        status = VersionUtils().version_comparison(input_ver=script_num_ver, targe_ver=db_num_ver,
                                                   verbose=False, compare_type='>=')
        return "upgrade" if status is True else "downgrade"
    except Exception as e:
        raise e


def __get_alembic_version_table_value() -> Optional[str]:
    """
    获取当前数据库中记录的版本信息
    :return: 当前数据库中的 hash 版本号
    :raise: Exception
    """
    try:
        if not inspect(Engine).has_table('alembic_version'):
            return None
        else:
            Base.metadata.reflect(bind=Engine, only=['alembic_version'])
            alembic_version_table = Base.metadata.tables['alembic_version']

        # 检查表中是否存在 version_num 字段
        if 'version_num' not in alembic_version_table.columns:
            return None
        else:
            # 使用 SQLAlchemy Core 的查询方法，orm 不适合这种简单的查询
            with Engine.connect() as conn:
                # 获取数据库中的 hash 版本号
                db_hash_ver = conn.execute(alembic_version_table.select()).mappings().first().get('version_num')
                # 防止返回的是个空值
                return db_hash_ver if db_hash_ver else None
    except Exception as e:
        raise e


def __get_alembic_script_versions(script_location: Path) -> Dict:
    """
    获取所有 Alembic 脚本的版本信息
    返回字典，键为 数字 版本号，值为 hash 版本号

    : param script_location: 脚本路径
    : return: 版本号 -> hash 值 的字典
    """
    script_vers = {}
    for file in script_location.iterdir():
        if file.is_file():
            parts = file.stem.split('_')
            if len(parts) > 1:
                # 数字 版本号
                key = '.'.join(parts[1:])
                # hash 版本号
                value = parts[0]
                script_vers[key] = value
    # 对版本号进行非法值转换
    if script_vers:
        script_vers = __conversion_version(script_vers=script_vers)
    return script_vers


def __get_update_db_version(script_vers: dict) -> Tuple[Optional[str], ...]:
    """
    获取等于或低于当前 APP_VERSION 的最近版本号作为升降级要求的版本号

    :param script_vers: 脚本版本号字典
    :return: 脚本 数字 版本号， 脚本 hash 值版本号
    """
    script_num_ver, script_hash_version = None, None
    # 排序好脚本版本号，从大到小，用于按顺序查找最近的脚本版本号
    script_vers = dict(sorted(script_vers.items(), key=lambda item: tuple(map(int, item[0].split('.'))), reverse=True))

    status = None
    for script_num_ver, script_hash_version in script_vers.items():
        # 判断当前版本号是否小于等于 APP_VERSION，从而找到最近的脚本版本号
        if VersionUtils().version_comparison(input_ver=script_num_ver, verbose=False, compare_type="<="):
            status = True
            break
    return script_num_ver if status else None, script_hash_version if status else None


def __conversion_version(script_vers: dict) -> Dict:
    """
    将 script_vers 内的 key 值的非法字符转换成 比对方法 可用的 int 类型值

    :param script_vers: 版本号字典
    :return: 除去非法字符的版本号字典
    """
    new_script_vers = {}
    for key, value in script_vers.items():
        key_list = VersionUtils().preprocess_version(key)
        new_key_list = VersionUtils().conversion_version(key_list)
        new_key = '.'.join([str(i) for i in new_key_list])
        new_script_vers[new_key] = value

    return new_script_vers


# if __name__ == '__main__':
#     update_db()
