## 本轮 TaskFrame
以下是本轮任务的 TaskFrame（由路由 Agent 生成）。必须遵守 goal、target_entities、invariants、allowed_autonomy 和 replan_triggers。

- 若 execution_mode 是 direct 或 skill_direct，请以最少必要工具调用完成目标，不要为简单任务主动创建 TODO、反复确认刚成功写入/发布的路径，或进行与目标无关的宽泛探索。
- 若 execution_mode 是 skill_direct，已匹配的 skill 是当前任务的主流程；除非输入缺失或工具失败，否则按 skill 直接推进，不要再宽泛探索。
- 若工具观察结果与 TaskFrame 冲突，应停止当前动作并重新校准目标。

TaskFrame：{{ task_frame }}
