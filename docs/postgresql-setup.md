# PostgreSQL 数据库配置指南

MoviePilot 现在支持 PostgreSQL 数据库，您可以根据需要选择使用 SQLite 或 PostgreSQL。

## 配置选项

### 1. 数据库类型选择

在配置目录下的 `app.env` 文件中设置（Docker 为 `/config/app.env`；本地安装用 `moviepilot config path` 查看实际路径）：

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
DB_POSTGRESQL_POOL_SIZE=10

# PostgreSQL 连接池溢出数量
DB_POSTGRESQL_MAX_OVERFLOW=50
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

### 从 SQLite 迁移到 PostgreSQL（以在 Windows 下操作为例）

1. 关闭 SQLite 的 WAL 模式（如果此前已经开启），并关闭 MoviePilot
2. 备份现有的 SQLite 数据库文件（`<配置目录>/user.db`，Docker 为 `/config/user.db`）
3. 按照上述要求修改配置为 PostgreSQL
4. 注意，由于 SQLite 与 PostgreSQL 对部分字段的类型（例如`json`类型）定义不同，请勿通过`user.db`在迁移阶段直接在空数据库的基础上创建表结构，而应按照下一条要求通过 MoviePilot 的初始化自动创建正确的表结构，只有在这种情况下迁移工具才能正确处理数据类型
5. 启动应用，让 MoviePilot 自动创建表结构，确认创建完成后关闭 MoviePilot
6. 使用如下 SQL 语句清理所有初始化完成的表数据，只保留表结构，避免默认的初始化数据干扰迁移
```sql
TRUNCATE TABLE agentchat, agenttask, agenttaskrun, alembic_version, downloadfailure, downloadfiles, downloadhistory, mediaserveritem, message, outboxmessage, passkey, pluginconfig, plugindata, serviceconfig, site, siteicon, sitestatistic, siteuserdata, subscribe, subscribehistory, systemconfig, transferhistory, transferpending, "user", userconfig, useridentity, workflow RESTART IDENTITY CASCADE;
```
6. 安装 Java 21 或更新的版本，并下载 dimitri/pgloader 中的 v4 版本 jar 包，即`pgloader.jar`
7. 创建`migrate.load`文件，并编辑如下内容，注意：Windows 下本地`.db`文件路径引用需要有 3 个斜线；`userrequest`表已经废弃，是 v1 阶段的残留物，下列配置文件将会自动去除
```
LOAD DATABASE
     FROM sqlite:///X:/path/to/user.db
     INTO postgresql://moviepilot:your-password@host:5432/moviepilot

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
-- MoviePilot PostgreSQL 自增序列修复脚本
-- 用途：修复从 SQLite 迁移到 PostgreSQL 后的自增 ID 冲突
-- 问题背景：
--   SQLite 的 AUTOINCREMENT 不依赖独立序列对象，数据行带着具体 id 值进入 PostgreSQL。
--   但 PostgreSQL 使用独立的 Sequence（如 plugindata_id_seq）生成新 ID。
--   迁移后 Sequence 仍停留在初始值（如 1），而表中已有 id=10186 的数据，
--   导致新插入时报 "duplicate key value violates unique constraint"。
-- 执行时机：
--   1. 迁移完成后、应用启动前（必做）
--   2. 运行中报主键冲突时（应急）
--   3. 从备份恢复数据后（建议做）
-- 安全说明：
--   本脚本只读表内最大 id 并调整序列，不修改任何数据行，可重复执行。

DO $$
DECLARE
    rec RECORD;
    seq_name TEXT;
    max_id BIGINT;
    tbl_full TEXT;
    fix_count INT := 0;
    err_count INT := 0;
BEGIN
    RAISE NOTICE '开始扫描 public schema 下的自增序列...';

    -- 遍历 public schema 下所有带自增序列的列
    -- 通过 pg_get_serial_sequence() 直接获取序列名，不依赖 pg_depend，兼容性好
    FOR rec IN
        SELECT
            n.nspname AS schema_name,
            c.relname AS table_name,
            a.attname AS column_name
        FROM pg_class c
        JOIN pg_namespace n ON c.relnamespace = n.oid
        JOIN pg_attribute a ON a.attrelid = c.oid
        WHERE n.nspname = 'public'
          AND c.relkind = 'r'                    -- 只处理普通表，排除视图/系统表
          AND a.attnum > 0                       -- attnum <= 0 是系统隐藏列
          AND NOT a.attisdropped                 -- 排除已删除但未清理的列
          AND c.relname NOT LIKE 'pg_%'          -- 排除 PostgreSQL 系统表
          AND c.relname NOT LIKE 'sql_%'
          AND pg_get_serial_sequence(
                quote_ident(n.nspname) || '.' || quote_ident(c.relname),
                a.attname
              ) IS NOT NULL                      -- 只保留确实有自增序列关联的列
        ORDER BY c.relname, a.attname
    LOOP
        BEGIN
            -- 构造带 schema 前缀的表名，quote_ident 自动处理 user 等关键字
            tbl_full := quote_ident(rec.schema_name) || '.' || quote_ident(rec.table_name);

            -- 获取该列当前最大值。COALESCE 处理空表（无数据时返回 0）
            EXECUTE format('SELECT COALESCE(MAX(%I), 0) FROM %s', rec.column_name, tbl_full)
                INTO max_id;

            -- 获取序列完整名称（含 schema）
            seq_name := pg_get_serial_sequence(tbl_full, rec.column_name);

            -- 重置序列：
            --   参数1: 序列名
            --   参数2: 重置到的值（max_id + 1，空表时为 1）
            --   参数3: is_called = false，表示 nextval() 下次直接返回该值，不再递增
            -- 效果：下一条 INSERT 拿到的 id 正好是表中未使用的最小值
            PERFORM setval(seq_name, max_id + 1, false);

            RAISE NOTICE '已修复: %.% (列 %) -> 序列 % 设为 % (表内最大 id = %)',
                rec.schema_name, rec.table_name, rec.column_name,
                seq_name, max_id + 1, max_id;
            fix_count := fix_count + 1;

        EXCEPTION WHEN OTHERS THEN
            -- 单表异常不阻断整体流程，记录后继续下一张表
            RAISE NOTICE '跳过 %.%: %', rec.schema_name, rec.table_name, SQLERRM;
            err_count := err_count + 1;
        END;
    END LOOP;

    RAISE NOTICE '完成。修复 % 个序列，跳过/错误 % 个。', fix_count, err_count;
END $$;

-- 验证：查看所有序列当前值，确认 last_value 大于对应表的最大 id
SELECT
    sequencename,
    last_value
FROM pg_sequences
WHERE schemaname = 'public'
ORDER BY sequencename;
```
12. 启动 MoviePilot，如果迁移成功，你应当在日志中看到类似下面的信息：
```
INFO:    [moviepilot] 5b3355c964bb_2_2_0.py - 发现 N 个表需要检查序列
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

MoviePilot 镜像不自带 PostgreSQL 服务端，只装了 `postgresql-client`。数据在外部
PostgreSQL 主机上，文件级备份必须在那台主机上做。可用的两条路径：

#### 1. MoviePilot 内置备份
```bash
# 走 pg_dump 自定义格式，产物落在 DB_BACKUP_PATH 或 <配置目录>/database_backup/
moviepilot database backup
```

#### 2. 在容器里用客户端连外部库
```bash
# 进入容器
docker exec -it moviepilot bash

# 使用 pg_dump 备份（连的是外部主机，不是 localhost）
pg_dump -h "$DB_POSTGRESQL_HOST" -p "$DB_POSTGRESQL_PORT" -U moviepilot -d moviepilot > /config/moviepilot_backup.sql

# 或使用 pg_dumpall 备份所有数据库
pg_dumpall -h "$DB_POSTGRESQL_HOST" -p "$DB_POSTGRESQL_PORT" -U moviepilot > /config/all_databases_backup.sql
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

- 外部 PostgreSQL 主机上的服务端日志（MoviePilot 容器内没有服务端，也没有它的日志）
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
