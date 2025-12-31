"""工具API端点
通过HTTP API暴露MoviePilot的智能体工具功能
"""

from typing import List, Any, Dict, Annotated, Union

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from app import schemas
from app.agent.tools.manager import MoviePilotToolsManager
from app.core.security import verify_apikey
from app.log import logger

# 导入版本号
try:
    from version import APP_VERSION
except ImportError:
    APP_VERSION = "unknown"

router = APIRouter()

# MCP 协议版本
MCP_PROTOCOL_VERSIONS = ["2025-11-25", "2025-06-18", "2024-11-05"]
MCP_PROTOCOL_VERSION = MCP_PROTOCOL_VERSIONS[0]  # 默认使用最新版本


def get_tools_manager() -> MoviePilotToolsManager:
    """
    获取工具管理器实例

    Returns:
        MoviePilotToolsManager实例
    """
    return MoviePilotToolsManager()


def create_jsonrpc_response(request_id: Union[str, int, None], result: Any) -> Dict[str, Any]:
    """创建 JSON-RPC 成功响应"""
    response = {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": result
    }
    return response


def create_jsonrpc_error(request_id: Union[str, int, None], code: int, message: str, data: Any = None) -> Dict[str, Any]:
    """创建 JSON-RPC 错误响应"""
    error = {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {
            "code": code,
            "message": message
        }
    }
    if data is not None:
        error["error"]["data"] = data
    return error


# ==================== MCP JSON-RPC 端点 ====================

@router.post("", summary="MCP JSON-RPC 端点", response_model=None)
async def mcp_jsonrpc(
        request: Request,
        _: Annotated[str, Depends(verify_apikey)] = None
) -> Union[JSONResponse, Response]:
    """
    MCP 标准 JSON-RPC 2.0 端点
    
    处理所有 MCP 协议消息（初始化、工具列表、工具调用等）
    """
    try:
        body = await request.json()
    except Exception as e:
        logger.error(f"解析请求体失败: {e}")
        return JSONResponse(
            status_code=400,
            content=create_jsonrpc_error(None, -32700, "Parse error", str(e))
        )

    # 验证 JSON-RPC 格式
    if not isinstance(body, dict) or body.get("jsonrpc") != "2.0":
        return JSONResponse(
            status_code=400,
            content=create_jsonrpc_error(body.get("id"), -32600, "Invalid Request")
        )

    method = body.get("method")
    params = body.get("params", {})
    request_id = body.get("id")

    # 如果有 id，则为请求；没有 id 则为通知
    is_notification = request_id is None

    try:
        # 处理初始化请求
        if method == "initialize":
            result = await handle_initialize(params)
            return JSONResponse(content=create_jsonrpc_response(request_id, result))

        # 处理已初始化通知
        elif method == "notifications/initialized":
            if is_notification:
                return Response(status_code=204)
            else:
                return JSONResponse(
                    status_code=400,
                    content={"error": "initialized must be a notification"}
                )

        # 处理工具列表请求
        if method == "tools/list":
            result = await handle_tools_list()
            return JSONResponse(content=create_jsonrpc_response(request_id, result))

        # 处理工具调用请求
        elif method == "tools/call":
            result = await handle_tools_call(params)
            return JSONResponse(content=create_jsonrpc_response(request_id, result))

        # 处理 ping 请求
        elif method == "ping":
            return JSONResponse(content=create_jsonrpc_response(request_id, {}))

        # 未知方法
        else:
            return JSONResponse(
                content=create_jsonrpc_error(request_id, -32601, f"Method not found: {method}")
            )

    except ValueError as e:
        logger.warning(f"MCP 请求参数错误: {e}")
        return JSONResponse(
            status_code=400,
            content=create_jsonrpc_error(request_id, -32602, "Invalid params", str(e))
        )
    except Exception as e:
        logger.error(f"处理 MCP 请求失败: {e}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content=create_jsonrpc_error(request_id, -32603, "Internal error", str(e))
        )


async def handle_initialize(params: Dict[str, Any]) -> Dict[str, Any]:
    """处理初始化请求"""
    protocol_version = params.get("protocolVersion")
    client_info = params.get("clientInfo", {})

    logger.info(f"MCP 初始化请求: 客户端={client_info.get('name')}, 协议版本={protocol_version}")

    # 版本协商：选择客户端和服务器都支持的版本
    negotiated_version = MCP_PROTOCOL_VERSION
    if protocol_version in MCP_PROTOCOL_VERSIONS:
        # 客户端版本在支持列表中，使用客户端版本
        negotiated_version = protocol_version
        logger.info(f"使用客户端协议版本: {negotiated_version}")
    else:
        # 客户端版本不支持，使用服务器默认版本
        logger.warning(f"协议版本不匹配: 客户端={protocol_version}, 使用服务器版本={negotiated_version}")
    
    return {
        "protocolVersion": negotiated_version,
        "capabilities": {
            "tools": {
                "listChanged": False  # 暂不支持工具列表变更通知
            },
            "logging": {}
        },
        "serverInfo": {
            "name": "MoviePilot",
            "version": APP_VERSION,
            "description": "MoviePilot MCP Server - 电影自动化管理工具",
        },
        "instructions": "MoviePilot MCP 服务器，提供媒体管理、订阅、下载等工具。"
    }


