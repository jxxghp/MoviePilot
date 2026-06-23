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
printf 'PACKAGE_CACHE_ROOT=%s\n' "${PACKAGE_CACHE_ROOT:-}" >> "${MP_FAKE_PIP_LOG}"
printf 'PIP_CACHE_DIR=%s\n' "${PIP_CACHE_DIR:-}" >> "${MP_FAKE_PIP_LOG}"
printf 'UV_CACHE_DIR=%s\n' "${UV_CACHE_DIR:-}" >> "${MP_FAKE_PIP_LOG}"
if [ "${MP_FAKE_PIP_FAIL:-}" = "1" ]; then
  exit 1
fi
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
unset PACKAGE_CACHE_ROOT PIP_CACHE_DIR UV_CACHE_DIR HTTP_PROXY HTTPS_PROXY http_proxy https_proxy
source "${UPDATE_FUNCS}" >/dev/null

: > "${MP_FAKE_PIP_LOG}"
test_connectivity_pip 0
assert_contains "argv=install -i https://mirror.example/simple pip-hello-world" "${MP_FAKE_PIP_LOG}"
assert_contains "HTTPS_PROXY=http://proxy.example:7890" "${MP_FAKE_PIP_LOG}"
assert_contains "PACKAGE_CACHE_ROOT=${TMP_DIR}/config/.cache" "${MP_FAKE_PIP_LOG}"
assert_contains "PIP_CACHE_DIR=${TMP_DIR}/config/.cache/pip" "${MP_FAKE_PIP_LOG}"
assert_contains "UV_CACHE_DIR=${TMP_DIR}/config/.cache/uv" "${MP_FAKE_PIP_LOG}"
if [[ "${PIP_OPTIONS}" != "-i ${PIP_PROXY}" ]]; then
  echo "mirror branch must preserve index option: ${PIP_OPTIONS}" >&2
  exit 1
fi
if [[ "${PIP_OPTIONS}" == *"--proxy"* ]]; then
  echo "PIP_OPTIONS must not contain --proxy: ${PIP_OPTIONS}" >&2
  exit 1
fi
if [[ -n "${HTTP_PROXY:-}" || -n "${HTTPS_PROXY:-}" || -n "${http_proxy:-}" || -n "${https_proxy:-}" ]]; then
  echo "pip connectivity must not leak PROXY_HOST into parent proxy env" >&2
  env | grep -E '^(HTTP_PROXY|HTTPS_PROXY|http_proxy|https_proxy)=' >&2 || true
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
if [[ -n "${HTTP_PROXY:-}" || -n "${HTTPS_PROXY:-}" || -n "${http_proxy:-}" || -n "${https_proxy:-}" ]]; then
  echo "proxy connectivity must not leak PROXY_HOST into parent proxy env" >&2
  env | grep -E '^(HTTP_PROXY|HTTPS_PROXY|http_proxy|https_proxy)=' >&2 || true
  exit 1
fi

MP_FAKE_PIP_LOG="${TMP_DIR}/update-explicit-standard-proxy.log"
export MP_FAKE_PIP_LOG
(
  export VENV_PATH="${TMP_DIR}/venv"
  export CONFIG_DIR="${TMP_DIR}/config"
  export MOVIEPILOT_AUTO_UPDATE=false
  export PIP_PROXY=""
  export PROXY_HOST="http://proxy.example:7890"
  export HTTP_PROXY="http://explicit.example:8080"
  export HTTPS_PROXY="http://explicit.example:8080"
  export http_proxy="http://explicit.example:8080"
  export https_proxy="http://explicit.example:8080"
  source "${UPDATE_FUNCS}" >/dev/null
  test_connectivity_pip 1
  if [[ "${HTTP_PROXY}" != "http://explicit.example:8080" || "${HTTPS_PROXY}" != "http://explicit.example:8080" ]]; then
    echo "explicit standard proxy env must be preserved" >&2
    env | grep -E '^(HTTP_PROXY|HTTPS_PROXY|http_proxy|https_proxy)=' >&2 || true
    exit 1
  fi
)
assert_contains "HTTPS_PROXY=http://proxy.example:7890" "${MP_FAKE_PIP_LOG}"

MP_FAKE_PIP_LOG="${TMP_DIR}/update-explicit-cache.log"
export MP_FAKE_PIP_LOG
(
  export VENV_PATH="${TMP_DIR}/venv"
  export CONFIG_DIR="${TMP_DIR}/config"
  export MOVIEPILOT_AUTO_UPDATE=false
  export PACKAGE_CACHE_ROOT="${TMP_DIR}/update-custom-package-cache"
  export PIP_CACHE_DIR="${TMP_DIR}/explicit-pip-cache"
  export UV_CACHE_DIR="${TMP_DIR}/explicit-uv-cache"
  export PIP_PROXY="https://mirror.example/simple"
  export PROXY_HOST="http://proxy.example:7890"
  source "${UPDATE_FUNCS}" >/dev/null
  test_connectivity_pip 0
)

assert_contains "PACKAGE_CACHE_ROOT=${TMP_DIR}/update-custom-package-cache" "${MP_FAKE_PIP_LOG}"
assert_contains "PIP_CACHE_DIR=${TMP_DIR}/explicit-pip-cache" "${MP_FAKE_PIP_LOG}"
assert_contains "UV_CACHE_DIR=${TMP_DIR}/explicit-uv-cache" "${MP_FAKE_PIP_LOG}"

