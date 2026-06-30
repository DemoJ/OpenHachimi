# Router-Planner-Executor 硬编排拆除报告

> 重构日期:2026-06-30
> 参照项目:NousResearch/hermes-agent
> 状态:4 阶段全部完成,623 单测通过,代码层与 prompt 层整合就绪,待端到端手测

---

## 一、为什么要做这次重构

### 原架构的问题

原 OpenHachimi 的单 turn 编排是 `router → planner → executor → replan → repair` 的多阶段串行链路。实测体感有几个突出问题:

1. **"创建计划"和"执行"在一条流里一口气涌出**:planner 的 `create_todos` 是 output tool,一调即终止 graph;executor 紧接着用同一份 history 启动,中间没有"给用户看计划"的独立环节。用户看到的是 `[System] 检测到任务需要前置规划…` → 只读工具卡片 → `✅ 创建计划` → 立刻就是 executor 的执行流,全部在同一条流里涌出。

2. **执行中途突然"更新计划"**:执行受阻时 `_replan_after_execution_signal` 会**二次拉起 planner** 重建计划,叠加 `create_todos` 的 same-turn 全量替换语义,造成"走着走着突然更新计划、再执行再更新"的视觉抖动。

3. **`final_verification_repair` 又插一轮**:验证未过时再跑一次 executor"补齐缺口",叠加成一串 `[System]` 提示。

4. **router 过度规划**:router 是独立 LLM,可把中等任务误判成 `planned`,强制走完整 planner→executor→replan→repair 链路。

### 根因

"前置 router 分流 + 独立 planner run + 中途 replan/repair"三套机制叠加。每套各自有上限、各自有意为之,但**在同一条用户消息的响应流里依次发生**,没有任何"暂停问用户"的断点,体感就是"突兀地反复改计划"。

### Hermes 的对照做法

对照 NousResearch/hermes-agent:**单主 agent ReAct 循环 + todo 作为普通工具自主计划 + delegate 委派分治 + verification 停止闸门**。没有 router 前置分流、没有独立 planner、没有单 turn 内 replan。复杂任务的推进完全依赖模型在单循环中自主决策,停止时的完整性校验由 verification 闸门一次完成。

本次重构照搬 Hermes 思路。

---

## 二、改了什么(按阶段)

### 阶段 1:工具层解耦(已完成,纯增量,不破坏链路)

**目标**:拆除 `with_execution_guard` 硬拦截,把"按计划执行"从代码硬约束改为 prompt 软引导 + verification 闸门兜底。

**改动文件**:
- `tools/planning.py`:删除 `with_execution_guard` / `get_current_task_for_tool` / `ExecutionGuardViolation` 及辅助函数(`_format_guard_*` / `_coerce_tool_names` / `_tool_name_candidates` / `_GUARD_TASK_SUMMARY_LIMIT`);`TodoState` 删 `created_turn_seq` 字段;`create_todos` 删除 same-turn refine 特殊逻辑(保留跨轮 merge 保护——有活动计划时必须 `merge=True` 才能改)
- `tools/registry.py`:30+ 变动工具移除 `with_execution_guard` 包裹,改纯 `with_execution_ledger(with_todo_reminder(...))`
- `storage/session_store.py`:删 `created_turn_seq` 反序列化
- 测试:`test_planning.py` 删 5 个 guard 测试、改 cross_turn/same_turn 测试断言;`test_execution_ledger.py` 改 guard 测试为普通失败测试;`test_session_store.py` 删 `created_turn_seq`

**语义变化**:
- **旧**:活跃计划里必须恰好一个 in-progress 任务才允许变动工具,否则 `ExecutionGuardViolation` 硬拦截
- **新**:变动工具不再被硬拦截。靠 ① prompt 告知"复杂任务先建 todo 再执行" ② `with_todo_reminder`(3 次未更新 todo 提醒) ③ verification 停止闸门事后校验

### 阶段 2:verification 闸门新建(已完成,独立增量)

**目标**:照搬 Hermes 的 `verification_stop`,把"模型编辑代码后想直接结束"做成停止闸门,替代旧的中途 repair。

