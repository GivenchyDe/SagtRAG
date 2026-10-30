from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.api.admin_auth import initialize_first_admin, router as admin_auth_router
from app.api.admin_dashboard import router as admin_dashboard_router
from app.api.documents import router as documents_router
from app.api.admin_management import router as admin_management_router
from app.api.admin_qa import router as admin_qa_router
from app.api.routes import router
from app.api.wecom_callback import router as wecom_callback_router
from app.config import get_settings
from app.db import SessionFactory, engine
from app.integrations import connected_object_store, create_redis_client
from app.queueing import InboundPublisher, IngestionPublisher

settings = get_settings()


@asynccontextmanager
async def lifespan(application: FastAPI):
    """在 API 进程生命周期内连接并释放数据库、队列、缓存和对象存储。"""
    publisher = InboundPublisher()
    ingestion_publisher = IngestionPublisher()
    redis_client = create_redis_client(settings)
    application.state.inbound_publisher = publisher
    application.state.ingestion_publisher = ingestion_publisher
    application.state.redis = redis_client
    try:
        await initialize_first_admin(SessionFactory)
        await redis_client.ping()
        await publisher.connect()
        await ingestion_publisher.connect()
        async with connected_object_store(settings) as object_store:
            application.state.object_store = object_store
            yield
    finally:
        await publisher.close()
        await ingestion_publisher.close()
        await redis_client.aclose()
        await engine.dispose()


app = FastAPI(title="SagtRAG API", version="0.1.0", lifespan=lifespan)

origins = [origin.strip() for origin in settings.allowed_web_origins.split(",") if origin.strip()]
if origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
    )

# 员工自助接口与管理员接口使用两套独立认证依赖。
app.include_router(router)
app.include_router(admin_auth_router)
app.include_router(admin_dashboard_router)
app.include_router(documents_router)
app.include_router(admin_management_router)
app.include_router(admin_qa_router)
app.include_router(wecom_callback_router)


@app.get("/health/live", include_in_schema=False)
async def liveness() -> dict[str, str]:
    """返回不依赖外部组件状态的进程存活信号。"""
    return {"status": "live"}



