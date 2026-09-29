-- S065：单 PostgreSQL 实例上 `nvsop` / `training` 两个 database 的真实角色权限隔离。
--
-- 由 `center-role-init` / `training-role-init` 服务用安装身份（超级用户 `nvsop`）各自执行，
-- 通过 `-v database=<name> -v runtime_role=<role>` 传入当前 database 及其运行角色。
-- 脚本幂等：角色重复创建被容忍，属性与密码每次收敛；重复运行不会清空数据。
--
-- 密码只从环境变量读取（`\getenv`），不出现在命令行、Compose 展开或日志里。

-- 安装身份与运行身份分离：runtime 为非超级用户，不继承任何高权角色。
-- center/training 两个初始化服务可能并发；两个异常都覆盖，避免竞态让服务非零退出。
DO $$
BEGIN
    BEGIN
        CREATE ROLE nvsop_runtime;
    EXCEPTION WHEN duplicate_object OR unique_violation THEN NULL;
    END;
    BEGIN
        CREATE ROLE training_runtime;
    EXCEPTION WHEN duplicate_object OR unique_violation THEN NULL;
    END;
END
$$;

-- 每次收敛属性，防止既有实例上的角色漂移成高权身份。
ALTER ROLE nvsop_runtime WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;
ALTER ROLE training_runtime WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;

\getenv runtime_password RUNTIME_PASSWORD
ALTER ROLE :"runtime_role" PASSWORD :'runtime_password';

-- PUBLIC 不能旁路 CONNECT：只允许安装身份与当前 database 的 runtime 连接。
-- 这条 CONNECT 边界是本隔离的安全边界：任何未被显式授权的角色都无法连入本 database。
REVOKE CONNECT ON DATABASE :"database" FROM PUBLIC;
GRANT CONNECT ON DATABASE :"database" TO :"runtime_role";

-- schema 权限同样收敛到 runtime；PUBLIC 不再持有 `public` schema 的任何权限。
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO :"runtime_role";

-- 已存在对象：runtime 只拿到运行所需 DML / 序列 / 函数权限，不拿 owner 或 CREATE。
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO :"runtime_role";
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO :"runtime_role";
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO :"runtime_role";

-- 未来对象由安装身份创建；default privileges 让表/序列/函数授权自动延续。
ALTER DEFAULT PRIVILEGES FOR ROLE nvsop IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO :"runtime_role";
ALTER DEFAULT PRIVILEGES FOR ROLE nvsop IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO :"runtime_role";
ALTER DEFAULT PRIVILEGES FOR ROLE nvsop IN SCHEMA public
    GRANT EXECUTE ON FUNCTIONS TO :"runtime_role";

-- 函数 EXECUTE 与类型（枚举）USAGE 保留 PostgreSQL 对 PUBLIC 的内建默认：函数内建授权无法经
-- ALTER DEFAULT PRIVILEGES 撤销（撤销唯一内建项等价于回到内建默认），类型的 PUBLIC USAGE 也是
-- runtime 解析枚举列所必需。这不构成旁路——上面的 CONNECT 撤销已保证只有安装身份与 runtime 能连入。
