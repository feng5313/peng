#!/usr/bin/env bash
#
# 鹏 (Peng) 一键安装脚本 — Linux / macOS / WSL
#
# 用法:
#   ./install.sh                交互式安装（推荐）
#   ./install.sh --with-service 安装完成后注册 systemd 系统服务（需 sudo）
#   ./install.sh --yes          非交互模式，全部使用默认选项（CI 友好）
#   ./install.sh --help         查看帮助
#
# 脚本功能:
#   1. 检测/安装 uv（Astral 官方安装器）
#   2. 检测/安装 Python 3.12（uv python install）
#   3. uv sync 创建 .venv 并安装全部依赖
#   4. 验证安装（import peng + fly --help）
#   5. 检查大模型 API Key 配置并给出指引
#   6. 可选: 注册 systemd 系统服务（fly install，含 PostgreSQL 自动部署）
#
# 幂等性: 可重复运行，已安装的组件会自动跳过。
#
set -euo pipefail

# ---------------------------------------------------------------------------
# 全局变量
# ---------------------------------------------------------------------------
PENG_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd)"
ASSUME_YES=0
WITH_SERVICE=0

# 颜色输出（非 TTY 时自动禁用）
if [ -t 1 ]; then
    RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
    BLUE='\033[0;34m'; BOLD='\033[1m'; CYAN='\033[0;36m'; NC='\033[0m'
else
    RED=''; GREEN=''; YELLOW=''; BLUE=''; BOLD=''; CYAN=''; NC=''
fi

info()    { printf "${BLUE}[INFO]${NC} %s\n" "$*"; }
success() { printf "${GREEN}[ OK ]${NC} %s\n" "$*"; }
warn()    { printf "${YELLOW}[WARN]${NC} %s\n" "$*"; }
error()   { printf "${RED}[FAIL]${NC} %s\n" "$*" >&2; }
banner()  { printf "\n${BOLD}${CYAN}== %s ==${NC}\n" "$*"; }

die() { error "$@"; exit 1; }

# ---------------------------------------------------------------------------
# 帮助与参数解析
# ---------------------------------------------------------------------------
usage() {
    cat <<'EOF'
鹏 (Peng) 一键安装脚本 (Linux / macOS / WSL)

用法: ./install.sh [选项]

选项:
  -y, --yes           非交互模式，全部使用默认选项（不注册系统服务）
      --with-service  安装完成后注册 systemd 系统服务并启动（需 sudo 密码）
      --no-banner     不显示横幅信息
  -h, --help          显示本帮助

环境变量:
  PENG_SKIP_UV_INSTALL=1   跳过 uv 自动安装（使用系统已有 uv）
  PENG_SKIP_PY_INSTALL=1   跳过 Python 3.12 自动安装（使用系统已有 3.12）
EOF
}

for arg in "$@"; do
    case "$arg" in
        -y|--yes)       ASSUME_YES=1 ;;
        --with-service) WITH_SERVICE=1 ;;
        --no-banner)    : ;;
        -h|--help)      usage; exit 0 ;;
        *)              error "未知参数: $arg"; usage; exit 1 ;;
    esac
done

# ---------------------------------------------------------------------------
# 前置检查
# ---------------------------------------------------------------------------
banner "鹏 (Peng) 安装程序"

OS_NAME="$(uname -s)"
OS_ARCH="$(uname -m)"
case "$OS_NAME" in
    Linux*)  PLATFORM="linux" ;;
    Darwin*) PLATFORM="macos" ;;
    *)       die "不支持的操作系统: $OS_NAME（本脚本仅支持 Linux / macOS / WSL）" ;;
esac
info "检测到系统: ${OS_NAME} (${OS_ARCH})"

[ "$OS_ARCH" = "x86_64" ] || [ "$OS_ARCH" = "arm64" ] || [ "$OS_ARCH" = "aarch64" ] \
    || warn "未常见架构 ${OS_ARCH}，将继续尝试安装"

# 必须在项目根目录内运行（脚本应位于仓库根）
[ -f "${PENG_ROOT}/pyproject.toml" ] \
    || die "未在项目根目录找到 pyproject.toml。
