# OpenHachimi Agent

基于 [PydanticAI](https://github.com/pydantic/pydantic-ai) 构建的中文命令行 AI Agent。

---

> 📖 **普通用户请看 [用户指南 (USER_GUIDE.md)](USER_GUIDE.md)**：详细的安装、配置和功能说明，适合非技术人员阅读。
> 
> 技术细节和架构设计请继续阅读本文档和 [ARCHITECTURE.md](ARCHITECTURE.md)。

---

## 快速开始（一键部署）

> 只需一条命令，脚本自动完成 clone、安装依赖、初始化配置、部署后台服务。

### Linux / macOS

```bash
curl -fsSL https://raw.githubusercontent.com/DemoJ/OpenHachimi/main/install.sh | bash
```

### Windows（PowerShell）

```powershell
Invoke-WebRequest -Uri https://raw.githubusercontent.com/DemoJ/OpenHachimi/main/deploy.py -OutFile deploy.py
python deploy.py
```

部署完成后，**编辑 `OpenHachimi/user/config.yaml`，填写你的 API Key**，然后运行：

```bash
hachimi       # 进入 CLI 对话（需先将 .venv/bin 加入 PATH，见部署完成提示）
```

> **常用部署选项**（在项目目录内运行）
>
> ```bash
> # 指定监听地址和端口
> bash deploy.sh --host 0.0.0.0 --port 9000
>
> # 只安装，不启动后台守护服务
> bash deploy.sh --skip-daemon
>
> # 查看所有选项
> bash deploy.sh --help
> ```

---

## 手动部署

如果你想自己控制每一步，或者已经 clone 了仓库：

```bash
git clone https://github.com/DemoJ/OpenHachimi.git
cd OpenHachimi
```

**1. 创建并激活虚拟环境**

```bash
# Linux / macOS
python3 -m venv .venv && source .venv/bin/activate

# Windows PowerShell
python -m venv .venv; .\.venv\Scripts\Activate.ps1
```

**2. 安装依赖**

```bash
pip install -U pip
pip install -e .
```

**3. 初始化配置**

```bash
# Linux / macOS
cp user/config.example.yaml user/config.yaml

# Windows PowerShell
Copy-Item user/config.example.yaml user/config.yaml
```

编辑 `user/config.yaml`，填写 `llm.api_key`（及可选的 `base_url`）：

```yaml
llm:
  api_key: sk-xxxxxxxx
  model: gpt-4o
  base_url: https://your-openai-compatible-server.example/v1  # 可选
```

**4. 部署后台守护服务**

```bash
hachimi deploy
```

---

## 使用

```bash
hachimi          # 进入 CLI 对话
hachimi --version  # 查看版本
hachimi update     # 检查并更新到最新版本
```

### 微信渠道接入

OpenHachimi 支持通过微信与 Agent 对话。

**配置步骤**：

1. **登录微信**：
   ```bash
   hachimi weixin
   ```
   扫描二维码完成登录，凭证自动保存到 `.memory/weixin_account.json`

2. **启动服务**：
   ```bash
   hachimi serve
   ```
   也可以先启动服务再运行 `hachimi weixin`，服务会自动检测新凭证并启动微信渠道。

3. **使用**：
   - 给登录的微信号发送消息，Agent 会自动回复
   - 支持私聊和群聊（群聊中需要 @机器人）
   - 支持语音转写、图片识别，以及文件/视频附件接收
   - 回复时会显示"正在输入..."状态

**注意事项**：
- 微信登录凭证会过期，过期后需重新运行 `hachimi weixin`
- 服务运行期间重新登录后，微信渠道会自动加载新的凭证
- 微信渠道暂不支持直接上传 Agent 生成的文件，会在回复中提供生成文件路径和 HTTP 下载路径
- 同一时间只能有一个客户端使用该微信账号

手动分步启动（适用于开发调试）：

```bash
hachimi serve    # 启动后台服务（默认 127.0.0.1:8765）
hachimi cli      # 新开终端，连接后台进入对话
```

CLI 内置命令：

| 命令 | 说明 |
|------|------|
| `/help` | 查看帮助 |
| `/roles` | 列出所有可用角色 |
| `/role <名称>` | 切换角色（保存当前对话，新建空白对话） |
| `/new` | 当前角色新建空白对话 |
| `/exit` | 退出 |

---

## 配置说明

配置文件：`user/config.yaml`（从 `user/config.example.yaml` 复制而来）。`app.http_api_token` 为空时，启动/重启 HTTP 服务会自动生成并写回配置文件；除 `/health` 外的 HTTP API 请求都需要携带 `Authorization: Bearer <token>`。

```yaml
app:
  default_role: default          # 启动时加载的默认角色
  http_api_token: ""            # HTTP API Token；留空时启动/重启会自动生成并写回配置文件

llm:
  api_key: sk-xxxxxxxx           # 必填：你的 API Key
  model: gpt-4o                  # 模型名称
  base_url: https://...          # 可选：兼容 OpenAI 接口的代理/网关
  supports_vision: auto          # 图片能力：auto / true / false

vision:
  enabled: true                  # 是否处理 Telegram/HTTP 图片附件
  fallback_enabled: true         # 主模型不支持图片时，调用辅助视觉模型识别
  model: ""                    # 辅助视觉模型；可留空。留空且主模型不支持图片时，仅告知主模型无法识别图片
  base_url: ""
  api_key: ""
  detail: auto                   # image_url.detail：auto / low / high
  max_images_per_message: 4
  max_image_size_mb: 10

paths:
  roles_dir: user/roles          # 角色配置目录
  memory_dir: .memory            # 对话记忆存储目录

logging:
  level: INFO
  dir: .logs
  console: false                 # 改为 true 可在终端同时输出日志

context:                          # 对话历史上下文压缩（防止长会话爆上下文）
  enabled: true
  threshold_percent: 0.75        # 真实用量达此比例时自动压缩
  hard_ceiling_percent: 0.90     # 粗略估计达此比例时轮内预检压缩
  protect_first_n: 3             # 始终保留的开头消息数
  protect_last_n: 20             # 尾部最少保留消息数
  tail_token_budget: 20000       # 尾部 token 预算
  rescue_to_memory: true         # 压缩丢弃内容抢救到记忆库（可召回找回）
  summary:                       # 摘要压缩辅助模型；留空则用主模型
    model: ""
    abort_on_failure: false      # 摘要失败：false=插兜底摘要，true=中止压缩
```

> 上下文压缩默认开启。长会话接近模型上下文上限时自动压缩历史，被压缩丢弃的内容会抢救到记忆库、后续仍可通过长期记忆召回。也可随时用 `/compress [主题]` 手动压缩。详见 [ARCHITECTURE.md](ARCHITECTURE.md#上下文管理)。

---

## 角色配置

角色文件放在 `user/roles/` 目录，每个 `.md` 文件对应一个角色：

- `default.md`：默认中文助手
- `code_assistant.md`：代码工程助手

新增角色：直接在 `user/roles/` 中新建 `.md` 文件，描述角色人设、目标和输出风格即可。

---

## 后台守护服务管理（Linux）

```bash
systemctl --user status openhachimi    # 查看状态
systemctl --user restart openhachimi   # 重启
journalctl --user -u openhachimi -f    # 实时查看日志
```

用户退出后保持服务运行（可选）：

```bash
sudo loginctl enable-linger $USER
```

在没有 systemd 的环境中，`hachimi deploy` 会生成本地启动脚本（`openhachimi-serve.sh` / `openhachimi-serve.bat`）。

---

## 工具能力

Agent 内置以下工具，均限制在当前工作区内：

| 工具 | 说明 |
|------|------|
| `list_files` | 列出目录内容 |
| `find_files` | 按名称或 glob 查找文件 |
| `search_text` | 在文本中搜索字符串 |
| `read_file` | 读取文件内容（支持指定行范围） |
| `write_file` | 新建或覆盖写入文件 |
| `replace_in_file` | 替换文件中的文本片段 |
| `make_directory` | 创建目录 |
| `run_command` | 执行非交互式系统命令（自动选择平台 shell） |
| `git_status` / `git_diff` | 查看 Git 状态和变更 |
