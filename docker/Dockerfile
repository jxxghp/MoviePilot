# syntax=docker/dockerfile:1

FROM ghcr.io/astral-sh/uv:0.12.5@sha256:e85be844203885286c60ffad8a858d48afb6c5a5c237ca0e67f12e74b8f174b1 AS uv


FROM rclone/rclone:1.75.0@sha256:b06aed988cf5967de7c25be5925240983981c757f4ed1ac9d2fa659d51d60548 AS rclone


FROM mwader/static-ffmpeg:8.1.1@sha256:735f84b905e00d5c618b667f0b053f83b1096f5fc404c607e6134bf2275a0e0a AS ffmpeg


FROM python:3.14.7-slim-trixie AS base


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


# 准备 python 环境
FROM base AS prepare_venv

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

# 按锁文件创建主程序虚拟环境
WORKDIR /app
COPY --from=uv /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock ./
RUN python3 -m venv --without-pip ${VENV_PATH} \
    && UV_PROJECT_ENVIRONMENT=${VENV_PATH} uv sync \
    --locked \
    --no-dev \
    --no-install-project

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

RUN set -eu; \
    test -n "${MOVIEPILOT_RESOURCES_REF}"; \
    python_ver="$(python3 -c 'import sys; print(f"cpython-{sys.version_info.major}{sys.version_info.minor}")')"; \
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
RUN mkdir -p /bundle/control /bundle/nginx /bundle/bin \
    && find . -maxdepth 1 -type f -name '*.sh' ! -name 'launcher.sh' -exec cp -f -t /bundle/control {} + \
    && cp -f launcher.sh /bundle/entrypoint.sh \
    && cp -f nginx.common.conf /bundle/nginx/common.conf \
    && cp -f nginx.template.conf /bundle/nginx/nginx.template.conf \
    && cp -f docker_http_proxy.conf /bundle/nginx/docker_http_proxy.conf \
    && printf '%s\n' '#!/usr/bin/env bash' 'set -euo pipefail' 'cd /app' 'exec "${VENV_PATH:-/opt/venv}/bin/python3" -m app.cli "$@"' > /bundle/bin/moviepilot \
    && bash -n /bundle/entrypoint.sh \
    && for control_script in /bundle/control/*.sh; do bash -n "${control_script}" || exit 1; done \
    && chmod 755 /bundle/entrypoint.sh /bundle/bin/moviepilot \
    && chmod 500 /bundle/control/*.sh

# final 阶段: 安装运行时依赖和配置最终镜像
FROM prepare_package AS final

ENV LD_PRELOAD="/usr/local/lib/libjemalloc.so" \
    MOVIEPILOT_DOCKER_KEEPALIVE_ON_FAILURE="true"

# 引入支持 amr 编码的静态 ffmpeg
COPY --from=ffmpeg /ffmpeg /usr/local/bin/
COPY --from=ffmpeg /ffprobe /usr/local/bin/
COPY --from=rclone /usr/local/bin/rclone /usr/bin/rclone

# python 环境
COPY --from=prepare_venv --chmod=777 ${VENV_PATH} ${VENV_PATH}
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
COPY --from=prepare_control /bundle/entrypoint.sh /entrypoint.sh
COPY --from=prepare_control /bundle/control /usr/local/lib/moviepilot/control
COPY --from=prepare_control /bundle/nginx/common.conf /etc/nginx/common.conf
COPY --from=prepare_control /bundle/nginx/nginx.template.conf /etc/nginx/nginx.template.conf
COPY --from=prepare_control /bundle/nginx/docker_http_proxy.conf /etc/nginx/docker_http_proxy.conf
COPY --from=prepare_control /bundle/bin/moviepilot /usr/local/bin/moviepilot

RUN mkdir -p ${HOME} \
    && groupadd -r moviepilot -g 918 \
    && useradd -r moviepilot -g moviepilot -d ${HOME} -s /bin/bash -u 918 \
    && python_ver=$(python3 -V | awk '{print $2}') \
    && echo "/app/" > ${VENV_PATH}/lib/python${python_ver%.*}/site-packages/app.pth \
    && echo 'fs.inotify.max_user_watches=5242880' >> /etc/sysctl.conf \
    && echo 'fs.inotify.max_user_instances=5242880' >> /etc/sysctl.conf \
    && echo "zh_CN.UTF-8 UTF-8" >> /etc/locale.gen \
    && locale-gen zh_CN.UTF-8

# 载荷按独立更新频率写入镜像，后端源码保持在最后一层。
WORKDIR /app

COPY --link --from=prepare_frontend /public /public
# /app/app/plugins 是扩展的安装挂载点，纯数据目录：不含宿主源码，可以整个挂宿主目录
# 上去做跨升级持久化（docker run -v /宿主目录:/app/app/plugins），覆盖后 app.plugins
# 仍以命名空间包解析
COPY --link --from=prepare_plugins /plugins /app/app/plugins
COPY --link --from=prepare_resources /resources /app/app/application/site
COPY --link --from=prepare_backend /app /app

EXPOSE 3000
VOLUME [ "${CONFIG_DIR}" ]
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 CMD /usr/bin/curl -fsS "http://127.0.0.1:${PORT:-3001}/health/ready" >/dev/null || exit 1
ENTRYPOINT [ "/usr/bin/tini", "-g", "--", "/entrypoint.sh" ]
