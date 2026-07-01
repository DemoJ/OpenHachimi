#!/usr/bin/env bash
# =============================================================================
# OpenHachimi 一键部署脚本（支持自举）
#
# 用法一：直接下载运行（自动 clone 项目）
#   curl -fsSL https://raw.githubusercontent.com/DemoJ/OpenHachimi/main/deploy.sh | bash
#
# 用法二：在项目目录中运行
#   bash deploy.sh [选项]
#
# 选项：
#   -H, --host HOST       监听地址（留空则用配置文件 app.server_host，默认 127.0.0.1）
#   -p, --port PORT       监听端口（留空则用配置文件 app.server_port，默认 8765）
#   --skip-daemon         只安装依赖，不部署后台守护服务
#   --skip-webui          跳过 WebUI 前端构建（/ui 页面将不可用）
#   --repo URL            自定义 Git 仓库地址
#   --dir DIR             指定克隆目标目录（默认 ./OpenHachimi）
#   -h, --help            显示帮助
# =============================================================================

set -euo pipefail

# ── 颜色输出 ──────────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
RESET='\033[0m'

info()    { echo -e "${BLUE}[INFO]${RESET}  $*"; }
success() { echo -e "${GREEN}[OK]${RESET}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${RESET}  $*"; }
error()   { echo -e "${RED}[ERROR]${RESET} $*" >&2; exit 1; }

# ── 默认参数 ──────────────────────────────────────────────────────────────────
# 留空表示不向 hachimi deploy 传参，改用配置文件 app.server_host / app.server_port。
HOST=""
PORT=""
SKIP_DAEMON=false
SKIP_WEBUI=false
REPO_URL="https://github.com/DemoJ/OpenHachimi.git"
CLONE_DIR="./OpenHachimi"

# ── 解析命令行参数 ────────────────────────────────────────────────────────────
usage() {
    cat <<EOF
${BOLD}OpenHachimi 一键部署脚本${RESET}

用法：
  bash deploy.sh [选项]

选项：
  -H, --host HOST       后台服务监听地址（留空则用配置文件 app.server_host，默认 127.0.0.1 仅本机）
  -p, --port PORT       后台服务监听端口（留空则用配置文件 app.server_port，默认 8765）
  --skip-daemon         只安装依赖，不部署后台守护服务
  --skip-webui          跳过 WebUI 前端构建（/ui 页面将不可用）
  --repo URL            自定义 Git 仓库地址
  --dir DIR             指定克隆目标目录（默认：./OpenHachimi）
  -h, --help            显示此帮助并退出

示例：
  # 一键下载并部署（无需提前 clone 项目）
  curl -fsSL https://raw.githubusercontent.com/DemoJ/OpenHachimi/main/deploy.sh | bash

  # 指定监听地址和端口
  bash deploy.sh --host 0.0.0.0 --port 9000

  # 只安装，不启动后台守护
  bash deploy.sh --skip-daemon
EOF
    exit 0
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        -H|--host)      HOST="$2"; shift 2 ;;
        -p|--port)      PORT="$2"; shift 2 ;;
        --skip-daemon)  SKIP_DAEMON=true; shift ;;
        --skip-webui)   SKIP_WEBUI=true; shift ;;
        --repo)         REPO_URL="$2"; shift 2 ;;
        --dir)          CLONE_DIR="$2"; shift 2 ;;
        -h|--help)      usage ;;
        *)              error "未知参数：$1，使用 -h 查看帮助。" ;;
    esac
done

echo ""
echo -e "${BOLD}=== OpenHachimi 部署 ===${RESET}"
echo ""

# ── 步骤 1：检查 Python 版本 ──────────────────────────────────────────────────
info "步骤 1/5：检查 Python 环境..."

