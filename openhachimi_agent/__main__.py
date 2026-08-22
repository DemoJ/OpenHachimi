"""OpenHachimi 命令入口。

子命令速查：
  hachimi              进入 CLI 对话（默认行为）
  hachimi status       查看后台服务状态
  hachimi start        启动后台服务
  hachimi stop         停止后台服务
  hachimi restart      重启后台服务
  hachimi log          实时查看服务日志
  hachimi config       编辑配置文件
  hachimi deploy       部署并注册后台守护服务
  hachimi serve        直接在前台运行 HTTP 服务（调试用）
  hachimi update       更新到最新版本
  hachimi install      安装 Playwright 浏览器驱动
  hachimi schedule     管理定时任务
"""

import argparse
import asyncio
import logging
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.error import HTTPError

import uvicorn

from openhachimi_agent.app_logging import configure_logging
from openhachimi_agent.core.config import load_config
from openhachimi_agent.core.version import PACKAGE_NAME, get_version
from openhachimi_agent.daemon.deploy import DEFAULT_HOST, DEFAULT_PORT, SERVICE_NAME, deploy_daemon, undeploy_daemon, webui_url
from openhachimi_agent.interface.cli import get_server_url, request_json, run_embedded_cli


logger = logging.getLogger(__name__)

# ── 颜色输出（终端支持时启用）─────────────────────────────────────────────────
_USE_COLOR = sys.stdout.isatty() and platform.system().lower() != "windows"

def _c(code: str, text: str) -> str:
    """用 ANSI 色码包裹文字，不支持颜色时原样返回。"""
    if not _USE_COLOR:
        return text
    return f"\033[{code}m{text}\033[0m"

def _ok(msg: str)   -> None: print(_c("32", f"[OK]    {msg}"))
def _info(msg: str) -> None: print(_c("34", f"[INFO]  {msg}"))
def _warn(msg: str) -> None: print(_c("33", f"[WARN]  {msg}"))
def _err(msg: str)  -> None: print(_c("31", f"[ERROR] {msg}"), file=sys.stderr)


# ── systemd 工具函数 ───────────────────────────────────────────────────────────

def _has_systemd() -> bool:
    """检查当前系统是否支持 systemctl --user。"""
    return (
        platform.system().lower() == "linux"
        and shutil.which("systemctl") is not None
    )


def _systemctl(*args: str, check: bool = False) -> int:
    """运行 systemctl --user <args>，返回退出码。"""
    cmd = ["systemctl", "--user", *args]
    result = subprocess.run(cmd, check=False)
    if check and result.returncode != 0:
        _err(f"systemctl 命令失败（退出码 {result.returncode}）：{' '.join(cmd)}")
        sys.exit(result.returncode)
    return result.returncode


def _require_systemd() -> None:
    """若不支持 systemd，打印提示并退出。"""
    if not _has_systemd():
        _err("当前系统不支持 systemd，无法使用此命令。")
        _info("请手动运行后台脚本，或使用 `hachimi serve` 在前台启动服务。")
        sys.exit(1)


def _deployed_endpoint() -> tuple[str, int]:
    """读取配置文件的监听地址，供 cmd_start/cmd_restart 显示访问地址。

    service 文件只运行 `hachimi serve`（不写死 host/port），serve 启动时读配置文件，
    故此处也读配置文件，与实际监听地址保持一致。
    """
    try:
        config = load_config()
        return config.server_host, config.server_port
    except Exception:
        return DEFAULT_HOST, DEFAULT_PORT


def _configured_token() -> str | None:
    """读取配置文件中的 HTTP API Token，供启动/部署成功后打印。"""
    try:
        return load_config().http_api_token
    except Exception as exc:
        logger.debug("failed to load HTTP API token for display: %s", exc)
        return None


