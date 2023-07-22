FROM python:3.10.11-slim
ENV LANG="C.UTF-8" \
    TZ="Asia/Shanghai" \
    PUID=0 \
    PGID=0 \
    UMASK=000 \
    NGINX_PORT=3000 \
    CONFIG_DIR="/config" \
    API_TOKEN="moviepilot" \
    AUTH_SITE="iyuu" \
    DOWNLOAD_PATH="/downloads" \
    DOWNLOAD_CATEGORY="false" \
    TORRENT_TAG="MOVIEPILOT" \
    LIBRARY_PATH="" \
    LIBRARY_CATEGORY="false" \
    TRANSFER_TYPE="copy" \
    COOKIECLOUD_HOST="https://nastool.org/cookiecloud" \
    COOKIECLOUD_KEY="" \
    COOKIECLOUD_PASSWORD="" \
    MESSAGER="telegram" \
    TELEGRAM_TOKEN="" \
    TELEGRAM_CHAT_ID="" \
    DOWNLOADER="qbittorrent" \
    QB_HOST="127.0.0.1:8080" \
    QB_USER="admin" \
    QB_PASSWORD="adminadmin" \
    MEDIASERVER="emby" \
    EMBY_HOST="http://127.0.0.1:8096" \
    EMBY_API_KEY="" \
    DOUBAN_USER_IDS=""
WORKDIR "/app"
COPY . .
RUN apt-get update \
    && apt-get -y install musl-dev nginx gettext-base \
    && mkdir -p /etc/nginx \
    && cp -f nginx.conf /etc/nginx/nginx.template.conf \
    && pip install -r requirements.txt \
    && python_ver=$(python3 -V | awk '{print $2}') \
    && echo "/app/" > /usr/local/lib/python${python_ver%.*}/site-packages/app.pth \
    && echo 'fs.inotify.max_user_watches=5242880' >> /etc/sysctl.conf \
    && echo 'fs.inotify.max_user_instances=5242880' >> /etc/sysctl.conf \
    && playwright install-deps chromium \
    && rm -rf /root/.cache/
EXPOSE 3000
VOLUME ["/config"]
ENTRYPOINT [ "bash", "-c", "/app/start.sh & nginx -g 'daemon off;'" ]
