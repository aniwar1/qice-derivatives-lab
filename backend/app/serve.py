from __future__ import annotations

import argparse
import fcntl
import socket

import uvicorn

from .config import Settings


def main() -> None:
    parser = argparse.ArgumentParser(description="启动期策本地研究工作台")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("端口必须在 1 到 65535 之间")

    settings = Settings.from_env()
    data = settings.workspace_root / "local-data"
    data.mkdir(parents=True, exist_ok=True)
    with (data / "workbench.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.exit(1, "此数据目录的期策实例已在运行。\n")

        # 先绑定端口再初始化数据库，端口占用时不会中断已有任务。
        with socket.socket() as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                listener.bind(("127.0.0.1", args.port))
            except OSError as exc:
                parser.exit(1, f"无法监听 127.0.0.1:{args.port}，{exc.strerror}。\n")
            server_config = uvicorn.Config("app.main:app", host="127.0.0.1", port=args.port)
            try:
                uvicorn.Server(server_config).run(sockets=[listener])
            except KeyboardInterrupt:
                pass


if __name__ == "__main__":
    main()
