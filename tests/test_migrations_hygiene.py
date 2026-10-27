# Copyright (C) 2026 zealmugumya-creator. All rights reserved.

"""Migration hygiene guards (CI-runnable, no database required).

These do NOT verify SQL semantics against Postgres (that needs a live DB and is
tracked separately as an external blocker). They guard the *process* rules the
mandate imposes on schema change (Rules 5–7): every migration is reversible,
applies inside a transaction, is idempotent where it creates objects, and never
embeds a secret.
"""
from __future__ import annotations

import re
from pathlib import Path

MIGRATIONS = Path(__file__).resolve().parent.parent / "supabase" / "migrations"

_SECRET_PATTERNS = re.compile(
    r"(sb_secret_[A-Za-z0-9_\-]{10,}|sk_live_[A-Za-z0-9]{6,}|flw_sk_[A-Za-z0-9]{6,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----|xox[bprsa]-[A-Za-z0-9\-]{10,})",
    re.IGNORECASE,
)


def _pairs():
    ups = sorted(MIGRATIONS.glob("*.up.sql"))
    assert ups, "no migrations found — is supabase/migrations/ missing?"
    for up in ups:
        yield up, MIGRATIONS / (up.name.replace(".up.sql", ".down.sql"))


def test_every_up_has_a_down():
    for up, down in _pairs():
        assert down.exists(), f"{up.name} has no rollback ({down.name}) — Rule 6"


def _first_statement(sql: str) -> str:
    """First line of SQL that is not blank and not a `--` comment."""
    for line in sql.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("--"):
            return stripped
    return ""


def test_migrations_run_in_a_transaction():
    for up, _ in _pairs():
        sql = up.read_text(encoding="utf-8").lower()
        assert _first_statement(sql).startswith("begin;"), \
            f"{up.name} does not open a transaction"
        assert "commit;" in sql, f"{up.name} does not commit its transaction"


def test_create_table_is_idempotent():
    """Re-running a migration must not crash on existing objects (Rule 6)."""
    for up, _ in _pairs():
        sql = up.read_text(encoding="utf-8").lower()
        for m in re.finditer(r"create\s+(?:unindexed\s+)?table\s+(?!if\s+not\s+exists)", sql):
            raise AssertionError(f"{up.name}: non-idempotent CREATE TABLE at offset {m.start()}")


def test_no_secrets_embedded_in_migrations():
    for up, down in _pairs():
        for f in (up, down):
            if f.exists():
                assert not _SECRET_PATTERNS.search(f.read_text(encoding="utf-8")), \
                    f"{f.name} appears to embed a secret — Rule 4"


def test_migration_numbers_are_unique_and_ordered():
    nums = [p.name.split("_", 1)[0] for p, _ in _pairs()]
    assert len(nums) == len(set(nums)), "duplicate migration numbers"
    assert nums == sorted(nums), "migration numbers are not ordered"
