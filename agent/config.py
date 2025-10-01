"""
Agent 配置管理
"""
from pydantic import BaseModel, Field
from typing import Optional


class AgentConfig(BaseModel):
    """智能体配置"""
    
    # OpenAI 配置
    openai_api_key: str = Field(..., description="OpenAI API Key")
    openai_base_url: Optional[str] = Field(None, description="OpenAI Base URL")
    openai_model: str = Field(default="gpt-4-turbo-preview", description="模型名称")
    
    # Agent 配置
    system_prompt: Optional[str] = Field(None, description="系统提示词")
    max_iterations: int = Field(default=5, description="最大迭代次数")
    
    # 日志配置
    log_level: str = Field(default="INFO", description="日志级别")
    
    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
