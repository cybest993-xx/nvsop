#!/bin/sh
set -eu

# 训练/标注进程与 Center 共用单 PostgreSQL 实例，连接目标由 Compose 的 POSTGRES_DB=training 决定。
# S065：以 `training_runtime` 非超级用户连接，密码来自独立 secret 文件。
export POSTGRES_PASSWORD="$(cat /run/secrets/training-runtime-password)"
exec /app/run.sh
