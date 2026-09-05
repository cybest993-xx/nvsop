"""The deployment's first account, as a command.

A fresh deployment has no accounts, and every login is refused until one exists — so "用户
可以登录" runs through here, not only through the routes. Run it once when the deployment is
installed, with the credentials it should start from:

    python -m factory_sop.bootstrap --login-name wang.admin \
        --password-file /run/secrets/bootstrap-password

The password arrives as a file path, like every secret in this deployment (§六), and is read
by the same rule `Settings` applies to its own. The command is idempotent: run again — as a
container restart will — it reports that an account already exists and creates nothing.

This is a composition root like `entrypoint.py`: it reads the environment, owns the one
transaction (a command has no HTTP layer to hold the request-scoped one for it — ADR-0002's
rule is per unit of work, and this command's work is one), and delegates the rest.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping
from pathlib import Path

from factory_sop.auth.adapters.repository import PostgresUserRepository
from factory_sop.auth.usecases.bootstrap import register_first_operator
from factory_sop.observability import configure_logging, get_logger
from factory_sop.persistence import create_database_engine, session_factory
from factory_sop.settings import Settings, read_secret_file

_logger = get_logger("bootstrap")


def main(*, argv: list[str] | None = None, environ: Mapping[str, str] | None = None) -> int:
    """Create the first account from the arguments and environment handed in.

    Both are parameters rather than process state so the command is runnable from a test
    without mutating the process (harness §4); the real entrypoint passes nothing, which is
    `os.environ` and the process `argv`.
    """
    parser = argparse.ArgumentParser(
        prog="python -m factory_sop.bootstrap",
        description="Create the deployment's first account. Skips if any account exists.",
    )
    parser.add_argument("--login-name", required=True, help="The operator's login name.")
    parser.add_argument(
        "--display-name",
        help="Shown in the shell. Defaults to the login name.",
    )
    parser.add_argument(
        "--password-file",
        required=True,
        type=Path,
        help="Path to a file holding the initial password; empty files are refused.",
    )
    arguments = parser.parse_args(argv)

    settings = Settings.from_environment(os.environ if environ is None else environ)
    configure_logging(log_level=settings.log_level, stream=sys.stdout)
    password = read_secret_file(arguments.password_file, "--password-file")

    engine = create_database_engine(settings)
    try:
        session = session_factory(engine)()
        try:
            register_first_operator(
                login_name=arguments.login_name,
                password=password.get_secret_value(),
                display_name=arguments.display_name or arguments.login_name,
                users=PostgresUserRepository(session),
            )
            # The command's own unit of work: one commit, after the use case has said what it
            # did. A use case that raised leaves nothing behind, the same all-or-none the
            # request layer guarantees.
            session.commit()
        finally:
            session.close()
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
