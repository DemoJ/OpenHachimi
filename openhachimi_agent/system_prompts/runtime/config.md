## 配置文件
当前应用的用户配置目录位于：`{{ user_dir }}`
- 主配置文件: `{{ user_dir }}/config.yaml`
- MCP服务器配置: `{{ user_dir }}/mcp-servers.json` (若不存在则创建，格式须兼容 Claude Desktop，即包含 `mcpServers` 根节点)
当你需要配置 MCP 服务器或修改应用配置时，请直接使用该目录下的相应文件。
