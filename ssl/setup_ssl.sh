#!/usr/bin/env bash
# ==============================================================================
# 自动化配置内网/自签名 SSL 证书与解决 SSL 校验问题脚本 (Linux Bash 环境)
# 
# 本脚本提炼自与 Gemini 关于内网 SSL 证书报错 (SSLCertVerificationError/MITM/自签名证书)
# 的解决方案，提供三层解决策略：
#   1. 在线抓取或手动指定 CA 证书 (支持 GitHub / PyPI 官方源 / 任意指定网址)
#   2. 系统级信任库导入 (OS-level System Store)
#   3. Python/cURL/Git CA 证书链合并挂载 (CA Bundle Merge & Env Setup)
#   4. Python sitecustomize.py 全局 SSL 豁免热补丁 (Global Unverified Context Patch)
# ==============================================================================

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

log_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

print_banner() {
    echo "======================================================================"
    echo "         Linux 内网 / 自签名 SSL 证书自动化配置工具"
    echo "======================================================================"
}

# 检查 root 权限 (部分操作需要)
check_sudo() {
    if [ "$EUID" -ne 0 ]; then
        log_warn "当前非 Root 用户，修改系统级信任库或全局 Profile 可能需要 sudo 权限。"
        SUDO="sudo"
    else
        SUDO=""
    fi
}

# 0. 在线从目标站点自动提取内网/代理 CA 证书
fetch_remote_ca() {
    local target_host="$1"
    local output_file="$2"

    # 清除 scheme (如 https://) 并提取 host 和 port
    target_host=$(echo "$target_host" | sed -e 's|^[^/]*//||' -e 's|/.*$||')
    if [[ "$target_host" != *:* ]]; then
        target_host="${target_host}:443"
    fi

    log_info "正在通过 OpenSSL 从目标服务器 $target_host 抓取 CA 证书链..."

    if ! command -v openssl &>/dev/null; then
        log_error "未安装 openssl 命令，无法在线抓取证书。请安装 openssl 或手动提供 -c <cert.crt>。"
        return 1
    fi

    mkdir -p "$(dirname "$output_file")"
    
    # 使用 openssl s_client 连接并截取证书 PEM 块
    if echo | openssl s_client -showcerts -connect "$target_host" 2>/dev/null | sed -ne '/-BEGIN CERTIFICATE-/,/-END CERTIFICATE-/p' >> "$output_file"; then
        if [ -s "$output_file" ]; then
            log_success "成功提取 $target_host 的 CA 证书并追加至: $output_file"
            return 0
        fi
    fi

    log_error "从 $target_host 抓取证书失败或截获结果为空。"
    return 1
}

# 解析预设关键词 (github / pypi) 或任意网址
parse_and_fetch_targets() {
    local raw_target="$1"
    local output_file="$2"

    # 清空临时存储文件
    > "$output_file"

    case "$raw_target" in
        github)
            log_info "解析预设关键词 'github'，将依次抓取 github.com 与 raw.githubusercontent.com 的 CA 证书..."
            fetch_remote_ca "github.com:443" "$output_file"
            fetch_remote_ca "raw.githubusercontent.com:443" "$output_file"
            ;;
        pypi|pip)
            log_info "解析预设关键词 'pypi/pip'，将依次抓取 PyPI 官方源 pypi.org 与 files.pythonhosted.org 的 CA 证书..."
            fetch_remote_ca "pypi.org:443" "$output_file"
            fetch_remote_ca "files.pythonhosted.org:443" "$output_file"
            ;;
        *)
            log_info "抓取自定义目标网址: $raw_target"
            fetch_remote_ca "$raw_target" "$output_file"
            ;;
    esac
}

# 1. 导入系统级信任根证书 (Linux System Trust Store)
install_system_ca() {
    local cert_file="$1"
    if [ ! -f "$cert_file" ] || [ ! -s "$cert_file" ]; then
        log_error "证书文件不存在或内容为空: $cert_file"
        return 1
    fi

    log_info "正在尝试将证书添加到系统全局信任库..."

    check_sudo

    if [ -f /etc/os-release ]; then
        . /etc/os-release
        case "$ID" in
            ubuntu|debian|deepin|mint)
                $SUDO cp "$cert_file" /usr/local/share/ca-certificates/internal_custom_ca.crt
                $SUDO update-ca-certificates
                log_success "Debian/Ubuntu 系统 CA 证书更新完成！"
                ;;
            centos|rhel|rocky|alma|fedora)
                $SUDO cp "$cert_file" /etc/pki/ca-trust/source/anchors/internal_custom_ca.crt
                $SUDO update-ca-trust
                log_success "CentOS/RHEL/Fedora 系统 CA 证书更新完成！"
                ;;
            alpine)
                $SUDO cp "$cert_file" /usr/local/share/ca-certificates/internal_custom_ca.crt
                $SUDO update-ca-certificates
                log_success "Alpine Linux 系统 CA 证书更新完成！"
                ;;
            *)
                log_warn "未自动识别的 Linux 发行版 ID: $ID，请手动将证书添加至系统的 CA 存储库中。"
                ;;
        esac
    else
        log_warn "未找到 /etc/os-release，跳过系统级 CA 证书更新。"
    fi
}

