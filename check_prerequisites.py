#!/usr/bin/env python3

import argparse
import sys

import psycopg

from config import parse_args


def check_extensions(conn: psycopg.Connection) -> None:
    required = {"pg_trgm", "fuzzystrmatch"}
    with conn.transaction():
        cur = conn.execute(
            "select extname from pg_extension where extname = any(%(exts)s)",
            {"exts": list(required)},
        )
        installed = {row[0] for row in cur.fetchall()}
    missing_exts = required - installed
    if missing_exts:
        print("missing extensions; install with:")
        for ext in sorted(missing_exts):
            print(f"  create extension if not exists {ext};")
        sys.exit(1)


def check_indexes(conn: psycopg.Connection, cfg: argparse.Namespace) -> None:
    field = cfg.field
    tables = {
        cfg.exact_table,
        cfg.substring_table,
        cfg.similarity_table,
        cfg.alnum_runs_table,
        cfg.nonalnum_runs_table,
        cfg.prefix_table,
    }
    indexdefs: dict[str, list[str]] = {t: [] for t in tables}
    with conn.transaction():
        cur = conn.execute(
            "select tablename, indexdef from pg_indexes where schemaname = %(schema)s and tablename = any(%(tables)s)",
            {"schema": cfg.schema, "tables": list(tables)},
        )
        for table_name, indexdef in cur.fetchall():
            indexdefs[table_name].append(indexdef.lower())
        cur = conn.execute(
            """
            select collation_name from information_schema.columns
            where table_schema = %(schema)s and table_name = %(table)s and column_name = %(field)s
            """,
            {"schema": cfg.schema, "table": cfg.prefix_table, "field": field},
        )
        row = cur.fetchone()
        # row is None → table/column not in information_schema (will fail at index check below)
        # row[0] is None → column uses the database default collation; check datcollate
        if row is not None and row[0] is None:
            cur = conn.execute("select datcollate from pg_database where datname = current_database()")
            db_row = cur.fetchone()
            collation = db_row[0] if db_row is not None else None
        elif row is not None:
            collation = row[0]
        else:
            collation = None
    btree_any = [f"using btree ({field})", f"using btree ({field} text_pattern_ops)"]
    if collation in ("C", "POSIX"):
        prefix_btree = btree_any
    else:
        prefix_btree = [f"using btree ({field} text_pattern_ops)"]
    requirements: list[tuple[str, list[str]]] = [
        (cfg.exact_table, btree_any),
        (cfg.substring_table, [f"using gin ({field} gin_trgm_ops)"]),
        (cfg.similarity_table, [f"using gin ({field} gin_trgm_ops)"]),
        (cfg.alnum_runs_table, [f"max_repeated_alnum_run({field})"]),
        (cfg.nonalnum_runs_table, [f"max_nonalnum_run({field})"]),
        (cfg.prefix_table, prefix_btree),
    ]
    missing: list[str] = []
    for table, patterns in requirements:
        if not any(any(p in d for d in indexdefs[table]) for p in patterns):
            msg = f"  {table}: {' or '.join(patterns)}"
            if msg not in missing:
                missing.append(msg)
    if missing:
        print("missing index patterns:")
        for m in missing:
            print(m)
        sys.exit(1)


def main() -> None:
    cfg = parse_args()
    conn = psycopg.connect(cfg.db_connection_string)
    check_extensions(conn)
    check_indexes(conn, cfg)
    conn.close()
    print("all indexes ok")


if __name__ == "__main__":
    main()
