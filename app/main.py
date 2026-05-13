from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.routes import admin, health


def create_app() -> FastAPI:
    app = FastAPI(title="Unboks Internal Control Panel", version="0.1.0")
    app.mount("/static", StaticFiles(directory="app/static"), name="static")
    app.include_router(health.router)
    app.include_router(admin.router)
    return app


app = create_app()
