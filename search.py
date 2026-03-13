#!/usr/bin/env python3

import argparse
import curses
import queue
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import configargparse
import psycopg

MAX_RESULTS = 7


def parse_args() -> argparse.Namespace:
    p = configargparse.ArgParser(default_config_files=["config.ini"])
    p.add("-c", "--config", is_config_file=True, help="config file path")
    p.add("--db-connection-string", required=True, env_var="DB_CONNECTION_STRING")
    p.add("--table", required=True)
    p.add("--field", required=True)
    p.add("--debounce-ms", type=int, default=300)
    p.add(
        "--leg-times",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="show per-leg timings",
    )
    return p.parse_args()


def analyze_query(query: str) -> tuple[int, int, int, int] | None:
    alnum_count = 0
    max_alnum_run = 0
    alnum_run = 0
    max_repeated_alnum_run = 0
    repeated_alnum_run = 0
    max_nonalnum_run = 0
    nonalnum_run = 0
    prev = None
    for c in query:
        if c.isascii() and (c.isalpha() and c.islower() or c.isdigit()):
            alnum_count += 1
            alnum_run += 1
            max_alnum_run = max(max_alnum_run, alnum_run)
            nonalnum_run = 0
        elif c in ("_", "-", "@"):
            alnum_run = 0
            nonalnum_run += 1
            max_nonalnum_run = max(max_nonalnum_run, nonalnum_run)
        else:
            return None
        if c == prev and c.isascii() and (c.isalpha() and c.islower() or c.isdigit()):
            repeated_alnum_run += 1
        else:
            repeated_alnum_run = 1 if c.isascii() and (c.isalpha() and c.islower() or c.isdigit()) else 0
        max_repeated_alnum_run = max(max_repeated_alnum_run, repeated_alnum_run)
        prev = c
    return alnum_count, max_alnum_run, max_repeated_alnum_run, max_nonalnum_run


def check_indexes(conn: psycopg.Connection, cfg: argparse.Namespace) -> None:
    table = cfg.table
    field = cfg.field
    with conn.transaction():
        cur = conn.execute(
            "select indexdef from pg_indexes where tablename = %(table)s",
            {"table": table},
        )
        defs = [row[0].lower() for row in cur.fetchall()]
    required = [
        f"using gin ({field} gin_trgm_ops)",
        f"using btree ({field} text_pattern_ops)",
        f"max_repeated_alnum_run({field})",
        f"max_nonalnum_run({field})",
    ]
    missing = [p for p in required if not any(p in d for d in defs)]
    if missing:
        print("missing index patterns:")
        for p in missing:
            print(f"  {p}")
        sys.exit(1)


