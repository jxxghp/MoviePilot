#!/bin/bash
# shellcheck shell=bash

set -u

# root bootstrap 只使用镜像系统工具；业务入口保留用户 PATH 优先级，同时确保镜像系统工具可用。
LAUNCHER_INHERITED_PATH="${PATH:-}"
LAUNCHER_ENTRYPOINT_PATH="${LAUNCHER_INHERITED_PATH:+${LAUNCHER_INHERITED_PATH}:}/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
PATH="/usr/sbin:/usr/bin:/sbin:/bin"
export PATH

SOURCE_CONTROL_DIR="${MOVIEPILOT_SOURCE_CONTROL_DIR:-/app/docker}"
IMAGE_CONTROL_DIR="${MOVIEPILOT_IMAGE_CONTROL_DIR:-/usr/local/lib/moviepilot/control}"
RUNTIME_ROOT="${MOVIEPILOT_RUNTIME_CONTROL_ROOT:-/run/moviepilot/control}"
UPDATE_PENDING_FILE="${CONFIG_DIR:-/config}/temp/__update_pending__"
UPDATE_PREVIOUS_APP="/app.__update_previous__"
CONTROL_FILES=()
CONTROL_REQUIRED_FILES=(entrypoint.sh update.sh browser.sh cert.sh)

