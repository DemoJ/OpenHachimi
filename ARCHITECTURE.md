# OpenHachimi Agent 项目架构文档

本文档详细说明 OpenHachimi Agent 的代码结构、模块职责和核心组件设计。

## 目录结构概览

```
openhachimi_agent/
├── __init__.py              # 包入口
├── __main__.py              # 命令行入口（CLI 子命令解析）
├── app_logging.py           # 日志配置
│
├── agent/                   # Agent 构建
│   ├── __init__.py
│   └── factory.py           # Agent 实例工厂
│
├── content/                 # 内容管理
│   ├── __init__.py
│   ├── prompts.py           # 系统提示词加载
│   └── roles.py             # 角色配置加载
│
├── core/                    # 核心基础设施
│   ├── __init__.py
│   └── config.py            # 应用配置加载与验证
│
├── daemon/                  # 守护进程管理
│   ├── __init__.py
│   └── deploy.py            # systemd / 本地脚本部署
│
├── interface/               # 用户接口层
│   ├── __init__.py
│   ├── cli.py               # 命令行交互逻辑
│   └── http.py              # FastAPI HTTP 服务
│
├── service/                 # 业务服务层
│   ├── __init__.py
│   └── agent_service.py     # Agent 会话管理、消息处理
│
├── storage/                 # 存储层
│   ├── __init__.py
│   └── memory.py            # 消息历史持久化
│
├── tools/                   # 工具集
│   ├── __init__.py          # 工具导出汇总
│   ├── registry.py          # PydanticAI FunctionToolset 注册
│   ├── filesystem.py        # 文件发现、搜索、读取
│   ├── editing.py           # 文件写入、替换、目录创建
│   ├── command.py           # 命令执行
│   ├── git.py               # Git 状态与 diff 查询
│   └── utils.py             # 工具共享辅助函数
│
├── transport/               # 传输层
│   ├── __init__.py
│   └── api_models.py        # HTTP API 数据模型
│
└── system_prompts/          # 内置系统提示词
    ├── __init__.py
    └── base.md              # 基础系统提示词
```

顶层文件：

```
.
├── main.py                  # 开发入口（直接运行启动嵌入式 CLI）
├── deploy.py                # 一键部署脚本
├── pyproject.toml           # 项目元数据与依赖
├── README.md                # 使用说明
├── ARCHITECTURE.md          # 本架构文档
├── .gitignore               # Git 忽略规则
│
├── user/                    # 用户配置目录
│   ├── config.example.yaml  # 配置模板
│   ├── config.yaml          # 用户配置（需自行创建）
│   └── roles/               # 自定义角色
│       ├── default.md       # 默认助手角色
│       └── code_assistant.md # 代码助手角色
│
├── .logs/                   # 日志目录
│   └── openhachimi.log      # 运行日志
│
└── .memory/                 # 会话记忆目录（运行时生成）
    └── <role>/              # 每个角色独立目录
        ├── <session_id>.json # 会话消息历史
        └── latest           # 当前会话 ID 标记
```

---

## 模块职责说明

### 1. 命令入口层

#### `__main__.py`

- 解析 CLI 子命令：`deploy`、`serve`、`cli`、默认嵌入式 CLI
- 根据命令调用对应的模块入口
- 无子命令时启动嵌入式单进程 CLI

#### `main.py`

- 开发调试入口，直接调用 `__main__.main()`
- 适合本地快速启动，无需安装包

---

### 2. 核心层（core）

#### `config.py`

- `AppConfig`：集中管理所有运行时配置的数据类
- `load_config()`：从 `user/config.yaml` 加载并解析配置
- 配置项包括：
  - 路径：`base_dir`、`roles_dir`、`memory_dir`、`log_dir`
  - LLM：`model_name`、`openai_base_url`、`openai_api_key`
  - 应用：`default_role_name`
  - 日志：`log_level`、`log_console`

---

### 3. 内容管理层（content）

#### `prompts.py`

