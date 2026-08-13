from dataclasses import replace

from openhachimi_agent.core.config import MCPConfig, MCPServerConfig
from openhachimi_agent.tools import mcp as mcp_module
from openhachimi_agent.tools.mcp import load_mcp_toolsets


class FakeHTTPTransport:
    calls = []

    def __init__(self, url, *, headers=None):
        self.url = url
        self.headers = headers
        self.calls.append((url, headers))


class FakeMCPToolset:
    def __init__(self, *, client):
        self.client = client


def test_load_mcp_toolsets_passes_http_headers(mock_config, monkeypatch):
    FakeHTTPTransport.calls = []
    monkeypatch.setattr(mcp_module, "StreamableHttpTransport", FakeHTTPTransport)
    monkeypatch.setattr(mcp_module, "MCPToolset", FakeMCPToolset)
    config = replace(
        mock_config,
        mcp=MCPConfig(
            servers={
                "luckin": MCPServerConfig(
                    type="http",
                    url="https://example.test/mcp",
                    headers={"Authorization": "Bearer test-token"},
                )
            }
        ),
    )

    toolsets = load_mcp_toolsets(config)

    assert len(toolsets) == 1
    assert FakeHTTPTransport.calls == [
        ("https://example.test/mcp", {"Authorization": "Bearer test-token"})
    ]