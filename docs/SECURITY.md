# 安全模型与信任边界

本文档说明 OpenHachimi 的安全设计、信任边界与已知取舍。面向部署者和希望了解
安全边界的用户。对应的安全加固回归测试见 `tests/unit/test_security_hardening.py`。

---

## 信任模型总览

```
不可信输入                      边界                         可信执行环境
─────────────                ─────────                    ─────────────
任意微信好友/群消息    ─┐
任意 Telegram 用户消息  ├─→ 消息渠道(按设计开放,无白名单) ─→ LLM → 工具层防护
WebUI 访问者          ─┘   HTTP API:全局 Bearer Token      (黑名单/确认/裁剪)
定时任务提示词          ─→ scheduler 安全扫描 + 工具裁剪
远程技能安装 URL       ─→ SSRF 防护 + 人工确认
```

**核心原则:所有进入 LLM 的消息内容都视为不可信输入。** 微信/Telegram 渠道
按设计对任何发送者开放(这是产品决策),因此提示词注入是必须假设的常态而非
例外——防护重心在"注入成功后能造成什么"。

## 各层防护

### 1. 消息渠道(开放 by design)

渠道没有发送者白名单,任何能发消息给 bot 的人都可驱动 Agent。相应的纵深防御:

- **危险命令黑名单**(`tools/permission.py`,与 `tools/utils.py` 共享同一份定义):
  命中删除/格式化/下载即执行(`curl|x| sh`)、解释器内联(`python -c`、
  `powershell -enc`)、磁盘破坏(`dd`/`shred`/`mkfs`)、反弹 shell(`nc`、
  `/dev/tcp/`)、持久化(`crontab`/`schtasks`/`authorized_keys`)等模式时,
  必须经 `clarify_user` 用户确认。用户可用 `user/permission-blacklist.json`
  追加自定义模式。`permission.mode: allow_all` 会跳过全部检查(自担风险)。
- **目录删除确认**:`delete_path` 删除目录(递归)前必须用户确认。
- **远程技能安装确认**:`install_skill` 从 URL 安装前必须用户确认——技能的
  SKILL.md 指令会持续注入 Agent 上下文,等同持久化提示词注入。
- **注意群聊场景**:确认请求会发回消息来源渠道。在群聊中,任何群成员的
  "允许"回复都会被接受。开放渠道 + 群聊 = 确认机制仅防误操作,不防恶意。

### 2. HTTP API 与 WebUI

- 全局 Bearer Token 中间件(`secrets.token_urlsafe(32)` 自动生成,常时比较),
  默认只监听 `127.0.0.1`。**持有 Token 即等同宿主机管理员权限**——可通过
  `PUT /mcp` 配置 stdio 型 MCP 服务器执行任意命令。Token 泄露 = RCE,请像
  保护 SSH 私钥一样保护它。
- GET 下载端点支持 `?token=` 查询参数(为 `<img>`/`<a>` 标签设计)。取舍:
  token 会进入访问日志与浏览器历史。公网部署建议使用反向代理 + 自定义
  Authorization 头,并避免把带 token 的 URL 分享出去。
- 所有响应带 `X-Content-Type-Options: nosniff`;附件/artifact 下载强制
  `Content-Disposition: attachment`,防止 HTML/SVG 附件在 WebUI 同源内联
  渲染造成存储型 XSS(WebUI token 存于 localStorage,同源 JS 可读取)。
- 附件上传:拒绝 `.html/.htm/.xhtml/.svg/.hta` 等可执行网页扩展名;
  `app.allowed_attachment_mime_types` 配置后强制 MIME 白名单。
- `/config` 与 `/mcp` 的 secret 字段返回掩码(前3+后4);回写时若提交的
  是掩码原值则保留原值,不会用掩码覆盖真实密钥。

### 3. 定时任务(无人值守执行)

无人值守场景没有用户确认兜底,采用双重防护:

- **提示词扫描**(`scheduler/security.py`):创建/更新时与每次执行前扫描,
  拒绝提示注入、读密钥(`.ssh`/`config.yaml`/`id_rsa`)、密钥外传(密钥词 +
  渠道/URL 邻近)、下载即执行、内联代码执行、持久化载体等模式。这是启发式
  辅助防线,可被变形绕过,不是强保证。
- **高危工具裁剪**(`agent/factory.py`):`run_mode=scheduled` 时默认剔除
  `run_command`/`send_command_input`/`write_file`/`replace_in_file`/
  `delete_path`/`install_skill`,只保留只读、检索与报告产出类工具。可通过
  `scheduler.security.allow_dangerous_tools_in_scheduled_runs: true` 放开
  (不推荐)。定时任务内修改调度表始终被禁止。

### 4. 持久化数据脱敏

- 会话历史落库前经 `redact_persisted_data` 结构化脱敏(保留消息结构可回
  解析,只替换敏感键下的字符串值与文本中的密钥模式)。
- 日志中的命令全文、验证证据(命令 + 输出摘要)均过 `redact_text`。
- 脱敏模式覆盖:`access_token`/`client_secret`/`appsecret` 等带前缀键、
  JSON 引号形态、`Bearer`、`sk-`/`ghp_`/`AKIA`/`xox`/`AIza` 前缀、裸
  Telegram bot token(`数字:AA...` 35 位哈希)。查看 `core/redaction.py`
  获取完整列表。
- **历史数据清理**:`hachimi clean-secrets` 扫描并重写会话/记忆/调度三个
  SQLite 库中的存量明文密钥(自动备份 `*.pre-clean-secrets.bak`,VACUUM
  抹除残留页)。曾明文落盘的密钥清理后仍应视作已暴露,尽快轮换。
- 库文件创建时收紧权限为 0600(Linux/macOS;Windows 下 chmod 无效)。

### 5. 更新链路

`hachimi update` 会 `git reset --hard` 远端并重新安装依赖(`pip install -e .`
与 `npm install` 会执行安装脚本)。remote 指向非官方仓库
(`github.com/DemoJ/OpenHachimi`)时会要求交互确认。git remote 被篡改 +
 交互确认被误确认 = 供应链 RCE,请保护好项目目录的 git 配置。

## 已知取舍(接受的风险)

| 取舍 | 理由 |
| --- | --- |
| 渠道无发送者白名单 | 产品决策:任何人都可使用 bot。代价是必须假设提示词注入常态化 |
| `?token=` URL 传递 | `<img>` 标签无法设 header。建议仅本机/内网使用 |
| WebUI token 存 localStorage | 业界常规;同源 XSS 已通过附件 attachment/nosniff/扩展名拒绝多层封堵 |
| 定时任务扫描为正则黑名单 | 启发式辅助防线;真正的控制是 scheduled 工具裁剪 |
| `curl \| bash` 一键安装 | 官方仓库 HTTPS,无签名/校验和。仓库被盗即 RCE 链,介意者请手动 clone |
| 危险命令黑名单可绕过 | 黑名单只防误操作与常见攻击;`allow_all` 模式下无任何防护 |

## 安全报告

发现安全问题请通过 GitHub Issues 报告,敏感问题请使用 Security Advisories。
