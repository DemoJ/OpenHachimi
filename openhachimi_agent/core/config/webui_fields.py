"""WebUI 设置页字段定义(声明式数据)。

每个分组对应前端一个设置页,字段表描述控件类型(kind)、yaml dotted 路径(path)、
分组(group)、UI 文案与校验项(options/editable)。本文件不含逻辑,仅是数据源,
前端驱动变更:新增设置页在此追加 FIELD 列表并注册到 SETTINGS_FIELD_GROUPS 即可。
"""

from typing import Any

CONFIG_KIND_SECRET = "secret"
CONFIG_KIND_STRING = "string"
CONFIG_KIND_SELECT = "select"
CONFIG_KIND_BOOL = "bool"
CONFIG_KIND_INT = "int"
CONFIG_KIND_FLOAT = "float"  # 浮点(如压缩阈值比例),前端复用 number 输入
CONFIG_KIND_MULTI = "multi"  # 字符串列表(多选);value 为 list[str],items 须在 options 内

# AI 模型设置页字段定义。path 为 yaml 中的 dotted 路径;kind 决定控件类型与写回格式;
# group 用于前端分组渲染;label/description 为 UI 文案。后续新增设置分组时扩展此表即可。
AI_MODEL_FIELDS: list[dict[str, Any]] = [
    {"path": "llm.api_key", "kind": CONFIG_KIND_SECRET, "group": "llm",
     "label": "API Key", "description": "OpenAI 兼容服务的访问密钥"},
    {"path": "llm.model", "kind": CONFIG_KIND_STRING, "group": "llm",
     "label": "模型", "description": "主模型名称,如 gpt-5.2 / hachimi"},
    {"path": "llm.base_url", "kind": CONFIG_KIND_STRING, "group": "llm",
     "label": "Base URL", "description": "OpenAI 兼容服务地址,如 https://api.openai.com/v1"},
    {"path": "llm.supports_vision", "kind": CONFIG_KIND_SELECT, "group": "llm",
     "label": "图片支持", "options": ["auto", "true", "false"],
     "description": "auto 自动按模型名判断;true 强制直传图片;false 使用视觉辅助模型"},
    {"path": "vision.enabled", "kind": CONFIG_KIND_BOOL, "group": "vision",
     "label": "启用图片处理", "description": "是否处理图片附件"},
    {"path": "vision.fallback_enabled", "kind": CONFIG_KIND_BOOL, "group": "vision",
     "label": "回退视觉模型", "description": "主模型不支持图片时,调用辅助视觉模型识别后再交给主模型"},
    {"path": "vision.model", "kind": CONFIG_KIND_STRING, "group": "vision",
     "label": "视觉模型", "description": "辅助视觉模型名称;留空且主模型不支持图片时不识别"},
    {"path": "vision.base_url", "kind": CONFIG_KIND_STRING, "group": "vision",
     "label": "Base URL", "description": "留空则复用主模型 base_url"},
    {"path": "vision.api_key", "kind": CONFIG_KIND_SECRET, "group": "vision",
     "label": "API Key", "description": "留空则复用主模型 api_key"},
    {"path": "vision.detail", "kind": CONFIG_KIND_SELECT, "group": "vision",
     "label": "图片精度", "options": ["auto", "low", "high"],
     "description": "OpenAI image_url.detail"},
    {"path": "vision.max_images_per_message", "kind": CONFIG_KIND_INT, "group": "vision",
     "label": "单消息最多图片", "description": "单条消息附带图片数量上限"},
    {"path": "vision.max_image_size_mb", "kind": CONFIG_KIND_INT, "group": "vision",
     "label": "单图大小上限 (MB)", "description": "单张图片大小上限,单位 MB"},
    {"path": "context.summary.model", "kind": CONFIG_KIND_STRING, "group": "summary",
     "label": "摘要模型", "description": "上下文压缩用的辅助模型;留空则使用主模型"},
    {"path": "context.summary.base_url", "kind": CONFIG_KIND_STRING, "group": "summary",
     "label": "Base URL", "description": "留空则复用主模型 base_url"},
    {"path": "context.summary.api_key", "kind": CONFIG_KIND_SECRET, "group": "summary",
     "label": "API Key", "description": "留空则复用主模型 api_key"},
    {"path": "context.summary.max_tokens", "kind": CONFIG_KIND_INT, "group": "summary",
     "label": "摘要 token 上限", "description": "摘要输出 token 上限;结构化摘要通常 1-3K"},
    {"path": "context.summary.abort_on_failure", "kind": CONFIG_KIND_BOOL, "group": "summary",
     "label": "失败即中止", "description": "关闭=插入兜底摘要;开启=中止压缩并冻结对话"},
]