**新建文件**:
- `agent/verification_evidence.py`:工具分类谓词。`_EDIT_TOOLS`(write_file/replace_in_file/run_command/browser_click/install_skill 等,成功后置 stale)+ `_VERIFY_TOOLS`(run_command/git_diff/read_file/list_files/browser_get_state 等,成功后清 stale)。`is_edit_tool` / `is_verify_tool` 谓词
- `agent/verification_stop.py`:轻量状态机(复用 execution_ledger,**不开新 SQLite 库**,比 Hermes 轻)。`mark_tool_succeeded`(编辑置 stale/验证清 stale)+ `reset_turn_verification`(每轮入口清零)+ `build_verify_on_stop_nudge`(闸门判定,最多 nudge 2 次后放行避免死循环)
- `tests/unit/test_verification_stop.py`:8 个单测

**改动文件**:
- `agent/execution.py:with_execution_ledger`:工具 succeeded 后自动调 `mark_tool_succeeded`
- `agent/factory.py:_validate_execution_result`:接入 verification 闸门。闸门 1(verification_stop evidence 缺口:编辑后无 fresh evidence → nudge)在闸门 2(final_verification_signal:未完成 TODO + 最近工具失败)之前

**关键设计**:`run_command` 身兼 edit/verify 两职——它成功后既算编辑(置 stale)也算验证(立即清 stale),净效果是"模型用 run_command 自查过,没有遗留缺口"。

### 阶段 3:turn 编排重写,删三文件(已完成,核心大手术)

**目标**:彻底删除 router/planner/executor 三角色,统一成单一主 agent。

**新建文件**:
- `service/agent_runtime/main_agent.py`:替代旧 `executor.py`。含:
  - `run_main_agent`:视觉预处理 → 单次 `agent.run` → deferred 短路 → verification signal 上抛。**无 replan/repair 内部循环**
  - `resume_main_agent`:clarify_user 跨轮 deferred 灌回(从 `execute_task_resume` 迁入,删 plan 状态机依赖)
  - `message_with_attachments` / 视觉处理 / `preflight_compress_history`:从 `executor.py` 原样迁入
  - `ExecutionOutcome` dataclass

**删除文件**:
- `service/agent_runtime/router.py`(router agent + should_route_message + resolve_task_frame + decide_plan_continuation)
- `service/agent_runtime/planner.py`(planner agent + run_planner + needs_planning + _emit_output_tool_card)
- `service/agent_runtime/executor.py`(execute_task + _replan_after_execution_signal + final_verification_repair + _build_executor/retry/repair_message)
- `system_prompts/runtime/{planner_task,executor_replan,executor_repair,executor_retry,continuation_decision,task_frame_block}.md`
- `system_prompts/agents/{router,planner,continuation}.md`
- `tests/unit/test_agent_runtime.py`(测已删 router/planner 逻辑)

**改动文件**:
- `agent/factory.py`:收敛为 `build_main_agent`(create_todos 从 `ToolOutput` 改回普通工具、output_type 统一 `[str, DeferredToolRequests]`、scheduled 模式注入 scheduled_executor prompt)+ `build_subagent_agent`;删 `build_router/continuation/planner/executor/scheduled_executor`;`_validate_execution_result` 注册条件 `agent_type == "main"`;`_build_base_agent` 签名加 `run_mode`
- `tools/registry.py`:删 `PLANNER_TOOLSET`/`WORKSPACE_TOOLSET`/`SCHEDULED_EXECUTOR_TOOLSET`,`EXECUTOR_TOOLSET` 改名 `MAIN_TOOLSET` 并把 `create_todos` 加入
- `tools/__init__.py`:导出改为 `MAIN_TOOLSET`
- `service/agent_runtime/turn.py`:`_resolve_turn_outcome` 重写——删 router/planner 分支,改调 `run_main_agent`/`resume_main_agent`;`_finalize_outcome`/`_handle_run_agent_exception` 删 plan 状态机调用,改纯异常传播;`_run_turn_locked` 删 `should_route` 调用
- `service/agent_runtime/agent_cache.py`:`build_agent_by_type` 收敛为 main/subagent 两类,缓存键含 run_mode 区分 interactive/scheduled
- `service/agent_service.py:_get_agent`:默认 `agent_type="main"`,接 `run_mode` 参数
- `service/agent_runtime/context.py`:删 `complete_current_plan`/`fail_current_plan`(保留 `suspend/restore_suspended_plan` 给 clarify_user 用)
- `service/agent_runtime/turn_stream.py:_stall_event`:删 `fail_current_plan`,无活动 todos 时直接给提示
- `service/agent_runtime/turn_setup.py` / `context_cache.py` / `turn_postprocess.py`:`"executor"` 引用改 `"main"`
- `content/runtime_context.py`:删 `_task_frame_block`(TaskFrame 不再注入 system prompt);`_direct_mode_block` 触发条件改为"无活动 TODO 即注入"(不再依赖 router 的 execution_mode 字段);删 `json` import
- 测试:`test_clarify_resume.py` 重写(改测 run_main_agent/resume_main_agent);`test_research/test_scheduler_tool_registry.py` 改测 MAIN_TOOLSET;`test_runtime_context.py` 修 direct_mode 触发条件断言

