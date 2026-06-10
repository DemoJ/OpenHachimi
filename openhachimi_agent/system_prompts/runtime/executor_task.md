请执行以下用户任务。必须遵守 TaskFrame 中的 goal、target_entities、invariants、allowed_autonomy 和 replan_triggers；如果 TaskFrame.execution_mode 是 direct 或 skill_direct，请以最少必要工具调用完成目标，避免为简单任务创建 TODO、重复确认刚成功写入/发布的路径，或进行与目标无关的宽泛探索；如果 TaskFrame.execution_mode 是 skill_direct，已匹配的 skill 是主流程，除非输入缺失或工具失败，否则按 skill 直接推进；如果工具观察结果与 TaskFrame 冲突，应停止当前动作并重新校准目标。
TaskFrame：{{ task_frame }}
用户原始任务：{{ user_message }}