请先克隆仓库后进入目录运行:
    git clone <仓库地址> peng && cd peng && ./install.sh"
info "项目根目录: ${PENG_ROOT}"

# 基础命令检查
for cmd in git; do
    command -v "$cmd" >/dev/null 2>&1 || die "缺少命令: $cmd，请先安装（Debian/Ubuntu: sudo apt install $cmd）"
done

# ---------------------------------------------------------------------------
# 1. 安装 uv
# ---------------------------------------------------------------------------
banner "步骤 1/5: 检查 uv 包管理器"

UV_BIN="$(command -v uv 2>/dev/null || true)"
if [ -z "$UV_BIN" ] && [ -x "${HOME}/.local/bin/uv" ]; then
    UV_BIN="${HOME}/.local/bin/uv"
    export PATH="${HOME}/.local/bin:${PATH}"
fi

if [ -n "$UV_BIN" ]; then
    success "uv 已安装: $(${UV_BIN} --version 2>/dev/null || echo uv)"
elif [ "${PENG_SKIP_UV_INSTALL:-0}" = "1" ]; then
    die "PENG_SKIP_UV_INSTALL=1 但系统未安装 uv"
else
    info "未检测到 uv，开始安装（官方安装器）..."
    command -v curl >/dev/null 2>&1 \
        || die "缺少命令: curl，请先安装（Debian/Ubuntu: sudo apt install curl）"
    curl -LsSf https://astral.sh/uv/install.sh | sh
    [ -x "${HOME}/.local/bin/uv" ] || die "uv 安装失败：未找到 ~/.local/bin/uv"
    UV_BIN="${HOME}/.local/bin/uv"
    export PATH="${HOME}/.local/bin:${PATH}"
    success "uv 安装完成: $(${UV_BIN} --version)"
fi

# ---------------------------------------------------------------------------
# 2. 安装 Python 3.12
# ---------------------------------------------------------------------------
banner "步骤 2/5: 检查 Python 3.12"

PY_OK=0
if command -v python3 >/dev/null 2>&1; then
    PY_VER="$(python3 -c 'import sys;print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null || echo 0)"
    [ "$PY_VER" = "3.12" ] && PY_OK=1
fi

if [ "$PY_OK" = "0" ]; then
    # uv 可管理的 Python 也算
    if "${UV_BIN}" python find 3.12 >/dev/null 2>&1; then
        PY_OK=1
        info "已找到 uv 管理的 Python 3.12"
    fi
fi

if [ "$PY_OK" = "0" ]; then
    if [ "${PENG_SKIP_PY_INSTALL:-0}" = "1" ]; then
        die "PENG_SKIP_PY_INSTALL=1 但系统没有 Python 3.12（要求 >=3.12,<3.13）"
    fi
    info "未检测到 Python 3.12，使用 uv 安装..."
    "${UV_BIN}" python install 3.12
    success "Python 3.12 安装完成"
else
    success "Python 3.12 已就绪"
fi

# ---------------------------------------------------------------------------
# 3. 安装依赖 (uv sync)
# ---------------------------------------------------------------------------
banner "步骤 3/5: 安装依赖 (uv sync)"

cd "${PENG_ROOT}"
"${UV_BIN}" sync --python 3.12
success "依赖安装完成"

VENV_PYTHON="${PENG_ROOT}/.venv/bin/python"
[ -x "${VENV_PYTHON}" ] || die "虚拟环境创建失败: 未找到 ${VENV_PYTHON}"

# ---------------------------------------------------------------------------
# 4. 验证安装
# ---------------------------------------------------------------------------
banner "步骤 4/5: 验证安装"

"${VENV_PYTHON}" -c "import peng" \
    || die "peng 包导入失败，请检查上方依赖安装日志"
success "peng 包导入正常"

"${PENG_ROOT}/bin/fly" --help >/dev/null 2>&1 \
    && success "fly 命令可用" \
    || warn "bin/fly --help 返回非零，可稍后运行 ./bin/fly --help 人工确认"

