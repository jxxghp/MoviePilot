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

# PostgreSQL 端口；使用 Unix Socket 时可留空
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

### 3. Unix Socket 连接

如果 PostgreSQL 通过 Unix Socket 暴露，可以把 `DB_POSTGRESQL_HOST` 设置为套接字目录。

```bash
DB_TYPE=postgresql
DB_POSTGRESQL_HOST=/var/run/postgresql
DB_POSTGRESQL_PORT=
DB_POSTGRESQL_DATABASE=moviepilot
DB_POSTGRESQL_USERNAME=moviepilot
DB_POSTGRESQL_PASSWORD=moviepilot
```

如需显式指定 socket 端口，也可以保留 `DB_POSTGRESQL_PORT`，程序会生成带 `host=/path/to/socket` 查询参数的 PostgreSQL URL。

## Docker 部署

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

1. 关闭 SQLite 的 WAL 模式（如果此前已经开启），并关闭 MoviePilot
2. 备份现有的 SQLite 数据库文件（`config/user.db`）
3. 按照上述要求修改配置为 PostgreSQL
4. 注意，由于 SQLite 与 PostgreSQL 对部分字段的类型（例如`json`类型）定义不同，请勿通过`user.db`在迁移阶段直接在空数据库的基础上创建表结构，而应按照下一条要求通过 MoviePilot 的初始化自动创建正确的表结构，只有在这种情况下迁移工具才能正确处理数据类型
5. 启动应用，让 MoviePilot 自动创建表结构，确认创建完成后关闭 MoviePilot
6. 使用如下 SQL 语句清理所有初始化完成的表数据，只保留表结构，避免默认的初始化数据干扰迁移
```sql
TRUNCATE TABLE agentchat, agenttask, alembic_version, downloadfailure, downloadfiles, downloadhistory, mediaserveritem, message, passkey, plugindata, site, siteicon, sitestatistic, siteuserdata, subscribe, subscribehistory, systemconfig, transferhistory, "user", userconfig, workflow RESTART IDENTITY CASCADE;
```
6. 安装 Java 21 或更新的版本，并下载 dimitri/pgloader 中的 v4 版本 jar 包，即`pgloader.jar`
7. 创建`migrate.load`文件，并编辑如下内容，注意：Windows 下本地`.db`文件路径引用需要有 3 个斜线；`userrequest`表已经废弃，是 v1 阶段的残留物，下列配置文件将会自动去除
```
LOAD DATABASE
     FROM sqlite:///X:/path/to/user.db
     INTO postgresql://moviepilot:password@host:5432/moviepilot

WITH
     data only,
     reset sequences

EXCLUDING TABLE NAMES LIKE 'userrequest'

SET work_mem TO '16MB',
    maintenance_work_mem TO '512MB'

CAST
     type integer to boolean when (= precision 1)
;
```
8. 使用`java --enable-native-access=ALL-UNNAMED -jar .\pgloader.jar .\migrate.load`启动迁移。迁移过程中可能产生大量警告和报错，主要与孤儿索引`idx__PRIMARY`相关，形如`WARN  [main] pgloader.core - Primary Keys failed (skipping): ERROR: index "idx__PRIMARY" does not belong to table "plugindata"  浣嶇疆锛?9`
9. 清理孤儿索引：`DROP INDEX IF EXISTS "idx__PRIMARY";`
10. 修正 Alembic 版本号：`UPDATE alembic_version SET version_num = 'd58298a0879f';`
11. 由于 SQLite 与 PostgreSQL 的自增序列原理不同，直接导入会出现冲突，需手动更新自增序列，执行：
```sql
-- ============================================================
-- MoviePilot 迁移后自增序列统一修复脚本
-- 功能：自动遍历 public schema 下所有表，重置自增序列到最大 ID + 1
-- 执行方式：在 psql 或任意 PostgreSQL 客户端中直接执行
-- ============================================================

DO $$
DECLARE
    rec RECORD;
    seq_name TEXT;
    col_name TEXT;
    max_id BIGINT;
    tbl_name TEXT;
BEGIN
    RAISE NOTICE '开始修复自增序列...';

    -- 遍历 public schema 下所有带自增序列的表和列
    FOR rec IN
        SELECT 
            c.relname AS table_name,
            a.attname AS column_name,
            pg_get_serial_sequence(c.relname::text, a.attname::text) AS sequence_name
        FROM pg_class c
        JOIN pg_namespace n ON c.relnamespace = n.oid
        JOIN pg_attribute a ON a.attrelid = c.oid
        JOIN pg_attrdef ad ON ad.adrelid = c.oid AND ad.adnum = a.attnum
        WHERE n.nspname = 'public'
          AND c.relkind = 'r'  -- 普通表
          AND a.attnum > 0     -- 排除系统列
          AND NOT a.attisdropped
          AND pg_get_serial_sequence(c.relname::text, a.attname::text) IS NOT NULL
        ORDER BY c.relname
    LOOP
        tbl_name := rec.table_name;
        col_name := rec.column_name;
        seq_name := rec.sequence_name;

        -- 获取该表该列的最大值（空表则为 0）
        EXECUTE format('SELECT COALESCE(MAX(%I), 0) FROM %I', col_name, tbl_name) INTO max_id;

        -- 重置序列到 max_id + 1
        IF seq_name IS NOT NULL THEN
            EXECUTE format('ALTER SEQUENCE %s RESTART WITH %s', seq_name, max_id + 1);
            RAISE NOTICE '表 % 列 % -> 序列 % 已重置到 %', tbl_name, col_name, seq_name, max_id + 1;
        END IF;
    END LOOP;

    RAISE NOTICE '自增序列修复完成！';
END $$;

-- ============================================================
-- 验证：查看所有序列的当前值
-- ============================================================
SELECT 
    sequencename,
    last_value
FROM pg_sequences
WHERE schemaname = 'public'
ORDER BY sequencename;
```
12. 启动 MoviePilot，如果迁移成功，你应当在日志中看到类似下面的信息：
```
INFO:    [moviepilot] 5b3355c964bb_2_2_0.py - 发现 21 个表需要检查序列
INFO:    [moviepilot] a946dae52526_2_2_1.py - 开始PostgreSQL数据库userid字段迁移...
INFO:    [moviepilot] a946dae52526_2_2_1.py - PostgreSQL数据库userid字段迁移完成
INFO:    [moviepilot] 41ef1dd7467c_2_2_2.py - SystemConfig 表去重操作已完成。
INFO:     Started server process [129]
```

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
