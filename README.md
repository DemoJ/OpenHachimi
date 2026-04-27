# OpenHachimi Agent

这是一个基于 [PydanticAI](https://github.com/pydantic/pydantic-ai)
构建的中文命令行 Agent 项目。

## 本地初始化

建议使用 Python 3.10 及以上版本。

```powershell
C:\Users\diyun-pc\.pyenv\pyenv-win\versions\3.13.2\python.exe -m venv .venv
.\.venv\Scripts\python.exe -m pip install -U pip
.\.venv\Scripts\python.exe -m pip install -e .
Copy-Item .env.example .env
```

然后在 `.env` 中填写你的 API Key。

`.env` 示例：

```env
OPENAI_API_KEY=请在这里填写你的_OpenAI_API_Key
OPENAI_MODEL=gpt-5.2
OPENAI_BASE_URL=
OPENHACHIMI_ROLE=default
```

如果你要接自定义网关、代理服务，或者兼容 OpenAI 接口的模型服务，可以设置 `OPENAI_BASE_URL`。

示例：

```env
OPENAI_BASE_URL=https://your-openai-compatible-server.example/v1
```

## 运行方式

```powershell
.\.venv\Scripts\python.exe .\main.py
```

启动后你可以直接在终端里连续对话，程序会自动保留当前会话上下文。

## 命令说明

- `/help`：查看帮助信息
- `/roles`：查看当前可用角色
- `/role default`：切换到指定角色，并清空当前会话上下文
- `/clear`：清空当前会话上下文
- `/exit`：退出程序

## 角色配置

角色配置统一放在 [roles](C:/Users/diyun-pc/Desktop/code/OpenHachimi/roles) 目录中，每个角色对应一个 Markdown 文件。

- [default.md](C:/Users/diyun-pc/Desktop/code/OpenHachimi/roles/default.md:1)：默认中文助手
- [code_assistant.md](C:/Users/diyun-pc/Desktop/code/OpenHachimi/roles/code_assistant.md:1)：偏工程实现的代码助手

你可以直接新增更多 `.md` 文件，例如 `product_manager.md`、`translator.md`、`reviewer.md`。文件内容会被直接作为该角色的系统指令加载。
