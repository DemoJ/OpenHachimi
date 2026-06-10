import json
from pathlib import Path

from openhachimi_agent.core.config import load_mcp_config


def test_load_mcp_config_reads_http_headers(tmp_path: Path):
    user_dir = tmp_path / "user"
    user_dir.mkdir()
    (user_dir / "mcp-servers.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "luckin": {
                        "url": "https://example.test/mcp",
                        "headers": {
                            "Authorization": "Bearer test-token",
                            "X-Number": 123,
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    config = load_mcp_config(user_dir)

    server = config.servers["luckin"]
    assert server.type == "http"
    assert server.url == "https://example.test/mcp"
    assert server.headers == {
        "Authorization": "Bearer test-token",
        "X-Number": "123",
    }


def test_load_mcp_config_leaves_missing_headers_as_none(tmp_path: Path):
    user_dir = tmp_path / "user"
    user_dir.mkdir()
    (user_dir / "mcp-servers.json").write_text(
        json.dumps({"mcpServers": {"local": {"url": "http://127.0.0.1:8000/mcp"}}}),
        encoding="utf-8",
    )

    config = load_mcp_config(user_dir)

    assert config.servers["local"].headers is None