def _print_access_info(host: str, port: int) -> None:
    """打印服务访问地址（API + WebUI）与访问令牌。前端未构建时给出构建提示。"""
    print()
    _ok(f"API   地址：http://{host}:{port}")
    url = webui_url(host, port)
    if url:
        _ok(f"WebUI 地址：{url}")
    else:
        _warn("WebUI 未构建，/ui 不可用。运行 `cd webui && npm run build` 后重启服务。")
    token = _configured_token()
    if token:
        from openhachimi_agent.core.config.webui_io import mask_secret

        # 只显示掩码:完整 token 打到控制台会进终端回滚/共享会话记录。
        _ok(f"访问令牌（HTTP API Token）：{mask_secret(token)}（完整值见 user/config.yaml）")
    else:
        _warn("未读取到访问令牌（HTTP API Token），请检查配置文件 app.http_api_token。")
    print()


# 集中维护的命令清单，便于 deploy/start/restart/serve 复用打印。
# 列表项格式：(命令, 说明)
_COMMAND_HELP: tuple[tuple[str, str], ...] = (
    ("hachimi",             "进入 CLI 对话（默认行为）"),
    ("hachimi status",      "查看后台服务状态"),
    ("hachimi start",       "启动后台服务"),
    ("hachimi stop",        "停止后台服务"),
    ("hachimi restart",     "重启后台服务（并轮换访问令牌）"),
    ("hachimi log",         "实时查看服务日志（Ctrl-C 退出，-n N 显示最近 N 行）"),
    ("hachimi config",      "用编辑器打开配置文件"),
    ("hachimi serve",       "在前台运行 HTTP 服务（调试用）"),
    ("hachimi deploy",      "部署并注册后台守护服务"),
    ("hachimi install",     "安装 Playwright 浏览器驱动（chromium）"),
    ("hachimi update",      "更新到最新版本（--force 强制重装依赖）"),
    ("hachimi uninstall",   "卸载后台守护服务（--purge 清虚拟环境）"),
    ("hachimi schedule",    "管理定时任务（add/list/pause/resume/remove/run/inbox/logs）"),
    ("hachimi clean-secrets", "扫描并清理历史数据中的明文密钥（自动备份）"),
    ("hachimi weixin",      "微信原生扫码登录配置"),
    ("hachimi -V",          "查看当前版本"),
)


def _print_commands_help(title: str = "可用命令") -> None:
    """打印 hachimi CLI 可用命令清单，供部署/启动完成后提示用户。

    若当前 hachimi 不在 PATH 中（短命令不可直接使用），会额外打印加入 PATH 的提示。
    """
    print()
    print(_c("1", f"== {title} =="))
    width = max(len(cmd) for cmd, _ in _COMMAND_HELP)
    for cmd, desc in _COMMAND_HELP:
        print(f"  {_c('1', cmd.ljust(width))}  {desc}")

    if shutil.which("hachimi") is None:
        # 用户当前 PATH 没有 hachimi，提示加入 PATH 以使用短命令。
        print()
        _info("当前 shell 未检测到 `hachimi` 命令，加入虚拟环境的 bin 目录后即可直接使用上述短命令：")
        venv_bin = Path(sys.executable).parent
        print(f"  echo 'export PATH=\"{venv_bin}:$PATH\"' >> ~/.bashrc && source ~/.bashrc")
    print()


# ── 子命令实现 ────────────────────────────────────────────────────────────────

def cmd_status(_args: argparse.Namespace) -> None:
    """查看后台服务状态。"""
    _require_systemd()
    _systemctl("status", SERVICE_NAME)


def cmd_start(_args: argparse.Namespace) -> None:
    """启动后台服务。"""
    _require_systemd()
    _info(f"正在启动 {SERVICE_NAME} 服务...")
    _systemctl("start", SERVICE_NAME, check=True)
    _ok("服务已启动。")
    host, port = _deployed_endpoint()
    _print_access_info(host, port)
    _info("使用 `hachimi status` 确认运行状态。")
    _print_commands_help()


def cmd_stop(_args: argparse.Namespace) -> None:
    """停止后台服务。"""
    _require_systemd()
    _info(f"正在停止 {SERVICE_NAME} 服务...")
    _systemctl("stop", SERVICE_NAME, check=True)
    _ok("服务已停止。")


