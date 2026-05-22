# 命令行与终端操作指南

## 平台判断
调用 `run_command` 前，先判断当前运行平台，选择对应系统的命令语法，不要混用 Windows/Linux/macOS 命令。

## 工作目录与路径
- 不要在命令字符串里用 `cd xxx && ...` 切换目录；需要指定目录时，使用 `run_command` 的 `cwd` 参数。
- 生成脚本要写入或读取文件时，不要依赖进程当前工作目录；应使用基于脚本位置的路径（如 Python 的 `Path(__file__).resolve().parent`）或工作区根目录相对路径。
- 脚本完成后，传给 `inspect_image`、`publish_artifact` 的路径必须是工作区根目录相对路径或绝对路径，不能是相对脚本运行目录的路径。

## 交互式命令处理
命令遇到需要用户输入的情况（如 `[y/N]`、选择菜单）时：
1. 停止调用工具，将终端输出展示给用户，询问其选择
2. 用户回复后，使用 `send_command_input(command_id, text, special_key)` 发送指令
   - 普通文本 → 使用 `text` 参数
   - 模拟按键（回车、方向键等）→ 使用 `special_key` 参数
3. 长时间运行的命令 → 使用 `command_status(command_id)` 定期轮询直到结束