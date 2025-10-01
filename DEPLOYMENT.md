# 📦 部署指南

本文档介绍如何在不同环境中部署电影智能助手系统。

## 📋 前置要求

- Python 3.8+
- pip 或 conda
- （可选）Docker
- （可选）LLM API Key（OpenAI、Azure OpenAI 等）

## 🚀 快速部署

### 方法 1: 本地部署

#### 1. 克隆或下载项目

```bash
# 假设项目在当前目录
cd movie-agent
```

#### 2. 安装依赖

```bash
pip install -r requirements.txt
```

#### 3. 配置环境变量（可选）

```bash
# 复制环境变量模板
cp .env.example .env

# 编辑 .env 文件
# 如果不使用 LLM，保持 LLM_PROVIDER=none 即可
nano .env
```

#### 4. 运行测试

```bash
python3 test_agent.py
```

#### 5. 启动服务器

```bash
python3 api_server.py
```

服务器将在 http://localhost:5000 启动

#### 6. 访问演示界面

打开浏览器访问: http://localhost:5000/demo

### 方法 2: Docker 部署

#### 1. 创建 Dockerfile

```dockerfile
FROM python:3.9-slim

# 设置工作目录
WORKDIR /app

# 安装依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制项目文件
COPY . .

# 暴露端口
EXPOSE 5000

# 设置环境变量
ENV PYTHONUNBUFFERED=1

# 启动命令
CMD ["python3", "api_server.py"]
```

#### 2. 构建镜像

```bash
docker build -t movie-agent:latest .
```

#### 3. 运行容器

```bash
docker run -d \
  --name movie-agent \
  -p 5000:5000 \
  -e LLM_PROVIDER=none \
  movie-agent:latest
```

#### 4. 查看日志

```bash
docker logs -f movie-agent
```

### 方法 3: Docker Compose 部署

#### 1. 创建 docker-compose.yml

```yaml
version: '3.8'

services:
  movie-agent:
    build: .
    ports:
      - "5000:5000"
    environment:
      - API_HOST=0.0.0.0
      - API_PORT=5000
      - DEBUG=False
      - LLM_PROVIDER=none
    volumes:
      - ./downloads:/app/downloads
      - ./logs:/app/logs
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:5000/health"]
      interval: 30s
      timeout: 10s
      retries: 3

  # 可选：添加 Nginx 反向代理
  nginx:
    image: nginx:alpine
    ports:
      - "80:80"
    volumes:
      - ./nginx.conf:/etc/nginx/nginx.conf:ro
    depends_on:
      - movie-agent
    restart: unless-stopped
```

#### 2. 启动服务

```bash
docker-compose up -d
```

#### 3. 查看状态

```bash
docker-compose ps
docker-compose logs -f
```

#### 4. 停止服务

```bash
docker-compose down
```

## 🔧 生产环境部署

### 使用 Gunicorn + Nginx

#### 1. 安装 Gunicorn

```bash
pip install gunicorn gevent
```

#### 2. 创建 gunicorn 配置文件

`gunicorn_config.py`:
```python
import multiprocessing

bind = "0.0.0.0:5000"
workers = multiprocessing.cpu_count() * 2 + 1
worker_class = "gevent"
worker_connections = 1000
timeout = 120
keepalive = 5

# 日志
accesslog = "./logs/access.log"
errorlog = "./logs/error.log"
loglevel = "info"

# 进程命名
proc_name = "movie-agent"

# 优雅重启
max_requests = 1000
max_requests_jitter = 100
```

#### 3. 启动 Gunicorn

```bash
gunicorn -c gunicorn_config.py api_server:app
```

#### 4. 配置 Nginx

`/etc/nginx/sites-available/movie-agent`:
```nginx
upstream movie_agent {
    server 127.0.0.1:5000;
    # 可以添加多个后端
    # server 127.0.0.1:5001;
    # server 127.0.0.1:5002;
}

server {
    listen 80;
    server_name your-domain.com;

    # 静态文件缓存
    location /static/ {
        alias /path/to/static/;
        expires 30d;
    }

    # WebSocket 支持
    location /socket.io/ {
        proxy_pass http://movie_agent;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_read_timeout 86400;
    }

    # API 请求
    location / {
        proxy_pass http://movie_agent;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # 健康检查
    location /health {
        access_log off;
        proxy_pass http://movie_agent;
    }
}
```

