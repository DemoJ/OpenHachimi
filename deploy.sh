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
#   -H, --host HOST       监听地址（默认 127.0.0.1）
#   -p, --port PORT       监听端口（默认 8765）
#   --skip-daemon         只安装依赖，不部署后台守护服务
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
HOST="127.0.0.1"
PORT=8765
SKIP_DAEMON=false
REPO_URL="https://github.com/DemoJ/OpenHachimi.git"
CLONE_DIR="./OpenHachimi"

# ── 解析命令行参数 ────────────────────────────────────────────────────────────
usage() {
    cat <<EOF
${BOLD}OpenHachimi 一键部署脚本${RESET}

用法：
  bash deploy.sh [选项]

选项：
  -H, --host HOST       后台服务监听地址（默认：127.0.0.1）
  -p, --port PORT       后台服务监听端口（默认：8765）
  --skip-daemon         只安装依赖，不部署后台守护服务
  --repo URL            自定义 Git 仓库地址
  --dir DIR             指定克隆目标目录（默认：./OpenHachimi）
  -h, --help            显示此帮助并退出

示例：
  # 一键下载并部署（无需提前 clone 项目）
  bash <(curl -fsSL https://raw.githubusercontent.com/DemoJ/OpenHachimi/main/deploy.sh)

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
        --repo)         REPO_URL="$2"; shift 2 ;;
        --dir)          CLONE_DIR="$2"; shift 2 ;;
        -h|--help)      usage ;;
        *)              error "未知参数：$1，使用 -h 查看帮助。" ;;
    esac
done

echo ""
echo -e "${BOLD}=== OpenHachimi 一键部署 ===${RESET}"
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

# ── 步骤 2：确保项目目录存在（自举 clone）────────────────────────────────────
info "步骤 2/5：准备项目目录..."

# 判断脚本是否已经在项目目录内（存在 pyproject.toml 即认定为项目根）
if [[ -f "pyproject.toml" ]]; then
    PROJECT_ROOT="$(pwd)"
    success "已在项目目录中：$PROJECT_ROOT"
else
    # 不在项目目录，执行自举 clone
    if ! command -v git &>/dev/null; then
        error "未找到 git，请先安装 git 再重试。"
    fi

    CLONE_DIR="$(realpath "$CLONE_DIR")"
    if [[ -d "$CLONE_DIR/.git" ]]; then
        info "目录已存在，拉取最新代码：$CLONE_DIR"
        git -C "$CLONE_DIR" pull --ff-only
    else
        info "克隆项目到：$CLONE_DIR"
        git clone "$REPO_URL" "$CLONE_DIR"
    fi

    PROJECT_ROOT="$CLONE_DIR"
    success "项目已就绪：$PROJECT_ROOT"

    # 切换到项目目录，后续操作均在此目录
    cd "$PROJECT_ROOT"
fi

VENV_DIR="$PROJECT_ROOT/.venv"
CONFIG_EXAMPLE="$PROJECT_ROOT/user/config.example.yaml"
CONFIG_FILE="$PROJECT_ROOT/user/config.yaml"

# ── 步骤 3：创建或复用虚拟环境 ──────────────────────────────────────────────
info "步骤 3/5：准备虚拟环境..."

if [[ -f "$VENV_DIR/bin/python" ]]; then
    success "虚拟环境已存在，复用：$VENV_DIR"
else
    info "创建虚拟环境：$VENV_DIR"

    # 尝试创建，如果失败则检测是否为 Debian/Ubuntu 缺少 python3-venv 的问题
    if ! "$PYTHON" -m venv "$VENV_DIR" 2>/tmp/_oh_venv_err; then
        if grep -qi "ensurepip\|venv" /tmp/_oh_venv_err 2>/dev/null; then
            # 获取 Python 次版本号，用于安装对应的 python3.X-venv
            MINOR="$("$PYTHON" -c 'import sys; print(sys.version_info.minor)')"
            VENV_PKG="python3.${MINOR}-venv"
            warn "检测到缺少 $VENV_PKG，尝试自动安装..."

            if command -v apt-get &>/dev/null; then
                if sudo apt-get install -y "$VENV_PKG" >/dev/null 2>&1; then
                    success "已安装 $VENV_PKG，重新创建虚拟环境..."
                    "$PYTHON" -m venv "$VENV_DIR"
                else
                    error "自动安装 $VENV_PKG 失败，请手动执行：\n  sudo apt-get install -y $VENV_PKG\n然后重新运行此脚本。"
                fi
            else
                # 打印原始错误，并给出通用提示
                cat /tmp/_oh_venv_err >&2
                error "创建虚拟环境失败。请先安装 python3-venv（或等效包）后重试。"
            fi
        else
            cat /tmp/_oh_venv_err >&2
            error "创建虚拟环境失败，请查看上方错误信息。"
        fi
    fi

    success "虚拟环境创建完成。"
fi

VENV_PYTHON="$VENV_DIR/bin/python"
VENV_HACHIMI="$VENV_DIR/bin/hachimi"

# ── 步骤 4：安装依赖 ─────────────────────────────────────────────────────────
info "步骤 4/5：安装项目依赖（pip install -e .）..."

"$VENV_PYTHON" -m pip install -U pip --quiet
"$VENV_PYTHON" -m pip install -e "$PROJECT_ROOT" --quiet

success "依赖安装完成。"

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
    info "步骤 5/5：部署后台守护服务（host=$HOST port=$PORT）..."
    "$VENV_HACHIMI" deploy --host "$HOST" --port "$PORT"
fi

# ── 完成提示 ─────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}${BOLD}========================================${RESET}"
echo -e "${GREEN}${BOLD}  部署完成！${RESET}"
echo -e "${GREEN}${BOLD}========================================${RESET}"
echo ""
echo -e "  项目目录：${BOLD}$PROJECT_ROOT${RESET}"
echo -e "  可执行文件：${BOLD}$VENV_HACHIMI${RESET}"
echo ""
echo -e "  ${BOLD}常用命令：${RESET}"
echo -e "    进入 CLI 对话：  ${BOLD}$VENV_HACHIMI${RESET}"
echo -e "    查看服务状态：  ${BOLD}systemctl --user status openhachimi${RESET}"
echo -e "    实时查看日志：  ${BOLD}journalctl --user -u openhachimi -f${RESET}"
echo ""
echo -e "  如需将 hachimi 加入全局 PATH，可运行："
echo -e "    ${BOLD}echo 'export PATH=\"$VENV_DIR/bin:\$PATH\"' >> ~/.bashrc && source ~/.bashrc${RESET}"
echo ""
if [[ -f "$CONFIG_FILE" ]]; then
    grep -q "sk-xxxxxxxx" "$CONFIG_FILE" 2>/dev/null && \
        echo -e "  ${YELLOW}${BOLD}[提醒] 配置文件中仍使用示例 API Key，请记得修改：$CONFIG_FILE${RESET}"
fi
