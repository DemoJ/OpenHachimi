#!/usr/bin/env bash
# =============================================================================
# OpenHachimi Linux 一键部署脚本
# 用法：bash deploy.sh [选项]
#   -H, --host HOST       监听地址（默认 127.0.0.1）
#   -p, --port PORT       监听端口（默认 8765）
#   --skip-daemon         只安装依赖，不部署后台守护服务
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

# ── 脚本自身所在目录（项目根目录）──────────────────────────────────────────────
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$PROJECT_ROOT/.venv"
CONFIG_EXAMPLE="$PROJECT_ROOT/user/config.example.yaml"
CONFIG_FILE="$PROJECT_ROOT/user/config.yaml"

# ── 解析命令行参数 ────────────────────────────────────────────────────────────
usage() {
    cat <<EOF
${BOLD}OpenHachimi Linux 一键部署脚本${RESET}

用法：
  bash deploy.sh [选项]

选项：
  -H, --host HOST       后台服务监听地址（默认：127.0.0.1）
  -p, --port PORT       后台服务监听端口（默认：8765）
  --skip-daemon         只安装依赖，不部署后台守护服务
  -h, --help            显示此帮助并退出

示例：
  # 使用默认设置一键部署
  bash deploy.sh

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
        -h|--help)      usage ;;
        *)              error "未知参数：$1，使用 -h 查看帮助。" ;;
    esac
done

# ── 步骤 1：检查 Python 版本 ──────────────────────────────────────────────────
echo ""
echo -e "${BOLD}=== OpenHachimi 一键部署 ===${RESET}"
echo ""

info "步骤 1/5：检查 Python 环境..."

PYTHON=""
for cmd in python3 python; do
    if command -v "$cmd" &>/dev/null; then
        VER="$($cmd -c 'import sys; print(sys.version_info[:2])')"
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
    error "未找到 Python 3.10 或更高版本。\n请先安装 Python（推荐使用系统包管理器或 pyenv）：\n  Ubuntu/Debian：sudo apt install python3\n  CentOS/RHEL：  sudo dnf install python3\n  Arch：         sudo pacman -S python"
fi

# ── 步骤 2：创建或复用虚拟环境 ──────────────────────────────────────────────
info "步骤 2/5：准备虚拟环境..."

if [[ -f "$VENV_DIR/bin/python" ]]; then
    success "虚拟环境已存在，复用：$VENV_DIR"
else
    info "创建虚拟环境：$VENV_DIR"
    "$PYTHON" -m venv "$VENV_DIR"
    success "虚拟环境创建完成。"
fi

VENV_PYTHON="$VENV_DIR/bin/python"
VENV_PIP="$VENV_DIR/bin/pip"
VENV_HACHIMI="$VENV_DIR/bin/hachimi"

# ── 步骤 3：安装依赖 ─────────────────────────────────────────────────────────
info "步骤 3/5：安装项目依赖（pip install -e .）..."

"$VENV_PYTHON" -m pip install -U pip --quiet
"$VENV_PYTHON" -m pip install -e "$PROJECT_ROOT" --quiet

success "依赖安装完成。"

# ── 步骤 4：初始化配置文件 ───────────────────────────────────────────────────
info "步骤 4/5：检查配置文件..."

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
        echo -e "  ${YELLOW}${BOLD}[提醒] 检测到配置文件中仍使用示例 API Key，请记得修改 $CONFIG_FILE${RESET}"
fi