def _rotate_http_api_token() -> None:
    """重启前轮换 HTTP API Token：生成新令牌写回配置文件，服务重启后即用新令牌。

    旧令牌随重启立即失效，避免长期不变的令牌泄露后被无限期利用。
    轮换失败不阻塞重启（服务沿用旧令牌），只给出警告。
    """
    from openhachimi_agent.core.config import load_raw_config, mask_secret, rotate_http_api_token

    try:
        config = load_config()
    except Exception as exc:
        _warn(f"读取配置失败，本次跳过访问令牌轮换：{exc}")
        return
    config_path = config.config_path
    if not config_path.exists():
        _warn("配置文件不存在，本次跳过访问令牌轮换。")
        return
    try:
        new_token = rotate_http_api_token(config_path, load_raw_config(config_path))
    except OSError as exc:
        _warn(str(exc))
        _warn("本次重启将沿用旧令牌。")
        return
    # 只显示掩码：完整 token 打到控制台会进终端回滚/共享会话记录。
    _ok(f"已轮换访问令牌（HTTP API Token）：{mask_secret(new_token)}（完整值见 user/config.yaml）")


def cmd_restart(_args: argparse.Namespace) -> None:
    """重启后台服务。每次重启前轮换 HTTP API Token。"""
    _require_systemd()
    _info(f"正在重启 {SERVICE_NAME} 服务...")
    # 先写新令牌再重启：新起的 serve 进程读到的就是新令牌，
    # 旧进程持有的旧令牌随之失效。之后 _print_access_info 重读配置展示新令牌掩码。
    _rotate_http_api_token()
    _systemctl("restart", SERVICE_NAME, check=True)
    _ok("服务已重启。")
    host, port = _deployed_endpoint()
    _print_access_info(host, port)
    _info("使用 `hachimi status` 确认运行状态。")
    _print_commands_help()


def cmd_log(args: argparse.Namespace) -> None:
    """实时查看服务日志（默认跟随，Ctrl-C 退出）。"""
    _require_systemd()
    cmd = ["journalctl", "--user", "-u", SERVICE_NAME]
    if not args.no_follow:
        cmd.append("-f")
    if args.lines:
        cmd += ["-n", str(args.lines)]
    os.execvp(cmd[0], cmd)   # 替换当前进程，让 journalctl 直接接管终端


def cmd_config(_args: argparse.Namespace) -> None:
    """用编辑器打开配置文件。"""
    config = load_config()
    config_path = config.config_path
    if not config_path.exists():
        _err(f"配置文件不存在：{config_path}")
        _info("请先运行 `hachimi deploy` 完成初始部署。")
        sys.exit(1)

    # 按优先级选择编辑器(notepad 兜底:Windows 官方部署路径下常见无 vim/nano)
    editor = (
        os.environ.get("EDITOR")
        or os.environ.get("VISUAL")
        or shutil.which("nano")
        or shutil.which("vim")
        or shutil.which("vi")
        or (shutil.which("notepad") if platform.system() == "Windows" else None)
    )
    if not editor:
        _err("未找到可用的文本编辑器，请设置 $EDITOR 环境变量。")
        _info(f"配置文件路径：{config_path}")
        sys.exit(1)

    _info(f"使用 {editor} 打开配置文件：{config_path}")
    os.execvp(editor, [editor, str(config_path)])


def cmd_clean_secrets(args: argparse.Namespace) -> None:
    """扫描并清理持久化数据（会话/长期记忆/定时任务库）中的历史明文密钥。"""
    from openhachimi_agent.storage.clean_secrets import clean_all_databases, collect_database_paths

    try:
        config = load_config()
    except Exception as exc:
        _err(f"加载配置失败：{exc}")
        sys.exit(1)

    paths = collect_database_paths(config)
    existing = [p for p in paths if p.exists()]
    if not existing:
        _warn("未找到任何数据库文件，无需清理。")
        return

    _info("将扫描以下数据库，重写其中命中的明文密钥（清理前自动备份）：")
    for path in existing:
        print(f"  {path}")
    _info("建议先停止后台服务（hachimi stop）再执行，避免清理期间服务并发写入。")
    if not args.yes:
        try:
            reply = input("继续执行清理？[y/N] ")
        except (EOFError, KeyboardInterrupt):
            reply = ""
        if reply.strip().lower() not in ("y", "yes"):
            _info("已取消。")
            return

    results = clean_all_databases(config)
    if not results:
        _ok("未发现需要清理的明文密钥。")
        return
    for db, stats in results.items():
        _ok(f"{db}:")
        for table, count in stats.items():
            print(f"    {table}: 重写 {count} 行")
    _warn("备份文件后缀为 .pre-clean-secrets.bak，确认服务正常后可删除。")
    _warn("曾明文落盘的密钥（API key / bot token）建议尽快轮换——清理无法撤回此前的暴露。")