async def handle_tools_list() -> Dict[str, Any]:
    """处理工具列表请求"""
    manager = get_tools_manager()
    tools = manager.list_tools()

    # 转换为 MCP 工具格式
    mcp_tools = []
    for tool in tools:
        mcp_tool = {
            "name": tool.name,
            "description": tool.description,
            "inputSchema": tool.input_schema
        }
        mcp_tools.append(mcp_tool)

    return {
        "tools": mcp_tools
    }


async def handle_tools_call(params: Dict[str, Any]) -> Dict[str, Any]:
    """处理工具调用请求"""
    tool_name = params.get("name")
    arguments = params.get("arguments", {})

    if not tool_name:
        raise ValueError("Missing tool name")

    manager = get_tools_manager()
    
    try:
        result_text = await manager.call_tool(tool_name, arguments)
        
        return {
            "content": [
                {
                    "type": "text",
                    "text": result_text
                }
            ]
        }
    except Exception as e:
        logger.error(f"工具调用失败: {tool_name}, 错误: {e}", exc_info=True)
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"错误: {str(e)}"
                }
            ],
            "isError": True
        }


@router.delete("", summary="终止 MCP 会话", response_model=None)
async def delete_mcp_session(
        _: Annotated[str, Depends(verify_apikey)] = None
) -> Union[JSONResponse, Response]:
    """
    终止 MCP 会话（无状态模式下仅返回成功）
    """
    return Response(status_code=204)




# ==================== 兼容的 RESTful API 端点 ====================

@router.get("/tools", summary="列出所有可用工具", response_model=List[Dict[str, Any]])
async def list_tools(
        _: Annotated[str, Depends(verify_apikey)]
) -> Any:
    """
    获取所有可用的工具列表
    
    返回每个工具的名称、描述和参数定义
    """
    try:
        manager = get_tools_manager()
        # 获取所有工具定义
        tools = manager.list_tools()

        # 转换为字典格式
        tools_list = []
        for tool in tools:
            tool_dict = {
                "name": tool.name,
                "description": tool.description,
                "inputSchema": tool.input_schema
            }
            tools_list.append(tool_dict)

        return tools_list
    except Exception as e:
        logger.error(f"获取工具列表失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"获取工具列表失败: {str(e)}")


@router.post("/tools/call", summary="调用工具", response_model=schemas.ToolCallResponse)
async def call_tool(
        request: schemas.ToolCallRequest,
        _: Annotated[str, Depends(verify_apikey)] = None
) -> Any:
    """
    调用指定的工具
        
    Returns:
        工具执行结果
    """
    try:
        # 使用当前用户ID创建管理器实例
        manager = get_tools_manager()

        # 调用工具
        result_text = await manager.call_tool(request.tool_name, request.arguments)

        return schemas.ToolCallResponse(
            success=True,
            result=result_text
        )
    except Exception as e:
        logger.error(f"调用工具 {request.tool_name} 失败: {e}", exc_info=True)
        return schemas.ToolCallResponse(
            success=False,
            error=f"调用工具失败: {str(e)}"
        )


@router.get("/tools/{tool_name}", summary="获取工具详情", response_model=Dict[str, Any])
async def get_tool_info(
        tool_name: str,
        _: Annotated[str, Depends(verify_apikey)]
) -> Any:
    """
    获取指定工具的详细信息
        
    Returns:
        工具的详细信息，包括名称、描述和参数定义
    """
    try:
        manager = get_tools_manager()
        # 获取所有工具
        tools = manager.list_tools()

        # 查找指定工具
        for tool in tools:
            if tool.name == tool_name:
                return {
                    "name": tool.name,
                    "description": tool.description,
                    "inputSchema": tool.input_schema
                }

        raise HTTPException(status_code=404, detail=f"工具 '{tool_name}' 未找到")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"获取工具信息失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"获取工具信息失败: {str(e)}")


@router.get("/tools/{tool_name}/schema", summary="获取工具参数Schema", response_model=Dict[str, Any])
async def get_tool_schema(
        tool_name: str,
        _: Annotated[str, Depends(verify_apikey)]
) -> Any:
    """
    获取指定工具的参数Schema（JSON Schema格式）
        
    Returns:
        工具的JSON Schema定义
    """
    try:
        manager = get_tools_manager()
        # 获取所有工具
        tools = manager.list_tools()

        # 查找指定工具
        for tool in tools:
            if tool.name == tool_name:
                return tool.input_schema

        raise HTTPException(status_code=404, detail=f"工具 '{tool_name}' 未找到")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"获取工具Schema失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"获取工具Schema失败: {str(e)}")
