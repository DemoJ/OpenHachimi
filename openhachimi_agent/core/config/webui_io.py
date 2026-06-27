"""WebUI 设置页配置读写引擎。

直接读写 config.yaml 原始内容(不走 AppConfig),保留注释/缩进/单位语义。
泛化写回函数支持任意 section 路径,与 persistence 的 _replace_or_insert_app_kv 同思路;
失败时回退到 yaml.safe_dump 重写整个文件(丢注释但保证可用)。
"""

from pathlib import Path
from typing import Any

import yaml

from openhachimi_agent.core.config._helpers import _quote_yaml_string, _yaml_safe_dump_to
from openhachimi_agent.core.config.webui_fields import (
    CONFIG_KIND_BOOL,
    CONFIG_KIND_FLOAT,
    CONFIG_KIND_INT,
    CONFIG_KIND_MULTI,
    CONFIG_KIND_SECRET,
    CONFIG_KIND_SELECT,
    CONFIG_KIND_STRING,
)


def load_raw_config(config_path: Path) -> dict[str, Any]:
    """读取 config.yaml 原始 dict(不做默认值填充/单位转换/路径解析)。

    WebUI 设置页直接读写原始 yaml,避免 AppConfig 的转换破坏文件语义
    (如 max_image_size_mb→bytes、相对路径→绝对、空值回退复用主模型)。
    """
    if not config_path.exists():
        return {}
    with config_path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data if isinstance(data, dict) else {}


def _get_by_path(data: dict[str, Any], path: str) -> Any:
    cur: Any = data
    for seg in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(seg)
        if cur is None:
            return None
    return cur


def _set_by_path(data: dict[str, Any], path: str, value: Any) -> None:
    segs = path.split(".")
    cur: dict[str, Any] = data
    for seg in segs[:-1]:
        nxt = cur.get(seg)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[seg] = nxt
        cur = nxt
    cur[segs[-1]] = value


def mask_secret(value: str) -> str:
    """敏感字段掩码:保留前3+后4,中间用 •••• 占位。过短则全掩。空值返回空串。"""
    if not value:
        return ""
    if len(value) <= 8:
        return "••••"
    return f"{value[:3]}••••{value[-4:]}"


def serialize_config_value(kind: str, raw: Any) -> Any:
    """yaml 原始值 → 前端 JSON 值。secret 的脱敏由调用方(mask_secret)处理。

    SELECT 字段(如 supports_vision)在 yaml 里可能写成裸 true/false(被解析成
    Python bool),需归一为 "true"/"false" 字符串,以匹配 options 白名单。
    """
    if kind in (CONFIG_KIND_STRING, CONFIG_KIND_SECRET):
        return "" if raw is None else str(raw)
    if kind == CONFIG_KIND_SELECT:
        if isinstance(raw, bool):
            return "true" if raw else "false"
        return "" if raw is None else str(raw)
    if kind == CONFIG_KIND_BOOL:
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str):
            return raw.strip().lower() in {"1", "true", "yes", "on"}
        return bool(raw)
    if kind == CONFIG_KIND_INT:
        try:
            return int(raw)
        except (TypeError, ValueError):
            return 0
    if kind == CONFIG_KIND_FLOAT:
        try:
            if raw is None or raw == "":
                return 0.0
            return float(raw)
        except (TypeError, ValueError):
            return 0.0
    if kind == CONFIG_KIND_MULTI:
        # yaml 列表归一为 list[str](仅保留 options 白名单内项);标量/逗号串也兼容。
        if raw is None:
            return []
        if isinstance(raw, list):
            return [str(x).strip() for x in raw if str(x).strip()]
        if isinstance(raw, str):
            return [s.strip() for s in raw.split(",") if s.strip()]
        return []
    return raw


