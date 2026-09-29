-- S065：单 PostgreSQL 实例上 `nvsop` / `training` 两个 database 的真实角色权限隔离。
--
-- 由 `center-role-init` / `training-role-init` 服务用安装身份（超级用户 `nvsop`）各自执行，
-- 通过 `-v database=<name> -v runtime_role=<role>` 传入当前 database 及其运行角色。
-- 每个服务只创建/收敛自己的 runtime 角色，所以两个服务并发时不会争用同一角色对象。
-- 脚本幂等：角色已存在时跳过创建，属性与密码每次收敛；重复运行不会清空数据。
--
-- 密码只从环境变量读取（`\getenv`），不出现在命令行、Compose 展开或日志里。

-- 安装身份与运行身份分离：runtime 为非超级用户，不继承任何高权角色。
-- psql 变量不能在 DO $$...$$ 内插值，用 `\gexec` 做存在性判断后再执行建角色语句。
SELECT format('CREATE ROLE %I', :'runtime_role')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'runtime_role')
\gexec

-- 每次收敛属性，防止既有实例上的角色漂移成高权身份。
ALTER ROLE :"runtime_role" WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;

\getenv runtime_password RUNTIME_PASSWORD
ALTER ROLE :"runtime_role" PASSWORD :'runtime_password';

-- PUBLIC 不能旁路 CONNECT：除安装身份（超级用户）外，只允许当前 database 的 runtime 连入。
-- 这条 CONNECT 边界是本隔离的安全边界。
REVOKE CONNECT ON DATABASE :"database" FROM PUBLIC;
GRANT CONNECT ON DATABASE :"database" TO :"runtime_role";

-- schema 权限同样收敛到 runtime；PUBLIC 不再持有 `public` schema 的任何权限。
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO :"runtime_role";

-- 已存在对象：runtime 只拿到运行所需 DML / 序列 / 类型 / 函数权限，不拿 owner 或 CREATE。
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO :"runtime_role";
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO :"runtime_role";
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO :"runtime_role";
-- `GRANT ... ON ALL TYPES IN SCHEMA` 不是 PostgreSQL 语法，用 `\gexec` 逐类型授权：覆盖本次
-- 初始化前已存在的类型；迁移等后续新建的类型由下面的 default privileges 覆盖。
SELECT format('GRANT USAGE ON TYPE %I TO %I', typname, :'runtime_role')
FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
WHERE n.nspname = 'public' AND t.typtype = 'e'
\gexec

-- 未来对象由安装身份创建；default privileges 让表/序列/类型/函数授权自动延续。
ALTER DEFAULT PRIVILEGES FOR ROLE nvsop IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO :"runtime_role";
ALTER DEFAULT PRIVILEGES FOR ROLE nvsop IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO :"runtime_role";
ALTER DEFAULT PRIVILEGES FOR ROLE nvsop IN SCHEMA public
    GRANT USAGE ON TYPES TO :"runtime_role";
ALTER DEFAULT PRIVILEGES FOR ROLE nvsop IN SCHEMA public
    GRANT EXECUTE ON FUNCTIONS TO :"runtime_role";

-- 这里不撤销 PUBLIC 对函数 EXECUTE / 类型 USAGE 的内建授权：实测 `ALTER DEFAULT PRIVILEGES
-- ... REVOKE ... FROM PUBLIC` 对这两类内建默认无效（撤销后 pg_default_acl 无记录，新建函数
-- 仍带 `=X/owner`）。runtime 通过上面的显式授权获得所需权限；CONNECT 撤销已保证除安装身份
-- 外的角色无法连入，内建 PUBLIC 授权因此无法被利用。
