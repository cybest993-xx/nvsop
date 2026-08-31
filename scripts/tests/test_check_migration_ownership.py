from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from check_migration_ownership import check_migrations

CREATE_TEMPLATE = """\
revision = "{revision}"
down_revision = {down_revision}


def upgrade() -> None:
    op.create_table("{table}", sa.Column("id", sa.Uuid(), primary_key=True))


def downgrade() -> None:
    op.drop_table("{table}")
"""


class MigrationOwnershipTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.versions = Path(self.temp_dir.name) / "migrations/versions"
        self.versions.mkdir(parents=True)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def write(self, name: str, body: str) -> None:
        (self.versions / name).write_text(body, encoding="utf-8")

    def write_create(
        self, name: str, table: str, *, revision: str = "a", down_revision: str = "None"
    ) -> None:
        self.write(
            name,
            CREATE_TEMPLATE.format(revision=revision, down_revision=down_revision, table=table),
        )

    def test_accepts_missing_directory(self) -> None:
        # C3 brings the first migration. Until then the check must return an explicit
        # success rather than an error, so the gate is meaningful before and after.
        self.assertEqual([], check_migrations(self.versions.parent / "absent"))

    def test_accepts_table_owned_by_the_filename_module(self) -> None:
        self.write_create("0001_auth_create_user.py", "auth_user")
        self.assertEqual([], check_migrations(self.versions))

    def test_rejects_table_owned_by_another_module(self) -> None:
        self.write_create("0001_auth_create_user.py", "device_camera")
        errors = check_migrations(self.versions)
        self.assertEqual(
            [
                "0001_auth_create_user.py migrates a table it does not own: device_camera; "
                "the device_ prefix belongs to device, not to auth"
            ],
            errors,
        )

    def test_rejects_table_without_a_module_prefix(self) -> None:
        self.write_create("0001_auth_create_user.py", "users")
        errors = check_migrations(self.versions)
        self.assertEqual(
            [
                "0001_auth_create_user.py migrates a table with no module prefix: users; "
                "physical table names carry their owning module's prefix"
            ],
            errors,
        )

    def test_rejects_filename_without_the_sequence_and_module_convention(self) -> None:
        self.write_create("add_users.py", "auth_user")
        errors = check_migrations(self.versions)
        self.assertEqual(
            [
                "add_users.py does not follow <sequence>_<module>_<slug>.py; "
                "the filename prefix is what makes migration ownership statically decidable"
            ],
            errors,
        )

    def test_rejects_unknown_module_in_filename(self) -> None:
        self.write_create("0001_mystery_create_user.py", "mystery_user")
        errors = check_migrations(self.versions)
        self.assertEqual(
            [
                "0001_mystery_create_user.py names a module that does not exist: mystery; "
                "declare the module in the harness before it owns a table"
            ],
            errors,
        )

    def test_rejects_duplicate_sequence_number(self) -> None:
        self.write_create("0001_auth_create_user.py", "auth_user", revision="a")
        self.write_create("0001_device_create_camera.py", "device_camera", revision="b")
        errors = check_migrations(self.versions)
        self.assertEqual(
            [
                "duplicate migration sequence 0001: "
                "0001_auth_create_user.py, 0001_device_create_camera.py; "
                "the history is a single linear one"
            ],
            errors,
        )

    def test_rejects_branching_history(self) -> None:
        self.write_create("0001_auth_create_user.py", "auth_user", revision="a")
        self.write_create(
            "0002_auth_create_role.py", "auth_role", revision="b", down_revision='"a"'
        )
        self.write_create(
            "0003_device_create_camera.py",
            "device_camera",
            revision="c",
            down_revision='"a"',
        )
        errors = check_migrations(self.versions)
        self.assertEqual(
            [
                'two migrations share down_revision "a": '
                "0002_auth_create_role.py, 0003_device_create_camera.py; "
                "the history is a single linear one"
            ],
            errors,
        )

    def test_reports_every_alembic_operation_that_names_a_table(self) -> None:
        self.write(
            "0001_auth_add_column.py",
            'revision = "a"\n'
            "down_revision = None\n\n\n"
            "def upgrade() -> None:\n"
            '    op.add_column("device_camera", sa.Column("name", sa.Text()))\n'
            '    op.create_index("ix_x", "template_version", ["id"])\n'
            '    op.drop_constraint("ck_y", "alert_violation")\n',
        )
        errors = check_migrations(self.versions)
        self.assertEqual(
            [
                "0001_auth_add_column.py migrates a table it does not own: alert_violation; "
                "the alert_ prefix belongs to alert, not to auth",
                "0001_auth_add_column.py migrates a table it does not own: device_camera; "
                "the device_ prefix belongs to device, not to auth",
                "0001_auth_add_column.py migrates a table it does not own: template_version; "
                "the template_ prefix belongs to template, not to auth",
            ],
            errors,
        )

    def test_allows_a_foreign_key_to_reference_another_modules_table(self) -> None:
        # A referent is a reference, not a modification: `device_camera` may point at
        # `device_station` and a cross-module foreign key stays legal. Only the table
        # being altered is owned by this migration.
        self.write(
            "0001_device_add_fk.py",
            'revision = "a"\n'
            "down_revision = None\n\n\n"
            "def upgrade() -> None:\n"
            "    op.create_foreign_key(\n"
            '        "fk", "device_camera", "template_version", ["t"], ["id"]\n'
            "    )\n",
        )
        self.assertEqual([], check_migrations(self.versions))

    def test_rejects_raw_sql_because_ownership_cannot_be_read_from_it(self) -> None:
        self.write(
            "0001_auth_raw.py",
            'revision = "a"\n'
            "down_revision = None\n\n\n"
            "def upgrade() -> None:\n"
            '    op.execute("ALTER TABLE device_camera ADD COLUMN name text")\n',
        )
        errors = check_migrations(self.versions)
        self.assertEqual(
            [
                "0001_auth_raw.py uses op.execute; table ownership cannot be read from raw "
                "SQL, so express the change with Alembic operations"
            ],
            errors,
        )


if __name__ == "__main__":
    unittest.main()
