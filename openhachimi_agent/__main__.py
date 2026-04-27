"""OpenHachimi 命令入口。"""

import argparse

import uvicorn

from openhachimi_agent.cli import run_cli
from openhachimi_agent.config import load_config
from openhachimi_agent.daemon import DEFAULT_HOST, DEFAULT_PORT, deploy_daemon


def main() -> None:
    parser = argparse.ArgumentParser(prog="openhachimi")
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("cli", help="连接本地后台服务并进入 CLI 对话")

    deploy_parser = subparsers.add_parser("deploy", help="部署并启动本地后台守护服务")
    deploy_parser.add_argument("--host", default=DEFAULT_HOST)
    deploy_parser.add_argument("--port", type=int, default=DEFAULT_PORT)

    serve_parser = subparsers.add_parser("serve", help="启动 localhost HTTP 后台服务")
    serve_parser.add_argument("--host", default=DEFAULT_HOST)
    serve_parser.add_argument("--port", type=int, default=DEFAULT_PORT)

    args = parser.parse_args()

    if args.command == "deploy":
        deploy_daemon(args.host, args.port)
        return

    if args.command == "serve":
        load_config()
        uvicorn.run("openhachimi_agent.server:app", host=args.host, port=args.port)
        return

    run_cli()


if __name__ == "__main__":
    main()
