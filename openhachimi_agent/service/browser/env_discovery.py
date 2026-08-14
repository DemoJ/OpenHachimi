"""Linux/桌面会话环境发现（供 Chrome 启动时继承 DISPLAY/XAUTHORITY 等）。

原实现内嵌在 lifecycle.py,此处独立成模块函数：
- 服务进程缺桌面环境变量时,从同用户图形会话进程 /proc 补齐；
- 锁屏/会话切换后 mutter 轮换 Xwayland cookie,动态刷新 XAUTHORITY；
- 纯 X11 会话拿不到环境时按 /tmp/.X11-unix socket 兜底推断 DISPLAY。
"""

import logging
import os
import socket
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

_WANTED_ENV_KEYS = (
    "DISPLAY",
    "WAYLAND_DISPLAY",
    "XAUTHORITY",
    "DBUS_SESSION_BUS_ADDRESS",
    "XDG_CURRENT_DESKTOP",
    "XDG_RUNTIME_DIR",
    "XDG_SESSION_TYPE",
)

_PREFERRED_PROCESS_NAMES = (
    "gnome-session",
    "gnome-shell",
    "plasmashell",
    "xfce4-session",
    "xrdp",
    "Xorg",
    "Xwayland",
    "chrome",
    "chromium",
)

# 虚拟显示进程：窗口渲染在其上用户不可见，绝不能作为有头模式的候选。
_VIRTUAL_DISPLAY_NAMES = (
    "xvfb",
    "xvnc",
    "x11vnc",
    "wayvnc",
    "weston",
)


def read_process_environ(pid: str) -> dict[str, str]:
    """读取 /proc/<pid>/environ 并解出环境变量（读取失败返回空 dict）。"""
    environ_path = f"/proc/{pid}/environ"
    try:
        with open(environ_path, "rb") as file:
            raw = file.read()
    except OSError:
        return {}

    env: dict[str, str] = {}
    for item in raw.split(b"\0"):
        if not item or b"=" not in item:
            continue
        key, value = item.split(b"=", 1)
        try:
            env[key.decode("utf-8")] = value.decode("utf-8")
        except UnicodeDecodeError:
            continue
    return env


def _x_display_alive(display: str) -> bool:
    """探测 DISPLAY 对应的 X server 是否仍存活（服务继承的 DISPLAY 可能已随
    桌面会话注销而失效，文件 socket 残留但无人监听，必须实际连接验证）。"""
    try:
        display_num = int(display.split(":", 1)[1].split(".", 1)[0])
    except (IndexError, ValueError):
        return False
    # Linux 上 X server 同时监听抽象 socket 与文件 socket，任一可连即存活。
    candidates = (f"\0/tmp/.X11-unix/X{display_num}", f"/tmp/.X11-unix/X{display_num}")
    for path in candidates:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(0.5)
                sock.connect(path)
            return True
        except OSError:
            continue
    return False


def _wayland_alive(wayland_display: str) -> bool:
    """探测 Wayland compositor socket 是否仍存活。"""
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    path = Path(runtime_dir) / wayland_display
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.5)
            sock.connect(str(path))
        return True
    except OSError:
        return False