# 2. 合并 Python certifi 证书链并配置环境变量
setup_ca_bundle() {
    local cert_file="$1"
    log_info "正在配置 Python / cURL / Git 级别的合并证书链 (CA Bundle Merge)..."

    CERT_DIR="$HOME/.certs"
    mkdir -p "$CERT_DIR"
    MERGED_BUNDLE="$CERT_DIR/custom_ca_bundle.pem"

    # 获取 Python certifi 路径
    if command -v python3 &>/dev/null; then
        PYTHON_CERT_PATH=$(python3 -c "import certifi; print(certifi.where())" 2>/dev/null || true)
    fi

    if [ -n "$PYTHON_CERT_PATH" ] && [ -f "$PYTHON_CERT_PATH" ]; then
        log_info "发现 Python certifi 基础证书: $PYTHON_CERT_PATH"
        cp "$PYTHON_CERT_PATH" "$MERGED_BUNDLE"
    else
        log_warn "未提取到 certifi 证书路径，将使用标准 Linux 系统 CA 证书库"
        if [ -f /etc/ssl/certs/ca-certificates.crt ]; then
            cp /etc/ssl/certs/ca-certificates.crt "$MERGED_BUNDLE"
        elif [ -f /etc/pki/tls/certs/ca-bundle.crt ]; then
            cp /etc/pki/tls/certs/ca-bundle.crt "$MERGED_BUNDLE"
        else
            touch "$MERGED_BUNDLE"
        fi
    fi

    # 追加内网/抓取的 CA 证书
    if [ -f "$cert_file" ] && [ -s "$cert_file" ]; then
        echo "" >> "$MERGED_BUNDLE"
        echo "# Custom Internal/Fetched CA Certificate Added on $(date)" >> "$MERGED_BUNDLE"
        cat "$cert_file" >> "$MERGED_BUNDLE"
        log_success "已成功将 $cert_file 追加合并至: $MERGED_BUNDLE"
    fi

    # 配置环境变量持久化
    BASHRC="$HOME/.bashrc"
    log_info "在 $BASHRC 中持久化写入 CA 环境变量..."

    cat << EOF >> "$BASHRC"

# ===== Internal SSL CA Bundle Environment Variables =====
export REQUESTS_CA_BUNDLE="$MERGED_BUNDLE"
export SSL_CERT_FILE="$MERGED_BUNDLE"
export CURL_CA_BUNDLE="$MERGED_BUNDLE"
EOF

    # 导出到当前 shell 上下文
    export REQUESTS_CA_BUNDLE="$MERGED_BUNDLE"
    export SSL_CERT_FILE="$MERGED_BUNDLE"
    export CURL_CA_BUNDLE="$MERGED_BUNDLE"

    if command -v git &>/dev/null; then
        git config --global http.sslCAInfo "$MERGED_BUNDLE"
        log_success "配置 Git http.sslCAInfo -> $MERGED_BUNDLE"
    fi
}

# 3. 创建 Python sitecustomize.py 全局 SSL 豁免热补丁
setup_python_sitecustomize() {
    log_info "正在配置 Python 解释器 sitecustomize.py 全局 SSL 豁免热补丁..."

    if ! command -v python3 &>/dev/null; then
        log_warn "未检测到 python3 命令，跳过 sitecustomize 补丁。"
        return 0
    fi

    SITE_PACKAGES=$(python3 -c "import site; print(site.getsitepackages()[0])" 2>/dev/null || python3 -c "import site; print(site.USER_SITE)" 2>/dev/null)

    if [ -z "$SITE_PACKAGES" ]; then
        log_error "无法找到 Python 的 site-packages 路径"
        return 1
    fi

    mkdir -p "$SITE_PACKAGES"
    SITECUSTOMIZE_FILE="$SITE_PACKAGES/sitecustomize.py"

    log_info "写入热补丁至: $SITECUSTOMIZE_FILE"

    cat << 'EOF' > "$SITECUSTOMIZE_FILE"
# Auto-generated by setup_ssl.sh - Global Unverified SSL Context Patch
import ssl
import sys

# 1. 强制全局关闭 Python 标准库 ssl 模块的证书验证
try:
    _create_unverified_https_context = ssl._create_unverified_context
except AttributeError:
    pass
else:
    ssl._create_default_https_context = _create_unverified_https_context

# 2. 禁用 urllib3 / requests 抛出的 InsecureRequestWarning 警告
try:
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
except ImportError:
    pass
EOF

    log_success "Python sitecustomize.py 全局 SSL 豁免热补丁配置完成！"
}

