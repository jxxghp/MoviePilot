FROM python:3.12.8-slim-bookworm AS base


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

RUN apt-get update && apt-get install -y --no-install-recommends \
    nginx \
    gettext-base \
    locales \
    procps \
    gosu \
    bash \
    curl \
    wget \
    busybox \
    dumb-init \
    jq \
    fuse3 \
    rsync \
    ffmpeg \
    nano \
    && dpkg-reconfigure --frontend noninteractive tzdata \
    && curl https://rclone.org/install.sh | bash \
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

# 安装 Python 构建依赖并创建虚拟环境
WORKDIR /app
COPY requirements.in requirements.in
RUN python3 -m venv ${VENV_PATH} \
    && pip install --upgrade "pip<25.0" \
    && pip install "Cython" "pip-tools<7.5" \
    && pip-compile requirements.in \
    && pip install -r requirements.txt

# 下载准备代码
FROM prepare_package AS prepare_code

WORKDIR /app

COPY . .
RUN FRONTEND_VERSION=$(sed -n "s/^FRONTEND_VERSION\s*=\s*'\([^']*\)'/\1/p" /app/version.py) \
    && curl -sL "https://github.com/jxxghp/MoviePilot-Frontend/releases/download/${FRONTEND_VERSION}/dist.zip" | busybox unzip -d / - \
    && mv /dist /public \
    && curl -sL "https://github.com/jxxghp/MoviePilot-Plugins/archive/refs/heads/main.zip" | busybox unzip -d /tmp - \
    && mv -f /tmp/MoviePilot-Plugins-main/plugins.v2/* /app/app/plugins/ \
    && cat /tmp/MoviePilot-Plugins-main/package.json | jq -r 'to_entries[] | select(.value.v2 == true) | .key' | awk '{print tolower($0)}' | \
    while read -r i; do if [ ! -d "/app/app/plugins/$i" ]; then mv "/tmp/MoviePilot-Plugins-main/plugins/$i" "/app/app/plugins/"; else echo "跳过 $i"; fi; done \
    && curl -sL "https://github.com/jxxghp/MoviePilot-Resources/archive/refs/heads/main.zip" | busybox unzip -d /tmp - \
    && mv -f /tmp/MoviePilot-Resources-main/resources.v2/* /app/app/helper/

# final 阶段: 安装运行时依赖和配置最终镜像
FROM prepare_package AS final

# python 环境
COPY --from=prepare_venv --chmod=777 ${VENV_PATH} ${VENV_PATH}

# playwright 环境
RUN playwright install-deps chromium \
    && playwright install-deps firefox \
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
    && cp -f /app/docker/update.sh /usr/local/bin/mp_update.sh \
    && cp -f /app/docker/entrypoint.sh /entrypoint.sh \
    && cp -f /app/docker/docker_http_proxy.conf /etc/nginx/docker_http_proxy.conf \
    && chmod +x /entrypoint.sh /usr/local/bin/mp_update.sh \
    && mkdir -p ${HOME} \
    && groupadd -r moviepilot -g 918 \
    && useradd -r moviepilot -g moviepilot -d ${HOME} -s /bin/bash -u 918 \
    && python_ver=$(python3 -V | awk '{print $2}') \
    && echo "/app/" > ${VENV_PATH}/lib/python${python_ver%.*}/site-packages/app.pth \
    && echo 'fs.inotify.max_user_watches=5242880' >> /etc/sysctl.conf \
    && echo 'fs.inotify.max_user_instances=5242880' >> /etc/sysctl.conf \
    && locale-gen zh_CN.UTF-8

EXPOSE 3000
VOLUME [ "${CONFIG_DIR}" ]
ENTRYPOINT [ "/entrypoint.sh" ]
