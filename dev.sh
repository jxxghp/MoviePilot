#!/bin/bash
# 更新环境
apt update
# 安装 Git
apt install -y git curl
# 更新后端代码
cd /
rm -rf /app
git clone https://github.com/jxxghp/MoviePilot app
pip install -r /app/requirements.txt
echo "后端程序更新成功"
# 检查前端最新版本
frontend_version=$(curl ${CURL_OPTIONS} "https://api.github.com/repos/jxxghp/MoviePilot-Frontend/releases/latest" | jq -r .tag_name)
# 更新前端代码
echo "正在下载前端程序 ${frontend_version}..."
curl -sL "https://github.com/jxxghp/MoviePilot-Frontend/releases/download/${frontend_version}/dist.zip" | busybox unzip -d /tmp -
if [ $? -eq 0 ]; then
    rm -rf /public
    mv /tmp/dist /public
    echo "程序更新成功，前端版本：${frontend_version}"
else
    echo "前端程序下载失败"
fi
echo "请重启容器生效"