def session_is_locked() -> bool | None:
    """通过 GNOME ScreenSaver DBus 判断桌面会话是否锁屏；无法查询返回 None（不拦截）。

    锁屏时 mutter 不处理新窗口握手，Chrome（无论 Wayland 还是 X11 通道）都会
    卡死在显示初始化。启动前先查一次，命中锁屏就立即报错引导用户解锁，
    而不是傻等 CDP 超时 × 3 次重试。
    """
    bus = os.environ.get("DBUS_SESSION_BUS_ADDRESS") or f"unix:path=/run/user/{os.getuid()}/bus"
    try:
        result = subprocess.run(
            [
                "dbus-send", "--session", f"--bus={bus}",
                "--dest=org.gnome.ScreenSaver", "--type=method_call", "--print-reply",
                "/org/gnome/ScreenSaver", "org.gnome.ScreenSaver.GetActive",
            ],
            capture_output=True, text=True, timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return "true" in result.stdout.lower()


def _browser_process_env_error() -> str:
    return (
        "当前没有可用的图形会话（Wayland 和 X11 均不可达，或桌面已锁屏）。"
        "请先在 Ubuntu 桌面上登录并解锁一次图形会话（解锁 Wayland 会话），"
        "浏览器窗口才会显示；或将 app.browser_headless 改为 true 使用无头模式。"
    )


def discover_linux_desktop_env(current_env: dict[str, str]) -> dict[str, str]:
    """从同用户的图形会话进程中补齐 systemd 后台服务缺失的桌面环境变量。"""
    if sys.platform != "linux":
        return {}

    current = {key: value for key in current_env if (value := current_env.get(key))}
    if current.get("DISPLAY") or current.get("WAYLAND_DISPLAY"):
        # 服务进程持有继承的桌面环境,但锁屏/会话切换后 mutter 会轮换
        # .mutter-Xwaylandauth.* cookie 文件,继承的 XAUTHORITY 可能已失效,
        # 导致 Chrome X11 模式报 Invalid MIT-MAGIC-COOKIE-1 key 后退出。
        # 即使已有 DISPLAY,也要动态刷新 XAUTHORITY。
        if current.get("DISPLAY") and not _x_display_alive(current["DISPLAY"]):
            # 继承的 DISPLAY 指向的 X server 已随桌面会话注销而消失，
            # 丢弃继承值重新发现，否则 Chrome 会被塞进一个不存在的显示。
            logger.warning(
                "继承的 DISPLAY=%s 无存活 X server，丢弃后重新发现桌面环境。",
                current["DISPLAY"],
            )
            current.pop("DISPLAY", None)
            current.pop("XAUTHORITY", None)
            # WAYLAND_DISPLAY 与 DISPLAY 同属一个旧会话，一并丢弃强制重新发现，
            # 否则残留的 WAYLAND 会让 headless=False 的存活检查误放行。
            current.pop("WAYLAND_DISPLAY", None)
        if current.get("DISPLAY"):
            return refresh_xauthority_for_display(current)

    uid = os.getuid()
    candidates: list[dict[str, str]] = []

    try:
        proc_entries = list(os.scandir("/proc"))
    except OSError:
        return current

    for entry in proc_entries:
        if not entry.name.isdigit():
            continue
        try:
            if entry.stat(follow_symlinks=False).st_uid != uid:
                continue
        except OSError:
            continue

        env = read_process_environ(entry.name)
        if not (env.get("DISPLAY") or env.get("WAYLAND_DISPLAY")):
            continue
        # 候选的 DISPLAY 必须指向存活 X server：服务进程自身/残留 session 可能
        # 带着已注销桌面的 DISPLAY，若不校验会让 Chrome 被塞进死掉的显示。
        if env.get("DISPLAY") and not _x_display_alive(env["DISPLAY"]):
            continue

        try:
            with open(f"/proc/{entry.name}/comm", "r", encoding="utf-8") as file:
                name = file.read().strip()
        except OSError:
            name = ""

        # 排除虚拟显示进程（Xvfb 等）：窗口在虚拟屏上，用户看不到。
        # comm 名称大小写不敏感（如 "Xvfb"）。
        if any(token in name.lower() for token in _VIRTUAL_DISPLAY_NAMES):
            continue

        score = 0
        if env.get("DISPLAY"):
            score += 2
        if env.get("DBUS_SESSION_BUS_ADDRESS"):
            score += 2
        if env.get("XAUTHORITY"):
            score += 1
        if any(token in name for token in _PREFERRED_PROCESS_NAMES):
            score += 3
        env["_score"] = str(score)
        candidates.append(env)

    if not candidates:
        inferred = infer_x11_env_from_socket()
        if inferred:
            logger.info("从 X11 socket 推断浏览器环境: DISPLAY=%s", inferred.get("DISPLAY"))
            return refresh_xauthority_for_display(inferred)
        return current

    best = max(candidates, key=lambda item: int(item.get("_score", "0")))
    desktop_env = {key: value for key in _WANTED_ENV_KEYS if (value := best.get(key))}
    logger.info(
        "从现有桌面会话补齐浏览器环境: DISPLAY=%s WAYLAND_DISPLAY=%s XDG_SESSION_TYPE=%s",
        desktop_env.get("DISPLAY"),
        desktop_env.get("WAYLAND_DISPLAY"),
        desktop_env.get("XDG_SESSION_TYPE"),
    )
    return refresh_xauthority_for_display(desktop_env)


def refresh_xauthority_for_display(env: dict[str, str]) -> dict[str, str]:
    """刷新 XAUTHORITY:锁屏/会话切换后 mutter 会轮换 .mutter-Xwaylandauth.* 文件,
    服务进程继承的旧 cookie 失效会导致 Chrome X11 模式连接被拒。这里扫描候选
    Xauthority 文件,选一个 mtime 最新且可用 cookie 的文件覆盖 env。

    候选来源(按优先级):
    1. 桌面会话进程(/proc/<pid>/environ 里 XAUTHORITY 指向的文件)
    2. /run/user/<uid>/.mutter-Xwaylandauth.*(mutter 为 Xwayland 生成的 cookie)
    3. ~/.Xauthority
    """
    if sys.platform != "linux":
        return env
    display = env.get("DISPLAY")
    if not display:
        return env

    candidates: list[tuple[Path, float]] = []

    def _candidate(path: Path) -> None:
        try:
            stat = path.stat()
        except OSError:
            return
        candidates.append((path, stat.st_mtime))

    # 1. 桌面会话进程指向的 XAUTHORITY
    uid = os.getuid()
    for entry in os.scandir("/proc"):
        if not entry.name.isdigit():
            continue
        try:
            if entry.stat(follow_symlinks=False).st_uid != uid:
                continue
        except OSError:
            continue
        proc_env = read_process_environ(entry.name)
        if xauth := proc_env.get("XAUTHORITY"):
            _candidate(Path(xauth))

    # 2. mutter 的 Xwayland cookie 文件
    runtime_dir = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{uid}")
    try:
        for path in runtime_dir.glob(".mutter-Xwaylandauth.*"):
            _candidate(path)
    except OSError:
        pass

    # 3. ~/.Xauthority 兜底
    _candidate(Path.home() / ".Xauthority")

    # 继承值去重并保留(即使 mtime 不是最新,只要可用就优先,避免反复切换文件)
    inherited = env.get("XAUTHORITY")
    sorted_candidates = sorted(candidates, key=lambda item: item[1], reverse=True)
    tried: set[str] = set()
    ordered: list[Path] = []
    if inherited:
        inherited_path = Path(inherited)
        if inherited_path in {item[0] for item in sorted_candidates}:
            ordered.append(inherited_path)
            tried.add(str(inherited_path))
    for path, _mtime in sorted_candidates:
        key = str(path)
        if key not in tried:
            tried.add(key)
            ordered.append(path)

    for path in ordered:
        if xauthority_covers_display(path, display):
            if str(path) != inherited:
                logger.info(
                    "刷新 XAUTHORITY: %s -> %s (DISPLAY=%s)",
                    inherited or "(继承)",
                    path,
                    display,
                )
                env["XAUTHORITY"] = str(path)
            return env

    if inherited:
        logger.warning(
            "未找到可用 Xauthority 文件,沿用继承值 XAUTHORITY=%s (DISPLAY=%s)",
            inherited,
            display,
        )
    return env


def xauthority_covers_display(path: Path, display: str) -> bool:
    """检查 Xauthority 文件是否包含目标 DISPLAY 的 cookie 条目(纯解析,不依赖 xauth 命令)。

    Xauthority 条目格式:family(2B) addr_len(2B) addr num_len(2B) num name_len(2B) name
    data_len(2B) data,全字段大端。DISPLAY 形如 ":1" / ":1.0",本地连接匹配
    FamilyLocal(256) 或 FamilyWild(65535) 且 number 与 display 数字一致的条目。
    """
    try:
        raw = path.read_bytes()
    except OSError:
        return False

    try:
        display_num = int(display.split(":", 1)[1].split(".", 1)[0])
    except (IndexError, ValueError):
        return False

    offset = 0
    while offset + 2 <= len(raw):
        family = int.from_bytes(raw[offset:offset + 2], "big")
        offset += 2
        if offset + 2 > len(raw):
            break
        addr_len = int.from_bytes(raw[offset:offset + 2], "big")
        offset += 2
        if offset + addr_len > len(raw):
            break
        offset += addr_len
        if offset + 2 > len(raw):
            break
        num_len = int.from_bytes(raw[offset:offset + 2], "big")
        offset += 2
        if offset + num_len > len(raw):
            break
        num_raw = raw[offset:offset + num_len]
        offset += num_len
        try:
            entry_num = int(num_raw.decode("ascii", "ignore")) if num_raw else None
        except ValueError:
            entry_num = None
        if offset + 2 > len(raw):
            break
        name_len = int.from_bytes(raw[offset:offset + 2], "big")
        offset += 2
        if offset + name_len > len(raw):
            break
        offset += name_len
        if offset + 2 > len(raw):
            break
        data_len = int.from_bytes(raw[offset:offset + 2], "big")
        offset += 2
        if offset + data_len > len(raw):
            break
        offset += data_len
        if family in {256, 65535}:
            # number 为空(mutter 的 Xwayland cookie)视为通配,匹配任意 DISPLAY
            if entry_num is None or entry_num == display_num:
                return True
    return False


def infer_x11_env_from_socket() -> dict[str, str]:
    """在拿不到会话环境时，根据 /tmp/.X11-unix/X* 兜底推断 X11 DISPLAY。"""
    if sys.platform != "linux":
        return {}

    socket_dir = "/tmp/.X11-unix"
    try:
        entries = [
            entry for entry in os.scandir(socket_dir)
            if entry.name.startswith("X") and entry.name[1:].isdigit()
        ]
    except OSError:
        return {}

    if not entries:
        return {}

    try:
        best = max(entries, key=lambda entry: entry.stat(follow_symlinks=False).st_mtime)
    except OSError:
        return {}

    # 残留的 socket 文件（桌面临时注销）可能没人监听，必须确认 X server 存活。
    inferred_display = f":{best.name[1:]}"
    if not _x_display_alive(inferred_display):
        return {}

    env = {
        "DISPLAY": inferred_display,
        "XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}",
        "DBUS_SESSION_BUS_ADDRESS": f"unix:path=/run/user/{os.getuid()}/bus",
    }
    xauthority = os.path.expanduser("~/.Xauthority")
    if os.path.exists(xauthority):
        env["XAUTHORITY"] = xauthority
    return env


def browser_process_env(headless: bool) -> dict[str, str]:
    """组合 Chrome 进程环境变量（NO_PROXY 本地绕过 + Linux 桌面环境发现）。

    有头模式策略（Wayland 优先）：
    1. Wayland socket 存活 → 保留 WAYLAND_DISPLAY，Chrome 原生 Wayland；
    2. Wayland 不可用但 X server 存活 → 去掉 WAYLAND_DISPLAY，Chrome 自动落回 X11；
    3. 两者都不可用 → 明确报错并引导用户「登录一次 Ubuntu 桌面解锁图形会话」，
       而不是把窗口塞进虚拟屏/死显示然后悄悄失败。
    """
    env = os.environ.copy()
    local_no_proxy = ["127.0.0.1", "localhost", "::1"]
    for key in ("NO_PROXY", "no_proxy"):
        values = [item.strip() for item in env.get(key, "").split(",") if item.strip()]
        existing = {item.lower() for item in values}
        for host in local_no_proxy:
            if host.lower() not in existing:
                values.append(host)
        env[key] = ",".join(values)
    if sys.platform == "linux":
        env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        env.update(discover_linux_desktop_env(env))
        if not headless:
            # Wayland 优先：socket 不存活时移除 WAYLAND_DISPLAY，让 Chrome 走 X11 回退。
            wayland = env.get("WAYLAND_DISPLAY")
            if wayland and not _wayland_alive(wayland):
                logger.warning(
                    "WAYLAND_DISPLAY=%s 无存活 compositor，回退 X11（若桌面锁屏/未登录请先解锁）。",
                    wayland,
                )
                env.pop("WAYLAND_DISPLAY", None)

            locked = session_is_locked()
            if locked:
                raise RuntimeError(
                    "检测到桌面会话已锁屏。请先在 Ubuntu 桌面解锁（或登录一次图形会话），"
                    "然后重试；mutter 锁屏时不会处理新窗口握手，Chrome 会卡死在显示初始化。"
                    "如无需窗口也可将 app.browser_headless 改为 true 使用无头模式。"
                )

            if not (env.get("DISPLAY") or env.get("WAYLAND_DISPLAY")):
                raise RuntimeError(
                    "当前没有可用的图形会话（DISPLAY 与 WAYLAND_DISPLAY 均无存活 server）。"
                    "请先在 Ubuntu 桌面登录一次图形会话（解锁 Wayland），"
                    "再重试；或把 app.browser_headless 改为 true 使用无头模式。"
                )
    return env