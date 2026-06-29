"""WebUI 设置页 network 分组配置读写测试。

覆盖字段定义注册、serialize_config_group 掩码、apply_config_updates 往返写回、
select 白名单校验、secret 掩码跳过/空串清除语义。直接读写 config.yaml 原始内容,
不走 AppConfig,故用一份贴近真实 config.example.yaml 的 app 段作 fixtures。
"""

import yaml

from openhachimi_agent.core.config import (
    NETWORK_FIELDS,
    BROWSER_FIELDS,
    MEMORY_FIELDS,
    CONTEXT_FIELDS,
    SCHEDULER_FIELDS,
    RESEARCH_FIELDS,
    PATHS_LOGGING_FIELDS,
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


# ================================================================ browser 分组


_BROWSER_SAMPLE_YAML = """\
app:
  default_role: default
  browser_headless: true
  browser_channel: "chrome"
  browser_user_agent: ""
  browser_window_size: "1920,1080"
  browser_idle_timeout: 300
  browser_cdp_wait_seconds: 45
"""


def _write_browser_config(tmp_path, text=_BROWSER_SAMPLE_YAML):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(text, encoding="utf-8")
    return config_path


def test_browser_group_registered():
    """browser 分组已在 SETTINGS_FIELD_GROUPS 注册,与 BROWSER_FIELDS 同一对象。"""
    assert "browser" in SETTINGS_FIELD_GROUPS
    assert SETTINGS_FIELD_GROUPS["browser"] is BROWSER_FIELDS
    assert len(BROWSER_FIELDS) == 6


def test_browser_field_paths_under_app():
    for f in BROWSER_FIELDS:
        assert f["path"].startswith("app."), f


def test_browser_channel_is_editable_select():
    """browser_channel 是 select 但 editable=True——可填预设或绝对路径,后端放宽白名单。"""
    f = next(x for x in BROWSER_FIELDS if x["path"] == "app.browser_channel")
    assert f["kind"] == "select"
    assert f.get("editable") is True
    assert set(f["options"]) == {"chrome", "chromium", "msedge"}


def test_serialize_browser_fields(tmp_path):
    config_path = _write_browser_config(tmp_path)
    raw = load_raw_config(config_path)
    values, masked = serialize_config_group(BROWSER_FIELDS, raw)

    assert values["app.browser_headless"] is True
    assert values["app.browser_channel"] == "chrome"
    # 空 string 归一为 ""
    assert values["app.browser_user_agent"] == ""
    assert values["app.browser_window_size"] == "1920,1080"
    assert values["app.browser_idle_timeout"] == 300
    assert values["app.browser_cdp_wait_seconds"] == 45
    # browser 分组无 secret,masked 应为空
    assert masked == []


def test_apply_browser_writes_bool_int_and_string(tmp_path):
    config_path = _write_browser_config(tmp_path)
    raw = load_raw_config(config_path)
    updates = {
        "app.browser_headless": False,
        "app.browser_idle_timeout": 0,
        "app.browser_user_agent": "Mozilla/5.0 custom",
        "app.browser_window_size": "1280,720",
        "app.browser_cdp_wait_seconds": 90,
    }
    result = apply_config_updates(config_path, raw, BROWSER_FIELDS, updates)

    assert set(result["written"]) == set(updates)
    saved = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert saved["app"]["browser_headless"] is False
    assert saved["app"]["browser_idle_timeout"] == 0
    assert saved["app"]["browser_user_agent"] == "Mozilla/5.0 custom"
    assert saved["app"]["browser_window_size"] == "1280,720"
    assert saved["app"]["browser_cdp_wait_seconds"] == 90


def test_apply_browser_channel_accepts_preset(tmp_path):
    """editable select 接受预设值。"""
    config_path = _write_browser_config(tmp_path)
    raw = load_raw_config(config_path)
    result = apply_config_updates(config_path, raw, BROWSER_FIELDS, {"app.browser_channel": "msedge"})

    assert "app.browser_channel" in result["written"]
    saved = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert saved["app"]["browser_channel"] == "msedge"


def test_apply_browser_channel_accepts_arbitrary_path(tmp_path):
    """editable select 接受任意绝对路径(白名单被放宽)。"""
    config_path = _write_browser_config(tmp_path)
    raw = load_raw_config(config_path)
    custom_path = "/usr/bin/google-chrome"
    result = apply_config_updates(config_path, raw, BROWSER_FIELDS, {"app.browser_channel": custom_path})

    assert "app.browser_channel" in result["written"]
    saved = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert saved["app"]["browser_channel"] == custom_path


def test_apply_browser_channel_empty_string_clears(tmp_path):
    """browser_channel 提交空串表示"用内置浏览器"(清除)。"""
    config_path = _write_browser_config(tmp_path)
    raw = load_raw_config(config_path)
    result = apply_config_updates(config_path, raw, BROWSER_FIELDS, {"app.browser_channel": ""})

    assert "app.browser_channel" in result["written"]
    saved = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert saved["app"]["browser_channel"] == ""


def test_apply_browser_rejects_non_int_timeout(tmp_path):
    """browser_idle_timeout 必须是整数。"""
    import pytest

    config_path = _write_browser_config(tmp_path)
    raw = load_raw_config(config_path)
    with pytest.raises(ValueError):
        apply_config_updates(config_path, raw, BROWSER_FIELDS, {"app.browser_idle_timeout": "abc"})


def test_apply_browser_ignores_path_not_in_group(tmp_path):
    """非 browser 分组白名单的 path 被跳过。"""
    config_path = _write_browser_config(tmp_path)
    raw = load_raw_config(config_path)
    result = apply_config_updates(config_path, raw, BROWSER_FIELDS, {"app.server_port": 9999})

    assert "app.server_port" in result["skipped"]
    assert result["written"] == []


# ================================================================ memory 分组


# 多级嵌套 memory 段(flat + 4 个子段),贴近 config.example.yaml。
_MEMORY_SAMPLE_YAML = """\
memory:
  enabled: true
  db_path: .memory/long_term_memory.sqlite3
  embedding:
    enabled: true
    model: text-embedding-3-large
    base_url: ""
    api_key: "emb-secret-key"
    dimensions: 3072
    batch_size: 32
    timeout_seconds: 30
  recall:
    max_context_tokens: 1800
    bm25_top_k: 50
    vector_top_k: 50
    rrf_k: 60
    rerank_top_k: 24
    final_l1_top_k: 10
    final_l2_top_k: 4
    include_l3_profile: true
  capture:
    enabled: true
    async_enabled: true
    min_turn_chars: 20
    extract_timeout_seconds: 60
  privacy:
    pii_redaction: true
    allow_secret_memory: false
    raw_turn_retention_days: 180
"""


def _write_memory_config(tmp_path, text=_MEMORY_SAMPLE_YAML):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(text, encoding="utf-8")
    return config_path


def test_memory_group_registered():
    """memory 分组已在 SETTINGS_FIELD_GROUPS 注册,与 MEMORY_FIELDS 同一对象。"""
    assert "memory" in SETTINGS_FIELD_GROUPS
    assert SETTINGS_FIELD_GROUPS["memory"] is MEMORY_FIELDS
    # 总开关2 + embedding7 + recall8 + capture4 + privacy3 = 24
    assert len(MEMORY_FIELDS) == 24


def test_memory_field_paths_under_memory():
    """memory 字段 path 全部以 memory. 开头。"""
    for f in MEMORY_FIELDS:
        assert f["path"].startswith("memory."), f


def test_memory_groups_cover_five_subtabs():
    """5 个子组齐全:general / embedding / recall / capture / privacy。"""
    groups = {f["group"] for f in MEMORY_FIELDS}
    assert groups == {"memory-general", "memory-embedding", "memory-recall", "memory-capture", "memory-privacy"}


def test_memory_embedding_api_key_is_secret():
    """embedding.api_key 为 secret(前端脱敏 + 提交掩码跳过);其余 memory 字段非 secret。"""
    secrets = {f["path"] for f in MEMORY_FIELDS if f["kind"] == "secret"}
    assert secrets == {"memory.embedding.api_key"}


def test_serialize_memory_masks_embedding_api_key(tmp_path):
    config_path = _write_memory_config(tmp_path)
    raw = load_raw_config(config_path)
    values, masked = serialize_config_group(MEMORY_FIELDS, raw)

    # 非空 secret 被掩码并记入 masked
    assert "memory.embedding.api_key" in masked
    assert values["memory.embedding.api_key"] == mask_secret("emb-secret-key")
    # 空字符串 base_url 归一为 ""(string 非 secret,不入 masked)
    assert values["memory.embedding.base_url"] == ""
    assert "memory.embedding.base_url" not in masked
    # 多级嵌套字段正确取值
    assert values["memory.enabled"] is True
    assert values["memory.db_path"] == ".memory/long_term_memory.sqlite3"
    assert values["memory.recall.rrf_k"] == 60
    assert values["memory.recall.include_l3_profile"] is True
    assert values["memory.privacy.allow_secret_memory"] is False


def test_apply_memory_writes_nested_bool_and_int(tmp_path):
    """改嵌套段 bool/int 写回 yaml,保留注释与其它字段。"""
    config_path = _write_memory_config(tmp_path)
    raw = load_raw_config(config_path)
    updates = {
        "memory.enabled": False,
        "memory.recall.bm25_top_k": 100,
        "memory.recall.include_l3_profile": False,
        "memory.privacy.raw_turn_retention_days": 90,
    }
    result = apply_config_updates(config_path, raw, MEMORY_FIELDS, updates)

    assert set(result["written"]) == set(updates)
    saved = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert saved["memory"]["enabled"] is False
    assert saved["memory"]["recall"]["bm25_top_k"] == 100
    assert saved["memory"]["recall"]["include_l3_profile"] is False
    assert saved["memory"]["privacy"]["raw_turn_retention_days"] == 90
    # 未改动字段保留
    assert saved["memory"]["recall"]["vector_top_k"] == 50


def test_apply_memory_writes_nested_string(tmp_path):
    """改 db_path / model 等嵌套字符串字段写回。"""
    config_path = _write_memory_config(tmp_path)
    raw = load_raw_config(config_path)
    result = apply_config_updates(
        config_path,
        raw,
        MEMORY_FIELDS,
        {
            "memory.db_path": ".memory/alt.sqlite3",
            "memory.embedding.model": "text-embedding-3-small",
        },
    )

    assert set(result["written"]) == {"memory.db_path", "memory.embedding.model"}
    saved = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert saved["memory"]["db_path"] == ".memory/alt.sqlite3"
    assert saved["memory"]["embedding"]["model"] == "text-embedding-3-small"


def test_apply_memory_embedding_api_key_masked_skipped(tmp_path):
    """提交等于当前掩码的 embedding.api_key 视为未改动,跳过写回。"""
    config_path = _write_memory_config(tmp_path)
    raw = load_raw_config(config_path)
    current = raw["memory"]["embedding"]["api_key"]
    masked = mask_secret(current)

    result = apply_config_updates(config_path, raw, MEMORY_FIELDS, {"memory.embedding.api_key": masked})

    assert "memory.embedding.api_key" in result["skipped"]
    saved = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert saved["memory"]["embedding"]["api_key"] == current


def test_apply_memory_embedding_api_key_new_overwrites(tmp_path):
    """提交新明文覆盖原密钥。"""
    config_path = _write_memory_config(tmp_path)
    raw = load_raw_config(config_path)
    result = apply_config_updates(
        config_path, raw, MEMORY_FIELDS, {"memory.embedding.api_key": "new-emb-key"}
    )

    assert "memory.embedding.api_key" in result["written"]
    saved = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert saved["memory"]["embedding"]["api_key"] == "new-emb-key"


def test_apply_memory_embedding_api_key_empty_clears(tmp_path):
    """空串清除 embedding.api_key(留空回退复用 llm.api_key)。"""
    config_path = _write_memory_config(tmp_path)
    raw = load_raw_config(config_path)
    result = apply_config_updates(config_path, raw, MEMORY_FIELDS, {"memory.embedding.api_key": ""})

    assert "memory.embedding.api_key" in result["written"]
    saved = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert saved["memory"]["embedding"]["api_key"] == ""


def test_apply_memory_creates_missing_subsection(tmp_path):
    """写一个 yaml 里不存在的子段字段时,能自动补 section 并写回。"""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("memory:\n  enabled: true\n", encoding="utf-8")
    raw = load_raw_config(config_path)
    result = apply_config_updates(config_path, raw, MEMORY_FIELDS, {"memory.privacy.pii_redaction": False})

    assert "memory.privacy.pii_redaction" in result["written"]
    saved = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert saved["memory"]["privacy"]["pii_redaction"] is False


def test_apply_memory_rejects_non_int(tmp_path):
    """recall 整数字段非整数时抛 ValueError。"""
    import pytest

    config_path = _write_memory_config(tmp_path)
    raw = load_raw_config(config_path)
    with pytest.raises(ValueError):
        apply_config_updates(config_path, raw, MEMORY_FIELDS, {"memory.recall.rrf_k": "lots"})


def test_apply_memory_ignores_path_not_in_group(tmp_path):
    """非 memory 分组白名单的 path 被跳过(防越权写其它段)。"""
    config_path = _write_memory_config(tmp_path)
    raw = load_raw_config(config_path)
    result = apply_config_updates(config_path, raw, MEMORY_FIELDS, {"app.server_port": 9999})

    assert "app.server_port" in result["skipped"]
    assert result["written"] == []
# ================================================================ context 分组(含 float)


_CONTEXT_SAMPLE_YAML = """\
context:
  enabled: true
  engine: compressor
  threshold_percent: 0.75
  hard_ceiling_percent: 0.90
  protect_first_n: 3
  protect_last_n: 20
  tail_token_budget: 20000
  anti_thrash: true
  min_savings_pct: 10
  context_length: 128
"""


def _write_context_config(tmp_path, text=_CONTEXT_SAMPLE_YAML):
    p = tmp_path / "config.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def test_context_group_registered():
    assert "context" in SETTINGS_FIELD_GROUPS
    assert SETTINGS_FIELD_GROUPS["context"] is CONTEXT_FIELDS
    assert len(CONTEXT_FIELDS) == 10  # enabled/engine/threshold/hard_ceiling/protect_first/protect_last/tail/anti_thrash/min_savings/length


def test_context_threshold_is_float():
    f = next(x for x in CONTEXT_FIELDS if x["path"] == "context.threshold_percent")
    assert f["kind"] == "float"
    f2 = next(x for x in CONTEXT_FIELDS if x["path"] == "context.hard_ceiling_percent")
    assert f2["kind"] == "float"


def test_serialize_context_floats(tmp_path):
    p = _write_context_config(tmp_path)
    raw = load_raw_config(p)
    values, masked = serialize_config_group(CONTEXT_FIELDS, raw)
    assert values["context.threshold_percent"] == 0.75
    assert values["context.hard_ceiling_percent"] == 0.90
    assert values["context.enabled"] is True
    assert values["context.context_length"] == 128
    assert masked == []


def test_apply_context_float_round_trip(tmp_path):
    p = _write_context_config(tmp_path)
    raw = load_raw_config(p)
    result = apply_config_updates(
        p, raw, CONTEXT_FIELDS,
        {"context.threshold_percent": 0.80, "context.hard_ceiling_percent": 0.95, "context.protect_last_n": 30},
    )
    assert set(result["written"]) == {"context.threshold_percent", "context.hard_ceiling_percent", "context.protect_last_n"}
    saved = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert abs(saved["context"]["threshold_percent"] - 0.8) < 1e-9
    assert abs(saved["context"]["hard_ceiling_percent"] - 0.95) < 1e-9
    assert saved["context"]["protect_last_n"] == 30


def test_apply_context_rejects_non_float(tmp_path):
    import pytest
    p = _write_context_config(tmp_path)
    raw = load_raw_config(p)
    with pytest.raises(ValueError):
        apply_config_updates(p, raw, CONTEXT_FIELDS, {"context.threshold_percent": "abc"})


# ================================================================ scheduler 分组(嵌套 delivery/security)


_SCHEDULER_SAMPLE_YAML = """\
scheduler:
  enabled: true
  db_path: .scheduler/tasks.sqlite3
  poll_interval_seconds: 60
  max_concurrency: 2
  default_timeout_seconds: 300
  claim_lock_seconds: 600
"""


def _write_scheduler_config(tmp_path, text=_SCHEDULER_SAMPLE_YAML):
    p = tmp_path / "config.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def test_scheduler_group_registered():
    assert "scheduler" in SETTINGS_FIELD_GROUPS
    assert SETTINGS_FIELD_GROUPS["scheduler"] is SCHEDULER_FIELDS
    assert len(SCHEDULER_FIELDS) == 11  # main6 + delivery2 + security3


def test_scheduler_delivery_default_mode_is_select():
    f = next(x for x in SCHEDULER_FIELDS if x["path"] == "scheduler.delivery.default_mode")
    assert f["kind"] == "select"
    assert set(f["options"]) == {"origin", "inbox", "explicit", "none"}


def test_serialize_scheduler_defaults_when_missing(tmp_path):
    p = _write_scheduler_config(tmp_path)
    raw = load_raw_config(p)
    values, masked = serialize_config_group(SCHEDULER_FIELDS, raw)
    assert values["scheduler.enabled"] is True
    assert values["scheduler.delivery.default_mode"] == ""
    assert values["scheduler.security.prompt_scan_enabled"] is False
    assert masked == []


def test_apply_scheduler_creates_delivery_subsection(tmp_path):
    import pytest
    p = _write_scheduler_config(tmp_path)
    raw = load_raw_config(p)
    result = apply_config_updates(
        p, raw, SCHEDULER_FIELDS,
        {"scheduler.delivery.default_mode": "inbox", "scheduler.delivery.fallback_to_inbox": False},
    )
    assert set(result["written"]) == {"scheduler.delivery.default_mode", "scheduler.delivery.fallback_to_inbox"}
    saved = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert saved["scheduler"]["delivery"]["default_mode"] == "inbox"
    assert saved["scheduler"]["delivery"]["fallback_to_inbox"] is False
    with pytest.raises(ValueError):
        apply_config_updates(p, raw, SCHEDULER_FIELDS, {"scheduler.delivery.default_mode": "weird-mode"})


def test_apply_scheduler_security_writes(tmp_path):
    p = _write_scheduler_config(tmp_path)
    raw = load_raw_config(p)
    apply_config_updates(
        p, raw, SCHEDULER_FIELDS,
        {"scheduler.security.prompt_scan_enabled": False, "scheduler.security.allow_interactive_tools_in_scheduled_runs": True},
    )
    saved = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert saved["scheduler"]["security"]["prompt_scan_enabled"] is False
    assert saved["scheduler"]["security"]["allow_interactive_tools_in_scheduled_runs"] is True


# ================================================================ research 分组(含 multi / secret)


_RESEARCH_SAMPLE_YAML = """\
research:
  enabled_backends:
    - duckduckgo
  brave_api_key: "bk-xxxx"
  tavily_api_key: ""
  search_timeout_seconds: 15
  max_backend_results: 10
  min_independent_sources: 3
  require_citations: true
  browser_fallback_enabled: true
"""


def _write_research_config(tmp_path, text=_RESEARCH_SAMPLE_YAML):
    p = tmp_path / "config.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def test_research_group_registered():
    assert "research" in SETTINGS_FIELD_GROUPS
    assert SETTINGS_FIELD_GROUPS["research"] is RESEARCH_FIELDS
    assert len(RESEARCH_FIELDS) == 8


def test_research_enabled_backends_is_multi():
    f = next(x for x in RESEARCH_FIELDS if x["path"] == "research.enabled_backends")
    assert f["kind"] == "multi"
    assert set(f["options"]) == {"duckduckgo", "brave", "tavily"}


def test_research_keys_are_secret():
    secrets = {f["path"] for f in RESEARCH_FIELDS if f["kind"] == "secret"}
    assert secrets == {"research.brave_api_key", "research.tavily_api_key"}


def test_serialize_research_multi_and_mask(tmp_path):
    p = _write_research_config(tmp_path)
    raw = load_raw_config(p)
    values, masked = serialize_config_group(RESEARCH_FIELDS, raw)
    assert values["research.enabled_backends"] == ["duckduckgo"]
    assert "research.brave_api_key" in masked
    assert values["research.brave_api_key"] == mask_secret("bk-xxxx")
    assert "research.tavily_api_key" not in masked
    assert values["research.tavily_api_key"] == ""


def test_serialize_research_multi_filters_unknown(tmp_path):
    text = "research:\n  enabled_backends:\n    - duckduckgo\n    - unkn0wn\n"
    p = _write_research_config(tmp_path, text)
    raw = load_raw_config(p)
    values, _ = serialize_config_group(RESEARCH_FIELDS, raw)
    assert values["research.enabled_backends"] == ["duckduckgo"]


def test_apply_research_multi_writes_list(tmp_path):
    p = _write_research_config(tmp_path)
    raw = load_raw_config(p)
    result = apply_config_updates(
        p, raw, RESEARCH_FIELDS,
        {"research.enabled_backends": ["duckduckgo", "tavily"]},
    )
    assert "research.enabled_backends" in result["written"]
    text = p.read_text(encoding="utf-8")
    assert "enabled_backends:" in text
    assert "- duckduckgo" in text
    assert "- tavily" in text
    saved = yaml.safe_load(text)
    assert saved["research"]["enabled_backends"] == ["duckduckgo", "tavily"]


def test_apply_research_multi_reorders_by_options(tmp_path):
    p = _write_research_config(tmp_path)
    raw = load_raw_config(p)
    apply_config_updates(p, raw, RESEARCH_FIELDS, {"research.enabled_backends": ["tavily", "duckduckgo"]})
    saved = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert saved["research"]["enabled_backends"] == ["duckduckgo", "tavily"]


def test_apply_research_multi_drops_unknown(tmp_path):
    p = _write_research_config(tmp_path)
    raw = load_raw_config(p)
    apply_config_updates(p, raw, RESEARCH_FIELDS, {"research.enabled_backends": ["duckduckgo", "hacker-search"]})
    saved = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert saved["research"]["enabled_backends"] == ["duckduckgo"]


def test_apply_research_multi_empty_writes_empty(tmp_path):
    p = _write_research_config(tmp_path)
    raw = load_raw_config(p)
    apply_config_updates(p, raw, RESEARCH_FIELDS, {"research.enabled_backends": []})
    saved = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert saved["research"]["enabled_backends"] in ([], None)


def test_apply_research_secret_masked_skipped(tmp_path):
    p = _write_research_config(tmp_path)
    raw = load_raw_config(p)
    current = raw["research"]["brave_api_key"]
    result = apply_config_updates(p, raw, RESEARCH_FIELDS, {"research.brave_api_key": mask_secret(current)})
    assert "research.brave_api_key" in result["skipped"]
    saved = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert saved["research"]["brave_api_key"] == current


def test_apply_research_secret_new_overwrites(tmp_path):
    p = _write_research_config(tmp_path)
    raw = load_raw_config(p)
    apply_config_updates(p, raw, RESEARCH_FIELDS, {"research.tavily_api_key": "tv-new"})
    saved = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert saved["research"]["tavily_api_key"] == "tv-new"


# ================================================================ paths-logging 分组


_PATHS_LOGGING_SAMPLE_YAML = """\
paths:
  roles_dir: user/roles
  memory_dir: .memory
  external_skills_dir: ""
  attachments_dir: .tmp/attachments
logging:
  level: INFO
  dir: .logs
  console: false
"""


def _write_paths_logging_config(tmp_path, text=_PATHS_LOGGING_SAMPLE_YAML):
    p = tmp_path / "config.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def test_paths_logging_group_registered():
    assert "paths-logging" in SETTINGS_FIELD_GROUPS
    assert SETTINGS_FIELD_GROUPS["paths-logging"] is PATHS_LOGGING_FIELDS
    assert len(PATHS_LOGGING_FIELDS) == 7  # paths4 + logging3


def test_logging_level_is_select_with_levels():
    f = next(x for x in PATHS_LOGGING_FIELDS if x["path"] == "logging.level")
    assert f["kind"] == "select"
    assert f["options"] == ["DEBUG", "INFO", "WARNING", "ERROR"]


def test_serialize_paths_logging(tmp_path):
    p = _write_paths_logging_config(tmp_path)
    raw = load_raw_config(p)
    values, masked = serialize_config_group(PATHS_LOGGING_FIELDS, raw)
    assert values["paths.roles_dir"] == "user/roles"
    assert values["paths.external_skills_dir"] == ""
    assert values["logging.level"] == "INFO"
    assert values["logging.console"] is False
    assert masked == []


def test_apply_paths_logging_writes(tmp_path):
    p = _write_paths_logging_config(tmp_path)
    raw = load_raw_config(p)
    apply_config_updates(
        p, raw, PATHS_LOGGING_FIELDS,
        {"logging.level": "DEBUG", "logging.console": True, "paths.memory_dir": ".memory2"},
    )
    saved = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert saved["logging"]["level"] == "DEBUG"
    assert saved["logging"]["console"] is True
    assert saved["paths"]["memory_dir"] == ".memory2"


def test_apply_paths_logging_rejects_unknown_level(tmp_path):
    import pytest
    p = _write_paths_logging_config(tmp_path)
    raw = load_raw_config(p)
    with pytest.raises(ValueError):
        apply_config_updates(p, raw, PATHS_LOGGING_FIELDS, {"logging.level": "TRACE"})
