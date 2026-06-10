Executor 在执行时触发了 TaskFrame 偏差或工具失败。请基于 TaskFrame、当前 TODO 和 execution ledger 摘要修订计划。
要求：保持 TaskFrame 的 goal、target_entities、invariants 不变；不要扩大任务目标；如果原计划错误，请调用 create_todos 重建一个更窄、更可执行的计划。
TaskFrame：{{ task_frame }}
Execution ledger replan signal：{{ execution_ledger_signal }}
用户原始任务：{{ user_message }}