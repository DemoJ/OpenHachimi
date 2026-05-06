# OpenHachimi Agent

这是一个基于 [PydanticAI](https://github.com/pydantic/pydantic-ai)
构建的中文命令行 Agent 项目。

## 本地初始化

建议使用 Python 3.10 及以上版本。

1. 创建虚拟环境

Windows PowerShell:

```powershell
python -m venv .venv
```

Linux / macOS:

```bash
python3 -m venv .venv
```

2. 激活虚拟环境

Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
```

Windows CMD:

```bat
.\.venv\Scripts\activate.bat
```

Linux / macOS:

```bash
source .venv/bin/activate
```

3. 安装依赖

```bash
python -m pip install -U pip
python -m pip install -e .
```

4. 复制配置文件

Windows PowerShell:

```powershell
Copy-Item user/config.example.yaml user/config.yaml
```

Linux / macOS:

```bash
cp user/config.example.yaml user/config.yaml
```

然后在 `user/config.yaml` 中填写你的 API Key。

`user/config.yaml` 示例：

```yaml
app:
  default_role: default

llm:
  api_key: sk-xxxxxxxx
  model: gpt-5.2
  base_url: https://your-openai-compatible-server.example/v1

paths:
  roles_dir: user/roles
  memory_dir: .memory

logging:
  level: INFO
  dir: .logs
  console: false
```

如果你要接自定义网关、代理服务，或者兼容 OpenAI 接口的模型服务，可以设置 `llm.base_url`。

示例：

```yaml
llm:
  base_url: https://your-openai-compatible-server.example/v1
```

## 一键部署

### Linux 一键部署（推荐）

在 Linux 环境下，直接运行项目根目录的 Shell 脚本即可完成全部部署流程：

```bash
bash deploy.sh
```

脚本会依次自动完成：

1. **检查 Python 版本**（需要 3.10+，否则给出安装提示）
2. **创建或复用 `.venv` 虚拟环境**
3. **安装项目依赖**（`pip install -e .`）
4. **初始化配置文件**：如果 `user/config.yaml` 不存在，从模板复制一份并提示填写 API Key
5. **部署后台守护服务**：在支持 systemd 的环境下注册 `systemd --user` 服务

**常用选项：**

```bash
# 指定监听地址和端口
bash deploy.sh --host 0.0.0.0 --port 9000

# 只安装依赖，不启动后台守护服务
bash deploy.sh --skip-daemon

# 查看所有可用选项
bash deploy.sh --help
```

部署完成后，将虚拟环境加入 PATH（可选，一次性操作）：

```bash
echo 'export PATH="$PWD/.venv/bin:$PATH"' >> ~/.bashrc && source ~/.bashrc
```

之后就可以直接使用 `hachimi` 命令。

---

### 跨平台 Python 部署脚本

项目同时提供跨平台的 Python 部署入口，适用于 Windows / Linux / macOS：

```bash
python deploy.py
```

部署脚本默认会：

- 创建或复用 `.venv`
- 执行 `pip install -e .`
- 如果缺少 `user/config.yaml`，从 `user/config.example.yaml` 复制一份
- 调用 `hachimi deploy` 部署后台守护服务

如果只想安装命令，不启动后台守护：

```bash
python deploy.py --skip-daemon
```

如果要修改监听地址：

```bash
python deploy.py --host 127.0.0.1 --port 8765
```

## 运行方式

安装后会提供 `hachimi` 和 `Hachimi` 两个命令入口。

一键部署并启动后台守护服务：

```bash
hachimi deploy
```

查看当前版本：

```bash
hachimi --version
```

检查并更新到最新版本：

```bash
hachimi update
```

进入 CLI 对话：

```bash
hachimi
```

CLI 中模型回复会边生成边输出，不需要等待完整响应结束。

日志默认写入 `.logs/openhachimi.log`。如果需要在终端同时查看日志，可以把 `user/config.yaml` 中的 `logging.console` 改成 `true`。

`deploy` 在 Linux + systemd 环境下会创建并启动 `systemd --user` 服务；其他环境会在项目目录生成本地后台启动脚本。

如果要手动启动后台服务：

```bash
hachimi serve
```

再打开另一个终端进入 CLI：

```bash
hachimi cli
```

`serve` 默认监听 `127.0.0.1:8765`，CLI 默认连接 `http://127.0.0.1:8765`。

开发时也可以直接运行单进程 CLI，不需要先启动后台服务：

```bash
python main.py
```

如果要修改地址：

```bash
hachimi deploy --host 127.0.0.1 --port 8765
OPENHACHIMI_SERVER_URL=http://127.0.0.1:8765 hachimi
```

单独运行 `python main.py` 会启动开发用的单进程 CLI；运行 `python main.py cli` 会连接已启动的后台服务。

## 命令说明

- `hachimi --version`：查看当前版本号
- `hachimi update`：检查远程版本并自动更新
- `/help`：查看帮助信息
- `/roles`：查看当前可用角色
- `/role default`：切换到指定角色，并为该角色新建一段空白对话
- `/new`：保存当前对话，并为当前角色新建一段空白对话
- `/exit`：退出程序

## 角色配置

角色配置统一放在 [user/roles](./user/roles/) 目录中，每个角色对应一个 Markdown 文件。

- [default.md](./user/roles/default.md)：默认中文助手
- [code_assistant.md](./user/roles/code_assistant.md)：偏工程实现的代码助手

你可以直接新增更多 `.md` 文件，例如 `product_manager.md`、`translator.md`、`reviewer.md`。这些文件只需要描述角色的人设、目标和输出风格。

系统提示词随当前版本内置在程序包中，不放在 `user/` 目录中。


## 持久化记忆

- 后台服务负责维护当前角色、当前会话和模型调用状态
- CLI 只是连接本机 `localhost` 服务的客户端，可以随时退出再进入
- 会话历史按“角色 + 对话”单独保存到 [`.memory`](./.memory/) 目录下
- 每个角色可以拥有多段历史对话，互不覆盖
- 程序启动时会自动恢复默认角色最近一次对话
- 使用 `/role <名称>` 切换角色时，会保存原对话，并为目标角色新建一段空白对话
- 使用 `/new` 会保存当前对话，并为当前角色新建一段空白对话
- 旧版 `.memory/<角色>.json` 历史文件仍会被兼容读取

这部分持久化能力基于 `pydantic-ai` 的消息历史接口实现：运行时传入 `message_history`，保存时使用 `all_messages_json()`，恢复时使用 `ModelMessagesTypeAdapter.validate_json(...)`

## 工具调用

当前项目使用 `pydantic-ai` 自带的 `FunctionToolset` 为 Agent 注册了几项基础工具：

- `list_files`：列出工作区目录内容
- `find_files`：按文件名或 glob 模式查找文件/目录
- `search_text`：在文本文件中搜索字符串
- `read_file`：读取文件内容，支持指定行范围
- `write_file`：新建或覆盖写入文件
- `make_directory`：创建目录
- `replace_in_file`：替换文件中的文本片段
- `run_command`：在工作区内执行非交互式系统命令
- `git_status`：查看当前 Git 状态
- `git_diff`：查看 Git diff

这些工具全部限制在当前工作区内，不能访问工作区外的路径。
其中 `run_command` 会按当前系统自动选择 shell：
- Windows 下使用 `pwsh` 或 `powershell`
- Linux/macOS 下使用当前 `SHELL` 或回退到 `/bin/sh`

使用 `run_command` 时，Agent 还会按内置系统提示优先判断当前平台，再选择对应命令语法。

它还额外做了基础安全限制，会拒绝明显危险的删除或强制清理命令。

## 后台守护

`hachimi deploy` 会优先使用当前用户的 systemd 服务，不需要手写 service 文件。部署后可用这些命令管理：

```bash
systemctl --user status openhachimi
systemctl --user restart openhachimi
journalctl --user -u openhachimi -f
```

如果服务器要求用户退出后仍保持 user service 运行，可以开启 linger：

```bash
sudo loginctl enable-linger $USER
```

在没有 systemd 的环境中，`hachimi deploy` 会生成 `openhachimi-serve.sh` 或 `openhachimi-serve.bat`，运行该脚本即可启动后台服务。

## 跨平台说明

- 项目本身按跨平台方式组织，Windows、Linux、macOS 都可以运行。
- Python 环境、虚拟环境路径和 shell 命令会因系统不同而不同，因此 README 对这些步骤分别给了示例。
- Agent 的 `run_command` 工具会根据当前系统自动选择合适的 shell，但具体执行的命令内容仍然需要符合目标平台语法。
