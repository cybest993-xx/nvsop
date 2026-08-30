# 异步任务以 PostgreSQL 为权威，Redis 只作传输

`job` 模块拥有 `job_application_job` 表，它是异步任务状态的唯一权威。入队走事务性 outbox：与业务变更同事务写 job 行，提交后再投递 Redis；ARQ worker 按 job id 幂等执行，语义为至少一次。

## Consequences

- Redis 只是队列与唤醒机制，投递丢失由 outbox 补投。默认 Redis 为业务状态权威会静默丢任务，而 5.79 GB 训练数据集导入、DDM 标注转换的状态是要在 Web 上追溯的业务事实。
- 任务处理器必须幂等，这是至少一次语义的对价，不是可选优化。
- 不直接调用 `arq.enqueue`：绕过 outbox 就绕过了权威记录。入队只经 `job` 门面。