### 阶段 4:prompt 整合(已完成)

**目标**:把已删 planner.md 的规划引导合并进主 agent prompt,统一口径为"主 agent 自主决定建不建 todo"。

**新建文件**:
- `system_prompts/agents/main_agent.md`:替代 executor.md。整合"何时建 TODO 何时直接做"+ "create_todos 用法"+ "缺信息时 clarify_user"+ "何时委派 delegate_task"+ "完成前自检(verification 闸门)"。明确 create_todos 是普通工具(调用后 run 不终止)

**删除文件**:
- `system_prompts/agents/executor.md`

**改动文件**:
- `agent/factory.py`:`load_system_prompt("agents/executor")` → `agents/main_agent`(2 处)
- `service/agent_runtime/context_snapshot.py`:静态段加载 `agents/executor` → `agents/main_agent`
- `system_prompts/runtime/executor_direct_mode.md`:文案更新,去掉"当前 session 的 execution_mode 是 direct"依赖,改为"无活动 TODO 即直接执行模式"
- `system_prompts/runtime/executor_todo_handoff.md`:文案更新,去掉"继承自 Planner 阶段"措辞
- 测试:`test_prompts.py` / `test_runtime_context.py` 改测 `agents/main_agent`

---

## 三、当前架构(重构后)

### 单 turn 执行流

```
用户消息
  ↓
turn.run_turn (保留编排骨架:per-session lock + task 调度 + stream/sync 分支 + 后置持久化)
  ↓
_run_agent_task → _resolve_turn_outcome:
  ├─ 若 session_state["_user_clarification"]: resume_main_agent  (clarify 跨轮灌回)
  └─ 否则: run_main_agent
       ├─ 视觉预处理(若有图片附件)
       ├─ 清零 verification / validator 计数
       ├─ preflight_compress_history(轮内压缩安全网)
       └─ 主 agent.run()  单次 ReAct 循环
            ├─ todo/create_todos 是普通工具(主 agent 自主决定要不要建计划)
            ├─ delegate_task 委派(子 agent 零记忆,现状不变)
            ├─ clarify_user CallDeferred(现状不变)
            └─ 停止时: output_validator 闸门 (_validate_execution_result)
                 ├─ 闸门1: verification_stop evidence 缺口
                 │    编辑后无 fresh evidence → ModelRetry nudge(最多 2 次)
                 ├─ 闸门2: final_verification_signal
                 │    ├─ 全部 blocked 合法暂停 → 放行
                 │    ├─ validator 硬熔断(≥2 次打回)→ 放行
                 │    └─ 否则 → ModelRetry 打回继续
                 └─ 否则放行
  ↓
_finalize_outcome: 写 result_holder(deferred question / verification signal / 正常 result)
  ↓
后置:持久化 + 压缩判定 + 记忆抽取 + 收尾 yield
```

### 三种 agent 实例

| agent_type | 用途 | 工具集 | output_type | validator |
|---|---|---|---|---|
| `main` (interactive) | 交互式主 agent | MAIN_TOOLSET(全套含 create_todos/clarify_user/delegate_task) | `[str, DeferredToolRequests]` | 有(verification 闸门) |
| `main` (scheduled) | 定时任务主 agent | MAIN_TOOLSET + scheduled_executor prompt 注入 | 同上 | 有 |
| `subagent` | delegate_task 委派的子 agent | 骨架(dynamic_toolset + mcp),主工具集运行时裁剪注入 | `str` | 无 |

### 保留不动的部分

- `tools/delegation.py` + `agent/subagents.py` + `tools/toolset_groups.py`:delegate 已对齐 Hermes 零记忆+工具裁剪,现状直接用
- `tools/clarification.py`:clarify_user CallDeferred 机制不变
- `agent/execution.py`:execution_ledger + `with_execution_ledger` + `get_final_verification_signal` 保留作 verification 数据源(`get_replan_signal` 仍保留但已无调用方,可视为待清理)
- `tools/planning.py:create_todos/update_todo/get_todos/with_todo_reminder/TodoStore`:保留
- 上下文压缩引擎 `context/` 全套、记忆系统 `memory/`:不动
- session_state 字段:`todo_state` / `execution_ledger` / `_user_clarification` / `current_turn_ledger_start_seq` / `last_turn_complete` 保留;`suspend_plan` / `plan_status` / `active_plan_lease` 仍由 clarify_user 写(读侧已基本不用,可视为待清理)

