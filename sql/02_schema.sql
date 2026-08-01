-- 1. 选库后应用 schema
-- 部署目标:MySQL 5.7+,建表时索引里的 DESC 关键字被 5.7 接受并忽略,反向扫描照常走索引
-- 字符集:utf8mb4,排序规则:utf8mb4_unicode_ci,行格式:DYNAMIC(允许大 TEXT 走 off-page)
-- 该文件可独立运行(包含 USE),也可与 01_create_db.sql 配合使用

USE hindsight_memorial;


CREATE TABLE IF NOT EXISTS memory_units (
    id                  BIGINT UNSIGNED NOT NULL AUTO_INCREMENT
                        COMMENT '自增主键,仅本地使用,与 Hindsight 无关',
    bank_id             VARCHAR(255)    NOT NULL
                        COMMENT 'Hindsight bank id,事件里带来的记忆库标识',
    unit_id             VARCHAR(64)     NOT NULL
                        COMMENT 'Hindsight memory_unit 的 UUID,与 bank_id 组成幂等键',
    content             TEXT            NOT NULL
                        COMMENT '该 memory_unit 的事实正文,reflect 的输入',
    created_at          DATETIME        NOT NULL
                        COMMENT '该事实在 Hindsight 侧的产生时间(UTC),poller 按此倒序取件',
    document_id         VARCHAR(255)    DEFAULT NULL
                        COMMENT '来源文档 id;webhook 的 data 为空对象时由 fallback 恢复,可能为 NULL',
    status              ENUM('pending','processing','processed','superseded','failed') NOT NULL DEFAULT 'pending'
                        COMMENT '状态机:pending 待处理 / processing 处理中 / processed 已完成 / superseded 被更新事实取代 / failed reflect 失败',
    superseded_reason   TEXT            DEFAULT NULL
                        COMMENT '被取代的原因,取自 reflect LLM 的 reasoning(截断 500 字符)',
    failure_reason      TEXT            DEFAULT NULL
                        COMMENT '失败摘要(截断 500 字符),完整堆栈只进日志',
    ingested_at         DATETIME        NOT NULL
                        COMMENT '本地入库时间(UTC),内容未变的重复投递不刷新此列',
    processed_at        DATETIME        DEFAULT NULL
                        COMMENT '进入终态(processed/superseded/failed)的时间(UTC)',
    PRIMARY KEY (id),
    UNIQUE KEY uq_bank_unit (bank_id, unit_id)
        COMMENT '单元级幂等键:Hindsight 重投同一事件时折叠为一行',
    KEY idx_status_created (status, created_at DESC)
        COMMENT 'poller 取件:WHERE status=pending ORDER BY created_at DESC LIMIT 1',
    KEY idx_status_ingested (status, ingested_at DESC)
        COMMENT '按入库时间排查积压用'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  ROW_FORMAT=DYNAMIC
  COMMENT='hindsight-memorial 本地对账表:webhook 落库 + poller 消费的持久化状态机'
