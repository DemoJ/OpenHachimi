请针对本轮 TaskFrame（已在 system prompt 中注入）制定执行计划。
你只需要制定计划（使用 create_todos），不需要自己执行任何调研或搜索。
Executor 拥有浏览器、文件操作、命令行、web_fetch、web_search、delegate_task 等全部工具，请基于对这些工具能力的理解来规划步骤。
如果用户提供了明确的 URL 或文件路径，计划应从直接访问该目标开始。
TaskFrame 是任务契约：计划必须继承 goal、target_entities、invariants，不得扩大或替换目标。

用户原始任务：{{ user_message }}
