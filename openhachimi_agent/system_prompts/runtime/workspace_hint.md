## 中间产物落点

模型自造的"任务过程中间产物"(一次性脚本、待发送邮件正文、临时草稿、本地报告)默认请写到 `.workspace/{{ session_id }}/<filename>`。当前会话的目录已隐式可用,首次写入时如不存在会自动创建。

只有以下两种情形写到仓库根 / 其他正式位置:
1. 用户明确指定了目标路径;
2. 任务本质就是修改项目源代码 / 工程文件(openhachimi_agent/**, tests/**, README.md 等)。

`.workspace/` 下的文件 git 不会跟踪、搜索工具(list_files / find_files / search_text)不会扫描、跨会话不会污染。
