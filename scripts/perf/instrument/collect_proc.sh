#!/usr/bin/env bash
# 读取采样开始时已经存在的容器进程，避免把采样器自身计入结果。

set -uo pipefail

self_pid="$$"
process_dirs=(/proc/[0-9]*)

printf 'pid\tppid\tthreads\trss_kib\tpss_kib\tuss_kib\tcomm\texe\tcmdline\n'

for process_dir in "${process_dirs[@]}"; do
    pid="${process_dir##*/}"
    if [[ "${pid}" == "${self_pid}" ]] || [[ ! -r "${process_dir}/status" ]]; then
        continue
    fi

    status_values="$(awk '
        /^PPid:/ { ppid=$2 }
        /^Threads:/ { threads=$2 }
        END { printf "%d %d", ppid + 0, threads + 0 }
    ' "${process_dir}/status" 2>/dev/null || true)"
    read -r ppid threads <<< "${status_values:-0 0}"

    rss_kib=0
    pss_kib=0
    uss_kib=0
    if [[ -r "${process_dir}/smaps_rollup" ]]; then
        memory_values="$(awk '
            /^Rss:/ { rss=$2 }
            /^Pss:/ { pss=$2 }
            /^Private_Clean:/ { uss += $2 }
            /^Private_Dirty:/ { uss += $2 }
            /^Private_Hugetlb:/ { uss += $2 }
            END { printf "%d %d %d", rss + 0, pss + 0, uss + 0 }
        ' "${process_dir}/smaps_rollup" 2>/dev/null || true)"
        read -r rss_kib pss_kib uss_kib <<< "${memory_values:-0 0 0}"
    fi

    comm=""
    if [[ -r "${process_dir}/comm" ]]; then
        IFS= read -r comm < "${process_dir}/comm" || true
    fi
    executable="$(readlink "${process_dir}/exe" 2>/dev/null || true)"
    command_line="$(tr '\000\t' '  ' < "${process_dir}/cmdline" 2>/dev/null || true)"
    comm="${comm//$'\t'/ }"
    executable="${executable//$'\t'/ }"
    command_line="${command_line//$'\t'/ }"

    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
        "${pid}" "${ppid:-0}" "${threads:-0}" \
        "${rss_kib:-0}" "${pss_kib:-0}" "${uss_kib:-0}" \
        "${comm}" "${executable}" "${command_line}"
done
