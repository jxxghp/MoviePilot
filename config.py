"""
配置文件
"""
import os
from dotenv import load_dotenv

# 加载环境变量
load_dotenv()


class Config:
    """配置类"""
    
    # API 服务器配置
    API_HOST = os.getenv('API_HOST', '0.0.0.0')
    API_PORT = int(os.getenv('API_PORT', '5000'))
    DEBUG = os.getenv('DEBUG', 'True').lower() == 'true'
    SECRET_KEY = os.getenv('SECRET_KEY', 'movie-agent-secret-key')
    
    # LLM 配置
    LLM_PROVIDER = os.getenv('LLM_PROVIDER', 'none')  # openai, azure, anthropic, none
    LLM_API_KEY = os.getenv('LLM_API_KEY', '')
    LLM_MODEL = os.getenv('LLM_MODEL', 'gpt-4')
    LLM_BASE_URL = os.getenv('LLM_BASE_URL', '')
    
    # Agent 配置
    AGENT_MAX_ITERATIONS = int(os.getenv('AGENT_MAX_ITERATIONS', '5'))
    AGENT_TIMEOUT = int(os.getenv('AGENT_TIMEOUT', '30'))  # 秒
    
    # 数据库配置（未来扩展）
    DATABASE_URL = os.getenv('DATABASE_URL', '')
    
    # 下载配置
    DOWNLOAD_PATH = os.getenv('DOWNLOAD_PATH', './downloads')
    MAX_CONCURRENT_DOWNLOADS = int(os.getenv('MAX_CONCURRENT_DOWNLOADS', '3'))
    
    # 日志配置
    LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO')
    LOG_FILE = os.getenv('LOG_FILE', 'movie_agent.log')
    
    @classmethod
    def get_llm_client(cls):
        """获取 LLM 客户端"""
        if cls.LLM_PROVIDER == 'none' or not cls.LLM_API_KEY:
            return None
        
        if cls.LLM_PROVIDER == 'openai':
            import openai
            return openai.OpenAI(
                api_key=cls.LLM_API_KEY,
                base_url=cls.LLM_BASE_URL if cls.LLM_BASE_URL else None
            )
        elif cls.LLM_PROVIDER == 'azure':
            import openai
            return openai.AzureOpenAI(
                api_key=cls.LLM_API_KEY,
                azure_endpoint=cls.LLM_BASE_URL
            )
        elif cls.LLM_PROVIDER == 'anthropic':
            # 可以添加 Anthropic Claude 支持
            pass
        
        return None
    
    @classmethod
    def print_config(cls):
        """打印配置信息"""
        print("=" * 60)
        print("📋 当前配置")
        print("=" * 60)
        print(f"API 服务器: {cls.API_HOST}:{cls.API_PORT}")
        print(f"调试模式: {cls.DEBUG}")
        print(f"LLM 提供商: {cls.LLM_PROVIDER}")
        print(f"LLM 模型: {cls.LLM_MODEL}")
        print(f"最大迭代次数: {cls.AGENT_MAX_ITERATIONS}")
        print(f"下载路径: {cls.DOWNLOAD_PATH}")
        print(f"日志级别: {cls.LOG_LEVEL}")
        print("=" * 60)
