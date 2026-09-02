# syntax=docker/dockerfile:1

ARG MOVIEPILOT_PYTHON_VARIANT="standard"
ARG MOVIEPILOT_PYTHON_VERSION="3.14.7"

FROM ghcr.io/astral-sh/uv:latest AS uv


# rclone v1.75.0 包含受 CVE-2026-46603 影响的 x/image v0.44.0，临时固定已修复的官方 Beta 摘要。
FROM rclone/rclone:beta@sha256:d6f5448594ecefefcf09cfeaf85cb7a21a866328032576ce2c1813e7b59c66dc AS rclone


FROM mwader/static-ffmpeg:8.1.1@sha256:735f84b905e00d5c618b667f0b053f83b1096f5fc404c607e6134bf2275a0e0a AS ffmpeg


FROM python:${MOVIEPILOT_PYTHON_VERSION}-slim-trixie AS base


FROM rust:slim-trixie AS rust_toolchain


# 准备外部制品所需的最小工具集
FROM base AS prepare_payload

RUN apt-get update \
    && apt-get install -y --no-install-recommends bash busybox ca-certificates curl jq \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*


# 准备软件包
FROM base AS prepare_package

ENV LANG="C.UTF-8" \
    TZ="Asia/Shanghai" \
    HOME="/moviepilot" \
    CONFIG_DIR="/config" \
    TERM="xterm" \
    DISPLAY=:987 \
    PUID=0 \
    PGID=0 \
    UMASK=000 \
    PYTHON_THREAD_INHERIT_CONTEXT=0 \
    VENV_PATH="/opt/venv"