- `load_system_prompt()`：从 `system_prompts/*.md` 加载内置系统提示词
- 使用 `importlib.resources` 读取包内资源，不依赖外部文件

#### `roles.py`

- `list_role_names()`：列出 `user/roles/` 下所有角色名称
- `load_role_content()`：加载指定角色的 Markdown 配置内容

---

### 4. Agent 构建层（agent）

#### `factory.py`

- `build_agent()`：根据配置和角色名称创建 PydanticAI Agent 实例
- 组合：OpenAI 兼容模型 + 系统提示词 + 角色指令 + 工具集
- 使用 `defer_model_check=True` 延迟模型连接验证

---

### 5. 服务层（service）

#### `agent_service.py`

核心业务逻辑，管理 Agent 会话生命周期：

- `AgentService` 类：
  - 维护当前角色、当前会话、消息历史
  - `state()`：返回当前状态
  - `list_roles()`：列出可用角色
  - `new_session()`：新建对话会话
  - `switch_role()`：切换角色并新建会话
  - `send_message()`：非流式消息处理
  - `stream_message()`：流式消息处理（异步生成器）

---

### 6. 存储层（storage）

#### `memory.py`

消息历史持久化管理：

- `load_message_history()`：恢复指定角色的会话历史
- `save_message_history()`：保存消息历史为 JSON
- `start_new_session()`：创建新会话 ID
- 会话 ID 格式：`YYYYMMDD-HHMMSS-<uuid8>`
- 支持从旧版单文件格式迁移（legacy）

---

### 7. 接口层（interface）

#### `cli.py`

命令行交互逻辑，支持两种模式：

- **嵌入式模式**（`run_embedded_cli`）：
  - 直接在本进程内创建 `AgentService`
  - 适合开发调试，无需后台服务
  
- **客户端模式**（`run_cli`）：
  - 连接已启动的 HTTP 后台服务
  - 通过 HTTP API 调用 Agent

支持的命令：
- `/help`、`/roles`、`/role <name>`、`/new`、`/exit`

#### `http.py`

FastAPI HTTP 服务，提供 API 端点：

| 端点 | 方法 | 说明 |
|------|------|------|
| `/health` | GET | 健康检查 |
| `/state` | GET | 当前状态 |
| `/roles` | GET | 可用角色列表 |
| `/chat` | POST | 非流式对话 |
| `/chat/stream` | POST | 流式对话 |
| `/new` | POST | 新建会话 |
| `/role` | POST | 切换角色 |

---

### 8. 传输层（transport）

#### `api_models.py`

HTTP API 使用的 Pydantic 数据模型：

- `AgentState`：当前状态响应
- `ChatRequest`：对话请求
- `ChatResponse`：对话响应
- `RoleSwitchRequest`：角色切换请求
- `CommandResponse`：命令执行响应
- `RolesResponse`：角色列表响应

---

### 9. 工具层（tools）

工作区操作工具集，注册到 PydanticAI Agent：

#### `filesystem.py`

- `list_files`：列出目录内容
- `find_files`：按 glob 模式查找文件
- `search_text`：文本内容搜索
- `read_file`：读取文件内容（支持行范围）

#### `editing.py`

- `write_file`：写入文件内容
- `make_directory`：创建目录
- `replace_in_file`：文本片段替换

#### `command.py`

- `run_command`：执行非交互式命令
- 自动选择 shell（Windows: pwsh/powershell, Linux/macOS: $SHELL）
- 危险命令过滤

#### `git.py`

- `git_status`：查看 Git 状态
- `git_diff`：查看差异

#### `utils.py`

工具共享辅助函数：

- `resolve_workspace_path`：路径解析与越界检查
- `normalize_relative_path`：转换为相对路径
- `iter_workspace_items`：遍历目录（跳过 .git、.venv 等）
- `read_text_file`：读取文本文件
- `assert_safe_command`：危险命令检测
- `run_subprocess`：子进程执行与输出截断

#### `registry.py`

- `WORKSPACE_TOOLSET`：将所有工具注册为 PydanticAI FunctionToolset

