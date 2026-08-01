-- 99_smoke_test.sql — 部署后健康检查(不写数据)
--
-- 用法(以 memorial 用户):
--   mysql -umemorial -p < sql/99_smoke_test.sql
--   mysql -umemorial -p -t < sql/99_smoke_test.sql   # -t 出表格
--
-- 应输出 1 行(空表)或 N 行(已有业务数据)。任何错误/警告都说明建表有问题。

USE hindsight_memorial;

SELECT status, COUNT(*) AS cnt
FROM memory_units
GROUP BY status
ORDER BY status;

-- 检查关键索引是否就位
SHOW INDEX FROM memory_units;
