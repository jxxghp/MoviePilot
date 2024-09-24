from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.startup.module_initializer import start_modules, shutdown_modules
from app.startup.routers import init_routers


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    定义应用的生命周期事件
    """
    print("Starting up...")
    start_modules(app)
    init_routers(app)
    yield
    print("Shutting down...")
    shutdown_modules(app)
