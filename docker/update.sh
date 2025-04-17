#!/bin/bash
# shellcheck shell=bash
# shellcheck disable=SC2086
# shellcheck disable=SC2144

Green="\033[32m"
Red="\033[31m"
Yellow='\033[33m'
Font="\033[0m"
INFO="[${Green}INFO${Font}]"
ERROR="[${Red}ERROR${Font}]"
WARN="[${Yellow}WARN${Font}]"
function INFO() {
    echo -e "${INFO} ${1}"
}
function ERROR() {
    echo -e "${ERROR} ${1}"
}
function WARN() {
    echo -e "${WARN} ${1}"
}

# 下载及解压
function download_and_unzip() {
    local retries=0
    local max_retries=3
    local url="$1"
    local target_dir="$2"
    INFO "→ 正在下载 ${url}..."
    while [ $retries -lt $max_retries ]; do
        if curl ${CURL_OPTIONS} "${url}" ${CURL_HEADERS} | busybox unzip -d ${TMP_PATH} - > /dev/null; then
            if [ -e ${TMP_PATH}/MoviePilot-* ]; then
                mv ${TMP_PATH}/MoviePilot-* ${TMP_PATH}/"${target_dir}"
            fi
            break
        else
            WARN "下载 ${url} 失败，正在进行第 $((retries + 1)) 次重试..."
            retries=$((retries + 1))
        fi
    done
    if [ $retries -eq $max_retries ]; then
        ERROR "下载 ${url} 失败，已达到最大重试次数！"
        return 1
    else
        return 0
    fi
}