# 网络与服务接入设置页字段定义。
# 改 server_host / server_port / http_api_token / telegram_bot_token / telegram_proxy_url
# 需重启进程才生效(端口/监听地址在启动期绑定,bot 在启动期建连);消息行为三项即时生效。
# server_host 用 select 限定 127.0.0.1 / 0.0.0.0,防止误下拉成公网监听;
# 若需绑定特定网卡 IP(如 192.168.1.10),请直接编辑 config.yaml——下拉刻意不提供该入口。
NETWORK_FIELDS: list[dict[str, Any]] = [
    # ── HTTP 服务(改后需重启) ──
    {"path": "app.server_host", "kind": CONFIG_KIND_SELECT, "group": "http",
     "label": "监听地址", "options": ["127.0.0.1", "0.0.0.0"],
     "description": "127.0.0.1=仅本机访问(最安全);0.0.0.0=开放局域网/公网,务必配合 token 与防火墙。改后需重启。如需绑定特定网卡 IP 请直接编辑 config.yaml"},
    {"path": "app.server_port", "kind": CONFIG_KIND_INT, "group": "http",
     "label": "监听端口", "description": "HTTP 服务监听端口;改后需重启才生效"},
    {"path": "app.http_api_token", "kind": CONFIG_KIND_SECRET, "group": "http",
     "label": "HTTP API Token", "description": "除 /health 外所有接口的访问令牌;改后需重启,且前端需用新 token 重新登录"},
    # ── Telegram(改后需重启) ──
    {"path": "app.telegram_bot_token", "kind": CONFIG_KIND_SECRET, "group": "telegram",
     "label": "Bot Token", "description": "通过 @BotFather 申请;留空不启用 Telegram 渠道。改后需重启(bot 启动期建连)"},
    {"path": "app.telegram_proxy_url", "kind": CONFIG_KIND_STRING, "group": "telegram",
     "label": "代理地址", "description": "无法直连 Telegram 时配置;支持 HTTP/SOCKS5,如 socks5://127.0.0.1:1080。改后需重启"},
    # ── 消息行为(即时生效,无需重启) ──
    {"path": "app.show_tool_calls", "kind": CONFIG_KIND_BOOL, "group": "behavior",
     "label": "显示工具调用", "description": "在 CLI/HTTP/Telegram 渠道显示工具调用进度"},
    {"path": "app.stream_idle_timeout_seconds", "kind": CONFIG_KIND_INT, "group": "behavior",
     "label": "流式空闲检查间隔(秒)", "description": "流式队列无新事件时的进展检查/心跳间隔秒数"},
    {"path": "app.max_attachment_size_mb", "kind": CONFIG_KIND_INT, "group": "behavior",
     "label": "附件大小上限 (MB)", "description": "Telegram/HTTP 附件大小上限,单位 MB"},
]

