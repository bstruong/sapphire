#!/usr/bin/env python3
"""Seed synthetic envelopes for Manual UI QA of the Budget pane.

Does not invent fake account balances — upload a statement (or rely on
existing Vault closings) for Ready to Assign. Creates starter envelopes only.
"""

from __future__ import annotations

import os
import sys

STARTER = (
    "SYNTHETIC Groceries",
    "SYNTHETIC Rent",
    "SYNTHETIC Buffer",
)


def main() -> int:
    if not os.environ.get("DATABASE_URL"):
        os.environ["DATABASE_URL"] = (
            "postgresql://postgres:sapphire@localhost:54329/sapphire"
        )

    import psycopg

    from app.budget.envelopes import create_envelope, list_envelopes
    from app.db.connection import init_db

    init_db()
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        existing = {item.name for item in list_envelopes(conn)}
        for name in STARTER:
            if name in existing:
                print(f"[skip] {name} already present")
                continue
            create_envelope(conn, name=name)
            print(f"[ok] created {name}")
        conn.commit()
    print("Seed complete. Open /dashboard → Budget after uploading a statement.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