PYTHON=""
for cmd in python3 python; do
    if command -v "$cmd" &>/dev/null; then
        MAJOR="$($cmd -c 'import sys; print(sys.version_info.major)')"
        MINOR="$($cmd -c 'import sys; print(sys.version_info.minor)')"
        if [[ "$MAJOR" -ge 3 && "$MINOR" -ge 10 ]]; then
            PYTHON="$cmd"
            success "找到 Python $MAJOR.$MINOR（$cmd）"
            break
        else
            warn "$cmd 版本过低（$MAJOR.$MINOR），需要 3.10+，跳过。"
        fi
    fi
done

if [[ -z "$PYTHON" ]]; then
    error "未找到 Python 3.10 或更高版本。\n请先安装：\n  Ubuntu/Debian：sudo apt install python3\n  CentOS/RHEL：  sudo dnf install python3\n  Arch：         sudo pacman -S python"
fi

# ── 确定项目根目录（deploy.sh 必须在项目根目录内运行）────────────────────────
# 脚本自身就在项目根，或者由 install.sh exec 过来（已 cd 好）
if [[ -f "pyproject.toml" ]]; then
    PROJECT_ROOT="$(pwd)"
elif [[ -f "$(dirname "${BASH_SOURCE[0]}")/pyproject.toml" ]]; then
    PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    cd "$PROJECT_ROOT"
else
    error "找不到 pyproject.toml，请在项目根目录内运行此脚本，或使用 install.sh 安装。"
fi

success "项目目录：$PROJECT_ROOT"

VENV_DIR="$PROJECT_ROOT/.venv"
CONFIG_EXAMPLE="$PROJECT_ROOT/user/config.example.yaml"
CONFIG_FILE="$PROJECT_ROOT/user/config.yaml"

# ── 辅助函数：检测 venv 是否健全（python + pip 均可用）────────────────────────
venv_is_healthy() {
    [[ -f "$VENV_DIR/bin/python" ]] && "$VENV_DIR/bin/python" -m pip --version &>/dev/null
}

# ── 辅助函数：尝试安装 python3-venv（仅 apt-get 环境）──────────────────────
install_venv_pkg() {
    local minor
    minor="$("$PYTHON" -c 'import sys; print(sys.version_info.minor)')"
    local pkg="python3.${minor}-venv"
    warn "检测到缺少虚拟环境支持包，尝试自动安装 $pkg ..."
    if command -v apt-get &>/dev/null; then
        if sudo apt-get install -y "$pkg" >/dev/null 2>&1; then
            success "已安装 $pkg。"
            return 0
        fi
        # 部分系统只有 python3-venv 而没有 python3.X-venv
        warn "$pkg 安装失败，尝试 python3-venv ..."
        if sudo apt-get install -y python3-venv >/dev/null 2>&1; then
            success "已安装 python3-venv。"
            return 0
        fi
        error "自动安装失败，请手动执行：\n  sudo apt-get install -y $pkg\n然后重新运行此脚本。"
    else
        error "创建虚拟环境失败。请先安装 python3-venv（或系统等效包）后重试。"
    fi
}

# ── 辅助函数：创建 venv，失败时自动修复并重试 ─────────────────────────────
create_venv() {
    rm -rf "$VENV_DIR"   # 清除可能存在的残破目录

    # 提前检测 ensurepip 是否可用（Ubuntu/Debian 默认不包含，会导致 venv 创建失败）
    # 比捕获错误文本更可靠，避免 stdout/stderr 重定向问题
    if ! "$PYTHON" -c "import ensurepip" 2>/dev/null; then
        install_venv_pkg
    fi

    # 创建 venv
    if ! "$PYTHON" -m venv "$VENV_DIR" 2>&1; then
        error "创建虚拟环境失败，请检查你的 Python 安装。"
    fi
}