# 浏览器自动化设置页字段定义。
# 全部位于 app 段;除 idle_timeout 设 0=不自动关闭外,均基本热生效——
# 改动在"下次启动浏览器实例时"生效(浏览器按需懒启动,故无需重启进程,当前实例不重启浏览器)。
# browser_channel 用 select + editable:下拉给 chrome/chromium/msedge 预设,
# 又允许填绝对路径(如 C:\\Program Files\\...或 /usr/bin/google-chrome)。
# editable select 跳过后端白名单校验,由浏览器启动逻辑自行兜底。
BROWSER_FIELDS: list[dict[str, Any]] = [
    {"path": "app.browser_headless", "kind": CONFIG_KIND_BOOL, "group": "instance",
     "label": "无头模式", "description": "开启=不显示浏览器窗口(服务器/无桌面环境推荐);关闭=显示窗口。下次启动浏览器实例时生效"},
    {"path": "app.browser_channel", "kind": CONFIG_KIND_SELECT, "group": "instance",
     "label": "浏览器通道", "options": ["chrome", "chromium", "msedge"], "editable": True,
     "description": "留空=用内置浏览器;可选 chrome/chromium/msedge 预设,或直接填可执行文件绝对路径。下次启动浏览器实例时生效"},
    {"path": "app.browser_user_agent", "kind": CONFIG_KIND_STRING, "group": "instance",
     "label": "User-Agent", "description": "自定义 User-Agent;留空则自动隐藏 Headless 特征。下次启动浏览器实例时生效"},
    {"path": "app.browser_window_size", "kind": CONFIG_KIND_STRING, "group": "instance",
     "label": "窗口尺寸", "description": "形如 1920,1080;留空则随机生成常见尺寸。下次启动浏览器实例时生效"},
    {"path": "app.browser_idle_timeout", "kind": CONFIG_KIND_INT, "group": "instance",
     "label": "空闲自动关闭(秒)", "description": "浏览器空闲多少秒后自动关闭以释放内存;0=不自动关闭。下次启动浏览器实例时生效"},
    {"path": "app.browser_cdp_wait_seconds", "kind": CONFIG_KIND_INT, "group": "instance",
     "label": "CDP 就绪等待(秒)", "description": "等待 Chrome CDP 调试端口就绪的最大秒数;冷启动慢或端口冲突时可调高。下次启动浏览器实例时生效"},
]

