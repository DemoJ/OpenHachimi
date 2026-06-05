# OpenHachimi 用户指南

> **写给普通用户**：如果你不是程序员，只是想用一个聪明的 AI 助手帮你处理各种事务，这篇文档就是为你准备的。技术细节请看 README.md 和 ARCHITECTURE.md。

---

## 目录

1. [这个产品是什么？](#这个产品是什么)
2. [为什么你需要它？（项目优势）](#为什么你需要它项目优势)
3. [安装指南](#安装指南)
4. [配置指南](#配置指南)
5. [如何使用](#如何使用)
6. [功能详解](#功能详解)
7. [常见问题](#常见问题)
8. [进阶技巧](#进阶技巧)

---

## 这个产品是什么？

### 一句话介绍

**OpenHachimi（哈基米）是一个智能 AI 助手，能用中文和你对话，帮你自动完成各种电脑操作和网络任务。**

### 它能做什么？

想象一下，你有一个坐在电脑前的智能助手，它会：

- 🗣️ **用中文和你聊天**：理解你的需求，用自然的中文回答
- 📁 **帮你管理文件**：查找文件、读取内容、整理文档、写笔记
- 🌐 **帮你浏览网页**：打开网站、点击按钮、填写表单、提取信息
- 🔍 **帮你搜索资料**：搜索互联网、整理信息、写研究报告
- 📷 **帮你看图片**：你发一张图片，它能识别内容并回答问题
- ⏰ **帮你定时提醒**：设置定时任务，到时间自动推送消息
- 💬 **多渠道接入**：不仅能用电脑命令行，还能用 Telegram 随时随地对话

### 它不是什么？

- ❌ 它不是简单的问答机器人（像 ChatGPT 网页版）
- ❌ 它不能帮你玩游戏或做违法的事
- ❌ 它不能访问你电脑上任意位置的文件（有安全限制）
- ❌ 它需要你提供 API Key（类似"使用门票"，后面会解释）

---

## 为什么你需要它？（项目优势）

### 1. 🇨🇳 全中文体验

市面上很多 AI 工具需要你用英文对话，或者回答全是英文。哈基米**从里到外都是中文**：
- 系统提示词是中文
- 默认用中文回答
- 理解中文语境和文化习惯

**你能得到什么**：不用纠结英文表达，直接用中文说你想做的事。

### 2. 📱 Telegram 随时随地用

你不用一直开着电脑。在手机上打开 Telegram，发消息给哈基米，它就能：
- 回答问题
- 帮你搜索资料
- 定时推送提醒到 Telegram

**你能得到什么**：出门在外也能用 AI 助手，像微信聊天一样简单。

### 3. 🤖 真正的"动手能力"

普通 AI 只能聊天，哈基米能**真正操作电脑**：

| 普通聊天 AI | 哈基米 |
|------------|--------|
| 只能回答文字 | 能帮你打开浏览器、填表单、点击按钮 |
| 只能建议你怎么改文件 | 能直接帮你改文件、写代码、整理文档 |
| 只能告诉你搜索什么关键词 | 能自己搜索、阅读网页、总结信息 |
| 你问完就结束 | 记得之前聊过什么，有长期记忆 |

**你能得到什么**：不用自己动手，说一句话它就帮你做完。

### 4. 🧠 长期记忆

哈基米会**记住你之前说的重要信息**：
- 你告诉它的偏好、习惯、常用设置
- 之前讨论的项目、计划、想法
- 几天甚至几周后还能回忆起来

**你能得到什么**：不用每次重新介绍背景，它认识你这个用户。

### 5. 🎭 角色切换

不同任务需要不同风格的助手。哈基米支持**多种角色**：
- **默认助手**：日常对话，简洁友好
- **代码助手**：帮你写代码、调试程序，更专业严谨
- 你还可以**自定义角色**：比如"写作助手"、"翻译助手"、"研究助手"

**你能得到什么**：一个助手多种用法，切换角色就行。

### 6. 🔒 相对可控

哈基米设计了多层安全机制，**尽量降低风险**：
- 路径限制：默认只能在你指定的工作区操作
- 命令过滤：检测并阻止常见的危险命令模式
- 操作日志：所有操作都有记录，方便排查

但请注意：AI 仍有**不可预测性**。它可能出现理解偏差、幻觉，偶尔做出不符合预期的操作。建议：
- 重要文件提前备份
- 让它操作前先确认它的理解
- 遇到奇怪的操作及时用 `/stop` 中断

**你能得到什么**：有安全机制作为"护栏"，但最终安全还需要你保持监督意识。

### 7. ⏰ 定时提醒

哈基米有内置的**定时任务系统**：
- 设置"每天早上 9 点推送天气"
- 设置"每周五下午提醒写周报"
- 设置"一个月后提醒续费"

**你能得到什么**：一个能配合 AI 能力的定时提醒系统。

### 8. 🚀 一键部署

不懂技术也没关系，**一条命令就能安装完成**：
- Windows：复制粘贴一行 PowerShell 命令
- Linux/macOS：复制粘贴一行 bash 命令

**你能得到什么**：几分钟内就能用起来，不用折腾复杂配置。

---

## 安装指南

### 准备工作

在安装之前，你需要准备：

1. **一台电脑**：Windows、macOS 或 Linux 都可以
2. **Python 环境**：如果没有，后面会教你安装
3. **API Key**：这是使用 AI 模型的"门票"

#### 什么是 API Key？

API Key 就像一个密码，让你能调用 AI 模型。你需要：

- 去 AI 服务商网站注册账号
- 获取 API Key（通常格式是 `sk-xxxxxxxx`）
- 常见服务商：
  - **DeepSeek**（推荐，国内服务）：https://platform.deepseek.com — 价格实惠、中文理解好、响应速度快
  - **OpenAI**：https://platform.openai.com — 国际主流服务商
  - **其他兼容服务**：国内有很多兼容 OpenAI 接口的服务商可选

### Windows 安装步骤

#### 方法一：一键部署（推荐）

1. **打开 PowerShell**：
   - 按 `Win + X`，选择"Windows PowerShell"或"终端"
   - 或者在开始菜单搜索 "PowerShell"

2. **复制并运行这条命令**：

   ```powershell
   Invoke-WebRequest -Uri https://raw.githubusercontent.com/DemoJ/OpenHachimi/main/deploy.py -OutFile deploy.py
   python deploy.py
   ```

3. **等待安装完成**：脚本会自动下载、安装依赖、配置服务

#### 方法二：手动安装

如果你想自己控制每一步：

1. **确保有 Python**：
   - 打开 PowerShell，输入 `python --version`
   - 如果显示版本号（如 Python 3.10.x），就OK
   - 如果没有，去 https://python.org 下载安装

2. **下载项目**：

   如果你有 git，可以直接克隆：
   ```powershell
   git clone https://github.com/DemoJ/OpenHachimi.git
   cd OpenHachimi
   ```

   如果没有 git：
   - **下载 git**：去 https://git-scm.com/download/win 下载安装，或用 winget 安装：`winget install Git.Git`
   - **或者直接下载 ZIP**：去 GitHub 页面 https://github.com/DemoJ/OpenHachimi 点击绿色 "Code" 按钮，选择 "Download ZIP"，解压后进入目录

3. **创建虚拟环境**：
   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   ```

4. **安装依赖**：
   ```powershell
   pip install -U pip
   pip install -e .
   ```

5. **复制配置模板**：
   ```powershell
   Copy-Item user\config.example.yaml user\config.yaml
   ```

### macOS / Linux 安装步骤

#### 方法一：一键部署（推荐）

打开终端，运行：

```bash
curl -fsSL https://raw.githubusercontent.com/DemoJ/OpenHachimi/main/install.sh | bash
```

#### 方法二：手动安装

1. **确保有 git**：
   - macOS：通常已自带，或在终端运行 `xcode-select --install` 安装开发者工具
   - Linux（Debian/Ubuntu）：`sudo apt install git`
   - Linux（CentOS/RHEL）：`sudo yum install git`
   - 如果没有，去 https://git-scm.com 下载安装

2. **下载项目**：
   ```bash
   git clone https://github.com/DemoJ/OpenHachimi.git
   cd OpenHachimi
   ```

3. **创建虚拟环境**：
   ```bash
   python3 -m venv .venv && source .venv/bin/activate
   ```

4. **安装依赖**：
   ```bash
   pip install -U pip
   pip install -e .
   ```

5. **复制配置模板**：
   ```bash
   cp user/config.example.yaml user/config.yaml
   ```

---

## 配置指南

### 找到配置文件

配置文件位于：`user/config.yaml`

用任意文本编辑器打开它（记事本、VS Code、Sublime Text 都可以）。

### 必填项：API Key

找到这一段：

```yaml
llm:
  api_key: sk-xxxxxxxx        # 必填：你的 API Key
  model: gpt-4o               # 模型名称
  base_url: https://...       # 可选：API 地址
```

**使用 DeepSeek（推荐）**：

```yaml
llm:
  api_key: sk-你的DeepSeekKey      # 从 DeepSeek 平台获取
  model: deepseek-chat            # DeepSeek 对话模型
  base_url: https://api.deepseek.com  # DeepSeek API 地址
```

**使用 OpenAI**：

```yaml
llm:
  api_key: sk-你的OpenAIKey       # 从 OpenAI 平台获取
  model: gpt-4o                   # 或 gpt-4-turbo
  base_url: https://api.openai.com/v1
```

如果你用的是其他兼容服务，`base_url` 和 `model` 要填服务商提供的地址和模型名称。

### 可选项：Telegram Bot

如果你想用 Telegram 和哈基米聊天：

1. **申请 Bot Token**：
   - 在 Telegram 搜索 `@BotFather`
   - 发送 `/newbot`
   - 按提示创建 Bot，获得 Token（格式：`123456789:ABCdefGHI...`）

2. **填入配置**：

```yaml
app:
  telegram_bot_token: "123456789:ABCdefGHI..."  # 你的 Bot Token
  telegram_proxy_url: ""                        # 如果在国内，可能需要代理
```

### 可选项：浏览器设置

如果要让哈基米帮你操作浏览器：

```yaml
app:
  browser_headless: true      # true=不显示浏览器窗口；false=显示窗口
  browser_channel: ""         # 留空用自带浏览器；可填 "chrome" 用本机 Chrome
```

### 可选项：视觉/图片功能

如果要处理图片：

```yaml
vision:
  enabled: true               # 开启图片处理
  max_images_per_message: 4   # 每次最多处理几张图片
  max_image_size_mb: 10       # 图片大小上限
```

### 配置完成后保存

保存文件，然后启动哈基米。

---

## 如何使用

### 启动方式

#### 方式一：嵌入式启动（最简单）

在项目目录下运行：

```bash
hachimi
```

这会直接进入对话模式，你在终端输入问题，哈基米回答。

#### 方式二：后台服务（推荐长期使用）

先部署后台服务：

```bash
hachimi deploy
```

这会注册一个后台守护服务。之后你可以随时进入对话：

```bash
hachimi        # 进入对话模式（自动连接后台服务）
```

后台服务会一直运行，即使你退出对话也不会停止。

#### 方式三：前台调试（仅用于排查问题）

```bash
hachimi serve
```

这会在前台运行 HTTP 服务，适合调试问题时使用。关闭终端服务就停止了。

### 服务管理

哈基米提供了完整的服务管理命令：

#### 常用命令速查

| 命令 | 作用 |
|------|------|
| `hachimi status` | 查看后台服务状态（是否正在运行） |
| `hachimi start` | 启动后台服务 |
| `hachimi stop` | 停止后台服务 |
| `hachimi restart` | 重启后台服务 |
| `hachimi log` | 实时查看服务日志（按 Ctrl+C 退出） |
| `hachimi config` | 用编辑器打开配置文件 |

#### 更新哈基米

```bash
# 检查并更新到最新版本
hachimi update

# 更新后重启服务才能生效
hachimi restart
```

#### 启动/停止/重启服务

```bash
# 启动后台服务
hachimi start

# 停止后台服务
hachimi stop

# 重启后台服务（更新后需要执行）
hachimi restart

# 查看服务是否正在运行
hachimi status
```

#### 查看日志（排查问题）

```bash
# 实时查看日志（按 Ctrl+C 退出）
hachimi log

# 查看最近 100 行日志后退出
hachimi log -n 100 --no-follow
```

#### 修改配置

```bash
# 用编辑器打开配置文件
hachimi config
```

修改配置后需要重启服务：

```bash
hachimi restart
```

#### Linux 系统服务补充说明

如果你用的是 Linux，`hachimi deploy` 会部署为 systemd 服务。除了上面的 `hachimi` 命令，你也可以直接用 systemctl：

```bash
# 与 hachimi start/stop/restart/status 功能相同
systemctl --user start openhachimi
systemctl --user stop openhachimi
systemctl --user restart openhachimi
systemctl --user status openhachimi

# 设置/取消开机自启动
systemctl --user enable openhachimi
systemctl --user disable openhachimi
```

> **提示**：如果想让服务在你退出登录后继续运行，需要执行：
> ```bash
> sudo loginctl enable-linger $USER
> ```

### CLI 内置命令

在对话界面中，你可以输入这些特殊命令：

| 命令 | 作用 |
|------|------|
| `/help` | 显示帮助信息 |
| `/roles` | 列出所有可用角色 |
| `/role <名称>` | 切换到指定角色（如 `/role code_assistant`） |
| `/new` | 开始新的对话（清空当前对话） |
| `/exit` | 退出程序 |

### Telegram 使用

如果配置了 Telegram Bot：

1. 在 Telegram 搜索你的 Bot
2. 发送 `/start` 开始对话
3. 直接发消息即可

Telegram 支持的命令：

| 命令 | 作用 |
|------|------|
| `/start` | 新建对话，显示欢迎信息 |
| `/new` | 开始新对话 |
| `/roles` | 列出角色 |
| `/role <名称>` | 切换角色 |
| `/stop` | 中断当前任务 |
| `/help` | 显示帮助 |

---

## 功能详解

### 1. 文件操作

哈基米可以帮你：

- **查找文件**："帮我找一下所有 .txt 文件"
- **读取文件**："看看 config.yaml 里写了什么"
- **写入文件**："帮我创建一个 notes.txt，写上今天的待办"
- **搜索内容**："在所有 Python 文件里找包含 'error' 的行"

**示例对话**：

```
你：帮我看看 README.md 的前 10 行
哈基米：我来读取 README.md 的前 10 行...
[显示文件内容]

你：帮我创建一个 todo.md，写上：
    1. 完成报告
    2. 发邮件给客户
    3. 预约牙医
哈基米：已创建 todo.md，内容如下...
[确认写入成功]
```

### 2. 浏览器自动化

哈基米可以帮你操作浏览器：

- **打开网页**："帮我打开 bilibili.com"
- **点击按钮**："点击登录按钮"
- **填写表单**："在搜索框输入 Python 教程"
- **提取内容**："把这个页面的正文提取出来"
- **滚动页面**："向下滚动看看更多内容"

**示例对话**：

```
你：帮我打开知乎，搜索"如何学习编程"
哈基米：我来帮你操作浏览器...
[打开浏览器，导航到知乎，输入搜索词]

你：把第一个问题的内容提取出来
哈基米：正在提取页面内容...
[返回提取的正文]
```

### 3. 网络搜索与研究

哈基米可以：

- **搜索信息**："搜索最新的 AI 新闻"
- **深度研究**："帮我研究一下新能源汽车市场现状"
- **整理来源**：自动去重、排序、标注引用

**示例对话**：

```
你：帮我查一下 Python 3.12 有什么新特性
哈基米：我来搜索相关信息...
[搜索多个来源]
[整理信息]
[给出总结，标注引用来源]
```

### 4. 图片识别

你可以发送图片，哈基米会：

- **识别内容**："这张图里有什么？"
- **回答问题**："这个截图里的报错是什么意思？"
- **OCR 文字**："把图片里的文字提取出来"

**示例对话**（Telegram）：

```
你：[发送一张截图]
你：这个错误怎么解决？
哈基米：我看到这是一个 Python ImportError... 原因是...
[给出解决方案]
```

### 5. 定时任务

哈基米可以：

- **设置提醒**："每天早上 8 点提醒我看新闻"
- **定时推送**："每周一推送本周待办"

**示例对话**：

```
你：设置一个任务，每天下午 5 点提醒我下班
哈基米：已创建定时任务：
  - 时间：每天 17:00
  - 内容：提醒下班
  - 推送渠道：Telegram
```

### 6. 角色切换

不同角色有不同的风格：

| 角色 | 特点 | 适用场景 |
|------|------|----------|
| default | 简洁友好，日常对话 | 一般问题、日常咨询 |
| code_assistant | 专业严谨，代码为主 | 写代码、调试、工程问题 |

**切换示例**：

```
你：/role code_assistant
哈基米：已切换到代码助手角色。

你：帮我写一个 Python 脚本读取 CSV 文件
哈基米：[给出详细的代码方案，包含注意事项]
```

### 7. 长期记忆

哈基米会记住你告诉它的重要信息：

```
第一天：
你：我的项目目录在 D:\work\myproject
哈基米：已记住你的项目目录位置。

第三天：
你：帮我看看项目里有没有 README
哈基米：我来查看 D:\work\myproject 目录...
[它记得你之前说的路径]
```

---

## 常见问题

### Q1：我完全不懂编程，能用吗？

**可以**。哈基米的对话是自然语言，你用中文说话就行。比如：

- "帮我搜索..."
- "打开这个网页..."
- "把这段文字保存到..."

你不需要写代码，只需要说你想做什么。

### Q2：API Key 是什么？怎么获取？

**API Key 是使用 AI 模型的"门票"**。

获取步骤：
1. 去 AI 服务商网站（如 deepseek）注册账号
2. 在账号设置里找到 "API Keys"
3. 创建新的 Key，复制保存

注意：API Key 是付费的，不同服务商收费不同。

### Q3：安装失败怎么办？

常见问题：

- **"python 不是内部命令"**：需要安装 Python
- **"pip 安装失败"**：可能是网络问题，试试用国内镜像
- **"权限不足"**：Windows 上用管理员模式打开 PowerShell

如果不确定，可以在 GitHub 提 Issue 寻求帮助。

### Q4：Telegram Bot 连不上？

如果你在国内：
- Telegram 需要代理才能访问
- 在配置里填写 `telegram_proxy_url`

格式示例：
```yaml
telegram_proxy_url: "socks5://127.0.0.1:1080"
```

### Q5：怎么让哈基米只回答不操作？

你可以在对话中明确说：
- "只回答问题，不要修改文件"
- "给我建议，我自己操作"

哈基米会按你的要求行动。

### Q6：数据安全吗？

哈基米有多层安全机制作为"护栏"，但**不能保证绝对安全**：

安全机制：
- **路径限制**：默认限制在工作区，但 AI 可能理解偏差
- **命令过滤**：检测危险命令模式，但无法覆盖所有情况
- **日志记录**：操作可追溯，方便事后排查

风险提示：
- AI 存在幻觉和不可预测性，可能做出不符合预期的操作
- 你的对话内容会发送到 AI 服务商（DeepSeek、OpenAI 等）
- 重要文件请提前备份，涉及敏感信息请自行判断风险

### Q7：可以换其他 AI 模型吗？

**可以**。修改配置：

```yaml
llm:
  model: deepseek-chat         # DeepSeek 对话模型
  # 或 model: deepseek-reasoner  # DeepSeek 深度思考模型
  base_url: https://api.deepseek.com
```

只要服务商兼容 OpenAI 接口格式，就能使用。常见选择：
- **DeepSeek**：国内首选，性价比高
- **OpenAI GPT 系列**：国际主流
- **其他国内服务商**：如智谱 GLM、阿里通义等（需确认兼容 OpenAI 接口格式）

### Q8：怎么更新？怎么重启？

**更新到最新版本**：

```bash
hachimi update
```

这会检查并自动更新到最新版本。

**更新后重启服务**：

```bash
hachimi restart
```

**如果更新命令失败**，可以手动更新：
```bash
cd OpenHachimi
git pull                              # 拉取最新代码
pip install -e .                      # 重新安装依赖
hachimi restart                       # 重启服务
```

**更多服务管理命令**：

| 命令 | 作用 |
|------|------|
| `hachimi status` | 查看服务是否运行 |
| `hachimi start` | 启动服务 |
| `hachimi stop` | 停止服务 |
| `hachimi restart` | 重启服务 |
| `hachimi log` | 查看日志 |

---

## 进阶技巧

### 自定义角色

你可以创建自己的角色：

1. 在 `user/roles/` 目录创建新文件，如 `translator.md`
2. 写入角色描述：

```markdown
# 翻译助手

你是一个专业的翻译助手。

## 角色目标
- 准确翻译中英文
- 保持原文风格和语气
- 专业术语给出注释

## 输出风格
- 先给出译文
- 再给出注释或说明
```

3. 在 CLI 中切换：`/role translator`

### 技能扩展

哈基米支持"技能"——预设好的任务模板。你可以：

- 使用现有技能
- 创建新技能

技能文件放在 `user/skills/` 目录，用 YAML 或 Markdown 格式描述任务流程。

### HTTP API 集成

如果你懂一点编程，可以通过 HTTP API 调用哈基米：

```
POST http://localhost:8765/chat
{
  "message": "你的问题"
}
```

可以集成到你的其他应用中。

### 调整记忆设置

如果你想哈基米记住更多信息：

```yaml
memory:
  enabled: true
  recall:
    max_context_tokens: 2000  # 记忆容量
```

---

## 总结

OpenHachimi 是一个能真正帮你做事的 AI 助手：

- ✅ 全中文交互，不用学英文
- ✅ 多渠道使用，Telegram 随时随地
- ✅ 能操作电脑，不只是聊天
- ✅ 有长期记忆，认识你这个用户
- ✅ 有安全机制作为护栏（但仍需你保持监督）
- ✅ 一键部署，几分钟就能用

如果你是普通用户，只想有个智能助手帮你处理日常事务，哈基米是一个值得尝试的选择。

---

**有问题？**
- GitHub Issues：https://github.com/DemoJ/OpenHachimi/issues
- 查看日志：`.logs/openhachimi.log`

**想了解更多技术细节？**
- README.md：快速入门
- ARCHITECTURE.md：架构设计