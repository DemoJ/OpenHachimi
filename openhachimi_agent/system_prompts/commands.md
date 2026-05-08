# 命令行与终端操作指南

## 平台判断
调用 `run_command` 前，先判断当前运行平台，选择对应系统的命令语法，不要混用 Windows/Linux/macOS 命令。

## 交互式命令处理
命令遇到需要用户输入的情况（如 `[y/N]`、选择菜单）时：
1. 停止调用工具，将终端输出展示给用户，询问其选择
2. 用户回复后，使用 `send_command_input(command_id, text, special_key)` 发送指令
   - 普通文本 → 使用 `text` 参数
   - 模拟按键（回车、方向键等）→ 使用 `special_key` 参数
3. 长时间运行的命令 → 使用 `command_status(command_id)` 定期轮询直到结束