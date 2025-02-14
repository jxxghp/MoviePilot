from datetime import datetime

from sqlalchemy import Column, Integer, JSON, Sequence, String

from app.db import Base


class Workflow(Base):
    """
    工作流表
    """
    # ID
    id = Column(Integer, Sequence('id'), primary_key=True, index=True)
    # 名称
    name = Column(String, index=True, nullable=False)
    # 描述
    description = Column(String)
    # 定时器
    timer = Column(String)
    # 状态：N-新建 R-运行中 P-暂停 S-成功 F-失败
    state = Column(String, nullable=False, index=True, default='N')
    # 当前执行动作
    current_action = Column(String)
    # 任务执行结果
    result = Column(String)
    # 已执行次数
    run_count = Column(Integer, default=0)
    # 任务列表
    actions = Column(JSON, default=list)
    # 执行上下文
    context = Column(JSON, default=dict)
    # 创建时间
    add_time = Column(String, default=datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    # 最后执行时间
    last_time = Column(String)