---

## 四、验证情况

| 验证项 | 结果 |
|---|---|
| 全套单元测试 `pytest tests/unit/` | ✅ 623 passed, 1 skipped |
| 导入冒烟(turn/main_agent/factory/registry) | ✅ 通过 |
| 三种 agent 构建冒烟(main interactive / main scheduled / subagent) | ✅ 通过 |
| scheduled 模式 prompt 拼接(含主 Agent + Scheduled Executor + 禁止调度写入) | ✅ 通过 |
| 端到端手测(真实 LLM) | ⏳ 待用户执行 |

---

## 五、待手测的场景与预期

启动 `python main.py` 后,重点观察以下场景:

1. **简单问答**(如"什么是递归"):应**不建 todo 直接回答**,无 `[System] 检测到任务需要前置规划` 提示,无中途 replan 提示。

2. **复杂任务**(如"读 X 文件并改成 Y" / "调研 A 和 B 并对比"):主 agent 自主建 todo → 逐步执行 → update_todo → 完成。**关键观察**:单 turn 内不再出现"创建计划→执行→突然更新计划→再执行"的抖动;失败时主 agent 自己 cancel+修订 todo,不再二次拉起 planner。

3. **clarify 场景**(缺凭据,如"帮我发邮件给 X"):主 agent 调 `clarify_user` → 本轮终止,question 呈现给用户 → 下一轮用户回复无感知灌回,从中断处继续。

4. **delegate 场景**(复杂子任务,如"审查这个模块的代码"):主 agent 调 `delegate_task` → 子 agent 全新会话执行 → 父拿 summary → 父对关键操作再验证。

5. **定时任务**:通过 scheduler 触发的任务应跑通,走 main agent + scheduled_executor prompt,scheduler 写工具被 `ensure_scheduler_mutation_allowed` 拦截。

6. **验证闸门**:主 agent 改了代码但没跑测试/lint 就说"完成"时,应被 `[系统:验证缺口]` nudge 打回;跑过 run_command 验证后放行。

---

## 六、后续可优化方向(按手测可能暴露的问题排序)

### 1. verification 闸门可能过度拦截(手测重点观察)

**风险**:当前 `_EDIT_TOOLS` 把所有 write_file/replace_in_file/run_command/browser_click 都算编辑,只要本轮调过任意一个,停止时若无 verify 工具就跟 nudge。对"改一行注释""写个小配置"这类低风险编辑可能过度拦截。

**优化方向**:
- 像 Hermes 那样按文件后缀过滤:纯文档/markdown 编辑(.md/.txt/.rst 等)不触发 nudge(它们无可验证运行行为)。当前 OpenHachimi 的 ledger 不存路径,要做这个得让 `with_execution_ledger` 记录 edit 工具的路径参数
- 或调低 `_MAX_NUDGE_ATTEMPTS`(当前 2),或对"编辑后立即同 turn 内有 read_file 核对"放宽认定

### 2. `get_replan_signal` 已成死代码

**现状**:`agent/execution.py:get_replan_signal` 保留(它原本给 `_replan_after_execution_signal` 用),但 replan 路径已删,现在无调用方。

**优化方向**:确认无外部依赖后删除 `get_replan_signal` 及其测试,精简 execution.py。

### 3. plan 状态机字段半残留

**现状**:`suspend_current_plan`/`restore_suspended_plan` 仍保留(clarify_user 用),但 `plan_status`/`active_plan_lease`/`suspended_plan` 这些 session_state 字段读侧基本无消费方(WebUI 不展示 plan_status,turn 已不读写)。

**优化方向**:审计这些字段的读侧,若确实无用则精简 `suspend_current_plan` 的写入(只保留 `todo_state.is_active` 翻转这个 clarify 真正需要的语义)。

### 4. `intent.py` 的 router 专用产物已成死代码

**现状**:`classify_intent_heuristic`/`coerce_intent_decision`/`coerce_task_frame`/`build_task_frame`/`PlanContinuationDecision` 这些函数/类保留(router 已删,无调用方),但 `TaskFrame` 类被 `create_todos` 经 `session_state["task_frame"]` 间接读(现在没人写这个字段了)。