# 显示帮助信息
show_help() {
    print_banner
    echo "用法: bash $0 [选项]"
    echo ""
    echo "选项说明:"
    echo "  -c, --cert <path>        指定本地已有的 CA 证书文件 (.crt / .pem)"
    echo "  -f, --fetch <host/url>   在线抓取目标网址证书。支持传入网址 (如 github.com, pypi.org)"
    echo "                           或预设快捷词: 'github' (抓取 GitHub) / 'pypi' (抓取 pip 官方源)"
    echo "  -i, --install-system     将 CA 证书导入 Linux 系统级信任库 (需 sudo 权限)"
    echo "  -m, --merge-bundle       合并 CA 证书到 Python certifi 并配置环境变量"
    echo "  -p, --patch-python       在 Python site-packages 中安装 sitecustomize.py 全局免 SSL 校验热补丁"
    echo "  -a, --all                执行全部操作 (自动抓取/导入 + 合并 CA + Python 热补丁)"
    echo "  -h, --help               显示帮助信息"
    echo ""
    echo "使用示例:"
    echo "  1. 抓取 GitHub 官方证书并完成全套内网信任配置:"
    echo "     bash $0 --all --fetch github.com"
    echo "     或快捷方式: bash $0 --all --fetch github"
    echo ""
    echo "  2. 抓取 pip/PyPI 官方包镜像源证书并完成配置:"
    echo "     bash $0 --all --fetch pypi.org"
    echo "     或快捷方式: bash $0 --all --fetch pypi"
    echo ""
    echo "  3. 拥有本地证书文件时全自动配置:"
    echo "     bash $0 --all --cert /path/to/company_ca.crt"
    echo ""
    echo "  4. 无证书且不抓取时，仅启用 Python 全局免 SSL 校验热补丁:"
    echo "     bash $0 --patch-python"
    echo ""
}

# 主程序逻辑
main() {
    print_banner

    if [ $# -eq 0 ]; then
        show_help
        exit 0
    fi

    DO_SYSTEM=0
    DO_MERGE=0
    DO_PATCH=0
    CERT_FILE=""
    FETCH_TARGET=""

    while [[ $# -gt 0 ]]; do
        case "$1" in
            -c|--cert)
                CERT_FILE="$2"
                shift 2
                ;;
            -f|--fetch)
                FETCH_TARGET="$2"
                shift 2
                ;;
            -i|--install-system)
                DO_SYSTEM=1
                shift
                ;;
            -m|--merge-bundle)
                DO_MERGE=1
                shift
                ;;
            -p|--patch-python)
                DO_PATCH=1
                shift
                ;;
            -a|--all)
                DO_SYSTEM=1
                DO_MERGE=1
                DO_PATCH=1
                shift
                ;;
            -h|--help)
                show_help
                exit 0
                ;;
            *)
                if [ -f "$1" ]; then
                    CERT_FILE="$1"
                else
                    log_error "未知参数或无效文件: $1"
                    show_help
                    exit 1
                fi
                shift
                ;;
        esac
    done

    # 如果指定了 --fetch 在线抓取
    if [ -n "$FETCH_TARGET" ]; then
        AUTO_FETCHED_CERT="$HOME/.certs/fetched_auto_ca.crt"
        if parse_and_fetch_targets "$FETCH_TARGET" "$AUTO_FETCHED_CERT"; then
            CERT_FILE="$AUTO_FETCHED_CERT"
        fi
    fi

    # 执行选定的操作
    if [ $DO_SYSTEM -eq 1 ]; then
        if [ -n "$CERT_FILE" ] && [ -f "$CERT_FILE" ] && [ -s "$CERT_FILE" ]; then
            install_system_ca "$CERT_FILE"
        else
            log_warn "未提供有效的证书文件，跳过系统 CA 证书导入。"
        fi
    fi

    if [ $DO_MERGE -eq 1 ]; then
        setup_ca_bundle "$CERT_FILE"
    fi

    if [ $DO_PATCH -eq 1 ]; then
        setup_python_sitecustomize
    fi

    log_success "所有配置步骤处理完毕！"
    echo -e "${YELLOW}提示: 若配置了环境变量，请运行 'source ~/.bashrc' 或重新打开终端会话以使其完全生效。${NC}"
}

main "$@"