# ── 辅助函数：构建 WebUI 前端 ─────────────────────────────────────────────────
# 产物输出到 openhachimi_agent/webui_dist/（见 webui/vite.config.ts），
# 后端 http.py 会从该目录挂载 /ui。由于 webui_dist 被 .gitignore 排除，
# 线上 git clone/pull 后不会自带产物，必须在部署时构建。
# 返回 0 构建成功，返回 1 跳过（Node/npm 缺失等非致命情况）。
build_webui() {
    local webui_dir="$PROJECT_ROOT/webui"
    if [[ ! -d "$webui_dir" ]]; then
        warn "未找到 webui 目录（$webui_dir），跳过前端构建。"
        return 1
    fi

    # 检测 Node.js / npm
    if ! command -v node &>/dev/null || ! command -v npm &>/dev/null; then
        warn "未检测到 Node.js / npm，跳过前端构建。/ui 网页将不可用（API 不受影响）。"
        warn "安装 Node.js 18+ 后重新运行本脚本即可："
        warn "  Ubuntu/Debian：curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash - && sudo apt-get install -y nodejs"
        warn "  macOS：        brew install node"
        return 1
    fi

    local node_ver node_major
    node_ver="$(node -p 'process.versions.node')"
    node_major="${node_ver%%.*}"
    if [[ "$node_major" -lt 18 ]]; then
        warn "Node.js 版本过低（v${node_ver}），Vite 5 需要 18+，跳过前端构建。"
        return 1
    fi
    success "检测到 Node.js v${node_ver}。"

    # npm ci 会删 node_modules 全量重装，更新场景太慢。
    # 有 node_modules 走 npm install --prefer-offline（优先本地缓存），首次才 npm ci。
    # --foreground-scripts 让 postinstall 输出可见，避免「卡住」错觉。
    local npm_flags=(--no-audit --no-fund --foreground-scripts)
    info "安装前端依赖..."
    if [[ -d "$webui_dir/node_modules" ]]; then
        if ! (cd "$webui_dir" && npm install --prefer-offline "${npm_flags[@]}"); then
            error "前端依赖安装失败，请查看上方错误信息。"
        fi
    elif [[ -f "$webui_dir/package-lock.json" ]]; then
        if ! (cd "$webui_dir" && npm ci "${npm_flags[@]}"); then
            warn "npm ci 失败，改用 npm install..."
            (cd "$webui_dir" && npm install --prefer-offline "${npm_flags[@]}") || \
                error "前端依赖安装失败，请查看上方错误信息。"
        fi
    else
        (cd "$webui_dir" && npm install --prefer-offline "${npm_flags[@]}") || \
            error "前端依赖安装失败，请查看上方错误信息。"
    fi

    info "构建前端（npm run build）..."
    (cd "$webui_dir" && npm run build) || \
        error "前端构建失败，请查看上方错误信息。\n可使用 --skip-webui 跳过构建以先部署后端 API。"

    success "前端构建完成，产物位于 openhachimi_agent/webui_dist/。"
    return 0
}

# ── 步骤 3：准备虚拟环境 ────────────────────────────────────────────────────
info "步骤 2/5：准备虚拟环境..."

if venv_is_healthy; then
    success "虚拟环境健全，复用：$VENV_DIR"
elif [[ -d "$VENV_DIR" ]]; then
    warn "检测到残破的虚拟环境（pip 不可用），清除并重建..."
    create_venv
    success "虚拟环境重建完成。"
else
    info "创建虚拟环境：$VENV_DIR"
    create_venv
    success "虚拟环境创建完成。"
fi

# 最终校验
if ! venv_is_healthy; then
    error "虚拟环境创建后 pip 仍不可用，请检查你的 Python 安装。"
fi

VENV_PYTHON="$VENV_DIR/bin/python"
VENV_HACHIMI="$VENV_DIR/bin/hachimi"

# ── 步骤 4：安装依赖 ─────────────────────────────────────────────────────────
info "步骤 3/5：安装项目依赖（pip install -e .）..."