# 记忆系统设置页字段定义(功能域最复杂,前端在「记忆」页内再分 5 个子卡片组)。
# 用 group 字段划分子组:memory-general / -embedding / -recall / -capture / -privacy。
# memory.embedding.api_key 为 secret(留空回退复用 llm.api_key,与 summary 辅助模型一致)。
# 切换 总开关 enabled / 改 db_path / 改 embedding.* 建议重启进程(DB/embedding client 在启动期初始化);
# recall / capture / privacy 多为检索与捕获策略,基本在下次检索/捕获时生效,记为热生效。
# recall 全组在前端默认折叠并标注「高级调参,非必要勿改」——由 card 元数据(collapsible/defaultCollapsed)驱动,
# 字段表本身只负责 data。
MEMORY_FIELDS: list[dict[str, Any]] = [
    # ── 总开关 ──
    {"path": "memory.enabled", "kind": CONFIG_KIND_BOOL, "group": "memory-general",
     "label": "启用记忆系统", "description": "关闭则完全停用长期记忆写入与召回。改后建议重启进程才彻底停用后台捕获/调度"},
    {"path": "memory.db_path", "kind": CONFIG_KIND_STRING, "group": "memory-general",
     "label": "数据库路径", "description": "长期记忆 SQLite 路径(相对路径以 user 目录为根)。改后建议重启,且不会自动迁移已有数据"},
    # ── Embedding 向量化 ──
    {"path": "memory.embedding.enabled", "kind": CONFIG_KIND_BOOL, "group": "memory-embedding",
     "label": "启用 Embedding", "description": "关闭则只用 BM25 关键词召回,不做向量检索。改后建议重启进程"},
    {"path": "memory.embedding.model", "kind": CONFIG_KIND_STRING, "group": "memory-embedding",
     "label": "Embedding 模型", "description": "向量化模型名,默认 text-embedding-3-large。改模型后已存向量不再兼容,需重建记忆库"},
    {"path": "memory.embedding.base_url", "kind": CONFIG_KIND_STRING, "group": "memory-embedding",
     "label": "Base URL", "description": "Embedding 服务地址;留空则复用 llm.base_url。改后建议重启进程"},
    {"path": "memory.embedding.api_key", "kind": CONFIG_KIND_SECRET, "group": "memory-embedding",
     "label": "API Key", "description": "Embedding 服务密钥;留空则复用 llm.api_key。改后建议重启进程"},
    {"path": "memory.embedding.dimensions", "kind": CONFIG_KIND_INT, "group": "memory-embedding",
     "label": "向量维度", "description": "需与模型输出维度一致(默认 3072)。改后须重建记忆库,旧向量不再兼容"},
    {"path": "memory.embedding.batch_size", "kind": CONFIG_KIND_INT, "group": "memory-embedding",
     "label": "批大小", "description": "单次向量化请求的文本条数;过大可能超时/被限流。改后下次批处理生效"},
    {"path": "memory.embedding.timeout_seconds", "kind": CONFIG_KIND_INT, "group": "memory-embedding",
     "label": "超时(秒)", "description": "单次 Embedding 请求超时秒数。改后下次请求生效"},
    # ── 召回检索(高级调参,前端默认折叠) ──
    {"path": "memory.recall.max_context_tokens", "kind": CONFIG_KIND_INT, "group": "memory-recall",
     "label": "上下文 token 上限", "description": "召回内容注入对话的最大 token 预算。下次召回生效"},
    {"path": "memory.recall.bm25_top_k", "kind": CONFIG_KIND_INT, "group": "memory-recall",
     "label": "BM25 top_k", "description": "关键词召回候选数;从 BM25 取前 K 条。下次召回生效"},
    {"path": "memory.recall.vector_top_k", "kind": CONFIG_KIND_INT, "group": "memory-recall",
     "label": "向量 top_k", "description": "向量召回候选数;从向量索引取前 K 条。下次召回生效"},
    {"path": "memory.recall.rrf_k", "kind": CONFIG_KIND_INT, "group": "memory-recall",
     "label": "RRF k", "description": "RRF 融合平滑常数(通常 60);K 越大排名越平滑。下次召回生效"},
    {"path": "memory.recall.rerank_top_k", "kind": CONFIG_KIND_INT, "group": "memory-recall",
     "label": "Rerank top_k", "description": "RRF 融合后送入重排的候选数。下次召回生效"},
    {"path": "memory.recall.final_l1_top_k", "kind": CONFIG_KIND_INT, "group": "memory-recall",
     "label": "L1 终筛 top_k", "description": "重排后一级终筛保留数(注入 L2 概要)。下次召回生效"},
    {"path": "memory.recall.final_l2_top_k", "kind": CONFIG_KIND_INT, "group": "memory-recall",
     "label": "L2 终筛 top_k", "description": "二级终筛保留的最终记忆条数。下次召回生效"},
    {"path": "memory.recall.include_l3_profile", "kind": CONFIG_KIND_BOOL, "group": "memory-recall",
     "label": "注入 L3 概览", "description": "是否在召回时附带 L3 用户/偏好概览。下次召回生效"},
    # ── 记忆捕获 ──
    {"path": "memory.capture.enabled", "kind": CONFIG_KIND_BOOL, "group": "memory-capture",
     "label": "启用记忆捕获", "description": "关闭则不再从对话提取记忆。改后下次捕获生效"},
    {"path": "memory.capture.async_enabled", "kind": CONFIG_KIND_BOOL, "group": "memory-capture",
     "label": "异步捕获", "description": "开启则在后台异步提取记忆,不阻塞回复;关闭则同步提取。改后下次捕获生效"},
    {"path": "memory.capture.min_turn_chars", "kind": CONFIG_KIND_INT, "group": "memory-capture",
     "label": "最小捕获字符数", "description": "单轮短于此字符数不触发捕获;过滤无意义寒暄。改后下次捕获生效"},
    {"path": "memory.capture.extract_timeout_seconds", "kind": CONFIG_KIND_INT, "group": "memory-capture",
     "label": "提取超时(秒)", "description": "单次记忆提取超时秒数;异步模式下超时即放弃该轮。改后下次捕获生效"},
    # ── 隐私 ──
    {"path": "memory.privacy.pii_redaction", "kind": CONFIG_KIND_BOOL, "group": "memory-privacy",
     "label": "PII 脱敏", "description": "写入记忆前对个人身份信息(邮箱/电话等)做脱敏。改后下次捕获生效"},
    {"path": "memory.privacy.allow_secret_memory", "kind": CONFIG_KIND_BOOL, "group": "memory-privacy",
     "label": "允许记忆机密", "description": "开启才允许记忆被标记为机密的内容;关闭则强制忽略机密标记。改后下次捕获生效"},
    {"path": "memory.privacy.raw_turn_retention_days", "kind": CONFIG_KIND_INT, "group": "memory-privacy",
     "label": "原始轮次保留(天)", "description": "原始对话轮次保留天数;超期清理,仅保留提取后的结构化记忆。改后下次清理周期生效"},
]