def search(
    conn: psycopg.Connection,
    cfg: argparse.Namespace,
    query: str,
) -> tuple[list[str], list[tuple[str, float]]]:
    if not query:
        return [], []
    analysis = analyze_query(query)
    if analysis is None:
        return [], []
    alnum_count, max_alnum_run, max_repeated_alnum_run, max_nonalnum_run = analysis
    table = psycopg.sql.Identifier(cfg.table)
    field = psycopg.sql.Identifier(cfg.field)
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    timings: list[tuple[str, float]] = []

    @contextmanager
    def all_legs_pipeline() -> Iterator[None]:
        t0 = time.monotonic()
        if cfg.leg_times:
            yield
        else:
            with conn.pipeline():
                yield
        timings.append(("total", (time.monotonic() - t0) * 1000))

    @contextmanager
    def leg_pipeline(name: str) -> Iterator[None]:
        if cfg.leg_times:
            t0 = time.monotonic()
            with conn.pipeline():
                yield
            timings.append((name, (time.monotonic() - t0) * 1000))
        else:
            yield

    with conn.transaction(), all_legs_pipeline():
        cur = conn.cursor()

        # Exact match guarantees the exact needle appears in results first, even if other legs miss it
        with leg_pipeline("exact"):
            cur.execute(
                psycopg.sql.SQL(
                    """
                    create temp table _search_results on commit drop as
                    select {field} from {table}
                    where {field} = %(query)s
                    """
                ).format(field=field, table=table),
                {"query": query},
                prepare=False,
            )

        # Substring search via GIN trigram index.
        # Needs longest alnum run >= 3 — shorter inputs produce only space-padded trigrams ("  a", " aa", "aa ")
        # that can match over half the table, producing a huge bitmap of candidate rows that GIN must scan.
        # Long repeated-char runs are handled by the repeated-alnum-runs and nonalnum-runs legs instead.
        # Forcing GIN bitmap prevents the planner from picking a slow btree index-only scan.
        # We don't use this leg for:
        #     "aa" (huge bitmap, glacially slow; we fall back to prefix leg instead)
        #     "_" (no useful trigrams; we fall back to prefix leg instead)
        #     "aaaaaa" (GIN doesn't count occurrences of the same trigram,
        #               so bitmap includes every row with the "aaa" trigram, but most don't contain "aaaaaa";
        #               we fall back to repeated-alnum-runs leg instead)
        if max_alnum_run >= 3 and max_repeated_alnum_run < 5:
            with leg_pipeline("sub"):
                cur.execute("set local enable_seqscan = off")
                cur.execute("set local enable_indexscan = off")
                cur.execute("set local enable_indexonlyscan = off")
                cur.execute("set local enable_bitmapscan = on")
                cur.execute(
                    psycopg.sql.SQL(
                        """
                        insert into _search_results
                        select {field} from {table}
                        where {field} like '%%' || %(escaped)s || '%%'
                        limit 100
                        """
                    ).format(field=field, table=table),
                    {"escaped": escaped},
                    prepare=False,
                )

        # Word similarity via GIN — finds fuzzy matches (typos, partial names).
        # We force GIN over pkey scan here.
        # Needs 2+ alnum chars and 4+ total chars —
        # shorter queries produce a huge bitmap matching too many rows.
        # We don't use this leg for:
        #     "a" (huge bitmap, slow; we fall back to prefix leg instead)
        #     "ab" (non-selective similarity; we fall back to prefix leg instead)
        #     "___" (GIN ignores non-alphanumeric characters; we fall back to prefix leg instead)
        if alnum_count >= 2 and len(query) >= 4:
            with leg_pipeline("sim"):
                cur.execute("set local enable_seqscan = off")
                cur.execute("set local enable_indexscan = off")
                cur.execute("set local enable_indexonlyscan = off")
                cur.execute("set local enable_bitmapscan = on")
                cur.execute("set local pg_trgm.word_similarity_threshold = 0.5")
                cur.execute(
                    psycopg.sql.SQL(
                        """
                        insert into _search_results
                        select {field} from {table}
                        where {field} %%> %(query)s
                        limit 100
                        """
                    ).format(field=field, table=table),
                    {"query": query},
                    prepare=False,
                )

        # Substring search for patterns with long repeated alnum runs (aaaaa, eeeeee).
        # This is a fallback because GIN doesn't count occurrences of the same trigram,
        # so GIN suggests every row with the "aaa" trigram, but most don't contain "aaaaaa".
        # The planner can still narrow results using BitmapAnd with the GIN trigram index very effectively,
        # since alnum patterns have useful trigrams.
        # We don't use this leg for:
        #     "aaaa" (this index would return too many runs of 4 symbols; we fall back to GIN substring leg instead)
        #     "_____" (nonalnum chars have no trigrams, so no BitmapAnd; nonalnum-runs leg has its own smaller index)
        if max_repeated_alnum_run >= 5:
            with leg_pipeline("alnum"):
                cur.execute("set local enable_seqscan = off")
                cur.execute("set local enable_indexscan = off")
                cur.execute("set local enable_indexonlyscan = off")
                cur.execute("set local enable_bitmapscan = on")
                cur.execute(
                    psycopg.sql.SQL(
                        """
                        insert into _search_results
                        select {field} from {table}
                        where max_repeated_alnum_run({field}) >= %(max_run)s
                        and {field} like '%%' || %(escaped)s || '%%'
                        limit 100
                        """
                    ).format(field=field, table=table),
                    {"max_run": max_repeated_alnum_run, "escaped": escaped},
                    prepare=False,
                )

        # Substring search for patterns with nonalnum runs (___, _____, __________).
        # We use partial covering index on max_nonalnum_run to read the index only, not the table.
        # Disabling bitmap scan prevents GIN from being used. GIN can worsen timings by two orders of magnitude.
        # We don't use this leg for:
        #     "__" (too many rows with 2+ consecutive nonalnum chars; we fall back to prefix leg instead)
        if max_nonalnum_run >= 3:
            with leg_pipeline("noalnum"):
                cur.execute("set local enable_seqscan = off")
                cur.execute("set local enable_indexscan = off")
                cur.execute("set local enable_indexonlyscan = on")
                cur.execute("set local enable_bitmapscan = off")
                cur.execute(
                    psycopg.sql.SQL(
                        """
                        insert into _search_results
                        select {field} from {table}
                        where max_nonalnum_run({field}) >= %(max_run)s
                        and {field} like '%%' || %(escaped)s || '%%'
                        limit 100
                        """
                    ).format(field=field, table=table),
                    {"max_run": max_nonalnum_run, "escaped": escaped},
                    prepare=False,
                )

        # Prefix match via btree text_pattern_ops — fallback for short patterns (aa, ab, b__, _a_, __)
        # where GIN index produces a huge bitmap to scan because there are too many matches.
        # We don't use this leg for:
        #     "abc" (GIN substring leg handles it instantly)
        #     "___" (nonalnum-runs leg handles it)
        if max_alnum_run < 3 and max_nonalnum_run < 3:
            with leg_pipeline("prefix"):
                cur.execute("set local enable_seqscan = off")
                cur.execute("set local enable_indexscan = off")
                cur.execute("set local enable_bitmapscan = off")
                cur.execute("set local enable_indexonlyscan = on")
                cur.execute(
                    psycopg.sql.SQL(
                        """
                        insert into _search_results
                        select {field} from {table}
                        where {field} like %(escaped)s || '%%'
                        limit 100
                        """
                    ).format(field=field, table=table),
                    {"escaped": escaped},
                    prepare=False,
                )

        # Results are deduplicated and sorted by trigram distance.
        with leg_pipeline("sort"):
            cur.execute(
                psycopg.sql.SQL(
                    """
                    select {field} from
                    (select distinct {field} from _search_results) sub
                    order by {field} <-> %(query)s
                    limit 7
                    """
                ).format(field=field),
                {"query": query},
                prepare=False,
            )
        results = [row[0] for row in cur.fetchall()]
    return results, timings