def cmd_install(_args: argparse.Namespace) -> None:
    """安装 Playwright 浏览器驱动（chromium）。"""
    _info("正在安装 Playwright 浏览器驱动（chromium）...")
    result = subprocess.run(
        [sys.executable, "-m", "playwright", "install", "chromium"],
        check=False,
    )
    if result.returncode == 0:
        _ok("Playwright 浏览器驱动安装完成。")
    else:
        _err("安装失败，请检查网络连接或手动运行：")
        _info("  playwright install chromium")
        sys.exit(result.returncode)


def cmd_uninstall(args: argparse.Namespace) -> None:
    """卸载后台守护服务，可选清理虚拟环境或整个项目。"""
    _warn("即将执行卸载操作，此操作不可撤销！")

    # 构造提示信息
    actions = ["停止并注销后台守护服务"]
    if args.purge:
        actions.append("删除虚拟环境（.venv）")
    if args.remove_all:
        actions += [
            "删除虚拟环境（.venv）",
            "删除整个项目目录",
            "  ⚠ 项目目录内包含你的全部对话记录(.memory)、配置与 API Key(user/)、"
            "定时任务(.scheduler)——删除前请自行备份,删除后无法恢复",
        ]
        args.purge = True  # remove_all 隐含 purge

    print("将执行以下操作：")
    for act in dict.fromkeys(actions):  # 去重保序
        print(f"  - {act}")
    print()

    if not args.yes:
        try:
            answer = input("确认继续？输入 yes 继续，其他任意键取消：").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            _info("已取消。")
            return
        if answer != "yes":
            _info("已取消。")
            return

    undeploy_daemon(
        remove_venv=args.purge or args.remove_all,
        remove_project=args.remove_all,
    )
    _ok("卸载完成。")


def cmd_deploy(args: argparse.Namespace) -> None:
    """部署并注册后台守护服务。"""
    config = load_config()
    configure_logging(config)
    # 命令行 --host/--port 显式指定时写回配置文件，使参数持久化。
    # service 文件只运行 `hachimi serve`，serve 启动时读配置 → 改配置后 restart 即生效。
    if args.host is not None or args.port is not None:
        import yaml
        from openhachimi_agent.core.config import persist_server_endpoint
        host = args.host if args.host is not None else config.server_host
        port = args.port if args.port is not None else config.server_port
        with config.config_path.open("r", encoding="utf-8") as f:
            raw_config = yaml.safe_load(f) or {}
        persist_server_endpoint(config.config_path, raw_config, host, port)
        logger.info("deploy: 命令行参数已写回配置 host=%s port=%s", host, port)
    deploy_daemon()
    _print_commands_help("部署完成 · 可用命令")


