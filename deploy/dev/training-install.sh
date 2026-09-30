#!/bin/sh
set -eu

# S066：在空 `training` database 上安装五类训练/标注进程（data-generation、cr-training、
# ddm-training、evaluation、annotation-backend）所需的对象。
#
# DDL 复用 Vendor 的合并脚本，不在本仓库复制；本脚本只负责判断安装状态、把 DDL 与版本记录
# 放进同一事务，并把消费的 Vendor 基座提交、DDL 路径与内容摘要写进 `nvsop_training_install`。
# Vendor 提交取自 `docs/base/verified-commits.md` 的 NVIDIA 子树基座提交；该 DDL 未被任何已登记
# patch 触及，内容摘要才是实际消费版本的权威标识。
# 空库首次安装；已记录同一版本时跳过；已记录其它版本或未知非空/部分状态时明确拒绝，
# 不自动删除或覆盖既有数据（受支持历史数据的增量升级由 S067 负责）。

DDL_FILE="${NVSOP_TRAINING_DDL_FILE:-/opt/nvsop/training-ddl/01-init-tables.sql}"
DDL_PATH="${NVSOP_TRAINING_DDL_PATH:?缺少 NVSOP_TRAINING_DDL_PATH}"
VERSION_SQL="/opt/nvsop/training-install-version.sql"
INSTALL_DATABASE="${PGDATABASE:-training}"
EXPECTED_COMMIT="${NVSOP_TRAINING_VENDOR_COMMIT:?缺少 NVSOP_TRAINING_VENDOR_COMMIT}"
EXPECTED_SHA="${NVSOP_TRAINING_DDL_SHA256:?缺少 NVSOP_TRAINING_DDL_SHA256}"

# 安装身份密码只从独立 secret 文件读入环境，不进入命令行或日志。
export PGPASSWORD="$(cat /run/secrets/center-db-password)"

# 先核对实际 DDL 是否就是锁定的 Vendor 版本，避免用未经复核的 DDL 安装。
actual_sha="$(sha256sum "$DDL_FILE" | cut -d' ' -f1)"
if [ "$actual_sha" != "$EXPECTED_SHA" ]; then
  echo "training DDL 与锁定的 Vendor 版本不符：期望 $EXPECTED_SHA，实际 $actual_sha" >&2
  exit 1
fi

# 只读探测：版本表缺失时按 database 内是否已有非系统对象区分空库与未知状态；版本表存在时
# 比对已记录的提交与摘要；匹配时再只读核对锁定 Vendor DDL 明确拥有的对象。
# 分步是因为直接引用可能不存在的版本表会在解析期报错。
state="$(
  psql -v ON_ERROR_STOP=1 -d "$INSTALL_DATABASE" -tA <<'SQL'
SELECT CASE
  WHEN to_regclass('public.nvsop_training_install') IS NULL THEN
    CASE
      WHEN EXISTS (
        SELECT 1 FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname NOT LIKE 'pg\_%' AND n.nspname <> 'information_schema'
          AND c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f')
      ) OR EXISTS (
        SELECT 1 FROM pg_type t
        JOIN pg_namespace n ON n.oid = t.typnamespace
        WHERE n.nspname NOT LIKE 'pg\_%' AND n.nspname <> 'information_schema'
          AND t.typtype IN ('e', 'd')
      ) THEN 'unknown'
      ELSE 'empty'
    END
  ELSE 'present'
END;
SQL
)"

if [ "$state" = "present" ]; then
  state="$(
    psql -v ON_ERROR_STOP=1 -d "$INSTALL_DATABASE" -tA \
      -v commit="$EXPECTED_COMMIT" -v sha="$EXPECTED_SHA" <<'SQL'
SELECT CASE
  WHEN NOT EXISTS (
    SELECT 1 FROM nvsop_training_install
    WHERE vendor_commit = :'commit' AND ddl_sha256 = :'sha'
  ) THEN 'mismatch'
  WHEN (
    SELECT count(*)
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public'
      AND c.relkind IN ('r', 'p')
      AND c.relname IN (
        'dataset', 'video', 'chunk', 'annotation', 'augmented_data',
        'augmentation_stages', 'training_job', 'ddm_training_job',
        'evaluation_job', 'e2e_evaluation_job'
      )
  ) <> 10 THEN 'drifted'
  WHEN (
    SELECT count(*)
    FROM pg_type t
    JOIN pg_namespace n ON n.oid = t.typnamespace
    WHERE n.nspname = 'public'
      AND t.typtype = 'e'
      AND t.typname IN ('status_enum', 'training_status_enum')
  ) <> 2 THEN 'drifted'
  WHEN (
    SELECT count(*)
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public'
      AND c.relkind = 'i'
      AND c.relname IN (
        'idx_augmentation_stages_augmentation_id',
        'idx_augmentation_stages_stage_name'
      )
  ) <> 2 THEN 'drifted'
  WHEN NOT EXISTS (
    SELECT 1
    FROM pg_constraint con
    JOIN pg_class rel ON rel.oid = con.conrelid
    JOIN pg_namespace n ON n.oid = rel.relnamespace
    WHERE n.nspname = 'public'
      AND rel.relname = 'augmentation_stages'
      AND con.conname = 'unique_augmentation_stage'
      AND con.contype = 'u'
  ) THEN 'drifted'
  ELSE 'installed'
END;
SQL
  )"
fi

case "$state" in
  installed)
    echo "training 对象已是 $EXPECTED_COMMIT / $EXPECTED_SHA，跳过安装"
    exit 0
    ;;
  empty)
    ;;
  mismatch)
    echo "training 已记录不同的安装版本，拒绝覆盖；增量升级由 S067 负责" >&2
    exit 1
    ;;
  drifted)
    echo "training 安装标记匹配，但锁定 Vendor DDL 的必需对象缺失或类型不符，拒绝自动修复" >&2
    exit 1
    ;;
  *)
    echo "training 处于未知非空/部分安装状态，拒绝自动修复：$state" >&2
    exit 1
    ;;
esac

# Vendor DDL 与版本记录在同一事务提交：DDL 全部成功才写版本，失败则整体回滚，不留半安装状态。
psql -v ON_ERROR_STOP=1 -d "$INSTALL_DATABASE" --single-transaction \
  -v vendor_commit="$EXPECTED_COMMIT" -v ddl_path="$DDL_PATH" -v ddl_sha256="$EXPECTED_SHA" \
  -f "$DDL_FILE" -f "$VERSION_SQL"

echo "training 对象安装完成：$DDL_PATH @ $EXPECTED_COMMIT / $EXPECTED_SHA"