ENV PATH="${VENV_PATH}/bin:${PATH}"

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && install -d -m 0755 /usr/share/postgresql-common/pgdg \
    && curl -fsSL \
    --connect-timeout 10 \
    --max-time 30 \
    --retry 3 \
    --retry-all-errors \
    --retry-delay 2 \
    --retry-max-time 90 \
    "https://www.postgresql.org/media/keys/ACCC4CF8.asc" \
    -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
    && chmod 0644 /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
    && printf '%s\n' \
    'deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt trixie-pgdg main' \
    > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
    nginx \
    gettext-base \
    locales \
    procps \
    gosu \
    bash \
    wget \
    git \
    gh \
    busybox \
    tini \
    cron \
    jq \
    ripgrep \
    less \
    unzip \
    fuse3 \
    rsync \
    openssh-client \
    sshpass \
    iproute2 \
    util-linux \
    netcat-openbsd \
    lsof \
    nano \
    unar \
    openssl \
    postgresql-client-18 \
    libchromaprint-tools \
    libjemalloc2 \
    && dpkg-reconfigure --frontend noninteractive tzdata \
    && ln -s /usr/lib/*-linux-gnu/libjemalloc.so.2 /usr/local/lib/libjemalloc.so \
    && apt-get autoremove -y \
    && apt-get clean \
    && rm -rf \
    /tmp/* \
    /var/lib/apt/lists/* \
    /var/tmp/*


# 准备 Python 环境的公共输入
FROM base AS prepare_venv_common

# 设置环境变量
ENV LANG="C.UTF-8" \
    TZ="Asia/Shanghai" \
    VENV_PATH="/opt/venv"

ENV PATH="${VENV_PATH}/bin:${PATH}"

# 安装系统构建依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    busybox \
    jq \
    wget

WORKDIR /app
COPY --from=uv /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock ./


# 准备标准 Python 运行环境
FROM prepare_venv_common AS prepare_venv_standard

RUN mkdir -p /opt/python \
    && python3 -m venv --without-pip ${VENV_PATH} \
    && UV_PROJECT_ENVIRONMENT=${VENV_PATH} uv sync \
        --locked \
        --no-default-groups \
        --group runtime-standard \
        --no-install-project


# free-threaded 原生依赖需要 Rust 和 C 构建工具链。
FROM prepare_venv_common AS prepare_venv_free-threaded

ARG MOVIEPILOT_PYTHON_VERSION

ENV CARGO_HOME="/usr/local/cargo" \
    RUSTUP_HOME="/usr/local/rustup" \
    UV_PYTHON_INSTALL_DIR="/opt/python"

ENV PATH="${CARGO_HOME}/bin:${VENV_PATH}/bin:${PATH}"

COPY --from=rust_toolchain /usr/local/cargo /usr/local/cargo
COPY --from=rust_toolchain /usr/local/rustup /usr/local/rustup

RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq-dev \
    && rm -rf /var/lib/apt/lists/* \
    && uv python install --no-bin "${MOVIEPILOT_PYTHON_VERSION}t" \
    && python_bin="$(uv python find --managed-python "${MOVIEPILOT_PYTHON_VERSION}t")" \
    && "${python_bin}" -m venv --without-pip ${VENV_PATH} \
    && UV_PROJECT_ENVIRONMENT=${VENV_PATH} uv sync \
        --locked \
        --no-default-groups \
        --group runtime-free-threaded \
        --no-install-project \
        --python "${python_bin}"


FROM prepare_venv_standard AS verify_venv_standard

COPY app/doctor/dependencies.py /tmp/moviepilot-runtime-dependencies.py
RUN "${VENV_PATH}/bin/python" /tmp/moviepilot-runtime-dependencies.py --full


FROM prepare_venv_free-threaded AS verify_venv_free-threaded

COPY app/doctor/dependencies.py /tmp/moviepilot-runtime-dependencies.py
RUN "${VENV_PATH}/bin/python" /tmp/moviepilot-runtime-dependencies.py --full


ARG MOVIEPILOT_PYTHON_VARIANT
FROM verify_venv_${MOVIEPILOT_PYTHON_VARIANT} AS prepare_venv

# 准备后端源码
FROM base AS prepare_backend

WORKDIR /app

COPY . .
RUN rm -rf /app/frontend-dist


# 准备前端制品
FROM prepare_payload AS prepare_frontend

ARG MOVIEPILOT_FRONTEND_VERSION=""
ARG MOVIEPILOT_FRONTEND_SHA256=""

COPY version.py /tmp/moviepilot-version.py
COPY frontend-dist/ /tmp/frontend-dist/

RUN set -eu; \
    mkdir -p /public; \
    if find /tmp/frontend-dist -mindepth 1 -type f ! -name '.gitkeep' -print -quit | grep -q .; then \
        rm -f /tmp/frontend-dist/.gitkeep; \
        cp -a /tmp/frontend-dist/. /public/; \
    else \
        frontend_version="${MOVIEPILOT_FRONTEND_VERSION:-$(sed -n "s/^FRONTEND_VERSION[[:space:]]*=[[:space:]]*'\([^']*\)'/\1/p" /tmp/moviepilot-version.py)}"; \
        test -n "${frontend_version}"; \
        curl -fsSL "https://github.com/jxxghp/MoviePilot-Frontend/releases/download/${frontend_version}/dist.zip" -o /tmp/frontend.zip; \
        if [ -n "${MOVIEPILOT_FRONTEND_SHA256}" ]; then \
            printf '%s  %s\n' "${MOVIEPILOT_FRONTEND_SHA256}" /tmp/frontend.zip | sha256sum -c -; \
        fi; \
        busybox unzip -q /tmp/frontend.zip -d /tmp/frontend-release; \
        cp -a /tmp/frontend-release/dist/. /public/; \
    fi; \
    test -f /public/index.html


# 准备镜像内置插件
FROM prepare_payload AS prepare_plugins

ARG MOVIEPILOT_PLUGINS_REF="main"

RUN set -eu; \
    test -n "${MOVIEPILOT_PLUGINS_REF}"; \
    curl -fsSL "https://github.com/jxxghp/MoviePilot-Plugins/archive/${MOVIEPILOT_PLUGINS_REF}.zip" -o /tmp/plugins.zip; \
    busybox unzip -q /tmp/plugins.zip -d /tmp/plugins-src; \
    plugin_root="$(find /tmp/plugins-src -mindepth 1 -maxdepth 1 -type d -print -quit)"; \
    test -n "${plugin_root}"; \
    mkdir -p /plugins; \
    cp -a "${plugin_root}/plugins.v2/." /plugins/; \
    jq -r 'to_entries[] | select(.value.v2 == true) | .key' "${plugin_root}/package.json" | awk '{print tolower($0)}' | \
    while read -r plugin_id; do \
        if [ ! -d "/plugins/${plugin_id}" ]; then \
            cp -a "${plugin_root}/plugins/${plugin_id}" /plugins/; \
        fi; \
    done


# 准备站点资源
FROM prepare_payload AS prepare_resources

ARG TARGETARCH
ARG MOVIEPILOT_RESOURCES_REF="main"
ARG MOVIEPILOT_PYTHON_VARIANT="standard"

RUN set -eu; \
    test -n "${MOVIEPILOT_RESOURCES_REF}"; \
    case "${MOVIEPILOT_PYTHON_VARIANT}" in \
        standard) python_ver="$(python3 -c 'import sys; print(f"cpython-{sys.version_info.major}{sys.version_info.minor}")')" ;; \
        free-threaded) python_ver="cpython-314t" ;; \
        *) printf 'Unsupported Python variant: %s\n' "${MOVIEPILOT_PYTHON_VARIANT}" >&2; exit 1 ;; \
    esac; \
    target_arch="${TARGETARCH:-$(uname -m)}"; \
    case "${target_arch}" in \
        arm64|aarch64) suffix="aarch64-linux-gnu" ;; \
        amd64|x86_64) suffix="x86_64-linux-gnu" ;; \
        *) printf 'Unsupported architecture: %s\n' "${target_arch}" >&2; exit 1 ;; \
    esac; \
    mkdir -p /resources; \
    curl -fsSL "https://raw.githubusercontent.com/jxxghp/MoviePilot-Resources/${MOVIEPILOT_RESOURCES_REF}/resources.v3/user.sites.v3.bin" -o /resources/user.sites.v3.bin; \
    curl -fsSL "https://raw.githubusercontent.com/jxxghp/MoviePilot-Resources/${MOVIEPILOT_RESOURCES_REF}/resources.v3/sites.${python_ver}-${suffix}.so" -o "/resources/sites.${python_ver}-${suffix}.so"


# 准备容器控制面
FROM prepare_payload AS prepare_control

WORKDIR /control

COPY docker/ ./
RUN mkdir -p \
    /bundle/rootfs/etc/nginx \
    /bundle/rootfs/usr/local/bin \
    /bundle/rootfs/usr/local/lib/moviepilot/control \
    && find . -maxdepth 1 -type f -name '*.sh' ! -name 'launcher.sh' \
    -exec cp -f -t /bundle/rootfs/usr/local/lib/moviepilot/control {} + \
    && cp -f launcher.sh /bundle/rootfs/entrypoint.sh \
    && cp -f nginx.common.conf /bundle/rootfs/etc/nginx/common.conf \
    && cp -f nginx.template.conf /bundle/rootfs/etc/nginx/nginx.template.conf \
    && cp -f docker_http_proxy.conf /bundle/rootfs/etc/nginx/docker_http_proxy.conf \
    && printf '%s\n' '#!/usr/bin/env bash' 'set -euo pipefail' 'cd /app' 'exec "${VENV_PATH:-/opt/venv}/bin/python3" -m app.cli "$@"' > /bundle/rootfs/usr/local/bin/moviepilot \
    && bash -n /bundle/rootfs/entrypoint.sh \
    && for control_script in /bundle/rootfs/usr/local/lib/moviepilot/control/*.sh; do bash -n "${control_script}" || exit 1; done \
    && chmod 755 /bundle/rootfs/entrypoint.sh /bundle/rootfs/usr/local/bin/moviepilot \
    && chmod 500 /bundle/rootfs/usr/local/lib/moviepilot/control/*.sh

# final 阶段: 安装运行时依赖和配置最终镜像
FROM prepare_package AS final

ENV LD_PRELOAD="/usr/local/lib/libjemalloc.so" \
    MOVIEPILOT_DOCKER_KEEPALIVE_ON_FAILURE="true"

# 引入支持 amr 编码的静态 ffmpeg
COPY --from=ffmpeg /ffmpeg /ffprobe /usr/local/bin/
COPY --from=rclone /usr/local/bin/rclone /usr/bin/rclone

# python 环境
COPY --from=prepare_venv --chmod=777 ${VENV_PATH} ${VENV_PATH}
COPY --from=prepare_venv /opt/python /opt/python
COPY --from=uv /uv /usr/local/bin/uv

# 浏览器运行依赖
RUN playwright install-deps chromium \
    && apt-get autoremove -y \
    && apt-get clean \
    && rm -rf \
    /tmp/* \
    /var/lib/apt/lists/* \
    /var/tmp/*

# 准备容器控制面和运行目录
COPY --link --from=prepare_control /bundle/rootfs/ /

RUN mkdir -p ${HOME} \
    && groupadd -r moviepilot -g 918 \
    && useradd -r moviepilot -g moviepilot -d ${HOME} -s /bin/bash -u 918 \
    && site_packages="$(${VENV_PATH}/bin/python -c 'import sysconfig; print(sysconfig.get_path("purelib"))')" \
    && echo "/app/" > "${site_packages}/app.pth" \
    && echo 'fs.inotify.max_user_watches=5242880' >> /etc/sysctl.conf \
    && echo 'fs.inotify.max_user_instances=5242880' >> /etc/sysctl.conf \
    && echo "zh_CN.UTF-8 UTF-8" >> /etc/locale.gen \
    && locale-gen zh_CN.UTF-8

# 载荷按独立更新频率写入镜像，后端源码保持在最后一层。
WORKDIR /app

COPY --link --from=prepare_frontend /public /public
COPY --link --from=prepare_plugins /plugins /app/app/plugins
COPY --link --from=prepare_resources /resources /app/app/application/site
COPY --link --from=prepare_backend /app /app

EXPOSE 3000
VOLUME [ "${CONFIG_DIR}" ]
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 CMD /usr/bin/curl -fsS "http://127.0.0.1:${PORT:-3001}/health/ready" >/dev/null || exit 1
ENTRYPOINT [ "/usr/bin/tini", "-g", "--", "/entrypoint.sh" ]
