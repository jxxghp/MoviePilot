FROM ghcr.io/astral-sh/uv:0.12.5@sha256:e85be844203885286c60ffad8a858d48afb6c5a5c237ca0e67f12e74b8f174b1 AS uv


FROM python:3.12.13-slim-bookworm AS base


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
    'deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt bookworm-pgdg main' \
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
    netcat-openbsd \
    lsof \
    nano \
    unar \
    postgresql-client-18 \
    libchromaprint-tools \
    libjemalloc2 \
    && dpkg-reconfigure --frontend noninteractive tzdata \
    && curl https://rclone.org/install.sh | bash \
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

# 下载准备代码
FROM prepare_package AS prepare_code

WORKDIR /app

COPY . .
# 构建上下文中存在 frontend-dist/ 时优先使用本地前端产物（fork 自有前端），
# 否则回退下载官方 release 的 dist.zip
RUN if [ -d /app/frontend-dist ]; then \
    mv /app/frontend-dist /public; \
    else \
    FRONTEND_VERSION=$(sed -n "s/^FRONTEND_VERSION\s*=\s*'\([^']*\)'/\1/p" /app/version.py) \
    && curl -sL "https://github.com/jxxghp/MoviePilot-Frontend/releases/download/${FRONTEND_VERSION}/dist.zip" | busybox unzip -d / - \
    && mv /dist /public; \
    fi \
    && curl -sL "https://github.com/jxxghp/MoviePilot-Plugins/archive/refs/heads/main.zip" | busybox unzip -d /tmp - \
    && mv -f /tmp/MoviePilot-Plugins-main/plugins.v2/* /app/app/plugins/ \
    && cat /tmp/MoviePilot-Plugins-main/package.json | jq -r 'to_entries[] | select(.value.v2 == true) | .key' | awk '{print tolower($0)}' | \
    while read -r i; do if [ ! -d "/app/app/plugins/$i" ]; then mv "/tmp/MoviePilot-Plugins-main/plugins/$i" "/app/app/plugins/"; else echo "跳过 $i"; fi; done \
    && curl -fsSL "https://raw.githubusercontent.com/jxxghp/MoviePilot-Resources/main/resources.v3/user.sites.v3.bin" -o /app/app/application/site/user.sites.v3.bin \
    && python_ver=$(python3 -c 'import sys; print(f"cpython-{sys.version_info.major}{sys.version_info.minor}")') \
    && ARCH=$(uname -m) \
    && if [ "$ARCH" = "aarch64" ]; then SUFFIX="aarch64-linux-gnu"; else SUFFIX="x86_64-linux-gnu"; fi \
    && curl -fsSL "https://raw.githubusercontent.com/jxxghp/MoviePilot-Resources/main/resources.v3/sites.${python_ver}-${SUFFIX}.so" -o "/app/app/application/site/sites.${python_ver}-${SUFFIX}.so"

# final 阶段: 安装运行时依赖和配置最终镜像
FROM prepare_package AS final

ENV LD_PRELOAD="/usr/local/lib/libjemalloc.so" \
    MOVIEPILOT_DOCKER_KEEPALIVE_ON_FAILURE="true"

# 引入支持 amr 编码的静态 ffmpeg
COPY --from=mwader/static-ffmpeg:8.1.1 /ffmpeg /usr/local/bin/
COPY --from=mwader/static-ffmpeg:8.1.1 /ffprobe /usr/local/bin/

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

# 准备运行代码
WORKDIR /app

COPY --from=prepare_code /app /app
COPY --from=prepare_code /public /public

RUN cp -f /app/docker/nginx.common.conf /etc/nginx/common.conf \
    && cp -f /app/docker/nginx.template.conf /etc/nginx/nginx.template.conf \
    && mkdir -p /usr/local/lib/moviepilot/control \
    && find /app/docker -maxdepth 1 -type f -name '*.sh' ! -name 'launcher.sh' -exec cp -f -t /usr/local/lib/moviepilot/control {} + \
    && cp -f /app/docker/launcher.sh /entrypoint.sh \
    && cp -f /app/docker/docker_http_proxy.conf /etc/nginx/docker_http_proxy.conf \
    && printf '%s\n' '#!/usr/bin/env bash' 'set -euo pipefail' 'cd /app' 'exec "${VENV_PATH:-/opt/venv}/bin/python3" -m app.cli "$@"' > /usr/local/bin/moviepilot \
    && bash -n /entrypoint.sh \
    && for control_script in /usr/local/lib/moviepilot/control/*.sh; do bash -n "${control_script}" || exit 1; done \
    && chmod 755 /entrypoint.sh /usr/local/bin/moviepilot \
    && chmod 500 /usr/local/lib/moviepilot/control/*.sh \
    && mkdir -p ${HOME} \
    && groupadd -r moviepilot -g 918 \
    && useradd -r moviepilot -g moviepilot -d ${HOME} -s /bin/bash -u 918 \
    && python_ver=$(python3 -V | awk '{print $2}') \
    && echo "/app/" > ${VENV_PATH}/lib/python${python_ver%.*}/site-packages/app.pth \
    && echo 'fs.inotify.max_user_watches=5242880' >> /etc/sysctl.conf \
    && echo 'fs.inotify.max_user_instances=5242880' >> /etc/sysctl.conf \
    && echo "zh_CN.UTF-8 UTF-8" >> /etc/locale.gen \
    && locale-gen zh_CN.UTF-8

EXPOSE 3000
VOLUME [ "${CONFIG_DIR}" ]
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 CMD /usr/bin/curl -fsS "http://127.0.0.1:${PORT:-3001}/api/v1/system/global?token=moviepilot" >/dev/null || exit 1
ENTRYPOINT [ "/usr/bin/tini", "-g", "--", "/entrypoint.sh" ]