**优化方向**:确认 `create_todos` 里 `task_frame = ctx.deps.session_state.get("task_frame", {})` 这个读取在 router 删除后永远拿到空 dict 后,可清理 `create_todos` 对 task_frame.goal/invariants 的继承逻辑,并删除 intent.py 的 router 专用函数(保留 `TaskFrame`/`IntentDecision` 类定义供潜在复用或一并删)。

### 5. scheduled 模式的 scheduler 写工具裁剪方式

**现状**:scheduled 模式不通过 `allowed_tools` 裁剪 scheduler 写工具,而是靠 `ensure_scheduler_mutation_allowed(run_mode="scheduled")` 在工具运行时拦截。这意味着模型仍"看得到" scheduler 写工具的 schema,可能尝试调用后被拒(浪费一次 tool call)。

**优化方向**:若手测发现模型在 scheduled 模式频繁尝试调 scheduler 写工具,可在 `build_main_agent` 时按 run_mode 传 `allowed_tools` 物理裁剪掉 scheduler 写工具名(参考 registry.py 的 `_SCHEDULED_MUTATION_NAMES`)。

### 6. 单 turn 内主 agent 失败后的恢复策略

**现状**:Hermes 式重构后,主 agent 工具连续失败时靠模型自己 cancel+修订 todo。若模型不主动修订(弱模型可能直接声明完成),只能靠 verification 闸门 2(unfinished_todos)打回。

**优化方向**:若手测发现弱模型在工具失败后"装作完成"的比例高,可考虑在 `with_execution_ledger` 检测到连续失败时,主动往 history 注入一条系统提示"你连续 N 次工具失败,请 update_todo 标 blocked 或换思路",而非完全依赖模型自觉。

### 7. delegate_task 的子 agent verification

**现状**:子 agent 走 `str` 输出、无 verification 闸门(factory 里 subagent 不注册 output_validator)。子 agent 返回的 summary 是自述,父 agent 被要求(python 层无强制)对关键操作再验证。

**优化方向**:若手测发现子 agent 频繁"假完成",可给 subagent 也注册一个轻量 verification(只检查 evidence 缺口,不检查 todo 完整性,因为子 agent 不该建 todo)。

---

## 七、回滚指引

本次重构是 4 阶段递进,每阶段独立可验证。若手测发现严重问题需回滚:

- **全部回滚**:`git checkout HEAD -- .` 回到重构前(注意会丢失阶段 1-4 全部改动,包括 verification 闸门这个新功能)
- **只回滚阶段 3-4(保留 verification 闸门)**:阶段 1+2 是纯增量且不破坏原链路,可保留;阶段 3 删三文件是大手术,若要回滚需恢复 router/planner/executor.py + 还原 turn.py/factory.py/registry.py。建议用 git 的阶段提交(见下)而非整体回滚

**建议**:在端到端手测前先 commit 当前成果(分 4 个提交对应 4 阶段),这样回滚粒度更细。当前所有改动尚未提交。

---

## 八、文件变更清单汇总

**新增(5)**:
- `openhachimi_agent/agent/verification_evidence.py`
- `openhachimi_agent/agent/verification_stop.py`
- `openhachimi_agent/service/agent_runtime/main_agent.py`
- `openhachimi_agent/system_prompts/agents/main_agent.md`
- `tests/unit/test_verification_stop.py`

**删除(14)**:
- `openhachimi_agent/service/agent_runtime/{router,planner,executor}.py`
- `openhachimi_agent/system_prompts/agents/{router,planner,continuation,executor}.md`
- `openhachimi_agent/system_prompts/runtime/{planner_task,executor_replan,executor_repair,executor_retry,continuation_decision,task_frame_block}.md`
- `tests/unit/test_agent_runtime.py`

**修改(20+)**:
- `agent/{__init__,factory,execution}.py`
- `content/runtime_context.py`
- `service/agent_runtime/{turn,agent_cache,context,context_cache,turn_postprocess,turn_setup,turn_stream}.py`
- `service/agent_service.py`
- `storage/session_store.py`
- `tools/{__init__,planning,registry}.py`
- `system_prompts/runtime/{executor_direct_mode,executor_todo_handoff}.md`
- `tests/unit/{test_clarify_resume,test_execution_ledger,test_planning,test_prompts,test_research_tool_registry,test_runtime_context,test_scheduler_tool_registry,test_session_store}.py`

**总计**:46 个文件变更(5 新增 + 14 删除 + 27 修改)
