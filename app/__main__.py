"""启动入口:python -m app 或 uvicorn app.main:app。"""
from __future__ import annotations

import uvicorn

from .config import settings

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        log_level="info",
    )