def cmd_serve(args: argparse.Namespace) -> None:
    """在前台直接运行 HTTP 服务（调试用）。"""
    config = load_config()
    configure_logging(config)
    from openhachimi_agent.interface.cli import _preflight_config_warnings

    for warning in _preflight_config_warnings(config):
        print(f"⚠️  {warning}\n")
    # 命令行 --host/--port 显式指定时覆盖配置文件 app.server_host/server_port。
    host = args.host if args.host is not None else config.server_host
    port = args.port if args.port is not None else config.server_port
    logger.info("serve command host=%s port=%s", host, port)
    # uvicorn.run 会阻塞并在启动时自行打印 API 地址，这里先打印 WebUI 与访问令牌提示。
    url = webui_url(host, port)
    if url:
        _ok(f"WebUI 地址：{url}")
    else:
        _warn("WebUI 未构建，/ui 不可用。运行 `cd webui && npm run build` 后重启服务。")
    if config.http_api_token:
        from openhachimi_agent.core.config.webui_io import mask_secret

        _ok(f"访问令牌（HTTP API Token）：{mask_secret(config.http_api_token)}（完整值见 user/config.yaml）")
    else:
        _warn("未读取到访问令牌（HTTP API Token），请检查配置文件 app.http_api_token。")
    # uvicorn.run 进入阻塞前先打印一次可用命令，便于在 serve 调试模式下了解管理入口。
    _print_commands_help()
    uvicorn.run("openhachimi_agent.interface.http:app", host=host, port=port)


def cmd_update(args: argparse.Namespace) -> None:
    """检查并更新到最新版本。"""
    from openhachimi_agent.core.updater import run_update
    run_update(force=args.force)


def cmd_cli(_args: argparse.Namespace) -> None:
    """进入 CLI 对话模式。"""
    try:
        asyncio.run(run_embedded_cli())
    except KeyboardInterrupt:
        # Ctrl-C 应干净退出而不是吐 KeyboardInterrupt traceback。
        print("\n已退出对话。")


def cmd_weixin(_args: argparse.Namespace) -> None:
    """运行微信 iLink 独立扫码登录"""
    from openhachimi_agent.interface.weixin.cli import main as weixin_main
    weixin_main()


def _local_timezone_name() -> str:
    """返回本机时区的 IANA 名(如 Asia/Shanghai);无法可靠取到时退回 UTC。

    候选顺序:TZ 环境变量 → tzlocal(若装了) → 系统 tzname。
    每个候选都用 ZoneInfo 校验——Windows 的 tzname 是"中国标准时间"这类
    本地化名,不是 IANA 名,直接用会被 ZoneInfo 拒收。
    """
    import time as _time

    candidates: list[str] = []
    tz_env = os.getenv("TZ", "").strip()
    if tz_env:
        candidates.append(tz_env)
    try:
        from tzlocal import get_localzone_name  # type: ignore[import-not-found]

        name = get_localzone_name()
        if name:
            candidates.append(name)
    except ImportError:
        pass
    names = getattr(_time, "tzname", None) or []
    candidates.extend(n for n in names if n)

    try:
        from zoneinfo import ZoneInfo

        for name in candidates:
            try:
                ZoneInfo(name)
                return name
            except Exception:
                continue
    except ImportError:
        pass
    return "UTC"


def _print_schedule(task: dict[str, object]) -> None:
    status = task.get("status", "enabled")
    status_text = "启用" if status == "enabled" else ("暂停" if status == "paused" else "已删除")
    running = "，运行中" if task.get("running") else ""
    # 枚举中文化:此前直接透出 enabled/origin/delivered 等英文原值。
    delivery_mode_map = {
        "origin": "来源渠道", "inbox": "收件箱", "explicit": "指定目标",
        "platform_home": "主渠道", "all": "全部", "none": "不投递",
    }
    mode = str(task.get("delivery_mode", "origin"))
    print(f"{task['id']}  {task['name']}  [{task['schedule_type']} {task['schedule_expr']}]  {status_text}{running}")
    print(f"  下次运行：{task.get('next_run_at') or '-'}")
    print(f"  角色/会话：{task.get('role') or '-'} / {task.get('session_id') or '-'}")
    print(f"  投递模式：{delivery_mode_map.get(mode, mode)}")
    if task.get("last_status"):
        print(f"  上次状态：{task.get('last_status')} {task.get('last_error') or ''}".rstrip())
    if task.get("last_delivery_status"):
        delivery_map = {
            "delivered": "已投递", "failed": "投递失败", "skipped": "跳过",
            "fallback_delivered": "已回落收件箱", "fallback_failed": "回落失败",
            "partial": "部分成功", "not_required": "无需投递", "resolved_empty": "无目标",
        }
        ds = str(task.get("last_delivery_status"))
        print(f"  上次投递：{delivery_map.get(ds, ds)} {task.get('last_delivery_error') or ''}".rstrip())


