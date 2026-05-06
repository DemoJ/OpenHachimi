#!/usr/bin/env bash
# =============================================================================
# OpenHachimi 引导安装脚本（install.sh）
#
# 用法：curl -fsSL https://raw.githubusercontent.com/DemoJ/OpenHachimi/main/install.sh | bash
#
# 本脚本只做一件事：确保项目已 clone/更新到本地，然后执行项目内的 deploy.sh。
# deploy.sh 随项目一起更新，永远是最新版本，不受 CDN 缓存影响。
# =============================================================================

set -euo pipefail

REPO_URL="https://github.com/DemoJ/OpenHachimi.git"
DEFAULT_DIR="$HOME/OpenHachimi"

# 允许通过环境变量覆盖安装目录
INSTALL_DIR="${OPENHACHIMI_DIR:-$DEFAULT_DIR}"

# 颜色
GREEN='\033[0;32m'
BLUE='\033[0;34m'
RED='\033[0;31m'
BOLD='\033[1m'
RESET='\033[0m'

info()  { echo -e "${BLUE}[INFO]${RESET}  $*"; }
ok()    { echo -e "${GREEN}[OK]${RESET}    $*"; }
error() { echo -e "${RED}[ERROR]${RESET} $*" >&2; exit 1; }

echo ""
echo -e "${BOLD}=== OpenHachimi 安装引导 ===${RESET}"
echo ""

# 检查 git
if ! command -v git &>/dev/null; then
    error "未找到 git，请先安装：\n  Ubuntu/Debian：sudo apt-get install -y git\n  macOS：brew install git"
fi

# clone 或 pull
if [[ -d "$INSTALL_DIR/.git" ]]; then
    info "更新项目代码：$INSTALL_DIR"
    git -C "$INSTALL_DIR" pull --ff-only
else
    info "克隆项目到：$INSTALL_DIR"
    git clone "$REPO_URL" "$INSTALL_DIR"
fi

ok "项目已就绪：$INSTALL_DIR"
echo ""

# 执行项目内的 deploy.sh（始终是最新版，不受 CDN 缓存影响）
info "启动部署脚本..."
exec bash "$INSTALL_DIR/deploy.sh" "$@"
