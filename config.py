import argparse

import configargparse


def parse_args() -> argparse.Namespace:
    p = configargparse.ArgParser(default_config_files=["config.ini"])
    p.add("-c", "--config", is_config_file=True, help="config file path")
    p.add("--db-connection-string", required=True, env_var="DB_CONNECTION_STRING", help="PostgreSQL connection string")
    p.add("--schema", default="public", help="database schema (default: %(default)s)")
    p.add("--table", required=True, help="default table for all search legs")
    p.add("--field", required=True, help="column name to search")
    p.add("--exact-table", help="table for exact match leg (default: --table)")
    p.add("--substring-table", help="table for substring leg (default: --table)")
    p.add("--similarity-table", help="table for similarity leg (default: --table)")
    p.add("--alnum-runs-table", help="table for repeated alnum runs leg (default: --table)")
    p.add("--nonalnum-runs-table", help="table for nonalnum runs leg (default: --table)")
    p.add("--prefix-table", help="table for prefix match leg (default: --table)")
    p.add("--debounce-ms", type=int, default=300, help="input debounce delay in ms (default: %(default)s)")
    p.add(
        "--leg-times",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="show per-leg timings",
    )
    cfg = p.parse_args()
    for attr in (
        "exact_table",
        "substring_table",
        "similarity_table",
        "alnum_runs_table",
        "nonalnum_runs_table",
        "prefix_table",
    ):
        if getattr(cfg, attr) is None:
            setattr(cfg, attr, cfg.table)
    return cfg
