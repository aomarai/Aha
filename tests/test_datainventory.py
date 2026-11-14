"""The privacy rule as a test: every table a migration creates must be in the data inventory."""

from __future__ import annotations

import re

from aha.datainventory import EXEMPT_TABLES, INVENTORY, TableData
from aha.dbmigrate import load_migrations

_CREATE_TABLE = re.compile(
    r'CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:\w+\.)?"?([A-Za-z_][A-Za-z0-9_]*)"?',
    re.IGNORECASE,
)


def created_tables(sql: str) -> set[str]:
    """Names of tables created by ``sql`` (comments are ignored)."""
    without_comments = re.sub(r"--[^\n]*", "", sql)
    return {match.lower() for match in _CREATE_TABLE.findall(without_comments)}


def test_every_table_a_migration_creates_is_declared_in_the_inventory() -> None:
    created: set[str] = set()
    for migration in load_migrations():
        created |= created_tables(migration.sql)

    undeclared = created - EXEMPT_TABLES - set(INVENTORY)

    assert not undeclared, (
        f"tables {sorted(undeclared)} are created by a migration but not declared in "
        "aha.datainventory.INVENTORY (who owns the rows, and how long they are kept); see #99"
    )


def test_every_inventory_entry_has_a_table() -> None:
    created: set[str] = set()
    for migration in load_migrations():
        created |= created_tables(migration.sql)

    assert set(INVENTORY) <= created, f"stale inventory entries: {sorted(set(INVENTORY) - created)}"


def test_every_inventory_entry_says_whose_rows_they_are_and_how_long_they_are_kept() -> None:
    for name, entry in INVENTORY.items():
        assert entry.user_column or entry.guild_column, f"{name}: no user or guild column"
        assert entry.retention.strip(), f"{name}: no retention"


def test_the_exempt_tables_are_not_also_inventoried() -> None:
    assert not EXEMPT_TABLES & set(INVENTORY)


def test_the_baseline_creates_no_personal_data_table() -> None:
    assert created_tables(load_migrations()[0].sql) == set()


def test_the_table_name_finder_handles_the_usual_spellings() -> None:
    sql = """
    -- CREATE TABLE commented_out (id int);
    CREATE TABLE reminders (id bigserial PRIMARY KEY);
    create table if not exists "Guild_Settings" (guild_id bigint);
    CREATE TABLE public.shard_stats (shard_id int);
    """

    assert created_tables(sql) == {"reminders", "guild_settings", "shard_stats"}


def test_the_checker_would_catch_an_undeclared_table() -> None:
    created = created_tables("CREATE TABLE secrets (user_id bigint);")

    assert created - EXEMPT_TABLES - set(INVENTORY) == {"secrets"}


def test_an_entry_describes_its_owner_columns() -> None:
    entry = TableData(user_column="user_id", guild_column="guild_id", retention="until delivered")

    assert (entry.user_column, entry.guild_column) == ("user_id", "guild_id")


def test_every_table_with_personal_columns_has_a_deletion_path() -> None:
    """Adding a table to the inventory without teaching ``aha.privacy`` to purge it fails here."""
    from aha.privacy import GUILD_TABLES, USER_TABLES

    with_user = {name for name, entry in INVENTORY.items() if entry.user_column}
    with_guild = {name for name, entry in INVENTORY.items() if entry.guild_column}

    assert with_user <= USER_TABLES, f"no /forgetme purge for {sorted(with_user - USER_TABLES)}"
    assert with_guild <= GUILD_TABLES, f"no guild purge for {sorted(with_guild - GUILD_TABLES)}"
    assert with_user >= USER_TABLES  # and nothing stale is claimed
    assert with_guild >= GUILD_TABLES