def keystroke_daemon(
    stdscr: curses.window,
    events: queue.Queue[tuple[str, Any]],
) -> None:
    while True:
        events.put(("key", stdscr.get_wch()))


def search_daemon(
    conn: psycopg.Connection,
    cfg: argparse.Namespace,
    search_q: queue.Queue[str],
    events: queue.Queue[tuple[str, Any]],
) -> None:
    while True:
        query = search_q.get()
        while not search_q.empty():
            query = search_q.get_nowait()
        time.sleep(cfg.debounce_ms / 1000)
        if not search_q.empty():
            continue
        results, timings = search(conn, cfg, query)
        events.put(("results", (results, timings)))


def redraw(
    scr: curses.window,
    query: str,
    res: list[str],
    timings: list[tuple[str, float]],
) -> None:
    scr.erase()
    prompt = "> " + query
    scr.addstr(0, 0, prompt)
    if timings:
        leg_str = " ".join(f"{name}:{t:.0f}" for name, t in timings)
        scr.addstr(0, len(prompt), f"  {leg_str}", curses.A_DIM)
    for i, r in enumerate(res[:MAX_RESULTS]):
        scr.addstr(i + 1, 2, r)
    scr.move(0, len(prompt))
    scr.refresh()


def interactive(
    stdscr: curses.window,
    conn: psycopg.Connection,
    cfg: argparse.Namespace,
) -> None:
    curses.use_default_colors()
    curses.curs_set(1)
    stdscr.keypad(True)

    events: queue.Queue[tuple[str, Any]] = queue.Queue()
    search_q: queue.Queue[str] = queue.Queue()

    threading.Thread(target=keystroke_daemon, args=(stdscr, events), daemon=True).start()
    threading.Thread(
        target=search_daemon,
        args=(conn, cfg, search_q, events),
        daemon=True,
    ).start()

    try:
        query = ""
        results: list[str] = []
        timings: list[tuple[str, float]] = []
        redraw(stdscr, query, results, timings)
        while True:
            try:
                kind, value = events.get()
            except KeyboardInterrupt:
                break
            if kind == "results":
                results, timings = value
                redraw(stdscr, query, results, timings)
                continue
            if kind == "key":
                if value == "\x1b":
                    break
                elif value in ("\x7f", "\b", curses.KEY_BACKSPACE):
                    query = query[:-1]
                elif (
                    isinstance(value, str)
                    and (value.isascii() and value.isalnum() or value in "_-@")
                    and len(query) < 32
                ):
                    query += value.lower()
                else:
                    continue
                search_q.put(query)
                redraw(stdscr, query, results, timings)
    finally:
        conn.close()


def main() -> None:
    cfg = parse_args()
    conn = psycopg.connect(cfg.db_connection_string)
    check_indexes(conn, cfg)
    curses.wrapper(interactive, conn, cfg)


if __name__ == "__main__":
    main()