#### 5. 启用站点

```bash
sudo ln -s /etc/nginx/sites-available/movie-agent /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx
```

### 使用 Supervisor 管理进程

#### 1. 安装 Supervisor

```bash
sudo apt-get install supervisor
```

#### 2. 创建配置文件

`/etc/supervisor/conf.d/movie-agent.conf`:
```ini
[program:movie-agent]
command=/path/to/venv/bin/gunicorn -c gunicorn_config.py api_server:app
directory=/path/to/movie-agent
user=www-data
autostart=true
autorestart=true
redirect_stderr=true
stdout_logfile=/var/log/movie-agent/stdout.log
stderr_logfile=/var/log/movie-agent/stderr.log
environment=PYTHONPATH="/path/to/movie-agent"
```

#### 3. 启动服务

```bash
sudo supervisorctl reread
sudo supervisorctl update
sudo supervisorctl start movie-agent
```

#### 4. 管理服务

```bash
# 查看状态
sudo supervisorctl status movie-agent

# 重启服务
sudo supervisorctl restart movie-agent

# 停止服务
sudo supervisorctl stop movie-agent

# 查看日志
sudo supervisorctl tail -f movie-agent
```

### 使用 Systemd 服务

#### 1. 创建服务文件

`/etc/systemd/system/movie-agent.service`:
```ini
[Unit]
Description=Movie Agent Service
After=network.target

[Service]
Type=simple
User=www-data
Group=www-data
WorkingDirectory=/path/to/movie-agent
Environment="PATH=/path/to/venv/bin"
ExecStart=/path/to/venv/bin/python3 api_server.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

#### 2. 启用并启动服务

```bash
sudo systemctl daemon-reload
sudo systemctl enable movie-agent
sudo systemctl start movie-agent
```

#### 3. 管理服务

```bash
# 查看状态
sudo systemctl status movie-agent

# 重启
sudo systemctl restart movie-agent

# 查看日志
sudo journalctl -u movie-agent -f
```

## ☁️ 云平台部署

### AWS EC2

#### 1. 启动 EC2 实例

- AMI: Ubuntu 20.04 LTS
- 实例类型: t2.micro 或更高
- 安全组: 开放 80, 443, 5000 端口

#### 2. 连接并部署

```bash
# SSH 连接
ssh -i your-key.pem ubuntu@your-ec2-ip

# 安装依赖
sudo apt-get update
sudo apt-get install python3-pip nginx -y

# 部署项目
git clone your-repo
cd movie-agent
pip3 install -r requirements.txt

# 启动服务
python3 api_server.py
```

### Heroku

#### 1. 创建 Procfile

```
web: gunicorn -k geventwebsocket.gunicorn.workers.GeventWebSocketWorker -w 1 api_server:app
```

#### 2. 创建 runtime.txt

```
python-3.9.16
```

#### 3. 部署

```bash
heroku create movie-agent
heroku config:set LLM_PROVIDER=none
git push heroku main
heroku open
```

### Google Cloud Run

#### 1. 创建 Dockerfile（已有）

#### 2. 部署

```bash
# 构建并推送镜像
gcloud builds submit --tag gcr.io/PROJECT_ID/movie-agent

# 部署到 Cloud Run
gcloud run deploy movie-agent \
  --image gcr.io/PROJECT_ID/movie-agent \
  --platform managed \
  --region us-central1 \
  --allow-unauthenticated
```

### Azure Container Instances

```bash
# 创建资源组
az group create --name movie-agent-rg --location eastus

# 部署容器
az container create \
  --resource-group movie-agent-rg \
  --name movie-agent \
  --image your-registry/movie-agent:latest \
  --dns-name-label movie-agent \
  --ports 5000
```

## 🔐 安全配置

### 1. HTTPS 配置（Let's Encrypt）

```bash
# 安装 Certbot
sudo apt-get install certbot python3-certbot-nginx

# 获取证书
sudo certbot --nginx -d your-domain.com

# 自动续期
sudo certbot renew --dry-run
```

### 2. 防火墙配置

```bash
# UFW
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw enable