MP_FAKE_PIP_LOG="${TMP_DIR}/update-fallback-no-proxy.log"
export MP_FAKE_PIP_LOG
(
  export VENV_PATH="${TMP_DIR}/venv"
  export CONFIG_DIR="${TMP_DIR}/config"
  export MOVIEPILOT_AUTO_UPDATE=false
  export PIP_PROXY="https://mirror.example/simple"
  export PROXY_HOST="http://proxy.example:7890"
  unset PACKAGE_CACHE_ROOT PIP_CACHE_DIR UV_CACHE_DIR HTTP_PROXY HTTPS_PROXY http_proxy https_proxy
  source "${UPDATE_FUNCS}" >/dev/null
  MP_FAKE_PIP_FAIL=1 test_connectivity_pip 0 && exit 1
  if [[ -n "${HTTPS_PROXY:-}" || -n "${https_proxy:-}" ]]; then
    echo "mirror failure must not leak proxy env" >&2
    env | grep -E '^(HTTP_PROXY|HTTPS_PROXY|http_proxy|https_proxy)=' >&2 || true
    exit 1
  fi
  test_connectivity_pip 2
  if [[ "${PIP_LOG}" != "不使用代理" ]]; then
    echo "fallback branch must report direct mode: ${PIP_LOG}" >&2
    exit 1
  fi
)

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
  unset PACKAGE_CACHE_ROOT PIP_CACHE_DIR UV_CACHE_DIR HTTP_PROXY HTTPS_PROXY http_proxy https_proxy
  export PIP_PROXY=""
  export PROXY_HOST="http://proxy.example:7890"
  source "${ENTRYPOINT_FUNCS}"
  apply_package_cache_env
  ensure_backend_runtime_dependencies
  if [[ -n "${HTTP_PROXY:-}" || -n "${HTTPS_PROXY:-}" || -n "${http_proxy:-}" || -n "${https_proxy:-}" ]]; then
    echo "dependency recovery must not leak PROXY_HOST into parent proxy env" >&2
    env | grep -E '^(HTTP_PROXY|HTTPS_PROXY|http_proxy|https_proxy)=' >&2 || true
    exit 1
  fi
) >/dev/null

assert_contains "argv=install -r /app/requirements.txt" "${MP_FAKE_PIP_LOG}"
assert_contains "HTTPS_PROXY=http://proxy.example:7890" "${MP_FAKE_PIP_LOG}"
assert_contains "PACKAGE_CACHE_ROOT=${TMP_DIR}/config/.cache" "${MP_FAKE_PIP_LOG}"
assert_contains "PIP_CACHE_DIR=${TMP_DIR}/config/.cache/pip" "${MP_FAKE_PIP_LOG}"
assert_contains "UV_CACHE_DIR=${TMP_DIR}/config/.cache/uv" "${MP_FAKE_PIP_LOG}"
assert_not_contains "--proxy" "${MP_FAKE_PIP_LOG}"

MP_FAKE_PIP_LOG="${TMP_DIR}/entrypoint-explicit-standard-proxy.log"
MP_FAKE_PYTHON_COUNT="${TMP_DIR}/python-count-explicit-standard-proxy"
export MP_FAKE_PIP_LOG MP_FAKE_PYTHON_COUNT
(
  export VENV_PATH="${TMP_DIR}/venv"
  export CONFIG_DIR="${TMP_DIR}/config"
  unset PACKAGE_CACHE_ROOT PIP_CACHE_DIR UV_CACHE_DIR
  export PIP_PROXY=""
  export PROXY_HOST="http://proxy.example:7890"
  export HTTP_PROXY="http://explicit.example:8080"
  export HTTPS_PROXY="http://explicit.example:8080"
  export http_proxy="http://explicit.example:8080"
  export https_proxy="http://explicit.example:8080"
  source "${ENTRYPOINT_FUNCS}"
  apply_package_cache_env
  ensure_backend_runtime_dependencies
  if [[ "${HTTP_PROXY}" != "http://explicit.example:8080" || "${HTTPS_PROXY}" != "http://explicit.example:8080" ]]; then
    echo "dependency recovery must preserve explicit standard proxy env" >&2
    env | grep -E '^(HTTP_PROXY|HTTPS_PROXY|http_proxy|https_proxy)=' >&2 || true
    exit 1
  fi
) >/dev/null

assert_contains "HTTPS_PROXY=http://proxy.example:7890" "${MP_FAKE_PIP_LOG}"

MP_FAKE_PIP_LOG="${TMP_DIR}/entrypoint-app-env.log"
MP_FAKE_PYTHON_COUNT="${TMP_DIR}/python-count-app-env"
cat > "${TMP_DIR}/config/app.env" <<EOF
PACKAGE_CACHE_ROOT='${TMP_DIR}/app-env-custom-package-cache'
PROXY_HOST='http://proxy.example:7890'
EOF
export MP_FAKE_PIP_LOG MP_FAKE_PYTHON_COUNT
(
  export VENV_PATH="${TMP_DIR}/venv"
  export CONFIG_DIR="${TMP_DIR}/config"
  unset PACKAGE_CACHE_ROOT PIP_CACHE_DIR UV_CACHE_DIR PIP_PROXY PROXY_HOST
  source "${ENTRYPOINT_FUNCS}"
  load_config_from_app_env
  apply_package_cache_env
  ensure_backend_runtime_dependencies
) >/dev/null

assert_contains "PACKAGE_CACHE_ROOT=${TMP_DIR}/app-env-custom-package-cache" "${MP_FAKE_PIP_LOG}"
assert_contains "PIP_CACHE_DIR=${TMP_DIR}/app-env-custom-package-cache/pip" "${MP_FAKE_PIP_LOG}"
assert_contains "UV_CACHE_DIR=${TMP_DIR}/app-env-custom-package-cache/uv" "${MP_FAKE_PIP_LOG}"

echo "Docker package env simulation passed"