# 外网访问不稳定时，pip 直连 pypi.org 易在 SSL 握手阶段失败（SSLEOFError）。
# 用户已通过 PIP_INDEX_URL/PIP_INDEX 指定镜像源时不覆盖；否则默认走清华 TUNA 镜像。
# 加大 retries/timeout 容忍抖动；镜像源参数会传递给 PEP 517 build 阶段（setuptools/wheel）。
if [[ -z "${PIP_INDEX_URL:-}" && -z "${PIP_INDEX:-}" ]]; then
    PIP_MIRROR_ARGS=(-i https://pypi.tuna.tsinghua.edu.cn/simple --trusted-host pypi.tuna.tsinghua.edu.cn)
else
    PIP_MIRROR_ARGS=()
fi
PIP_ROBUST_ARGS=(--retries 5 --timeout 60)

# pip 升级非必需，失败时只告警不中断后续依赖安装。
if ! "$VENV_PYTHON" -m pip install -U pip "${PIP_ROBUST_ARGS[@]}" "${PIP_MIRROR_ARGS[@]}" --quiet 2>/tmp/_oh_pip_err; then
    cat /tmp/_oh_pip_err >&2
    warn "pip 升级失败，已跳过（不影响后续依赖安装）。"
fi

if ! "$VENV_PYTHON" -m pip install -e "$PROJECT_ROOT" "${PIP_ROBUST_ARGS[@]}" "${PIP_MIRROR_ARGS[@]}" --quiet 2>/tmp/_oh_pip_err; then
    cat /tmp/_oh_pip_err >&2
    error "依赖安装失败，请查看上方错误信息。\n常见原因：\n  - 外网访问不稳定（SSL 握手失败），可设置镜像源：export PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple\n  - 需要走代理：export HTTPS_PROXY=http://127.0.0.1:7890\n  - 缺少系统编译依赖（尝试：sudo apt-get install -y build-essential）"
fi

success "依赖安装完成。"

# ── 步骤 4/5：构建 WebUI 前端 ────────────────────────────────────────────────
info "步骤 4/5：构建 WebUI 前端..."
if [[ "$SKIP_WEBUI" == true ]]; then
    warn "已跳过前端构建（--skip-webui）。/ui 网页将不可用，API 不受影响。"
else
    if build_webui; then
        :
    else
        warn "前端构建已跳过，后台 API 仍可正常使用，但 /ui 网页不可访问。"
    fi
fi

# ── 初始化配置文件（步骤 4.5，嵌入步骤 4 和 5 之间）────────────────────────
if [[ -f "$CONFIG_FILE" ]]; then
    success "配置文件已存在：$CONFIG_FILE"
else
    if [[ -f "$CONFIG_EXAMPLE" ]]; then
        mkdir -p "$(dirname "$CONFIG_FILE")"
        cp "$CONFIG_EXAMPLE" "$CONFIG_FILE"
        warn "已从模板创建配置文件：$CONFIG_FILE"
        warn "⚠  请在启动服务前编辑该文件，填写你的 llm.api_key 等配置！"
        warn "   编辑命令：nano $CONFIG_FILE"
    else
        warn "未找到配置模板 $CONFIG_EXAMPLE，跳过配置文件初始化。"
    fi
fi

# ── 步骤 5：部署后台守护服务 ─────────────────────────────────────────────────
if [[ "$SKIP_DAEMON" == true ]]; then
    info "步骤 5/5：已跳过后台守护部署（--skip-daemon）。"
else
    # HOST/PORT 为空时不传参，由 hachimi deploy 读取配置文件 app.server_host/server_port。
    DEPLOY_ARGS=()
    [[ -n "$HOST" ]] && DEPLOY_ARGS+=(--host "$HOST")
    [[ -n "$PORT" ]] && DEPLOY_ARGS+=(--port "$PORT")
    if [[ ${#DEPLOY_ARGS[@]} -eq 0 ]]; then
        info "步骤 5/5：部署后台守护服务（host/port 取自配置文件）..."
    else
        info "步骤 5/5：部署后台守护服务（host=$HOST port=$PORT，覆盖配置文件）..."
    fi
    "$VENV_HACHIMI" deploy "${DEPLOY_ARGS[@]}"
fi

# ── 完成提示 ─────────────────────────────────────────────────────────────────

# 检查 hachimi 是否已在 PATH 中
HACHIMI_CMD="$VENV_HACHIMI"
if command -v hachimi &>/dev/null; then
    HACHIMI_CMD="hachimi"
fi

echo ""
echo -e "${GREEN}${BOLD}========================================${RESET}"
echo -e "${GREEN}${BOLD}  部署完成！${RESET}"
echo -e "${GREEN}${BOLD}========================================${RESET}"
echo ""
echo -e "  项目目录：${BOLD}$PROJECT_ROOT${RESET}"
echo -e "  可执行文件：${BOLD}$VENV_HACHIMI${RESET}"
echo ""
echo -e "  ${BOLD}常用命令：${RESET}"
echo -e "    ${BOLD}$HACHIMI_CMD${RESET}            进入 CLI 对话"
echo -e "    ${BOLD}$HACHIMI_CMD status${RESET}      查看后台服务状态"
echo -e "    ${BOLD}$HACHIMI_CMD start${RESET}       启动后台服务"
echo -e "    ${BOLD}$HACHIMI_CMD stop${RESET}        停止后台服务"
echo -e "    ${BOLD}$HACHIMI_CMD restart${RESET}     重启后台服务"
echo -e "    ${BOLD}$HACHIMI_CMD log${RESET}         实时查看服务日志（Ctrl-C 退出）"
echo -e "    ${BOLD}$HACHIMI_CMD config${RESET}      编辑配置文件"
echo -e "    ${BOLD}$HACHIMI_CMD install${RESET}     安装 Playwright 浏览器驱动"
echo -e "    ${BOLD}$HACHIMI_CMD update${RESET}      更新到最新版本"
echo ""
if [[ "$HACHIMI_CMD" != "hachimi" ]]; then
    echo -e "  ${YELLOW}提示：将 hachimi 加入全局 PATH 以使用简短命令：${RESET}"
    echo -e "    ${BOLD}echo 'export PATH=\"$VENV_DIR/bin:\$PATH\"' >> ~/.bashrc && source ~/.bashrc${RESET}"
    echo ""
    echo -e "  加入 PATH 后，可直接使用以下短命令："
    echo -e "    ${BOLD}hachimi${RESET}            进入 CLI 对话"
    echo -e "    ${BOLD}hachimi status${RESET}      查看后台服务状态"
    echo -e "    ${BOLD}hachimi start${RESET}       启动后台服务"
    echo -e "    ${BOLD}hachimi stop${RESET}        停止后台服务"
    echo -e "    ${BOLD}hachimi restart${RESET}     重启后台服务"
    echo -e "    ${BOLD}hachimi log${RESET}         实时查看服务日志（Ctrl-C 退出）"
    echo -e "    ${BOLD}hachimi config${RESET}      编辑配置文件"
    echo -e "    ${BOLD}hachimi install${RESET}     安装 Playwright 浏览器驱动"
    echo -e "    ${BOLD}hachimi update${RESET}      更新到最新版本"
    echo -e "    ${BOLD}hachimi uninstall${RESET}   卸载后台守护服务"
    echo ""
fi
if [[ -f "$CONFIG_FILE" ]]; then
    grep -q "sk-xxxxxxxx" "$CONFIG_FILE" 2>/dev/null && \
        echo -e "  ${YELLOW}${BOLD}[提醒] 配置文件中仍使用示例 API Key，请记得修改：${RESET}"
    grep -q "sk-xxxxxxxx" "$CONFIG_FILE" 2>/dev/null && \
        echo -e "    ${BOLD}$HACHIMI_CMD config${RESET}"
    echo ""
fi