# 上下文压缩设置页字段定义(高级调参,前端整组默认折叠)。
# 阈值为相对模型上下文窗口的比例(float);context_length 单位 K(128=128K tokens)。
# 这一组"调好了就别动":误调可能导致对话爆窗口或被过度压缩,故前端默认折叠并加警示。
CONTEXT_FIELDS: list[dict[str, Any]] = [
    {"path": "context.enabled", "kind": CONFIG_KIND_BOOL, "group": "context-advanced",
     "label": "启用上下文压缩", "description": "关闭则长会话不压缩,可能爆上下文窗口。新会话生效"},
    {"path": "context.engine", "kind": CONFIG_KIND_STRING, "group": "context-advanced",
     "label": "压缩引擎", "description": "预留可插拔引擎名,默认 compressor。非必要勿改;改后新会话生效"},
    {"path": "context.threshold_percent", "kind": CONFIG_KIND_FLOAT, "group": "context-advanced",
     "label": "轮后触发阈值", "description": "真实 input_tokens 占窗口比例达此值触发主压缩(0.1-0.95)。调高更晚压缩、更易爆窗口;调低则更早压缩、可能过度。新会话生效"},
    {"path": "context.hard_ceiling_percent", "kind": CONFIG_KIND_FLOAT, "group": "context-advanced",
     "label": "轮内硬上限", "description": "轮内预检触发线(粗略估计,防单轮 replan/repair 撑爆窗口,0.2-0.99)。应高于轮后阈值。新会话生效"},
    {"path": "context.protect_first_n", "kind": CONFIG_KIND_INT, "group": "context-advanced",
     "label": "保护开头 N 条", "description": "始终不压缩的开头消息数(首轮指令等)。调大可保更多上下文但减少可压缩量。新会话生效"},
    {"path": "context.protect_last_n", "kind": CONFIG_KIND_INT, "group": "context-advanced",
     "label": "保护尾部 N 条", "description": "尾部最少保留的最近消息数。新会话生效"},
    {"path": "context.tail_token_budget", "kind": CONFIG_KIND_INT, "group": "context-advanced",
     "label": "尾部 token 预算", "description": "从末尾向前累计保留的 token 预算。新会话生效"},
    {"path": "context.anti_thrash", "kind": CONFIG_KIND_BOOL, "group": "context-advanced",
     "label": "反抖动", "description": "开启则连续两次压缩节省不足 min_savings_pct% 时停止压缩,避免反复压缩无收益。新会话生效"},
    {"path": "context.min_savings_pct", "kind": CONFIG_KIND_INT, "group": "context-advanced",
     "label": "最小节省百分比", "description": "单次压缩应达到的最低节省百分比(配合反抖动)。新会话生效"},
    {"path": "context.context_length", "kind": CONFIG_KIND_INT, "group": "context-advanced",
     "label": "上下文窗口(K)", "description": "模型上下文窗口大小,单位 K(128=128K tokens)。非 128K 模型需按实际填写(如 32K 填 32);0 用内置默认。必须与模型真实窗口一致,否则压缩阈值计算偏差"},
]