function collect_control_files() {
    local control_dir="$1"
    local path
    local file

    CONTROL_FILES=()
    for path in "${control_dir}"/*.sh; do
        [ -e "${path}" ] || [ -L "${path}" ] || continue
        file="$(basename "${path}")"
        [ "${file}" = "launcher.sh" ] && continue
        [ -f "${path}" ] || return 1
        [ ! -L "${path}" ] || return 1
    done
    while IFS= read -r path; do
        CONTROL_FILES+=("$(basename "${path}")")
    done < <(find "${control_dir}" -maxdepth 1 -type f -name '*.sh' ! -name 'launcher.sh' -print | LC_ALL=C sort)
    for path in "${CONTROL_REQUIRED_FILES[@]}"; do
        [ -f "${control_dir}/${path}" ] || return 1
        [ ! -L "${control_dir}/${path}" ] || return 1
        [[ " ${CONTROL_FILES[*]} " = *" ${path} "* ]] || return 1
    done
    [ "${#CONTROL_FILES[@]}" -ge "${#CONTROL_REQUIRED_FILES[@]}" ]
}

function control_bundle_generation() {
    local control_dir="$1"
    local file
    local checksum_line
    local digest
    local manifest=""

    collect_control_files "${control_dir}" || return 1
    for file in "${CONTROL_FILES[@]}"; do
        [ -f "${control_dir}/${file}" ] || return 1
        [ ! -L "${control_dir}/${file}" ] || return 1
        bash -n "${control_dir}/${file}" || return 1
    done

    for file in "${CONTROL_FILES[@]}"; do
        checksum_line="$(sha256sum "${control_dir}/${file}")" || return 1
        digest="${checksum_line%% *}"
        [[ "${digest}" =~ ^[0-9a-f]{64}$ ]] || return 1
        manifest+="${file} ${digest}"$'\n'
    done

    checksum_line="$(printf '%s' "${manifest}" | sha256sum)" || return 1
    digest="${checksum_line%% *}"
    [[ "${digest}" =~ ^[0-9a-f]{64}$ ]] || return 1
    printf '%s\n' "${digest}"
}

function source_bundle_is_trusted() {
    local control_dir="$1"
    local path
    local mode
    local ancestor

    [ -d "${control_dir}" ] || return 1
    ancestor="${control_dir}"
    while [ "${ancestor}" != "/" ]; do
        [ ! -L "${ancestor}" ] || return 1
        [ "$(stat -c '%u' "${ancestor}" 2>/dev/null || stat -f '%u' "${ancestor}")" = "0" ] || return 1
        mode="$(stat -c '%a' "${ancestor}" 2>/dev/null || stat -f '%Lp' "${ancestor}")"
        (( (8#${mode} & 8#022) == 0 )) || return 1
        ancestor="$(dirname "${ancestor}")"
    done

    collect_control_files "${control_dir}" || return 1
    for path in "${CONTROL_FILES[@]/#/${control_dir}/}"; do
        [ -f "${path}" ] || return 1
        [ ! -L "${path}" ] || return 1
        [ "$(stat -c '%u' "${path}" 2>/dev/null || stat -f '%u' "${path}")" = "0" ] || return 1
        mode="$(stat -c '%a' "${path}" 2>/dev/null || stat -f '%Lp' "${path}")"
        (( (8#${mode} & 8#022) == 0 )) || return 1
    done

    control_bundle_generation "${control_dir}" >/dev/null
}

function pending_update_state() {
    [ -f "${UPDATE_PENDING_FILE}" ] || return 1
    tr -d '\r\n' < "${UPDATE_PENDING_FILE}"
}

function pending_recovery_control_dir() {
    local state
    local previous_control_dir="${UPDATE_PREVIOUS_APP}/docker"

    state="$(pending_update_state 2>/dev/null || true)"
    case "${state}" in
    prepared|dependencies)
        [ -e "${UPDATE_PREVIOUS_APP}" ] || return 1
        if source_bundle_is_trusted "${previous_control_dir}"; then
            printf '%s\n' "${previous_control_dir}"
        else
            # pending 恢复只能使用旧代或镜像控制脚本，不能执行尚未提交的新源码控制脚本。
            printf '%s\n' "MoviePilot 旧代控制脚本不完整或不可信，回退到镜像内置版本。" >&2
            printf '%s\n' "${IMAGE_CONTROL_DIR}"
        fi
        return 0
        ;;
    esac
    return 1
}

function select_control_dir() {
    local recovery_dir
    if recovery_dir="$(pending_recovery_control_dir)"; then
        printf '%s\n' "${recovery_dir}"
        return 0
    fi
    if source_bundle_is_trusted "${SOURCE_CONTROL_DIR}"; then
        printf '%s\n' "${SOURCE_CONTROL_DIR}"
        return 0
    fi

    printf '%s\n' "MoviePilot 源码控制脚本不完整或不可信，回退到镜像内置版本。" >&2
    control_bundle_generation "${IMAGE_CONTROL_DIR}" >/dev/null || {
        printf '%s\n' "MoviePilot 镜像内置控制脚本无效，无法启动。" >&2
        return 1
    }
    printf '%s\n' "${IMAGE_CONTROL_DIR}"
}

function materialize_control_bundle() {
    local source_dir="$1"
    local generation="$2"
    local runtime_dir="${RUNTIME_ROOT}/${generation}"
    local staging_dir="${runtime_dir}.tmp.$$"
    local file
    local staged_generation

    collect_control_files "${source_dir}" || return 1
    mkdir -p "${RUNTIME_ROOT}" || return 1
    rm -rf "${staging_dir}" || return 1
    mkdir -m 0700 "${staging_dir}" || return 1
    for file in "${CONTROL_FILES[@]}"; do
        if ! cp "${source_dir}/${file}" "${staging_dir}/${file}" \
            || ! chmod 0500 "${staging_dir}/${file}"; then
            rm -rf "${staging_dir}"
            return 1
        fi
    done
    if ! staged_generation="$(control_bundle_generation "${staging_dir}")" \
        || [ "${staged_generation}" != "${generation}" ]; then
        rm -rf "${staging_dir}"
        return 1
    fi
    if ! rm -rf "${runtime_dir}" || ! mv "${staging_dir}" "${runtime_dir}"; then
        rm -rf "${staging_dir}"
        return 1
    fi
    printf '%s\n' "${runtime_dir}"
}

function launcher_main() {
    if [ "${1:-}" = "--source-generation" ]; then
        source_bundle_is_trusted "${SOURCE_CONTROL_DIR}" || return 1
        control_bundle_generation "${SOURCE_CONTROL_DIR}"
        return $?
    fi

    local selected_dir
    local generation
    local runtime_dir
    selected_dir="$(select_control_dir)" || return 1
    generation="$(control_bundle_generation "${selected_dir}")" || return 1
    if ! runtime_dir="$(materialize_control_bundle "${selected_dir}" "${generation}")"; then
        [ "${selected_dir}" != "${IMAGE_CONTROL_DIR}" ] || return 1
        printf '%s\n' "MoviePilot 源码控制脚本快照失败，回退到镜像内置版本。" >&2
        generation="$(control_bundle_generation "${IMAGE_CONTROL_DIR}")" || return 1
        runtime_dir="$(materialize_control_bundle "${IMAGE_CONTROL_DIR}" "${generation}")" || return 1
    fi

    export MP_CONTROL_DIR="${runtime_dir}"
    export MP_CONTROL_GENERATION="${generation}"
    PATH="${LAUNCHER_ENTRYPOINT_PATH}" exec /bin/bash "${runtime_dir}/entrypoint.sh" "$@"
}

function launch_image_control_fallback() {
    local image_entrypoint="${IMAGE_CONTROL_DIR}/entrypoint.sh"

    # 运行时快照不可用时仍允许镜像自带版本启动，避免临时目录故障阻断容器恢复。
    control_bundle_generation "${IMAGE_CONTROL_DIR}" >/dev/null || return 1
    printf '%s\n' "MoviePilot 控制脚本快照不可用，直接使用镜像内置版本启动。" >&2
    unset MP_CONTROL_DIR MP_CONTROL_GENERATION
    PATH="${LAUNCHER_ENTRYPOINT_PATH}" exec /bin/bash "${image_entrypoint}" "$@"
}

function launch_with_fallback() {
    if [ "${1:-}" = "--post-update-reexec" ]; then
        export MOVIEPILOT_BOOTSTRAP_UPDATE_DONE=1
        export MOVIEPILOT_BOOTSTRAP_REEXECUTED=1
        shift
    else
        unset MOVIEPILOT_BOOTSTRAP_UPDATE_DONE MOVIEPILOT_BOOTSTRAP_REEXECUTED
    fi

    launcher_main "$@" || launch_image_control_fallback "$@"
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    if [ "${1:-}" = "--source-generation" ]; then
        launcher_main "$@"
    else
        launch_with_fallback "$@"
    fi
fi
