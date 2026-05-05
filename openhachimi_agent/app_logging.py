"""应用日志配置。"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from openhachimi_agent.core.config import AppConfig


LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
LOG_FILE_NAME = "openhachimi.log"


def configure_logging(config: AppConfig) -> None:
    """配置应用日志，默认写入本地文件。"""
    level = getattr(logging, config.log_level, logging.INFO)
    config.log_dir.mkdir(parents=True, exist_ok=True)

    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    for handler in list(root_logger.handlers):
        if getattr(handler, "_openhachimi_handler", False):
            root_logger.removeHandler(handler)
            handler.close()

    formatter = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)
    file_handler = RotatingFileHandler(
        config.log_dir / LOG_FILE_NAME,
        maxBytes=2_000_000,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(level)
    file_handler._openhachimi_handler = True  # type: ignore[attr-defined]
    root_logger.addHandler(file_handler)

    if config.log_console:
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        console_handler.setLevel(level)
        console_handler._openhachimi_handler = True  # type: ignore[attr-defined]
        root_logger.addHandler(console_handler)

    logging.getLogger(__name__).info(
        "logging configured level=%s file=%s console=%s",
        config.log_level,
        config.log_dir / LOG_FILE_NAME,
        config.log_console,
    )

    # 屏蔽第三方库的 INFO 级别日志，避免 Telegram Polling 的 getUpdates 等请求污染日志
    # httpx 每 10s 一次的轮询请求只在 WARNING 以上才输出
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    # telegram 底层传输层日志同样静默
    logging.getLogger("telegram.ext.Updater").setLevel(logging.WARNING)

