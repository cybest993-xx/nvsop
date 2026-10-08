-- S066/S067：training 对象安装/升级的版本记录。
--
-- 由 `training-install.sh` 与 Vendor DDL 在同一事务内执行，因此只有 DDL 全部成功才会留下
-- 这一行；DDL 失败时整事务回滚，既不留对象也不留版本。`vendor_commit` / `ddl_path` /
-- `ddl_sha256` 是 S067 增量升级判断来源版本的基线；`source_version` 记录本次落地的来源形态
-- （空库安装为 `empty`，增量升级为识别到的已知 Vendor 标注结构版本）。
CREATE TABLE nvsop_training_install (
    vendor_commit text NOT NULL,
    ddl_path text NOT NULL,
    ddl_sha256 text NOT NULL,
    source_version text NOT NULL,
    installed_at timestamptz NOT NULL DEFAULT now()
);

INSERT INTO nvsop_training_install (vendor_commit, ddl_path, ddl_sha256, source_version)
VALUES (:'vendor_commit', :'ddl_path', :'ddl_sha256', :'source_version');