def cmd_schedule(args: argparse.Namespace) -> None:
    server_url = get_server_url()
    try:
        if args.schedule_command == "add":
            provided = [args.once is not None, args.interval is not None, args.cron is not None]
            if sum(provided) != 1:
                _err("请且仅请指定 --once、--interval、--cron 其中一个。")
                sys.exit(1)
            if args.once is not None:
                schedule_type, schedule_expr = "once", args.once
            elif args.interval is not None:
                schedule_type, schedule_expr = "interval", args.interval
            else:
                schedule_type, schedule_expr = "cron", args.cron
            payload = {
                "name": args.name,
                "prompt": args.prompt,
                "schedule_type": schedule_type,
                "schedule_expr": schedule_expr,
                "role": args.role,
                "session_id": args.session_id,
                # 默认用本机系统时区:中文用户几乎都期望"早上 9 点"按本地时间,
                # 此前默认 UTC 极易踩坑(--once 被当 UTC 解析,差 8 小时)。
                "timezone": args.timezone or _local_timezone_name(),
                "timeout_seconds": args.timeout,
                "delivery_mode": args.delivery_mode,
                "origin": {
                    "type": "cli_command",
                    "platform": "cli",
                },
            }
            if args.paused:
                payload["status"] = "paused"
            task = request_json(server_url, "POST", "/schedules", payload)
            _ok("定时任务已创建。")
            _print_schedule(task)
            return

        if args.schedule_command == "list":
            tasks = request_json(server_url, "GET", f"/schedules?include_deleted={str(args.all).lower()}")
            if not tasks:
                _info("暂无定时任务。")
                return
            for task in tasks:
                _print_schedule(task)
            return

        if args.schedule_command == "pause":
            task = request_json(server_url, "POST", f"/schedules/{args.id}/pause")
            _ok("定时任务已暂停。")
            _print_schedule(task)
            return

        if args.schedule_command == "resume":
            task = request_json(server_url, "POST", f"/schedules/{args.id}/resume")
            _ok("定时任务已恢复。")
            _print_schedule(task)
            return

        if args.schedule_command == "remove":
            request_json(server_url, "DELETE", f"/schedules/{args.id}")
            _ok("定时任务已删除。")
            return

        if args.schedule_command == "run":
            run = request_json(server_url, "POST", f"/schedules/{args.id}/run")
            _ok("定时任务已执行。")
            print(f"运行 ID：{run['id']}")
            print(f"状态：{run['status']}")
            if run.get("output"):
                print(f"输出：{str(run['output'])[:500]}")
            if run.get("error"):
                print(f"错误：{run['error']}")
            return

        if args.schedule_command == "inbox":
            runs = request_json(server_url, "GET", f"/schedules/inbox?limit={args.limit}&unread_only={str(not args.all).lower()}&mark_read={str(args.mark_read).lower()}")
            if not runs:
                _info("暂无定时任务收件箱消息。")
                return
            delivery_map = {
                "delivered": "已投递", "failed": "投递失败", "skipped": "跳过",
                "fallback_delivered": "已回落收件箱", "fallback_failed": "回落失败",
                "partial": "部分成功", "not_required": "无需投递", "resolved_empty": "无目标",
            }
            status_map = {"succeeded": "成功", "failed": "失败", "timeout": "超时", "running": "运行中"}
            for run in runs:
                ds = str(run.get("delivery_status") or "-")
                st = str(run.get("status", ""))
                print(
                    f"{run['id']}  {status_map.get(st, st)}  {run['started_at']}  "
                    f"{'-' if ds == '-' else delivery_map.get(ds, ds)}"
                )
                if run.get("error"):
                    print(f"  错误：{run['error']}")
                if run.get("output"):
                    print(f"  输出：{str(run['output'])[:500]}")
            return

        if args.schedule_command == "logs":
            runs = request_json(server_url, "GET", f"/schedules/{args.id}/runs?limit={args.limit}")
            if not runs:
                _info("暂无运行记录。")
                return
            status_map = {"succeeded": "成功", "failed": "失败", "timeout": "超时", "running": "运行中"}
            for run in runs:
                st = str(run.get("status", ""))
                print(f"{run['id']}  {status_map.get(st, st)}  {run['started_at']}  {run.get('duration_ms') or '-'}ms")
                if run.get("error"):
                    print(f"  错误：{run['error']}")
                if run.get("output"):
                    print(f"  输出：{str(run['output'])[:500]}")
            return
    except Exception as exc:
        # 服务端 400 等校验错误会带中文 detail;直接打印异常只会看到
        # "HTTP Error 400: Bad Request",真正的校验原因被吞掉。
        from openhachimi_agent.interface.cli import error_detail

        detail = error_detail(exc) if isinstance(exc, HTTPError) else str(exc)
        _err(f"定时任务命令失败：{detail}")
        if not isinstance(exc, HTTPError):
            # 只有非 HTTP 错误(连接失败等)才提示检查服务。
            _info(f"请确认后台服务已启动：{server_url}")
        sys.exit(1)

    _err("请指定 schedule 子命令。")
    sys.exit(1)


