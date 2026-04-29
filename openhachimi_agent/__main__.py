"""OpenHachimi 命令入口。"""

import argparse
import asyncio
import logging

import uvicorn

from openhachimi_agent.app_logging import configure_logging
from openhachimi_agent.core.config import load_config
from openhachimi_agent.daemon.deploy import DEFAULT_HOST, DEFAULT_PORT, deploy_daemon
from openhachimi_agent.interface.cli import run_cli, run_embedded_cli


logger = logging.getLogger(__name__)


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
        config = load_config()
        configure_logging(config)
        logger.info("deploy command host=%s port=%s", args.host, args.port)
        deploy_daemon(args.host, args.port)
        return

    if args.command == "serve":
        config = load_config()
        configure_logging(config)
        logger.info("serve command host=%s port=%s", args.host, args.port)
        uvicorn.run("openhachimi_agent.interface.http:app", host=args.host, port=args.port)
        return

    asyncio.run(run_embedded_cli())


if __name__ == "__main__":
    main()