def serialize_config_group(fields: list[dict[str, Any]], raw: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """返回 (values, masked)。secret 字段非空时用掩码替换并记入 masked。"""
    values: dict[str, Any] = {}
    masked: list[str] = []
    for f in fields:
        raw_val = _get_by_path(raw, f["path"])
        val = serialize_config_value(f["kind"], raw_val)
        if f["kind"] == CONFIG_KIND_MULTI:
            # 只保留 options 白名单内项,按 options 顺序排序展示。
            opts = f.get("options", [])
            val = [v for v in opts if v in val]
        if f["kind"] == CONFIG_KIND_SECRET and val:
            masked.append(f["path"])
            val = mask_secret(val)
        values[f["path"]] = val
    return values, masked


def _section_body_range(lines: list[str], idx_sec: int, section_indent: int) -> tuple[int, int]:
    """section 内容范围 [start, end):从 idx_sec+1 起到遇到缩进 <= section_indent 的非空非注释行止。"""
    end = len(lines)
    for idx in range(idx_sec + 1, len(lines)):
        stripped = lines[idx].strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(lines[idx]) - len(lines[idx].lstrip())
        if indent <= section_indent:
            end = idx
            break
    return idx_sec + 1, end


def _locate_section(lines: list[str], section_path: list[str]) -> tuple[int, int, int] | None:
    """逐级定位 section,返回 (body_start, body_end, section_indent)。任一级缺失返回 None。"""
    cur_start, cur_end, parent_indent = 0, len(lines), -1
    for name in section_path:
        idx_sec = None
        for idx in range(cur_start, cur_end):
            stripped = lines[idx].strip()
            if not stripped or stripped.startswith("#"):
                continue
            indent = len(lines[idx]) - len(lines[idx].lstrip())
            if indent <= parent_indent:
                break
            if stripped.startswith(f"{name}:"):
                idx_sec = idx
                break
        if idx_sec is None:
            return None
        parent_indent = len(lines[idx_sec]) - len(lines[idx_sec].lstrip())
        cur_start, cur_end = _section_body_range(lines, idx_sec, parent_indent)
    return cur_start, cur_end, parent_indent


def replace_yaml_section_kv(
    config_path: Path,
    section_path: list[str],
    key: str,
    value_str: str,
    raw_config: dict[str, Any],
    *,
    quote: bool = True,
) -> None:
    """在 yaml 指定 section 内替换或插入一个 key 行,保留注释/缩进/行尾。

    section_path 如 ["vision"] 或 ["context","summary"]。section 不存在或解析失败时,
    回退到 yaml.safe_dump 重写整个文件(raw_config 应已用 _set_by_path 同步为最新)。
    """
    formatted = _quote_yaml_string(value_str) if quote else str(value_str)
    try:
        text = config_path.read_text(encoding="utf-8")
        lines = text.splitlines(keepends=True)
        loc = _locate_section(lines, section_path)
        if loc is None:
            raise ValueError(f"section {'.'.join(section_path)} 不存在")
        body_start, body_end, section_indent = loc
        key_indent = section_indent + 2

        key_idx = None
        for idx in range(body_start, body_end):
            if lines[idx].strip().startswith(f"{key}:"):
                key_idx = idx
                break

        if key_idx is not None:
            line = lines[key_idx]
            line_ending = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
            comment = ""
            before_comment = line.rstrip("\r\n")
            if "#" in before_comment:
                comment = " " + before_comment[before_comment.index("#"):].strip()
            lines[key_idx] = f"{' ' * key_indent}{key}: {formatted}{comment}{line_ending}"
        else:
            lines.insert(body_end, f"{' ' * key_indent}{key}: {formatted}\n")
        config_path.write_text("".join(lines), encoding="utf-8")
    except Exception:
        config_path.write_text(_yaml_safe_dump_to(raw_config), encoding="utf-8")


def replace_yaml_section_list(
    config_path: Path,
    section_path: list[str],
    key: str,
    items: list[str],
    raw_config: dict[str, Any],
) -> None:
    """在 yaml 指定 section 内写回一个字符串列表(多选),保留其余注释/缩进。

    列表在 yaml 里写作多行 `- item`。本函数把 `key:` 及其后所有 `- ...` 连续行
    视为该列表块整体替换;原为单行 `key: a, b` 或 inline 形式也按块替换。
    section 不存在或解析失败时回退到 yaml.safe_dump 整体重写。
    """
    try:
        text = config_path.read_text(encoding="utf-8")
        lines = text.splitlines(keepends=True)
        loc = _locate_section(lines, section_path)
        if loc is None:
            raise ValueError(f"section {'.'.join(section_path)} 不存在")
        body_start, body_end, section_indent = loc
        key_indent = section_indent + 2
        item_indent = key_indent + 2

        # 定位 key 行
        key_idx = None
        for idx in range(body_start, body_end):
            if lines[idx].strip().startswith(f"{key}:"):
                key_idx = idx
                break

        # 计算新内容块
        if items:
            new_block = [f"{' ' * key_indent}{key}:\n"] + [
                f"{' ' * item_indent}- {item}\n" for item in items
            ]
        else:
            new_block = [f"{' ' * key_indent}{key}: []\n"]

        if key_idx is not None:
            # 找到该列表块结束:key 行之后连续的以 item_indent 开头的 `- ` 行(可跨空行/注释行)。
            block_end = key_idx + 1
            line_ending = "\r\n" if lines[key_idx].endswith("\r\n") else "\n" if lines[key_idx].endswith("\n") else ""
            # 行尾统一化新块(用 \n;末行无需尾随,重组时补)。
            while block_end < body_end:
                raw_line = lines[block_end]
                stripped = raw_line.strip()
                if (
                    stripped.startswith("- ")
                    and (len(raw_line) - len(raw_line.lstrip())) == item_indent
                ):
                    block_end += 1
                elif stripped == "" or stripped.startswith("#"):
                    # 空行/注释行只有在直到遇到下一个 key 前且紧跟列表项才并入;
                    # 简化:遇到不在缩进的空/注释且后面不是列表项则停止。
                    break
                else:
                    break
            # 用新块替换 [key_idx, block_end)
            new_block = [b.rstrip("\n") + line_ending for b in new_block[:-1]] + [new_block[-1].rstrip("\n") + line_ending]
            lines[key_idx:block_end] = new_block
        else:
            lines.insert(body_end, "".join(new_block))
        config_path.write_text("".join(lines), encoding="utf-8")
    except Exception:
        config_path.write_text(_yaml_safe_dump_to(raw_config), encoding="utf-8")


def apply_config_updates(
    config_path: Path,
    raw_config: dict[str, Any],
    fields: list[dict[str, Any]],
    updates: dict[str, Any],
) -> dict[str, Any]:
    """按 updates(路径→值)写回 yaml,逐字段同步 raw_config。

    - 白名单校验:updates 的 path 必须在 fields 内,否则跳过。
    - secret:incoming 等于当前掩码(未改动)则跳过;空串代表清除(回退复用主模型)。
    - select:值必须在 options 内。
    - 每写一个字段同步 _set_by_path 到 raw_config,供后续字段掩码比对与 fallback 使用。
    返回 {"written": [...], "skipped": [...]}。校验失败抛 ValueError。
    """
    field_map = {f["path"]: f for f in fields}
    written: list[str] = []
    skipped: list[str] = []
    for path, incoming in updates.items():
        f = field_map.get(path)
        if f is None:
            skipped.append(path)
            continue
        kind = f["kind"]

        if kind == CONFIG_KIND_SECRET:
            current = _get_by_path(raw_config, path)
            current_str = "" if current is None else str(current)
            if incoming == mask_secret(current_str):
                skipped.append(path)
                continue
            native = "" if incoming is None else str(incoming)
            value_str, quote = native, True
        elif kind == CONFIG_KIND_BOOL:
            native = incoming if isinstance(incoming, bool) else str(incoming).strip().lower() in {"1", "true", "yes", "on"}
            value_str, quote = ("true" if native else "false"), False
        elif kind == CONFIG_KIND_INT:
            try:
                native = int(incoming)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{path} 必须是整数") from exc
            value_str, quote = str(native), False
        elif kind == CONFIG_KIND_FLOAT:
            try:
                native = float(incoming) if incoming not in (None, "") else 0.0
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{path} 必须是数值") from exc
            value_str, quote = str(native), False
        elif kind == CONFIG_KIND_MULTI:
            # incoming 应为 list[str](或逗号串/单串);仅保留 options 白名单内项,按 options 顺序写回。
            opts = f.get("options", [])
            if isinstance(incoming, list):
                raw_items = [str(x).strip() for x in incoming if str(x).strip()]
            elif isinstance(incoming, str):
                raw_items = [s.strip() for s in incoming.split(",") if s.strip()] if incoming else []
            elif incoming is None:
                raw_items = []
            else:
                raise ValueError(f"{path} 必须是列表")
            allowed = set(opts)
            native = [x for x in opts if x in raw_items]  # 按 options 顺序、去重、仅白名单
            _set_by_path(raw_config, path, native)
            section_path = path.split(".")[:-1]
            key = path.split(".")[-1]
            replace_yaml_section_list(config_path, section_path, key, native, raw_config)
            written.append(path)
            continue
        else:  # string / select
            native = "" if incoming is None else str(incoming)
            if kind == CONFIG_KIND_SELECT and not f.get("editable") and native not in f.get("options", []):
                raise ValueError(f"{path} 必须是 {f.get('options')} 之一")
            value_str, quote = native, True

        _set_by_path(raw_config, path, native)
        section_path = path.split(".")[:-1]
        key = path.split(".")[-1]
        replace_yaml_section_kv(config_path, section_path, key, value_str, raw_config, quote=quote)
        written.append(path)
    return {"written": written, "skipped": skipped}