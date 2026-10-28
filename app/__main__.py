"""启动入口:python -m app 或 uvicorn app.main:app。"""
from __future__ import annotations

import threading
import webbrowser

import uvicorn

from .config import settings


def _open_browser_when_ready(server: uvicorn.Server) -> None:
    """服务就绪后自动打开浏览器;仅本机地址时(Docker/公网部署不受影响)。"""
    if settings.host not in ("127.0.0.1", "localhost"):
        return
    url = f"http://127.0.0.1:{settings.port}"

    def _wait_and_open() -> None:
        # 直接探测端口就绪,兼容所有 uvicorn 版本;最多等 60 秒。
        import socket
        import time
        for _ in range(300):
            try:
                with socket.create_connection(("127.0.0.1", settings.port), timeout=0.5):
                    break
            except OSError:
                time.sleep(0.2)
        webbrowser.open(url)

    threading.Thread(target=_wait_and_open, daemon=True).start()


if __name__ == "__main__":
    config = uvicorn.Config(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        log_level="info",
    )
    server = uvicorn.Server(config)
    _open_browser_when_ready(server)
    server.run()
