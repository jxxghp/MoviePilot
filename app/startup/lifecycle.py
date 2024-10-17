import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.startup.modules_initializer import shutdown_modules, start_modules
from app.startup.plugins_initializer import init_plugins_async
from app.startup.routers_initializer import init_routers


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    定义应用的生命周期事件
    """
    print("Starting up...")
    start_modules(app)
    init_routers(app)
    plugin_init_task = asyncio.create_task(init_plugins_async())
    try:
        yield
    finally:
        print("Shutting down...")
        try:
            plugin_init_task.cancel()
            await plugin_init_task
        except asyncio.CancelledError:
            print("Plugin installation task cancelled.")
        except Exception as e:
            print(f"Error during plugin installation shutdown: {e}")
        shutdown_modules(app)
