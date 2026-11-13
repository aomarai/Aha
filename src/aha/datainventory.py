"""The inventory of personal data the database may hold, and how to delete it.

Privacy rule (#99): **every table must declare who its rows belong to and how long they are kept**,
so a user's or a server's data can always be found and deleted. This module is that declaration,
and
``tests/test_datainventory.py`` fails when a migration creates a table that is not listed here (or
when an entry here has no table), so the rule cannot be forgotten.

To add a table: write the migration, then add an entry below naming the column that holds the Discord
user id and/or guild id (``None`` if the table is not per user or per guild), and say in words how
long rows are kept. Then implement the purge for it where the feature lives.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["EXEMPT_TABLES", "INVENTORY", "TableData"]


@dataclass(frozen=True, slots=True)
class TableData:
    """What a table holds, whose it is, and for how long."""

    #: Column holding the Discord user id the row belongs to, or ``None``.
    user_column: str | None
    #: Column holding the Discord guild id the row belongs to, or ``None``.
    guild_column: str | None
    #: Plain-language retention, mirrored in docs/privacy.md.
    retention: str


#: Tables that hold no personal data: the migration runner's own bookkeeping.
EXEMPT_TABLES = frozenset({"schema_migrations"})

#: Every other table. Empty until the first stateful feature (#96-#98) adds one.
INVENTORY: dict[str, TableData] = {}