# iptables
sudo iptables -A INPUT -p tcp --dport 80 -j ACCEPT
sudo iptables -A INPUT -p tcp --dport 443 -j ACCEPT
```

### 3. 环境变量安全

```bash
# 使用环境变量而不是硬编码
export LLM_API_KEY="your-secret-key"
export SECRET_KEY="your-secret-key"

# 或使用 .env 文件（不要提交到 Git）
echo ".env" >> .gitignore
```

## 📊 监控和日志

### 1. 应用日志

```python
# 在 api_server.py 中配置
import logging

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('logs/app.log'),
        logging.StreamHandler()
    ]
)
```

### 2. Nginx 访问日志

```nginx
access_log /var/log/nginx/movie-agent-access.log;
error_log /var/log/nginx/movie-agent-error.log;
```

### 3. 使用监控工具

#### Prometheus + Grafana

```python
# 添加 Prometheus 指标
from prometheus_flask_exporter import PrometheusMetrics

app = Flask(__name__)
metrics = PrometheusMetrics(app)
```

#### ELK Stack

```bash
# 安装 Filebeat
curl -L -O https://artifacts.elastic.co/downloads/beats/filebeat/filebeat-7.x.x-amd64.deb
sudo dpkg -i filebeat-7.x.x-amd64.deb

# 配置日志收集
sudo filebeat setup
sudo service filebeat start
```

## 🔄 CI/CD 配置

### GitHub Actions

`.github/workflows/deploy.yml`:
```yaml
name: Deploy

on:
  push:
    branches: [ main ]

jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v2
      
      - name: Set up Python
        uses: actions/setup-python@v2
        with:
          python-version: '3.9'
      
      - name: Install dependencies
        run: |
          pip install -r requirements.txt
      
      - name: Run tests
        run: |
          python3 test_agent.py test
      
      - name: Build Docker image
        run: |
          docker build -t movie-agent:${{ github.sha }} .
      
      - name: Deploy
        run: |
          # 部署脚本
          echo "Deploying..."
```

### GitLab CI

`.gitlab-ci.yml`:
```yaml
stages:
  - test
  - build
  - deploy

test:
  stage: test
  script:
    - pip install -r requirements.txt
    - python3 test_agent.py test

build:
  stage: build
  script:
    - docker build -t movie-agent .

deploy:
  stage: deploy
  script:
    - docker push movie-agent
    - ssh user@server "docker pull movie-agent && docker-compose up -d"
```

## 🧪 性能测试

### 使用 Locust

```python
# locustfile.py
from locust import HttpUser, task, between

class MovieAgentUser(HttpUser):
    wait_time = between(1, 3)
    
    @task
    def send_message(self):
        self.client.post("/api/message", json={
            "user_id": "test",
            "message": "搜索电影"
        })
```

运行测试：
```bash
locust -f locustfile.py --host=http://localhost:5000
```

## 📝 维护和备份

### 数据库备份

```bash
# PostgreSQL
pg_dump movie_agent > backup_$(date +%Y%m%d).sql

# MongoDB
mongodump --db movie_agent --out backup/
```

### 日志轮转

`/etc/logrotate.d/movie-agent`:
```
/var/log/movie-agent/*.log {
    daily
    rotate 7
    compress
    delaycompress
    notifempty
    create 0640 www-data www-data
    sharedscripts
    postrotate
        supervisorctl restart movie-agent > /dev/null
    endscript
}
```

## 🆘 故障排查

### 常见问题

#### 1. 端口被占用

```bash
# 查找占用端口的进程
sudo lsof -i :5000

# 杀死进程
sudo kill -9 <PID>
```

#### 2. 权限问题

```bash
# 修改文件权限
chmod +x api_server.py
chown -R www-data:www-data /path/to/movie-agent
```

#### 3. 依赖问题

```bash
# 重新安装依赖
pip install --upgrade -r requirements.txt
```

#### 4. 查看错误日志

```bash
# Systemd 日志
sudo journalctl -u movie-agent -n 100

# Supervisor 日志
sudo supervisorctl tail movie-agent stderr

# Nginx 日志
sudo tail -f /var/log/nginx/error.log
```

## 📞 支持

如有问题，请：
1. 查看日志文件
2. 检查配置文件
3. 参考 ARCHITECTURE.md
4. 提交 Issue

---

祝部署顺利！🚀
