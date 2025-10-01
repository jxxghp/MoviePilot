"""
Agent 基础类和工具基类
"""
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class ToolParameter(BaseModel):
    """工具参数定义"""
    name: str = Field(..., description="参数名称")
    type: str = Field(..., description="参数类型")
    description: str = Field(..., description="参数描述")
    required: bool = Field(default=True, description="是否必需")
    enum: Optional[List[str]] = Field(default=None, description="枚举值")


class ToolSchema(BaseModel):
    """工具schema定义"""
    name: str = Field(..., description="工具名称")
    description: str = Field(..., description="工具描述")
    parameters: List[ToolParameter] = Field(default_factory=list, description="工具参数")
    
    def to_function_call_schema(self) -> Dict[str, Any]:
        """转换为函数调用schema格式"""
        properties = {}
        required = []
        
        for param in self.parameters:
            param_schema = {
                "type": param.type,
                "description": param.description
            }
            if param.enum:
                param_schema["enum"] = param.enum
            
            properties[param.name] = param_schema
            if param.required:
                required.append(param.name)
        
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required
                }
            }
        }


class BaseTool(ABC):
    """工具基类"""
    
    def __init__(self):
        self._schema: Optional[ToolSchema] = None
    
    @property
    @abstractmethod
    def name(self) -> str:
        """工具名称"""
        pass
    
    @property
    @abstractmethod
    def description(self) -> str:
        """工具描述"""
        pass
    
    @property
    @abstractmethod
    def parameters(self) -> List[ToolParameter]:
        """工具参数"""
        pass
    
    @abstractmethod
    async def execute(self, **kwargs) -> Dict[str, Any]:
        """执行工具"""
        pass
    
    @property
    def schema(self) -> ToolSchema:
        """获取工具schema"""
        if self._schema is None:
            self._schema = ToolSchema(
                name=self.name,
                description=self.description,
                parameters=self.parameters
            )
        return self._schema
    
    def to_function_call_schema(self) -> Dict[str, Any]:
        """转换为函数调用schema"""
        return self.schema.to_function_call_schema()


class ToolResult(BaseModel):
    """工具执行结果"""
    success: bool = Field(..., description="是否成功")
    data: Any = Field(default=None, description="返回数据")
    message: str = Field(default="", description="消息")
    error: Optional[str] = Field(default=None, description="错误信息")
