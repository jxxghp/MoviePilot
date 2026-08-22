"""插件定时任务声明的契约校验。

调度是本仓里少有的「错了也不当场报错」的扩展点：表达式写错的任务安静地待在调度器
里，直到它本该触发的那一刻才失败，而那多半是凌晨。因此契约把调度表达式当场建成
触发器，建不出来即拒绝登记；实现是否可调用、任务标识是否成立同样在登记时判完。

调度参数只描述调度，必须能 JSON 序列化往返：跨进程时它原样成为握手报文，触发器
对象与 ``datetime`` 这类只在进程内成立的形状过不去。
"""

from __future__ import annotations

import inspect
import json
import re
from typing import Any, Dict, Mapping, Optional, Tuple

from apscheduler.triggers.base import BaseTrigger
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.runtime.extensions.contract.declaration import (
    ScheduleDeclaration,
    declaration_impl,
    declaration_schedule_identity,
    declaration_schedule_kwargs,
    declaration_schedule_trigger,
)

# 任务标识文法：任务标识会拼进调度器的任务 id，含分隔符会让宿主认错归属实例
SCHEDULE_JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

# 支持的调度类型，与宿主调度器实际能建出的触发器一一对应
SCHEDULE_TRIGGER_TYPES: Tuple[str, ...] = ("cron", "interval", "date")

# cron 五段表达式在 trigger_args 里的键名，与逐字段写法互斥
CRONTAB_ARG = "crontab"

# cron 五段表达式展开后依次落到的字段名
_CRONTAB_FIELDS: Tuple[str, ...] = ("minute", "hour", "day", "month", "day_of_week")

# cron 逐字段写法的字段名，用于判定与五段表达式是否重复表达
_CRON_FIELDS = frozenset(
    {"year", "month", "week", "day", "day_of_week", "hour", "minute", "second"}
)


def schedule_declaration_violation(declaration: Any) -> Optional[str]:
    """
    校验定时任务声明是否满足登记契约

    契约要求声明是 `ScheduleDeclaration` 实例、任务标识合文法、展示名称非空、
    调度类型在支持范围内、调度参数非空且能 JSON 序列化往返并真的建得出触发器、
    实现可调用且能接受声明的调用参数。任一不满足都拒绝整条声明，不留到该任务
    本该触发的那一刻才失败。

    :param declaration: `ScheduleDeclaration` 实例
    :return: 违反契约的描述；声明合规时为 None
    """
    if not isinstance(declaration, ScheduleDeclaration):
        return f"{declaration!r} 不是 ScheduleDeclaration 实例"
    try:
        impl = declaration_impl(declaration)
        job_id, name = declaration_schedule_identity(declaration)
        trigger, trigger_args = declaration_schedule_trigger(declaration)
        kwargs = declaration_schedule_kwargs(declaration)
    except Exception as error:
        return f"读取定时任务声明出错：{error}"
    if not job_id:
        return "未声明非空的任务标识 job_id"
    if not SCHEDULE_JOB_ID_RE.match(job_id):
        return f"job_id {job_id!r} 不合法，须匹配 {SCHEDULE_JOB_ID_RE.pattern}"
    if not name:
        return "未声明非空的任务展示名称 name"
    if kwargs is not None and not isinstance(kwargs, Mapping):
        return "kwargs 必须是映射"
    violation = _trigger_violation(trigger, trigger_args)
    if violation:
        return violation
    return _impl_violation(impl, kwargs)


def schedule_trigger_args(trigger: Any, trigger_args: Any) -> Dict[str, Any]:
    """
    把声明的调度参数归一为宿主调度器可直接展开的形式

    只做一件事：cron 的五段表达式展开成逐字段取值，其余参数原样保留。触发器本身
    交给宿主调度器按其时区建，不在此处建好——预先建好的触发器会带上本进程的本地
    时区，覆盖掉宿主为整个调度器配置的那一个。

    :param trigger: 已通过契约校验的调度类型
    :param trigger_args: 已通过契约校验的调度参数
    :return: 归一后的调度参数字典
    """
    args = dict(trigger_args or {})
    if trigger != "cron" or CRONTAB_ARG not in args:
        return args
    expression = args.pop(CRONTAB_ARG)
    args.update(zip(_CRONTAB_FIELDS, str(expression).split()))
    return args


