#!/bin/sh
set -eu

# 训练/标注进程与 Center 共用单 PostgreSQL 实例，连接目标由 Compose 的 POSTGRES_DB=training 决定。
# 运行身份隔离由 #223(S065) 负责；本票复用中心安装身份的密码 secret。
export POSTGRES_PASSWORD="$(cat /run/secrets/center-db-password)"
exec /app/run.sh
