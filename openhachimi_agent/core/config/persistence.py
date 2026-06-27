"""配置文件写回(持久化)层。

负责把部署/运行期产生的值写回 config.yaml 的 app 段(如 deploy --host/--port、
HTTP API Token 自动生成),保留原文件的注释/缩进/行尾。与 webui_io 的写回引擎
同源思路但作用在固定 app 段,各自独立实现。
"""

import secrets
from pathlib import Path
from typing import Any

from openhachimi_agent.core.config._helpers import (
    _as_mapping,
    _config_string,
    _quote_yaml_string,
    _yaml_safe_dump_to,
)


def _replace_or_insert_app_kv(
    config_path: Path, key: str, value: str, raw_config: dict[str, Any], *, quote: bool = True
) -> None:
    """在 config.yaml 的 app 段替换或插入一个 key: value 行，保留原有注释/缩进/行尾。

    用于将 deploy --host/--port 等命令行参数持久化回配置文件，使改配置后 restart 即生效。
    quote=False 时按裸值写入（适用于数字）。失败时回退到 yaml.safe_dump 重写整个文件。
    """
    formatted = _quote_yaml_string(value) if quote else str(value)
    try:
        text = config_path.read_text(encoding="utf-8")
        lines = text.splitlines(keepends=True)
        app_index = next((idx for idx, line in enumerate(lines) if line.strip() == "app:"), None)
        if app_index is None:
            raise ValueError("config.yaml 缺少 app 配置段")

        app_indent = len(lines[app_index]) - len(lines[app_index].lstrip())
        insert_index = app_index + 1
        key_index = None
        for idx in range(app_index + 1, len(lines)):
            stripped = lines[idx].strip()
            if stripped and not stripped.startswith("#"):
                indent = len(lines[idx]) - len(lines[idx].lstrip())
                if indent <= app_indent:
                    break
            if stripped.startswith(f"{key}:"):
                key_index = idx
                break
            insert_index = idx + 1

        new_line = f"{' ' * (app_indent + 2)}{key}: {formatted}\n"
        if key_index is not None:
            line = lines[key_index]
            line_ending = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
            comment = ""
            before_comment = line.rstrip("\r\n")
            if "#" in before_comment:
                comment = " " + before_comment[before_comment.index("#"):].strip()
            lines[key_index] = f"{' ' * (app_indent + 2)}{key}: {formatted}{comment}{line_ending}"
        else:
            lines.insert(insert_index, new_line)
        config_path.write_text("".join(lines), encoding="utf-8")
    except Exception:
        raw_config.setdefault("app", {})[key] = value
        config_path.write_text(_yaml_safe_dump_to(raw_config), encoding="utf-8")


def _replace_or_insert_http_api_token(config_path: Path, token: str, raw_config: dict[str, Any]) -> None:
    _replace_or_insert_app_kv(config_path, "http_api_token", token, raw_config)


def persist_server_endpoint(config_path: Path, raw_config: dict[str, Any], host: str, port: int) -> None:
    """将 host/port 写回配置文件 app.server_host / app.server_port。

    deploy --host/--port 透传至此，使命令行参数持久化，后续 restart 读配置即生效。
    """
    _replace_or_insert_app_kv(config_path, "server_host", host, raw_config)
    _replace_or_insert_app_kv(config_path, "server_port", str(port), raw_config, quote=False)
    raw_config.setdefault("app", {})["server_host"] = host
    raw_config.setdefault("app", {})["server_port"] = port


def _ensure_http_api_token(config_path: Path, raw_config: dict[str, Any]) -> str:
    app_config = _as_mapping(raw_config.get("app"), "app")
    token = _config_string(app_config, "http_api_token")
    if token:
        return token

    token = secrets.token_urlsafe(32)
    raw_config["app"] = app_config
    app_config["http_api_token"] = token
    try:
        _replace_or_insert_http_api_token(config_path, token, raw_config)
    except OSError as exc:
        raise OSError(
            f"无法写入 HTTP API Token 到配置文件：{config_path}。请修复文件权限或手动添加 app.http_api_token。"
        ) from exc
    return token