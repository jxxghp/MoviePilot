#!/bin/sh

set -eu

SCRIPT_PATH="$0"
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$SCRIPT_PATH")" && pwd)
COMMAND_NAME=$(basename -- "$SCRIPT_PATH")

if [ "${COMMAND_NAME}" = "uv-pip-compat" ] || [ "${COMMAND_NAME}" = "uv-pip-compat.sh" ]; then
    if [ "$#" -eq 0 ]; then
        echo "用法: uv-pip-compat <pip|pip-compile|pip-sync> [args...]" >&2
        exit 2
    fi
    COMMAND_NAME="$1"
    shift
fi

if [ -x "${SCRIPT_DIR}/uv" ]; then
    UV_BIN="${SCRIPT_DIR}/uv"
elif command -v uv >/dev/null 2>&1; then
    UV_BIN=$(command -v uv)
else
    echo "未找到 uv，可执行 pip 兼容层无法继续运行。" >&2
    exit 127
fi

has_environment_option() {
    while [ "$#" -gt 0 ]; do
        case "$1" in
            -p|--python|--python=*|-p*|--system|--user|\
            -t|--target|--target=*|-t*|--prefix|--prefix=*)
                return 0
                ;;
            --)
                return 1
                ;;
        esac
        shift
    done
    return 1
}

normalize_pip_proxy_args() {
    output_file="$1"
    shift
    original_args_file=$(mktemp)
    : > "${output_file}"
    trap 'rm -f "${original_args_file}"' EXIT HUP INT TERM

    for arg in "$@"; do
        printf '%s\n' "$arg" >> "${original_args_file}"
    done

    skip_next=0
    while IFS= read -r arg; do
        if [ "${skip_next}" -eq 1 ]; then
            proxy_value="${arg}"
            export HTTP_PROXY="${proxy_value}"
            export HTTPS_PROXY="${proxy_value}"
            export http_proxy="${proxy_value}"
            export https_proxy="${proxy_value}"
            skip_next=0
            continue
        fi
        case "$arg" in
            --proxy)
                skip_next=1
                ;;
            --proxy=*)
                proxy_value="${arg#--proxy=}"
                export HTTP_PROXY="${proxy_value}"
                export HTTPS_PROXY="${proxy_value}"
                export http_proxy="${proxy_value}"
                export https_proxy="${proxy_value}"
                ;;
            *)
                printf '%s\n' "$arg" >> "${output_file}"
                ;;
        esac
    done < "${original_args_file}"

    rm -f "${original_args_file}"
    trap - EXIT HUP INT TERM
}

uv_pip_with_venv_python() {
    command_name="$1"
    shift

    if [ -x "${SCRIPT_DIR}/python" ] && ! has_environment_option "$@"; then
        # uv 不会仅凭 pip 软链接位置锁定 venv，本地安装也不会激活 venv。
        # 因此需要在会读取或改写环境的 pip 子命令上显式绑定当前 venv 解释器。
        exec "${UV_BIN}" pip "${command_name}" --python "${SCRIPT_DIR}/python" "$@"
    fi
    exec "${UV_BIN}" pip "${command_name}" "$@"
}

case "${COMMAND_NAME}" in
    pip|pip3|pip3.*)
        if [ "$#" -eq 0 ]; then
            exec "${UV_BIN}" pip --help
        fi

        case "$1" in
            -V|--version|version)
                exec "${UV_BIN}" --version
                ;;
            help)
                shift
                exec "${UV_BIN}" help pip "$@"
                ;;
            check|freeze|install|list|show|sync|tree|uninstall)
                pip_command="$1"
                shift
                if [ "${pip_command}" = "install" ]; then
                    normalized_file=$(mktemp)
                    normalize_pip_proxy_args "${normalized_file}" "$@"
                    set --
                    while IFS= read -r arg; do
                        set -- "$@" "$arg"
                    done < "${normalized_file}"
                    rm -f "${normalized_file}"
                fi
                uv_pip_with_venv_python "${pip_command}" "$@"
                ;;
            *)
                exec "${UV_BIN}" pip "$@"
                ;;
        esac
        ;;
    pip-compile)
        exec "${UV_BIN}" pip compile "$@"
        ;;
    pip-sync)
        uv_pip_with_venv_python sync "$@"
        ;;
    *)
        echo "不支持的 pip 兼容命令入口：${COMMAND_NAME}" >&2
        exit 2
        ;;
esac