# 任务调度设置页字段定义。
# scheduler 需重启进程才生效(scheduler 在启动期初始化 DB 与轮询循环);
# delivery / security 同样在启动期读取。
SCHEDULER_FIELDS: list[dict[str, Any]] = [
    {"path": "scheduler.enabled", "kind": CONFIG_KIND_BOOL, "group": "scheduler-main",
     "label": "启用任务调度", "description": "关闭则不轮询执行定时任务。改后需重启进程才彻底停用"},
    {"path": "scheduler.db_path", "kind": CONFIG_KIND_STRING, "group": "scheduler-main",
     "label": "数据库路径", "description": "定时任务 SQLite 路径(相对路径以项目根为根)。改后建议重启,且不会自动迁移已有任务"},
    {"path": "scheduler.poll_interval_seconds", "kind": CONFIG_KIND_INT, "group": "scheduler-main",
     "label": "轮询间隔(秒)", "description": "调度器轮询任务表的间隔秒数。改后需重启进程"},
    {"path": "scheduler.max_concurrency", "kind": CONFIG_KIND_INT, "group": "scheduler-main",
     "label": "最大并发", "description": "同时执行的任务数上限。改后需重启进程"},
    {"path": "scheduler.default_timeout_seconds", "kind": CONFIG_KIND_INT, "group": "scheduler-main",
     "label": "默认超时(秒)", "description": "单次任务默认超时秒数。改后需重启进程"},
    {"path": "scheduler.claim_lock_seconds", "kind": CONFIG_KIND_INT, "group": "scheduler-main",
     "label": "认领锁(秒)", "description": "多实例时任务认领锁定时长秒数。改后需重启进程"},
    {"path": "scheduler.delivery.default_mode", "kind": CONFIG_KIND_SELECT, "group": "scheduler-delivery",
     "label": "默认投递模式", "options": ["origin", "inbox", "explicit", "none"],
     "description": "新建任务的默认投递模式:origin=回发起会话;inbox=记入收件箱;explicit=按 targets 投递;none=不投递。改后需重启进程"},
    {"path": "scheduler.delivery.fallback_to_inbox", "kind": CONFIG_KIND_BOOL, "group": "scheduler-delivery",
     "label": "投递失败回落收件箱", "description": "任务投递失败时是否回落记入收件箱。改后需重启进程"},
    {"path": "scheduler.security.prompt_scan_enabled", "kind": CONFIG_KIND_BOOL, "group": "scheduler-security",
     "label": "提示词扫描", "description": "开启则校验定时任务 cron 的 prompt 是否符合安全策略。改后需重启进程"},
    {"path": "scheduler.security.allow_scheduler_mutation_in_scheduled_runs", "kind": CONFIG_KIND_BOOL, "group": "scheduler-security",
     "label": "允许任务内改调度", "description": "定时任务执行中是否允许调用调度相关工具改动任务表。关闭更安全。改后需重启进程"},
    {"path": "scheduler.security.allow_interactive_tools_in_scheduled_runs", "kind": CONFIG_KIND_BOOL, "group": "scheduler-security",
     "label": "允许任务内交互工具", "description": "定时任务执行中是否允许调用交互类工具。关闭更安全。改后需重启进程"},
]