---

### 10. 守护进程层（daemon）

#### `deploy.py`

后台服务部署逻辑：

- `deploy_daemon()`：根据系统选择部署方式
- `deploy_systemd_user_service()`：Linux systemd user service
- `deploy_local_script()`：生成本地启动脚本（.sh/.bat）

---

### 11. 日志配置

#### `app_logging.py`

- `configure_logging()`：配置日志系统
- 文件日志：滚动写入 `.logs/openhachimi.log`（最大 2MB，保留 5份）
- 可选控制台输出：通过 `logging.console` 配置

---

## 数据流与调用关系

### 运行模式一：嵌入式 CLI

```
用户输入
    ↓
main.py / __main__.py
    ↓
interface/cli.py (run_embedded_cli)
    ↓
service/agent_service.py (AgentService)
    ↓
agent/factory.py (build_agent)
    ↓
PydanticAI Agent.run()
    ↓
tools/* (WORKSPACE_TOOLSET)
```

### 运行模式二：客户端-服务端

```
用户输入
    ↓
interface/cli.py (run_cli)
    ↓
HTTP Request → interface/http.py
    ↓
service/agent_service.py
    ↓
agent/factory.py → PydanticAI Agent
    ↓
tools/* → 工作区操作
    ↓
HTTP Response → CLI 输出
```

### 消息历史持久化流

```
Agent 对话完成
    ↓
service/agent_service.py
    ↓
storage/memory.py (save_message_history)
    ↓
.memory/<role>/<session_id>.json
```

---

## 配置说明

### user/config.yaml 结构

```yaml
app:
  default_role: default      # 默认角色名称

llm:
  api_key: sk-xxxxxxxx       # OpenAI API Key（必填）
  model: gpt-4o              # 模型名称
  base_url: https://...      # 自定义 API 地址（可选）

paths:
  roles_dir: user/roles      # 角色配置目录
  memory_dir: .memory        # 会话记忆目录

logging:
  level: INFO                # 日志级别
  dir: .logs                 # 日志目录
  console: false             # 是否输出到控制台
```

### 角色配置（user/roles/*.md）

角色配置文件为 Markdown 格式，描述角色的：

- 人设定位
- 行为目标
- 输出风格

示例结构：

```markdown
# 角色名称

你是 xxx 的 xxx。

## 角色目标

- ...

## 行为要求

- ...

## 输出风格

- ...
```

---

## 安全边界

### 路径安全

- `resolve_workspace_path` 限制所有文件操作在项目目录内
- 拒绝访问工作区外路径

### 命令安全

- `assert_safe_command` 检测危险命令模式：
  - `rm`、`del`、`rmdir`、`format`   - `git reset --hard`、`git clean`
  - `shutdown`、`restart-computer`

### 跳过目录

工具搜索时自动跳过：
- `.git`、`.venv`、`__pycache__`
- `.memory`、`.tmp`
- `openhachimi_agent.egg-info`

---

## 扩展指南

### 添加新角色

1. 在 `user/roles/` 下创建 `<role_name>.md`
2. 填写角色描述
3. CLI 中使用 `/role <role_name>` 切换

### 添加新工具

1. 在 `tools/` 下创建新模块或使用现有模块
2. 定义工具函数，签名：`def tool(ctx: RunContext[AppConfig], ...)`
3. 在 `tools/registry.py` 的 `WORKSPACE_TOOLSET` 中注册
4. 在 `tools/__init__.py` 的 `__all__` 中导出

### 添加新 HTTP 端点

1. 在 `transport/api_models.py` 定义请求/响应模型
2. 在 `interface/http.py` 添加路由函数
3. 在 `service/agent_service.py` 添加对应业务方法

---

## 版本信息

- 项目名称：openhachimi-agent
- Python 版本：>=3.10
- 核心依赖：
  - pydantic-ai-slim[openai]>=1.0.10
  - fastapi>=0.115.0
  - uvicorn[standard]>=0.32.0
  - PyYAML>=6.0.0