# ---------------------------------------------------------------------------
# 5. API Key 检查
# ---------------------------------------------------------------------------
banner "步骤 5/5: 检查大模型 API 密钥"

HAS_KEY=0
for env_var in ZHIPU_AI_API_KEY OPENAI_API_KEY DASHSCOPE_API_KEY DEEPSEEK_API_KEY; do
    if [ -n "${!env_var:-}" ]; then
        success "已设置 ${env_var}"
        HAS_KEY=1
    fi
done

if [ "$HAS_KEY" = "0" ]; then
    # 检查 shell rc 文件中是否配置（peng 运行时会从 rc 文件加载）
    RC_HIT=0
    for rc in "${HOME}/.bashrc" "${HOME}/.zshrc"; do
        if [ -f "$rc" ] && grep -qE "export\s+(ZHIPU_AI_API_KEY|OPENAI_API_KEY|DASHSCOPE_API_KEY)" "$rc" 2>/dev/null; then
            RC_HIT=1
        fi
    done
    if [ "$RC_HIT" = "1" ]; then
        success "已在 shell 配置文件中发现 API 密钥"
    else
        warn "未检测到任何大模型 API 密钥"
        cat <<EOF

  配置方法（二选一）:
    1. 环境变量（推荐）: 在 ~/.bashrc 或 ~/.zshrc 中添加:
         export ZHIPU_AI_API_KEY="your-api-key"        # 智谱AI
         export OPENAI_API_KEY="your-api-key"          # OpenAI 兼容端点
       然后执行: source ~/.bashrc
    2. 配置文件: 编辑 config/peng.json 的 providers 段，
       直接填写 api_key / base_url / model 字段。

EOF
        if [ "$ASSUME_YES" = "0" ]; then
            read -r -p "是否现在输入 ZHIPU_AI_API_KEY？（写入 ~/.bashrc，回车跳过）: " API_KEY_INPUT || true
            if [ -n "${API_KEY_INPUT:-}" ]; then
                RC_FILE="${HOME}/.bashrc"
                [ -n "${ZSH_VERSION:-}" ] && RC_FILE="${HOME}/.zshrc"
                printf '\nexport ZHIPU_AI_API_KEY="%s"\n' "${API_KEY_INPUT}" >> "${RC_FILE}"
                export ZHIPU_AI_API_KEY="${API_KEY_INPUT}"
                success "已写入 ${RC_FILE}"
            fi
        fi
    fi
fi

# ---------------------------------------------------------------------------
# 可选: 注册系统服务
# ---------------------------------------------------------------------------
if [ "$WITH_SERVICE" = "1" ]; then
    banner "注册 systemd 系统服务"
    if command -v systemctl >/dev/null 2>&1; then
        info "执行 fly install（将自动部署 PostgreSQL 并注册 peng 服务，可能需要 sudo 密码）..."
        "${PENG_ROOT}/bin/fly" install || die "fly install 失败，可稍后手动执行: ./bin/fly install"
        success "系统服务注册完成，使用 fly status 查看状态"
    else
        warn "当前环境无 systemd（容器/WSL 初始环境？），跳过服务注册"
        info "可稍后使用前台模式启动: ./bin/fly service --webui"
    fi
fi

# ---------------------------------------------------------------------------
# 完成
# ---------------------------------------------------------------------------
printf "\n"
success "鹏 (Peng) 安装完成！"
cat <<EOF

${BOLD}下一步:${NC}
  交互对话:      ${PENG_ROOT}/bin/fly
  前台服务+WebUI: ${PENG_ROOT}/bin/fly service --webui   (WebUI: http://localhost:6688)
  注册系统服务:  ${PENG_ROOT}/bin/fly install             (Linux systemd / Windows 计划任务)
  查看服务状态:  ${PENG_ROOT}/bin/fly status
  帮助:          ${PENG_ROOT}/bin/fly --help

  建议将 bin 目录加入 PATH:
    echo 'export PATH="${PENG_ROOT}/bin:\$PATH"' >> ~/.bashrc && source ~/.bashrc

${BOLD}许可提示:${NC} 个人/非商业用途免费；商业使用需获得商业授权，详见 LICENSE 与 README。
EOF