# 下载程序资源，$1: 后端版本路径
function install_backend_and_download_resources() {
    # 更新后端程序
    if ! download_and_unzip "${GITHUB_PROXY}https://github.com/jxxghp/MoviePilot/archive/refs/${1}" "App"; then
        WARN "后端程序下载失败，继续使用旧的程序来启动..."
        return 1
    fi
    INFO "后端程序下载成功"
    INFO "→ 正在安装依赖..."
    if ! pip install ${PIP_OPTIONS} --upgrade --root-user-action=ignore pip > /dev/null; then
        ERROR "pip 更新失败，请重新拉取镜像"
        return 1
    fi
    if ! pip install ${PIP_OPTIONS} --root-user-action=ignore -r ${TMP_PATH}/App/requirements.txt > /dev/null; then
        ERROR "依赖安装失败，请重新拉取镜像"
        return 1
    fi
    INFO "依赖安装成功"
    # 如果是"heads/v2.zip"，则查找v2开头的最新版本号
    if [[ "${1}" == "heads/v2.zip" ]]; then
        INFO "→ 正在获取前端最新版本号..."
        # 获取所有发布的版本列表，并筛选出以v2开头的版本号
        releases=$(curl ${CURL_OPTIONS} "https://api.github.com/repos/jxxghp/MoviePilot-Frontend/releases" ${CURL_HEADERS} | jq -r '.[].tag_name' | grep "^v2\.")
        if [ -z "$releases" ]; then
            WARN "未找到任何v2前端版本，继续启动..."
            return 1
        else
            # 找到最新的v2版本
            frontend_version=$(echo "$releases" | sort -V | tail -n 1)
        fi
        INFO "前端最新版本号：${frontend_version}"
    else
        INFO "→ 正在获取前端版本号..."
        # 从后端文件中读取前端版本号
        frontend_version=$(sed -n "s/^FRONTEND_VERSION\s*=\s*'\([^']*\)'/\1/p" ${TMP_PATH}/App/version.py)
        if [[ "${frontend_version}" != *v* ]]; then
            WARN "前端版本号获取失败，继续启动..."
            return 1
        fi
        INFO "前端版本号：${frontend_version}"
    fi
    # 更新前端程序
    if ! download_and_unzip "${GITHUB_PROXY}https://github.com/jxxghp/MoviePilot-Frontend/releases/download/${frontend_version}/dist.zip" "dist"; then
        WARN "前端程序下载失败，继续使用旧的程序来启动..."
        return 1
    fi
    INFO "前端程序下载成功"
    # 备份插件目录
    INFO "→ 正在备份插件目录..."
    rm -rf /plugins
    mkdir -p /plugins
    cp -a /app/app/plugins/* /plugins/
    rm -f /plugins/__init__.py
    # 备份站点资源
    INFO "→ 正在备份站点资源目录..."
    rm -rf /resources_bakcup
    mkdir /resources_bakcup
    cp -a /app/app/helper/user.sites.bin /resources_bakcup
    cp -a /app/app/helper/sites.cp* /resources_bakcup
    # 清空程序目录
    rm -rf /app
    mkdir -p /app
    # 复制新后端程序
    cp -a ${TMP_PATH}/App/* /app/
    # 复制新前端程序
    rm -rf /public
    mkdir -p /public
    cp -a ${TMP_PATH}/dist/* /public/
    INFO "程序部分更新成功，前端版本：${frontend_version}，后端版本：${1}"
    # 恢复插件目录
    cp -a /plugins/* /app/app/plugins/
    # 更新站点资源
    INFO "→ 开始更新站点资源..."
    if ! download_and_unzip "${GITHUB_PROXY}https://github.com/jxxghp/MoviePilot-Resources/archive/refs/heads/main.zip" "Resources"; then
        cp -a /resources_bakcup/* /app/app/helper/
        rm -rf /resources_bakcup
        WARN "站点资源下载失败，继续使用旧的资源来启动..."
        return 1
    fi
    # 复制新站点资源
    cp -a ${TMP_PATH}/Resources/resources/* /app/app/helper/
    INFO "站点资源更新成功"
    # 清理临时目录
    rm -rf "${TMP_PATH}"
    return 0
}

function test_connectivity_pip() {
    pip uninstall -y pip-hello-world > /dev/null 2>&1
    case "$1" in
    0)
        if [[ -n "${PIP_PROXY}" ]]; then
            if pip install -i ${PIP_PROXY} pip-hello-world > /dev/null 2>&1; then
                PIP_OPTIONS="-i ${PIP_PROXY}"
                PIP_LOG="镜像代理模式"
                return 0
            fi
        fi
        return 1
        ;;
    1)
        if [[ -n "${PROXY_HOST}" ]]; then
            if pip install --proxy=${PROXY_HOST} pip-hello-world > /dev/null 2>&1; then
                PIP_OPTIONS="--proxy=${PROXY_HOST}"
                PIP_LOG="全局代理模式"
                return 0
            fi
        fi
        return 1
        ;;
    2)
        PIP_OPTIONS=""
        PIP_LOG="不使用代理"
        return 0
        ;;
    esac
}

# 测试Github连通性
function test_connectivity_github() {
    case "$1" in
    0)
        if [[ -n "${GITHUB_PROXY}" ]]; then
            if curl -sL "${GITHUB_PROXY}https://raw.githubusercontent.com/jxxghp/MoviePilot/main/README.md" > /dev/null 2>&1; then
                GITHUB_LOG="镜像代理模式"
                return 0
            fi
        fi
        return 1
        ;;
    1)
        if [[ -n "${PROXY_HOST}" ]]; then
            if curl -sL -x ${PROXY_HOST} https://raw.githubusercontent.com/jxxghp/MoviePilot/main/README.md > /dev/null 2>&1; then
                CURL_OPTIONS="-sL -x ${PROXY_HOST}"
                GITHUB_LOG="全局代理模式"
                return 0
            fi
        fi
        return 1
        ;;
    2)
        CURL_OPTIONS="-sL"
        GITHUB_LOG="不使用代理"
        return 0
        ;;
    esac
}

# 版本号比较
function compare_versions() {
    local v1="$1"
    local v2="$2"
    # 去掉开头的 v 或 V
    v1="${v1#[vV]}"
    v2="${v2#[vV]}"
    local current_ver_parts=()
    local release_ver_parts=()
    IFS='.-' read -ra current_ver_parts <<< "$v1"
    IFS='.-' read -ra release_ver_parts <<< "$v2"
    local i
    local current_ver
    local release_ver

    for ((i = 0; i < ${#current_ver_parts[@]} || i < ${#release_ver_parts[@]}; i++)); do
        # 版本号不足位补 0
        local current_ver_part="${current_ver_parts[i]:-0}"
        local release_ver_part="${release_ver_parts[i]:-0}"
        current_ver=$(get_priority "$current_ver_part")
        release_ver=$(get_priority "$release_ver_part")

        # 任意一个为-5，不在合法版本号内，无法比较
        if (( current_ver == -5 || release_ver == -5 )); then
            ERROR "存在不合法版本号，无法判断，跳过更新步骤..."
            return 1
        else
            if (( current_ver > release_ver )); then
                WARN "当前版本高于远程版本，跳过更新步骤..."
                return 1
            elif (( current_ver < release_ver )); then
                INFO "发现新版本，开始自动升级..."
                install_backend_and_download_resources "tags/$2.zip"
                return 0
            else
                continue
            fi
        fi
    done
    WARN "当前版本已是最新版本，跳过更新步骤..."
}

# 优先级转换
function get_priority() {
    local version="$1"
    if [[ $version =~ ^[0-9]+$ ]]; then
        echo $version
    else
        case $version in
            "stable")
                echo -1
                ;;
            "rc")
                echo -2
                ;;
            "beta")
                echo -3
                ;;
            "alpha")
                echo -4
                ;;
            # 非数字的不合法版本号
            *)
                echo -5
                ;;
        esac
    fi
}

if [[ "${MOVIEPILOT_AUTO_UPDATE}" = "true" ]] || [[ "${MOVIEPILOT_AUTO_UPDATE}" = "release" ]] || [[ "${MOVIEPILOT_AUTO_UPDATE}" = "dev" ]]; then
    TMP_PATH=$(mktemp -d)
    if [ ! -d "${TMP_PATH}" ]; then
        # 如果自动生成 tmp 文件夹失败则手动指定，避免出现数据丢失等情况
        TMP_PATH=/tmp/mp_update_path
        if [ -d /tmp/mp_update_path ]; then
            rm -rf /tmp/mp_update_path
        fi
        mkdir -p /tmp/mp_update_path
    fi
    # 优先级：镜像站 > 全局 > 不代理
    # pip
    retries=0
    while true; do
        if test_connectivity_pip ${retries}; then
            break
        else
            retries=$((retries + 1))
        fi
    done
    # Github
    retries=0
    while true; do
        if test_connectivity_github ${retries}; then
            break
        else
            retries=$((retries + 1))
        fi
    done
    INFO "PIP：${PIP_LOG}，Github：${GITHUB_LOG}"
    if [ -n "${GITHUB_TOKEN}" ]; then
        CURL_HEADERS="--oauth2-bearer ${GITHUB_TOKEN}"
    else
        CURL_HEADERS=""
    fi
    if [ "${MOVIEPILOT_AUTO_UPDATE}" = "dev" ]; then
        INFO "Dev 更新模式"
        install_backend_and_download_resources "heads/v2.zip"
    else
        INFO "Release 更新模式"
        old_version=$(grep -m -1 "^\s*APP_VERSION\s*=\s*" /app/version.py | tr -d '\r\n' | awk -F'#' '{print $1}' | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')
        if [[ "${old_version}" == *APP_VERSION* ]]; then
            current_version=$(echo "${old_version}" | sed -rn "s/APP_VERSION\s*=\s*['\"](.*)['\"]/\1/gp")
            INFO "当前版本号：${current_version}"
            # 获取所有发布的版本列表，并筛选出以v2开头的版本号
            releases=$(curl ${CURL_OPTIONS} "https://api.github.com/repos/jxxghp/MoviePilot/releases" ${CURL_HEADERS} | jq -r '.[].tag_name' | grep "^v2\.")
            if [ -z "$releases" ]; then
                WARN "未找到任何v2后端版本，继续启动..."
            else
                # 找到最新的v2版本
                latest_v2=$(echo "$releases" | sort -V | tail -n 1)
                INFO "最新的v2后端版本号：${latest_v2}"
                # 使用版本号比较函数进行比较，并下载最新版本
                compare_versions "${current_version}" "${latest_v2}"
            fi
        else
            WARN "当前版本号获取失败，继续启动..."
        fi
    fi
    if [ -d "${TMP_PATH}" ]; then
        rm -rf "${TMP_PATH}"
    fi
elif [[ "${MOVIEPILOT_AUTO_UPDATE}" = "false" ]]; then
    INFO "程序自动升级已关闭，如需自动升级请在创建容器时设置环境变量：MOVIEPILOT_AUTO_UPDATE=release"
else
    INFO "MOVIEPILOT_AUTO_UPDATE 变量设置错误"
fi