def _trigger_violation(trigger: Any, trigger_args: Any) -> Optional[str]:
    """
    校验调度类型与调度参数能否建出触发器

    :param trigger: 声明的调度类型原始值
    :param trigger_args: 声明的调度参数原始值
    :return: 违反契约的描述；调度成立时为 None
    """
    if not isinstance(trigger, str) or not trigger.strip():
        return f"未声明非空的调度类型 trigger，支持 {'、'.join(SCHEDULE_TRIGGER_TYPES)}"
    if trigger not in SCHEDULE_TRIGGER_TYPES:
        return (
            f"调度类型 {trigger!r} 不受支持，"
            f"仅支持 {'、'.join(SCHEDULE_TRIGGER_TYPES)}"
        )
    if trigger_args is not None and not isinstance(trigger_args, Mapping):
        return "trigger_args 必须是映射"
    args = dict(trigger_args or {})
    if not args:
        return f"{trigger} 调度未给出任何调度参数 trigger_args，无从确定执行时机"
    violation = _serializable_violation(args)
    if violation:
        return violation
    if trigger == "cron":
        violation = _crontab_violation(args)
        if violation:
            return violation
    try:
        _build_trigger(trigger, schedule_trigger_args(trigger, args))
    except Exception as error:
        return f"{trigger} 调度参数 {args!r} 建不出触发器：{error}"
    return None


def _serializable_violation(args: Dict[str, Any]) -> Optional[str]:
    """
    校验调度参数能否 JSON 序列化往返

    跨进程时调度参数原样成为握手报文，因此它必须是纯 JSON 数据。``datetime``、
    元组、集合这类只在进程内成立的形状要么序列化不了，要么往返后变形，故按往返
    结果是否与原值相等判定，而不只看能否序列化。

    :param args: 调度参数
    :return: 违反契约的描述；可往返时为 None
    """
    try:
        restored = json.loads(json.dumps(args, allow_nan=False))
    except (TypeError, ValueError) as error:
        return f"trigger_args 不能 JSON 序列化，无法跨进程传输：{error}；时间请写成字符串"
    if restored != args:
        return "trigger_args 含 JSON 序列化后会变形的数据，无法跨进程传输"
    return None


def _crontab_violation(args: Dict[str, Any]) -> Optional[str]:
    """
    校验 cron 五段表达式的写法

    :param args: cron 调度参数
    :return: 违反契约的描述；写法成立时为 None
    """
    if CRONTAB_ARG not in args:
        return None
    expression = args[CRONTAB_ARG]
    if not isinstance(expression, str) or not expression.strip():
        return f"{CRONTAB_ARG} 必须是非空字符串"
    duplicated = sorted(_CRON_FIELDS & set(args))
    if duplicated:
        return (
            f"{CRONTAB_ARG} 与逐字段写法 {'、'.join(duplicated)} 互斥，"
            f"同时给出无从判断以哪一份为准"
        )
    if len(expression.split()) != 5:
        return f"{CRONTAB_ARG} {expression!r} 必须是标准五段表达式：分 时 日 月 周"
    return None


def _build_trigger(trigger: str, args: Dict[str, Any]) -> BaseTrigger:
    """
    按调度类型建出触发器

    :param trigger: 调度类型
    :param args: 归一后的调度参数
    :return: 触发器实例
    :raises Exception: 调度参数不成立
    """
    if trigger == "cron":
        return CronTrigger(**args)
    if trigger == "interval":
        return IntervalTrigger(**args)
    return DateTrigger(**args)


def _impl_violation(impl: Any, kwargs: Optional[Mapping[str, Any]]) -> Optional[str]:
    """
    校验实现能否被宿主按 ``impl(**kwargs)`` 调用

    同步实现与协程实现都接受：宿主调度器两种都能执行。

    :param impl: 声明携带的任务实现
    :param kwargs: 声明的调用参数
    :return: 违反契约的描述；调用形状成立时为 None
    """
    if impl is None or not callable(impl):
        return "impl 缺失或不可调用，宿主无从执行该任务"
    try:
        signature = inspect.signature(impl)
    except (TypeError, ValueError):
        # 内建函数等无法内省签名的可调用对象不构成违约，调用形状留到执行时决定
        return None
    try:
        signature.bind(**dict(kwargs or {}))
    except TypeError as error:
        return f"{impl!r} 的调用签名不接受声明的 kwargs：{error}"
    return None
