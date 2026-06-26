"""WebUI 设置页 network 分组配置读写测试。

覆盖字段定义注册、serialize_config_group 掩码、apply_config_updates 往返写回、
select 白名单校验、secret 掩码跳过/空串清除语义。直接读写 config.yaml 原始内容,
不走 AppConfig,故用一份贴近真实 config.example.yaml 的 app 段作 fixtures。
"""

import yaml

from openhachimi_agent.core.config import (
    NETWORK_FIELDS,
    SETTINGS_FIELD_GROUPS,
    apply_config_updates,
    load_raw_config,
    mask_secret,
    serialize_config_group,
)

# 一份贴近 user/config.example.yaml 的最小 app 段,含 network 分组全部字段。
_SAMPLE_YAML = """\
app:
  default_role: default
  telegram_bot_token: "123456:ABC-DEF"   # bot token
  telegram_proxy_url: ""
  http_api_token: "existing-secret-token"
  server_host: 127.0.0.1
  server_port: 8765
  show_tool_calls: true
  stream_idle_timeout_seconds: 60
  max_attachment_size_mb: 50
llm:
  api_key: sk-test
"""


def _write_config(tmp_path, text=_SAMPLE_YAML):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(text, encoding="utf-8")
    return config_path


# ---------------------------------------------------------------- 字段定义注册


def test_network_group_registered():
    """network 分组已在 SETTINGS_FIELD_GROUPS 注册,字段非空。"""
    assert "network" in SETTINGS_FIELD_GROUPS
    assert SETTINGS_FIELD_GROUPS["network"] is NETWORK_FIELDS
    assert len(NETWORK_FIELDS) == 8


def test_network_field_paths_under_app():
    """network 字段 path 全部以 app. 开头(本分组只暴露 app 段配置)。"""
    for f in NETWORK_FIELDS:
        assert f["path"].startswith("app."), f


def test_server_host_is_select_with_safe_options():
    """server_host 用 select,选项限定 127.0.0.1 / 0.0.0.0,防止误开公网。"""
    f = next(x for x in NETWORK_FIELDS if x["path"] == "app.server_host")
    assert f["kind"] == "select"
    assert f["options"] == ["127.0.0.1", "0.0.0.0"]


def test_secrets_marked_secret_kind():
    """http_api_token、telegram_bot_token 为 secret(前端脱敏 + 提交掩码跳过)。"""
    kinds = {f["path"]: f["kind"] for f in NETWORK_FIELDS}
    assert kinds["app.http_api_token"] == "secret"
    assert kinds["app.telegram_bot_token"] == "secret"


# ---------------------------------------------------------------- serialize 掩码


def test_serialize_masks_nonempty_secrets(tmp_path):
    config_path = _write_config(tmp_path)
    raw = load_raw_config(config_path)
    values, masked = serialize_config_group(NETWORK_FIELDS, raw)

    # 非空 secret 应被掩码并记入 masked
    assert "app.http_api_token" in masked
    assert values["app.http_api_token"] == mask_secret("existing-secret-token")
    assert "app.telegram_bot_token" in masked
    assert values["app.telegram_bot_token"] == mask_secret("123456:ABC-DEF")

    # 空 secret(telegram_proxy_url 是 string 不是 secret)不在 masked
    assert "app.telegram_proxy_url" not in masked

    # select 字段:yaml 里 127.0.0.1 是裸值(非 IP 数字),解析为字符串
    assert values["app.server_host"] == "127.0.0.1"
    # bool / int 正常反序列化
    assert values["app.show_tool_calls"] is True
    assert values["app.server_port"] == 8765
    assert values["app.max_attachment_size_mb"] == 50


# ---------------------------------------------------------------- apply 往返


def test_apply_writes_int_bool_and_select(tmp_path):
    """改端口/开关/监听地址后写回 yaml,保留注释,读回值正确。"""
    config_path = _write_config(tmp_path)
    raw = load_raw_config(config_path)
    updates = {
        "app.server_port": 9000,
        "app.show_tool_calls": False,
        "app.server_host": "0.0.0.0",
        "app.stream_idle_timeout_seconds": 120,
    }
    result = apply_config_updates(config_path, raw, NETWORK_FIELDS, updates)

    assert set(result["written"]) == set(updates)
    assert result["skipped"] == []

    # 注释保留(bot token 行的 # bot token 仍在)
    text = config_path.read_text(encoding="utf-8")
    assert "# bot token" in text

    saved = yaml.safe_load(text)
    assert saved["app"]["server_port"] == 9000
    assert saved["app"]["show_tool_calls"] is False
    assert saved["app"]["server_host"] == "0.0.0.0"
    assert saved["app"]["stream_idle_timeout_seconds"] == 120


def test_apply_rejects_select_outside_options(tmp_path):
    """server_host 值不在 options 白名单时抛 ValueError(防止误填公网 IP)。"""
    import pytest

    config_path = _write_config(tmp_path)
    raw = load_raw_config(config_path)
    with pytest.raises(ValueError):
        apply_config_updates(config_path, raw, NETWORK_FIELDS, {"app.server_host": "192.168.1.10"})


def test_apply_secret_masked_value_is_skipped(tmp_path):
    """提交等于当前掩码的 secret 视为未改动,跳过写回(不覆盖真实 token)。"""
    config_path = _write_config(tmp_path)
    raw = load_raw_config(config_path)
    current = raw["app"]["http_api_token"]
    masked = mask_secret(current)

    result = apply_config_updates(config_path, raw, NETWORK_FIELDS, {"app.http_api_token": masked})

    assert "app.http_api_token" in result["skipped"]
    # 文件里仍是原 token,未被掩码覆盖
    saved = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert saved["app"]["http_api_token"] == current


def test_apply_secret_empty_string_clears(tmp_path):
    """secret 提交空串代表清除(留空回退/停用 bot)。"""
    config_path = _write_config(tmp_path)
    raw = load_raw_config(config_path)
    result = apply_config_updates(config_path, raw, NETWORK_FIELDS, {"app.telegram_bot_token": ""})

    assert "app.telegram_bot_token" in result["written"]
    saved = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert saved["app"]["telegram_bot_token"] == ""


def test_apply_secret_new_value_overwrites(tmp_path):
    """secret 提交与掩码不同的新值时,写回新明文。"""
    config_path = _write_config(tmp_path)
    raw = load_raw_config(config_path)
    result = apply_config_updates(
        config_path, raw, NETWORK_FIELDS, {"app.http_api_token": "brand-new-token"}
    )

    assert "app.http_api_token" in result["written"]
    saved = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert saved["app"]["http_api_token"] == "brand-new-token"


def test_apply_ignores_path_not_in_group(tmp_path):
    """非本分组白名单的 path 被跳过,不写回(防越权改其它段配置)。"""
    config_path = _write_config(tmp_path)
    raw = load_raw_config(config_path)
    result = apply_config_updates(
        config_path, raw, NETWORK_FIELDS, {"llm.api_key": "sk-injected"}
    )

    assert "llm.api_key" in result["skipped"]
    assert result["written"] == []
    saved = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert saved["llm"]["api_key"] == "sk-test"