# ── 主入口 ────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="hachimi",
        description="OpenHachimi 管理工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
常用命令：
  hachimi                进入 CLI 对话（默认）
  hachimi status         查看后台服务状态
  hachimi restart        重启后台服务
  hachimi log            实时查看服务日志
  hachimi config         编辑配置文件
  hachimi install        安装 Playwright 浏览器驱动
  hachimi uninstall      卸载后台守护服务
  hachimi schedule       管理定时任务
  hachimi weixin         微信原生接入独立扫码登录""",
    )
    parser.add_argument(
        "-V",
        "--version",
        action="version",
        version=f"{PACKAGE_NAME} {get_version()}",
    )

    sub = parser.add_subparsers(dest="command", metavar="<子命令>")

    # ── 服务管理 ──────────────────────────────────────────────────────────────
    sub.add_parser("status",  help="查看后台服务状态")
    sub.add_parser("start",   help="启动后台服务")
    sub.add_parser("stop",    help="停止后台服务")
    sub.add_parser("restart", help="重启后台服务（每次重启自动轮换访问令牌）")

    log_p = sub.add_parser("log", help="实时查看服务日志（Ctrl-C 退出）")
    log_p.add_argument("-n", "--lines", type=int, default=None, metavar="N",
                       help="显示最近 N 行日志")
    log_p.add_argument("--no-follow", action="store_true",
                       help="不跟随日志，输出后直接退出")

    # ── 配置与工具 ────────────────────────────────────────────────────────────
    sub.add_parser("config",  help="用编辑器打开配置文件")
    sub.add_parser("install", help="安装 Playwright 浏览器驱动（chromium）")
    clean_p = sub.add_parser("clean-secrets", help="扫描并清理历史数据中的明文密钥（自动备份，-y 跳过确认）")
    clean_p.add_argument("-y", "--yes", action="store_true", help="跳过交互确认，直接执行")
    update_p = sub.add_parser("update",  help="检查并更新到最新版本")
    update_p.add_argument("--force", action="store_true", help="即使代码已是最新，也重新安装依赖")

    uninstall_p = sub.add_parser("uninstall", help="卸载后台守护服务")
    uninstall_p.add_argument("-y", "--yes", action="store_true",
                             help="跳过交互确认，直接执行")
    uninstall_p.add_argument("--purge", action="store_true",
                             help="同时删除虚拟环境（.venv）")
    uninstall_p.add_argument("--remove-all", action="store_true",
                             help="同时删除虚拟环境和整个项目目录（危险！）")

    # ── 部署与运行 ────────────────────────────────────────────────────────────
    deploy_p = sub.add_parser("deploy", help="部署并注册后台守护服务")
    deploy_p.add_argument("--host", default=None, help="监听地址，覆盖配置文件 app.server_host（默认 127.0.0.1 仅本机，0.0.0.0 开放访问）")
    deploy_p.add_argument("--port", type=int, default=None, help="监听端口，覆盖配置文件 app.server_port（默认 8765）")

    serve_p = sub.add_parser("serve", help="在前台运行 HTTP 服务（调试用）")
    serve_p.add_argument("--host", default=None, help="监听地址，覆盖配置文件 app.server_host（默认 127.0.0.1 仅本机，0.0.0.0 开放访问）")
    serve_p.add_argument("--port", type=int, default=None, help="监听端口，覆盖配置文件 app.server_port（默认 8765）")

    sub.add_parser("cli", help="进入 CLI 对话（与默认行为相同）")
    
    sub.add_parser("weixin", help="微信原生扫码登录配置")

    schedule_p = sub.add_parser("schedule", help="管理定时任务")
    schedule_sub = schedule_p.add_subparsers(dest="schedule_command", metavar="<操作>")

    schedule_add = schedule_sub.add_parser("add", help="创建定时任务")
    schedule_add.add_argument("--name", required=True, help="任务名称")
    schedule_add.add_argument("--prompt", required=True, help="到期后提交给 Agent 的提示词")
    schedule_add.add_argument("--once", help="一次性运行时间，例如 2026-05-27T10:30:00+08:00")
    schedule_add.add_argument("--interval", help="循环间隔，例如 10m、2h、86400")
    schedule_add.add_argument("--cron", help="cron 表达式，例如 '0 9 * * *'")
    schedule_add.add_argument(
        "--timezone",
        default=None,
        help="任务时区，默认取本机系统时区（如 Asia/Shanghai）。--once 时间与 cron 都按该时区解释",
    )
    schedule_add.add_argument("--role", default=None, help="使用的角色")
    schedule_add.add_argument("--session-id", default=None, help="使用的会话 ID")
    schedule_add.add_argument("--timeout", type=int, default=None, help="单次执行超时时间（秒）")
    schedule_add.add_argument("--paused", action="store_true", help="创建后先暂停")
    schedule_add.add_argument("--delivery-mode", default="origin", help="投递模式：origin/inbox/explicit/none，默认 origin")

    schedule_list = schedule_sub.add_parser("list", help="列出定时任务")
    schedule_list.add_argument("--all", action="store_true", help="包含已删除的任务")

    for action, help_text in (("pause", "暂停定时任务"), ("resume", "恢复定时任务"), ("remove", "删除定时任务"), ("run", "立即执行定时任务")):
        action_p = schedule_sub.add_parser(action, help=help_text)
        action_p.add_argument("id", help="任务 ID")
    inbox_p = schedule_sub.add_parser("inbox", help="查看定时任务收件箱")
    inbox_p.add_argument("--limit", type=int, default=20, help="记录数量")
    inbox_p.add_argument("--all", action="store_true", help="包含已读记录")
    inbox_p.add_argument("--mark-read", action="store_true", help="显示后标记为已读")

    logs_p = schedule_sub.add_parser("logs", help="查看运行记录")
    logs_p.add_argument("id", help="任务 ID")
    logs_p.add_argument("--limit", type=int, default=20, help="记录数量")

    args = parser.parse_args()

    # 命令分发表
    _dispatch = {
        "status":    cmd_status,
        "start":     cmd_start,
        "stop":      cmd_stop,
        "restart":   cmd_restart,
        "log":       cmd_log,
        "config":    cmd_config,
        "clean-secrets": cmd_clean_secrets,
        "install":   cmd_install,
        "update":    cmd_update,
        "uninstall": cmd_uninstall,
        "deploy":    cmd_deploy,
        "serve":     cmd_serve,
        "schedule":  cmd_schedule,
        "cli":       cmd_cli,
        "weixin":    cmd_weixin,
    }

    if args.command in _dispatch:
        _dispatch[args.command](args)
    else:
        # 无子命令 → 默认进入 CLI 对话
        try:
            asyncio.run(run_embedded_cli())
        except KeyboardInterrupt:
            print("\n已退出对话。")


if __name__ == "__main__":
    main()
