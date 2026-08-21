#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "${TMP_DIR}"' EXIT

mkdir -p "${TMP_DIR}/bin" "${TMP_DIR}/venv/bin" "${TMP_DIR}/config"

cat > "${TMP_DIR}/bin/uv" <<'SH'
#!/usr/bin/env bash
printf 'argv=%s\n' "$*" >> "${MP_FAKE_UV_LOG}"
printf 'HTTP_PROXY=%s\n' "${HTTP_PROXY:-}" >> "${MP_FAKE_UV_LOG}"
printf 'HTTPS_PROXY=%s\n' "${HTTPS_PROXY:-}" >> "${MP_FAKE_UV_LOG}"
printf 'PACKAGE_CACHE_ROOT=%s\n' "${PACKAGE_CACHE_ROOT:-}" >> "${MP_FAKE_UV_LOG}"
printf 'UV_CACHE_DIR=%s\n' "${UV_CACHE_DIR:-}" >> "${MP_FAKE_UV_LOG}"
printf 'UV_PROJECT_ENVIRONMENT=%s\n' "${UV_PROJECT_ENVIRONMENT:-}" >> "${MP_FAKE_UV_LOG}"
if [ "${MP_FAKE_UV_FAIL:-}" = "1" ]; then
  exit 1
fi
exit 0
SH
chmod +x "${TMP_DIR}/bin/uv"

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

# macOS 默认不提供 GNU timeout；模拟器只需保留被执行命令的参数和环境。
timeout() {
  if [[ "${1:-}" == --kill-after=* ]]; then
    shift
  fi
  shift
  "$@"
}

MP_FAKE_UV_LOG="${TMP_DIR}/update.log"
export MP_FAKE_UV_LOG
export UV_BIN="${TMP_DIR}/bin/uv"
export VENV_PATH="${TMP_DIR}/venv"
export CONFIG_DIR="${TMP_DIR}/config"
export MOVIEPILOT_AUTO_UPDATE=false
export PIP_PROXY="https://mirror.example/simple"
export PROXY_HOST="http://proxy.example:7890"
unset PACKAGE_CACHE_ROOT UV_CACHE_DIR HTTP_PROXY HTTPS_PROXY http_proxy https_proxy
source "${ROOT}/docker/update.sh" >/dev/null

: > "${MP_FAKE_UV_LOG}"
test_connectivity_package 0
assert_contains "argv=pip install --target " "${MP_FAKE_UV_LOG}"
assert_contains "--no-deps --default-index https://mirror.example/simple pip-hello-world" "${MP_FAKE_UV_LOG}"
assert_contains "HTTPS_PROXY=http://proxy.example:7890" "${MP_FAKE_UV_LOG}"
assert_contains "PACKAGE_CACHE_ROOT=${TMP_DIR}/config/.cache" "${MP_FAKE_UV_LOG}"
assert_contains "UV_CACHE_DIR=${TMP_DIR}/config/.cache/uv" "${MP_FAKE_UV_LOG}"
if [[ "${UV_OPTIONS[*]}" != "--default-index ${PIP_PROXY}" ]]; then
  echo "mirror branch must preserve uv index option: ${UV_OPTIONS[*]}" >&2
  exit 1
fi
if [[ -n "${HTTP_PROXY:-}" || -n "${HTTPS_PROXY:-}" || -n "${http_proxy:-}" || -n "${https_proxy:-}" ]]; then
  echo "package connectivity must not leak PROXY_HOST into parent proxy env" >&2
  env | grep -E '^(HTTP_PROXY|HTTPS_PROXY|http_proxy|https_proxy)=' >&2 || true
  exit 1
fi

