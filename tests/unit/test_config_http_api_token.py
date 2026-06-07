import yaml

from openhachimi_agent.core.config import _ensure_http_api_token


def test_ensure_http_api_token_generates_when_missing(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "app:\n"
        "  default_role: default\n"
        "llm:\n"
        "  api_key: sk-test\n",
        encoding="utf-8",
    )
    raw_config = yaml.safe_load(config_path.read_text(encoding="utf-8"))

    token = _ensure_http_api_token(config_path, raw_config)

    saved = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert token
    assert raw_config["app"]["http_api_token"] == token
    assert saved["app"]["http_api_token"] == token


def test_ensure_http_api_token_generates_when_blank(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "app:\n"
        "  http_api_token: \"\"  # 自动生成\n"
        "  default_role: default\n",
        encoding="utf-8",
    )
    raw_config = yaml.safe_load(config_path.read_text(encoding="utf-8"))

    token = _ensure_http_api_token(config_path, raw_config)

    text = config_path.read_text(encoding="utf-8")
    saved = yaml.safe_load(text)
    assert token
    assert saved["app"]["http_api_token"] == token
    assert "# 自动生成" in text


def test_ensure_http_api_token_keeps_existing_token(tmp_path):
    config_path = tmp_path / "config.yaml"
    original = "existing-token"
    config_path.write_text(
        "app:\n"
        f"  http_api_token: {original}\n"
        "  default_role: default\n",
        encoding="utf-8",
    )
    before = config_path.read_text(encoding="utf-8")
    raw_config = yaml.safe_load(before)

    token = _ensure_http_api_token(config_path, raw_config)

    assert token == original
    assert config_path.read_text(encoding="utf-8") == before
