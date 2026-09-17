"""Assert the migrated database contains every table and column the ORM maps.

The trap this catches: a model change lands without a migration (or with one
that never reaches the deploy), the new image's first query hits a missing
column, and the pod crashloops. CI runs the migration chain against a real
Postgres, then this script diffs ORM expectations against what the chain
actually built — a one-way check (models ⊆ schema), so extra DB objects
never false-positive.

Usage: schema_tripwire.py <db-url>   (or TRIPWIRE_DB_URL in the env)
Exits 1 and names every missing object if the ORM expects anything the
migrations never create.
"""

from __future__ import annotations

import os
import sys

from sqlalchemy import create_engine, inspect

from kobo_api.models import Base


def main() -> int:
    url = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("TRIPWIRE_DB_URL")
    if not url:
        print("usage: schema_tripwire.py <db-url> (or set TRIPWIRE_DB_URL)")
        return 2

    engine = create_engine(url)
    inspector = inspect(engine)

    missing: list[str] = []
    for table in Base.metadata.sorted_tables:
        if not inspector.has_table(table.name):
            missing.append(f"table {table.name!r} (no migration creates it)")
            continue
        present = {col["name"] for col in inspector.get_columns(table.name)}
        missing.extend(
            f"column {table.name}.{col.name} (model maps it; migrations never add it)"
            for col in table.columns
            if col.name not in present
        )

    if missing:
        print("Schema drift: the ORM expects objects the migration chain never creates.")
        print("A rolled image with this model would crashloop on its first query.")
        for item in missing:
            print(f"  - {item}")
        return 1

    print(f"Schema parity: all {len(Base.metadata.sorted_tables)} mapped tables present at head.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
