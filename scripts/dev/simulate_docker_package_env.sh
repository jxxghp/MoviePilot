#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "${TMP_DIR}"' EXIT

mkdir -p "${TMP_DIR}/venv/bin" "${TMP_DIR}/config"

cat > "${TMP_DIR}/venv/bin/pip" <<'SH'
#!/usr/bin/env bash
printf 'argv=%s\n' "$*" >> "${MP_FAKE_PIP_LOG}"
printf 'HTTP_PROXY=%s\n' "${HTTP_PROXY:-}" >> "${MP_FAKE_PIP_LOG}"
printf 'HTTPS_PROXY=%s\n' "${HTTPS_PROXY:-}" >> "${MP_FAKE_PIP_LOG}"
printf 'PIP_CACHE_DIR=%s\n' "${PIP_CACHE_DIR:-}" >> "${MP_FAKE_PIP_LOG}"
printf 'UV_CACHE_DIR=%s\n' "${UV_CACHE_DIR:-}" >> "${MP_FAKE_PIP_LOG}"
exit 0
SH
chmod +x "${TMP_DIR}/venv/bin/pip"

assert_contains() {
  local needle="$1"
  local file="$2"
  if ! grep -Fq -- "$needle" "$file"; then
    echo "missing expected text: $needle" >&2
    cat "$file" >&2
    exit 1
  fi
}

assert_not_contains() {
  local needle="$1"
  local file="$2"
  if grep -Fq -- "$needle" "$file"; then
    echo "unexpected text: $needle" >&2
    cat "$file" >&2
    exit 1
  fi
}

UPDATE_FUNCS="${TMP_DIR}/update-functions.sh"
awk '
  BEGIN {capture=1}
  /^if \[\[ "\$\{MOVIEPILOT_AUTO_UPDATE\}"/ {capture=0}
  capture {print}
' "${ROOT}/docker/update.sh" > "${UPDATE_FUNCS}"

MP_FAKE_PIP_LOG="${TMP_DIR}/update.log"
export MP_FAKE_PIP_LOG
export VENV_PATH="${TMP_DIR}/venv"
export CONFIG_DIR="${TMP_DIR}/config"
export MOVIEPILOT_AUTO_UPDATE=false
export PIP_PROXY="https://mirror.example/simple"
export PROXY_HOST="http://proxy.example:7890"
source "${UPDATE_FUNCS}" >/dev/null

: > "${MP_FAKE_PIP_LOG}"
test_connectivity_pip 0
assert_contains "argv=install -i https://mirror.example/simple pip-hello-world" "${MP_FAKE_PIP_LOG}"
if [[ "${PIP_OPTIONS}" != "-i ${PIP_PROXY}" ]]; then
  echo "mirror branch must preserve index option: ${PIP_OPTIONS}" >&2
  exit 1
fi
if [[ "${PIP_OPTIONS}" == *"--proxy"* ]]; then
  echo "PIP_OPTIONS must not contain --proxy: ${PIP_OPTIONS}" >&2
  exit 1
fi
assert_not_contains "user:pass" "${MP_FAKE_PIP_LOG}"

: > "${MP_FAKE_PIP_LOG}"
PIP_PROXY=""
test_connectivity_pip 1
assert_contains "argv=install pip-hello-world" "${MP_FAKE_PIP_LOG}"
assert_contains "HTTPS_PROXY=http://proxy.example:7890" "${MP_FAKE_PIP_LOG}"
if [[ -n "${PIP_OPTIONS}" ]]; then
  echo "proxy branch must keep PIP_OPTIONS empty: ${PIP_OPTIONS}" >&2
  exit 1
fi

ENTRYPOINT_FUNCS="${TMP_DIR}/entrypoint-functions.sh"
awk '
  BEGIN {capture=1}
  /^# 使用env配置/ {capture=0}
  capture {print}
' "${ROOT}/docker/entrypoint.sh" > "${ENTRYPOINT_FUNCS}"

cat > "${TMP_DIR}/venv/bin/python3" <<'SH'
#!/usr/bin/env bash
count_file="${MP_FAKE_PYTHON_COUNT}"
count=0
if [ -f "$count_file" ]; then
  count="$(cat "$count_file")"
fi
count=$((count + 1))
printf '%s' "$count" > "$count_file"
if [ "$count" -eq 1 ]; then
  exit 1
fi
exit 0
SH
chmod +x "${TMP_DIR}/venv/bin/python3"

MP_FAKE_PIP_LOG="${TMP_DIR}/entrypoint.log"
MP_FAKE_PYTHON_COUNT="${TMP_DIR}/python-count"
export MP_FAKE_PIP_LOG MP_FAKE_PYTHON_COUNT
(
  export VENV_PATH="${TMP_DIR}/venv"
  export CONFIG_DIR="${TMP_DIR}/config"
  export PIP_PROXY=""
  export PROXY_HOST="http://proxy.example:7890"
  source "${ENTRYPOINT_FUNCS}"
  ensure_backend_runtime_dependencies
) >/dev/null

assert_contains "argv=install -r /app/requirements.txt" "${MP_FAKE_PIP_LOG}"
assert_contains "HTTPS_PROXY=http://proxy.example:7890" "${MP_FAKE_PIP_LOG}"
assert_contains "PIP_CACHE_DIR=${TMP_DIR}/config/.cache/pip" "${MP_FAKE_PIP_LOG}"
assert_contains "UV_CACHE_DIR=${TMP_DIR}/config/.cache/uv" "${MP_FAKE_PIP_LOG}"
assert_not_contains "--proxy" "${MP_FAKE_PIP_LOG}"

echo "Docker package env simulation passed"