: > "${MP_FAKE_UV_LOG}"
PIP_PROXY=""
test_connectivity_package 1
assert_contains "argv=pip install --target " "${MP_FAKE_UV_LOG}"
assert_contains "HTTPS_PROXY=http://proxy.example:7890" "${MP_FAKE_UV_LOG}"
if [[ ${#UV_OPTIONS[@]} -ne 0 ]]; then
  echo "proxy branch must keep UV_OPTIONS empty: ${UV_OPTIONS[*]}" >&2
  exit 1
fi
if [[ -n "${HTTP_PROXY:-}" || -n "${HTTPS_PROXY:-}" || -n "${http_proxy:-}" || -n "${https_proxy:-}" ]]; then
  echo "proxy connectivity must not leak PROXY_HOST into parent proxy env" >&2
  env | grep -E '^(HTTP_PROXY|HTTPS_PROXY|http_proxy|https_proxy)=' >&2 || true
  exit 1
fi

MP_FAKE_UV_LOG="${TMP_DIR}/update-explicit-standard-proxy.log"
export MP_FAKE_UV_LOG
(
  export VENV_PATH="${TMP_DIR}/venv"
  export CONFIG_DIR="${TMP_DIR}/config"
  export PIP_PROXY=""
  export PROXY_HOST="http://proxy.example:7890"
  export HTTP_PROXY="http://explicit.example:8080"
  export HTTPS_PROXY="http://explicit.example:8080"
  export http_proxy="http://explicit.example:8080"
  export https_proxy="http://explicit.example:8080"
  source "${ROOT}/docker/update.sh" >/dev/null
  test_connectivity_package 1
  if [[ "${HTTP_PROXY}" != "http://explicit.example:8080" || "${HTTPS_PROXY}" != "http://explicit.example:8080" ]]; then
    echo "explicit standard proxy env must be preserved" >&2
    exit 1
  fi
)
assert_contains "HTTPS_PROXY=http://proxy.example:7890" "${MP_FAKE_UV_LOG}"

MP_FAKE_UV_LOG="${TMP_DIR}/update-explicit-cache.log"
export MP_FAKE_UV_LOG
(
  export VENV_PATH="${TMP_DIR}/venv"
  export CONFIG_DIR="${TMP_DIR}/config"
  export PACKAGE_CACHE_ROOT="${TMP_DIR}/update-custom-package-cache"
  export UV_CACHE_DIR="${TMP_DIR}/explicit-uv-cache"
  export PIP_PROXY="https://mirror.example/simple"
  export PROXY_HOST="http://proxy.example:7890"
  source "${ROOT}/docker/update.sh" >/dev/null
  test_connectivity_package 0
)
assert_contains "PACKAGE_CACHE_ROOT=${TMP_DIR}/update-custom-package-cache" "${MP_FAKE_UV_LOG}"
assert_contains "UV_CACHE_DIR=${TMP_DIR}/explicit-uv-cache" "${MP_FAKE_UV_LOG}"

MP_FAKE_UV_LOG="${TMP_DIR}/update-fallback-no-proxy.log"
export MP_FAKE_UV_LOG
(
  export VENV_PATH="${TMP_DIR}/venv"
  export CONFIG_DIR="${TMP_DIR}/config"
  export PIP_PROXY="https://mirror.example/simple"
  export PROXY_HOST="http://proxy.example:7890"
  unset PACKAGE_CACHE_ROOT UV_CACHE_DIR HTTP_PROXY HTTPS_PROXY http_proxy https_proxy
  source "${ROOT}/docker/update.sh" >/dev/null
  MP_FAKE_UV_FAIL=1 test_connectivity_package 0 && exit 1
  test_connectivity_package 2
  if [[ "${PACKAGE_LOG}" != "不使用代理" ]]; then
    echo "fallback branch must report direct mode: ${PACKAGE_LOG}" >&2
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

MP_FAKE_UV_LOG="${TMP_DIR}/entrypoint.log"
MP_FAKE_PYTHON_COUNT="${TMP_DIR}/python-count"
export MP_FAKE_UV_LOG MP_FAKE_PYTHON_COUNT
(
  export VENV_PATH="${TMP_DIR}/venv"
  export CONFIG_DIR="${TMP_DIR}/config"
  unset PACKAGE_CACHE_ROOT UV_CACHE_DIR HTTP_PROXY HTTPS_PROXY http_proxy https_proxy
  export PIP_PROXY=""
  export PROXY_HOST="http://proxy.example:7890"
  source "${ENTRYPOINT_FUNCS}"
  apply_package_cache_env
  ensure_backend_runtime_dependencies
  if [[ -n "${HTTP_PROXY:-}" || -n "${HTTPS_PROXY:-}" || -n "${http_proxy:-}" || -n "${https_proxy:-}" ]]; then
    echo "dependency recovery must not leak PROXY_HOST into parent proxy env" >&2
    exit 1
  fi
) >/dev/null

assert_contains "argv=sync --project /app --locked --no-dev --no-install-project --inexact" "${MP_FAKE_UV_LOG}"
assert_contains "HTTPS_PROXY=http://proxy.example:7890" "${MP_FAKE_UV_LOG}"
assert_contains "PACKAGE_CACHE_ROOT=${TMP_DIR}/config/.cache" "${MP_FAKE_UV_LOG}"
assert_contains "UV_CACHE_DIR=${TMP_DIR}/config/.cache/uv" "${MP_FAKE_UV_LOG}"
assert_contains "UV_PROJECT_ENVIRONMENT=${TMP_DIR}/venv" "${MP_FAKE_UV_LOG}"
assert_not_contains "requirements" "${MP_FAKE_UV_LOG}"

MP_FAKE_UV_LOG="${TMP_DIR}/entrypoint-explicit-standard-proxy.log"
MP_FAKE_PYTHON_COUNT="${TMP_DIR}/python-count-explicit-standard-proxy"
export MP_FAKE_UV_LOG MP_FAKE_PYTHON_COUNT
(
  export VENV_PATH="${TMP_DIR}/venv"
  export CONFIG_DIR="${TMP_DIR}/config"
  unset PACKAGE_CACHE_ROOT UV_CACHE_DIR
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
    exit 1
  fi
) >/dev/null
assert_contains "HTTPS_PROXY=http://proxy.example:7890" "${MP_FAKE_UV_LOG}"

MP_FAKE_UV_LOG="${TMP_DIR}/entrypoint-app-env.log"
MP_FAKE_PYTHON_COUNT="${TMP_DIR}/python-count-app-env"
cat > "${TMP_DIR}/config/app.env" <<EOF
PACKAGE_CACHE_ROOT='${TMP_DIR}/app-env-custom-package-cache'
PROXY_HOST='http://proxy.example:7890'
EOF
export MP_FAKE_UV_LOG MP_FAKE_PYTHON_COUNT
(
  export VENV_PATH="${TMP_DIR}/venv"
  export CONFIG_DIR="${TMP_DIR}/config"
  unset PACKAGE_CACHE_ROOT UV_CACHE_DIR PIP_PROXY PROXY_HOST
  source "${ENTRYPOINT_FUNCS}"
  load_config_from_app_env
  apply_package_cache_env
  ensure_backend_runtime_dependencies
) >/dev/null
assert_contains "PACKAGE_CACHE_ROOT=${TMP_DIR}/app-env-custom-package-cache" "${MP_FAKE_UV_LOG}"
assert_contains "UV_CACHE_DIR=${TMP_DIR}/app-env-custom-package-cache/uv" "${MP_FAKE_UV_LOG}"

echo "Docker package env simulation passed"
