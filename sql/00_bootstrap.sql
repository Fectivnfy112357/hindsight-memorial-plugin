-- 00_bootstrap.sql — hindsight-memorial 数据库初始化的 root 段
--
-- 这一段必须用 root(或具备 CREATE USER / GRANT 权限的账号)跑一次。
-- 之后所有 DML/DDL 都由 memorial 用户执行,不再需要 root。
--
-- 使用方式:
--   mysql -uroot -p < sql/00_bootstrap.sql
--
-- 然后用 memorial 用户跑 01_create_db.sql 和 02_schema.sql。
--
-- ⚠️ 部署到新机器时改两处:
--   1. 密码(Mem0rial_HS_2026_aK7x 仅为示例,生成方式见下)
--   2. host 通配(默认 172.23.% 对应 docker compose 的 hindsight_default 子网)
--
-- 生成密码: openssl rand -hex 16  # 32 个十六进制字符,够强度且不需要引号转义

-- 1. 建库。字符集与表一致,避免列级默认值不同导致索引/排序行为差异。
CREATE DATABASE IF NOT EXISTS hindsight_memorial
    DEFAULT CHARACTER SET utf8mb4
    COLLATE utf8mb4_unicode_ci;

-- 2. 建用户。
--    - '172.23.%': 容器通过 host.docker.internal 进来,源 IP 在 docker bridge 上,
--      大概率落在 172.23.0.0/16(hindsight_default 子网)。
--    - 'localhost': 同机直接连(运维/CI/调试用)。
--    如果你的 docker 子网不是 172.23.0.0/16,改下面两行。
CREATE USER IF NOT EXISTS 'memorial'@'172.23.%'
    IDENTIFIED BY 'Mem0rial_HS_2026_aK7x';
CREATE USER IF NOT EXISTS 'memorial'@'localhost'
    IDENTIFIED BY 'Mem0rial_HS_2026_aK7x';

-- 3. 授权。memorial 需要在这个库里:
--    - SELECT/INSERT/UPDATE/DELETE:日常读写
--    - CREATE/DROP/ALTER/INDEX/CREATE VIEW:启动时 init_db_on_conn 走
--      CREATE TABLE IF NOT EXISTS / CREATE INDEX,容器有权自检 schema 是否就位
--    - 不给 GRANT OPTION:不需要再授权出去
--    - 不给全局权限:限定到这一个库
GRANT SELECT, INSERT, UPDATE, DELETE,
      CREATE, DROP, ALTER, INDEX, CREATE VIEW
    ON hindsight_memorial.*
    TO 'memorial'@'172.23.%';
GRANT SELECT, INSERT, UPDATE, DELETE,
      CREATE, DROP, ALTER, INDEX, CREATE VIEW
    ON hindsight_memorial.*
    TO 'memorial'@'localhost';

FLUSH PRIVILEGES;
