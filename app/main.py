from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.config import get_settings
from app.routes import admin, connect, health, internal, onboarding, password_recovery, signup, tenant_api
from app.security import csrf_protect_admin_request


def create_app() -> FastAPI:
    app = FastAPI(title="Unboks Internal Control Panel", version="0.1.0")

    @app.middleware("http")
    async def admin_csrf_guard(request, call_next):
        blocked = csrf_protect_admin_request(request, get_settings())
        if blocked is not None:
            return blocked
        return await call_next(request)

    app.mount("/static", StaticFiles(directory="app/static"), name="static")
    app.include_router(health.router)
    app.include_router(onboarding.router)
    app.include_router(internal.router)
    app.include_router(connect.router)
    app.include_router(connect.public_router)
    app.include_router(signup.router)
    app.include_router(password_recovery.router)
    app.include_router(tenant_api.router)
    app.include_router(admin.router)
    return app


app = create_app()
