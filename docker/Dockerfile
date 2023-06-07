FROM alpine:3.17
RUN apk add --no-cache --virtual .build-deps \
        libffi-dev \
        gcc \
        musl-dev \
        libxml2-dev \
        libxslt-dev \
        python3 \
        py3-pip \
    && ln -sf /usr/bin/python3 /usr/bin/python \
    && python3 -m pip install --upgrade pip setuptools wheel \
    && apk del --purge .build-deps \
    && rm -rf /tmp/* /root/.cache /var/cache/apk/*
ENV LANG="C.UTF-8" \
    TZ="Asia/Shanghai" \
    PS1="\u@\h:\w \$ " \
    PUID=0 \
    PGID=0 \
    UMASK=000 \
    WORKDIR="/MoviePilot" \
    CONFIG_DIR="/config" \
    API_TOKEN="moviepilot" \
    LIBRARY_PATH="" \
    SUPERUSER="admin" \
    SUPERUSER_PASSWORD="password" \
    SEARCH_SOURCE="themoviedb" \
    SCRAP_SOURCE="themoviedb" \
    COOKIECLOUD_HOST="" \
    COOKIECLOUD_KEY="" \
    COOKIECLOUD_PASSWORD="" \
    USER_AGENT="" \
    MESSAGER="telegram" \
    TELEGRAM_TOKEN="" \
    TELEGRAM_CHAT_ID="" \
    DOWNLOADER="qbittorrent" \
    QB_HOST="" \
    QB_USER="" \
    QB_PASSWORD="" \
    MEDIASERVER="emby" \
    EMBY_HOST="" \
    EMBY_API_KEY="" \
    FILTER_RULE="" \
    TRANSFER_TYPE="copy" \
    DOUBAN_USER_IDS=""
COPY . ${WORKDIR}
WORKDIR ${WORKDIR}
RUN python3 -m pip install -r requirements.txt \
    && echo 'fs.inotify.max_user_watches=5242880' >> /etc/sysctl.conf \
    && echo 'fs.inotify.max_user_instances=5242880' >> /etc/sysctl.conf
EXPOSE 3001
VOLUME ["/config"]
ENTRYPOINT [ "python", "app/main.py" ]
