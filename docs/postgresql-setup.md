# PostgreSQL 数据库配置指南

MoviePilot 现在支持 PostgreSQL 数据库，您可以根据需要选择使用 SQLite 或 PostgreSQL。

## 配置选项

### 1. 数据库类型选择

在 `config/app.env` 文件中设置：

```bash
# 使用 SQLite（默认）
DB_TYPE=sqlite

# 使用 PostgreSQL
DB_TYPE=postgresql
```

### 2. PostgreSQL 配置参数

当 `DB_TYPE=postgresql` 时，以下配置生效：

```bash
# PostgreSQL 主机地址
DB_POSTGRESQL_HOST=localhost

# PostgreSQL 端口
DB_POSTGRESQL_PORT=5432

# PostgreSQL 数据库名
DB_POSTGRESQL_DATABASE=moviepilot

# PostgreSQL 用户名
DB_POSTGRESQL_USERNAME=moviepilot

# PostgreSQL 密码
DB_POSTGRESQL_PASSWORD=moviepilot

# PostgreSQL 连接池大小
DB_POSTGRESQL_POOL_SIZE=20

# PostgreSQL 连接池溢出数量
DB_POSTGRESQL_MAX_OVERFLOW=30
```

## Docker 部署

### 使用内置 PostgreSQL

如果您使用 Docker 部署，MoviePilot 容器内置了 PostgreSQL 服务：

#### 使用 Docker Compose（推荐）

1. 创建 `docker-compose.yml` 文件：
```yaml
version: '3.8'

services:
  moviepilot:
    image: jxxghp/moviepilot:latest
    container_name: moviepilot
    restart: unless-stopped
    ports:
      - "3000:3000"  # 前端端口
      - "3001:3001"  # API端口
    environment:
      - DB_TYPE=postgresql
      - DB_POSTGRESQL_HOST=localhost
      - DB_POSTGRESQL_PORT=5432
      - DB_POSTGRESQL_DATABASE=moviepilot
      - DB_POSTGRESQL_USERNAME=moviepilot
      - DB_POSTGRESQL_PASSWORD=moviepilot
    volumes:
      - ./config:/config
```

2. 启动服务：
```bash
docker-compose up -d
```

#### 使用 Docker 命令

1. 设置环境变量：
```bash
DB_TYPE=postgresql
```

2. 启动容器时，PostgreSQL 服务会自动：
   - 在配置目录下创建 `postgresql/` 子目录作为数据目录
   - 初始化 PostgreSQL 数据目录
   - 启动 PostgreSQL 服务
   - 创建数据库和用户
   - 配置连接权限

3. 数据持久化：
   - PostgreSQL 数据存储在 `${CONFIG_DIR}/postgresql/` 目录中
   - 日志文件存储在 `${CONFIG_DIR}/postgresql/logs/` 目录中
   - 这些目录会通过 Docker 卷映射持久化保存

### 使用外部 PostgreSQL

如果您想使用外部的 PostgreSQL 服务：

1. 确保外部 PostgreSQL 服务已启动并可访问
2. 设置环境变量指向外部服务：
```bash
DB_TYPE=postgresql
DB_POSTGRESQL_HOST=your-postgresql-host
DB_POSTGRESQL_PORT=5432
DB_POSTGRESQL_DATABASE=moviepilot
DB_POSTGRESQL_USERNAME=your-username
DB_POSTGRESQL_PASSWORD=your-password
```

## 数据迁移

### 从 SQLite 迁移到 PostgreSQL

1. 备份现有的 SQLite 数据库文件（`config/user.db`）
2. 修改配置为 PostgreSQL
3. 启动应用，数据库表会自动创建
4. 使用数据库迁移工具或手动导入数据

### 从 PostgreSQL 迁移到 SQLite

1. 导出 PostgreSQL 数据
2. 修改配置为 SQLite
3. 启动应用，数据库表会自动创建
4. 导入数据到 SQLite

## 数据备份

### PostgreSQL 数据备份

PostgreSQL 数据存储在 `${CONFIG_DIR}/postgresql/` 目录中，您可以通过以下方式进行备份：

#### 1. 文件级备份
```bash
# 备份整个PostgreSQL数据目录
tar -czf postgresql_backup_$(date +%Y%m%d_%H%M%S).tar.gz config/postgresql/
```

#### 2. 数据库级备份
```bash
# 进入容器
docker exec -it moviepilot bash

# 使用pg_dump备份
pg_dump -h localhost -U moviepilot -d moviepilot > /config/moviepilot_backup.sql

# 或使用pg_dumpall备份所有数据库
pg_dumpall -h localhost -U moviepilot > /config/all_databases_backup.sql
```

#### 3. 恢复数据
```bash
# 恢复单个数据库
psql -h localhost -U moviepilot -d moviepilot < /config/moviepilot_backup.sql

# 恢复所有数据库
psql -h localhost -U moviepilot < /config/all_databases_backup.sql
```

## 性能优化

### PostgreSQL 优化建议

1. **连接池配置**：
   - 根据应用负载调整 `DB_POSTGRESQL_POOL_SIZE`
   - 设置合适的 `DB_POSTGRESQL_MAX_OVERFLOW`

2. **数据库配置**：
   - 调整 `shared_buffers`
   - 配置 `work_mem`
   - 设置合适的 `maintenance_work_mem`

3. **索引优化**：
   - 为常用查询字段添加索引
   - 定期执行 `VACUUM` 和 `ANALYZE`

## 故障排除

### 常见问题

1. **连接失败**：
   - 检查 PostgreSQL 服务是否启动
   - 验证连接参数是否正确
   - 确认网络连接和防火墙设置

2. **权限问题**：
   - 确保用户有足够的数据库权限
   - 检查 `pg_hba.conf` 配置

3. **性能问题**：
   - 监控连接池使用情况
   - 检查慢查询日志
   - 优化数据库配置

### 日志查看

PostgreSQL 相关日志可以在以下位置查看：

- Docker 容器：`${CONFIG_DIR}/postgresql/logs/`
- 系统日志：`journalctl -u postgresql`

## 注意事项

1. **兼容性**：PostgreSQL 支持从 MoviePilot v2.0 开始
2. **备份**：建议定期备份数据库
3. **版本**：建议使用 PostgreSQL 12 或更高版本
4. **字符集**：确保使用 UTF-8 字符集

## 技术支持

如果遇到问题，请：

1. 查看应用日志
2. 检查 PostgreSQL 日志
3. 在 GitHub Issues 中报告问题