# 联网研究设置页字段定义。
# enabled_backends 用 multi 多选(duckduckgo/brave/tavily);
# brave_api_key / tavily_api_key 为 secret——后端选择与对应 key 前端联动显示。
RESEARCH_FIELDS: list[dict[str, Any]] = [
    {"path": "research.enabled_backends", "kind": CONFIG_KIND_MULTI, "group": "research-main",
     "label": "启用后端", "options": ["duckduckgo", "brave", "tavily"],
     "description": "勾选启用的搜索后端;duckduckgo 无需 Key,brave/tavily 需对应 API Key"},
    {"path": "research.brave_api_key", "kind": CONFIG_KIND_SECRET, "group": "research-main",
     "label": "Brave API Key", "description": "启用 brave 后端时必填。留空则 brave 后端报错"},
    {"path": "research.tavily_api_key", "kind": CONFIG_KIND_SECRET, "group": "research-main",
     "label": "Tavily API Key", "description": "启用 tavily 后端时必填。留空则 tavily 后端报错"},
    {"path": "research.search_timeout_seconds", "kind": CONFIG_KIND_INT, "group": "research-main",
     "label": "搜索超时(秒)", "description": "单次搜索请求超时秒数(上限 60)"},
    {"path": "research.max_backend_results", "kind": CONFIG_KIND_INT, "group": "research-main",
     "label": "单后端结果数上限", "description": "每个后端返回的最大结果数(上限 50)"},
    {"path": "research.min_independent_sources", "kind": CONFIG_KIND_INT, "group": "research-main",
     "label": "最少独立来源数", "description": "研究报告要求的最少独立来源数(上限 20);提高可增强可信度"},
    {"path": "research.require_citations", "kind": CONFIG_KIND_BOOL, "group": "research-main",
     "label": "强制引用", "description": "开启则研究报告必须附来源引用"},
    {"path": "research.browser_fallback_enabled", "kind": CONFIG_KIND_BOOL, "group": "research-main",
     "label": "浏览器兜底", "description": "API 后端全部失败时回退到浏览器自动化抓取。依赖浏览器实例可用"},
]

# 路径与日志设置页字段定义(基础设施,低频)。
# 路径改错可能导致服务找不到资源(角色/记忆/技能/附件);日志级别影响输出粒度。
# paths.* 用 string(相对路径以项目根为根);logging.level 用 select 限定标准日志级别。
PATHS_LOGGING_FIELDS: list[dict[str, Any]] = [
    {"path": "paths.roles_dir", "kind": CONFIG_KIND_STRING, "group": "paths",
     "label": "角色目录", "description": "角色定义所在目录(相对项目根)。改后需重启进程才重新加载角色;改错将找不到角色"},
    {"path": "paths.memory_dir", "kind": CONFIG_KIND_STRING, "group": "paths",
     "label": "记忆目录", "description": "记忆数据库等所在的根目录(相对项目根)。改后需重启进程;改错将找不到记忆库"},
    {"path": "paths.external_skills_dir", "kind": CONFIG_KIND_STRING, "group": "paths",
     "label": "外部技能目录", "description": "额外扫描读取的外部技能目录(相对项目根,留空则不额外扫描)。改后需重启进程"},
    {"path": "paths.attachments_dir", "kind": CONFIG_KIND_STRING, "group": "paths",
     "label": "附件目录", "description": "上传附件暂存目录(相对项目根)。改后需重启进程"},
    {"path": "logging.level", "kind": CONFIG_KIND_SELECT, "group": "logging",
     "label": "日志级别", "options": ["DEBUG", "INFO", "WARNING", "ERROR"],
     "description": "日志输出级别。改后需重启进程才彻底切换"},
    {"path": "logging.dir", "kind": CONFIG_KIND_STRING, "group": "logging",
     "label": "日志目录", "description": "日志文件目录(相对项目根)。改后需重启进程;改错将无法写日志"},
    {"path": "logging.console", "kind": CONFIG_KIND_BOOL, "group": "logging",
     "label": "控制台输出", "description": "是否同时向控制台输出日志。改后需重启进程"},
]

# 各设置页分组的字段定义注册表。新增分组在此追加 key → 字段列表即可。
SETTINGS_FIELD_GROUPS: dict[str, list[dict[str, Any]]] = {
    "ai-models": AI_MODEL_FIELDS,
    "network": NETWORK_FIELDS,
    "browser": BROWSER_FIELDS,
    "memory": MEMORY_FIELDS,
    "context": CONTEXT_FIELDS,
    "scheduler": SCHEDULER_FIELDS,
    "research": RESEARCH_FIELDS,
    "paths-logging": PATHS_LOGGING_FIELDS,
}