#!/usr/bin/env python3

import argparse
import curses
import queue
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

import psycopg

from check_indexes import check_indexes
from config import parse_args

MAX_RESULTS = 7


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
    exact_table = psycopg.sql.Identifier(cfg.schema, cfg.exact_table)
    substring_table = psycopg.sql.Identifier(cfg.schema, cfg.substring_table)
    similarity_table = psycopg.sql.Identifier(cfg.schema, cfg.similarity_table)
    alnum_runs_table = psycopg.sql.Identifier(cfg.schema, cfg.alnum_runs_table)
    nonalnum_runs_table = psycopg.sql.Identifier(cfg.schema, cfg.nonalnum_runs_table)
    prefix_table = psycopg.sql.Identifier(cfg.schema, cfg.prefix_table)
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
        with leg_pipeline("exact match"):
            cur.execute(
                psycopg.sql.SQL(
                    """
                    create temp table _search_results on commit drop as
                    select {field} from {table}
                    where {field} = %(query)s
                    """
                ).format(field=field, table=exact_table),
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
            with leg_pipeline("substring"):
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
                    ).format(field=field, table=substring_table),
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
            with leg_pipeline("similarity"):
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
                    ).format(field=field, table=similarity_table),
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
            with leg_pipeline("alnum runs"):
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
                    ).format(field=field, table=alnum_runs_table),
                    {"max_run": max_repeated_alnum_run, "escaped": escaped},
                    prepare=False,
                )

        # Substring search for patterns with nonalnum runs (___, _____, __________).
        # We use partial covering index on max_nonalnum_run to read the index only, not the table.
        # Disabling bitmap scan prevents GIN from being used. GIN can worsen timings by two orders of magnitude.
        # We don't use this leg for:
        #     "__" (too many rows with 2+ consecutive nonalnum chars; we fall back to prefix leg instead)
        if max_nonalnum_run >= 3:
            with leg_pipeline("nonalnum runs"):
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
                    ).format(field=field, table=nonalnum_runs_table),
                    {"max_run": max_nonalnum_run, "escaped": escaped},
                    prepare=False,
                )

        # Prefix match via btree text_pattern_ops — fallback for short patterns (aa, ab, b__, _a_, __)
        # where GIN index produces a huge bitmap to scan because there are too many matches.
        # We don't use this leg for:
        #     "abc" (GIN substring leg handles it instantly)
        #     "___" (nonalnum-runs leg handles it)
        if max_alnum_run < 3 and max_nonalnum_run < 3:
            with leg_pipeline("prefix match"):
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
                    ).format(field=field, table=prefix_table),
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


def search_daemon(
    conn: psycopg.Connection,
    cfg: argparse.Namespace,
    search_q: queue.Queue[str],
    results_q: queue.Queue[tuple[list[str], list[tuple[str, float]]]],
) -> None:
    while True:
        query = search_q.get()
        while not search_q.empty():
            query = search_q.get_nowait()
        time.sleep(cfg.debounce_ms / 1000)
        if not search_q.empty():
            continue
        results_q.put(search(conn, cfg, query))


DIVIDER_COL = 36


def redraw(
    stdscr: curses.window,
    win: curses.window,
    query: str,
    res: list[str],
    timings: list[tuple[str, float]],
) -> None:
    leg_count = sum(1 for name, _ in timings if name != "total")
    timing_rows = max(leg_count, 1)
    _, cols = win.getmaxyx()
    win.resize(MAX_RESULTS + timing_rows + 3, cols)
    win.erase()
    stdscr.erase()
    stdscr.noutrefresh()
    h_divider = timing_rows + 1

    win.box()

    # Vertical divider in top section
    win.addch(0, DIVIDER_COL, curses.ACS_TTEE)
    for row in range(1, h_divider):
        win.addch(row, DIVIDER_COL, curses.ACS_VLINE)

    # Horizontal divider before results
    win.addch(h_divider, 0, curses.ACS_LTEE)
    win.hline(h_divider, 1, curses.ACS_HLINE, cols - 2)
    win.addch(h_divider, DIVIDER_COL, curses.ACS_BTEE)
    win.addch(h_divider, cols - 1, curses.ACS_RTEE)

    prompt = "> " + query
    win.addstr(1, 1, prompt)

    leg_timings = [x for x in timings if x[0] != "total"]
    total_timing = next((t for name, t in timings if name == "total"), None)

    for i, (name, t) in enumerate(leg_timings):
        win.addstr(
            1 + i,
            DIVIDER_COL + 2,
            f"{name:<14}{t:>4.0f} ms",
            curses.A_DIM,
        )

    if total_timing is not None:
        total_str = f"total time: {total_timing:.0f} ms"
        win.addstr(h_divider - 1, DIVIDER_COL - 1 - len(total_str), total_str, curses.A_DIM)

    for i, r in enumerate(res[:MAX_RESULTS]):
        win.addstr(h_divider + 1 + i, 3, r)

    win.move(1, 1 + len(prompt))
    win.noutrefresh()
    curses.doupdate()


def interactive(
    stdscr: curses.window,
    conn: psycopg.Connection,
    cfg: argparse.Namespace,
) -> None:
    curses.use_default_colors()
    curses.curs_set(1)

    win = curses.newwin(MAX_RESULTS + 4, 61, 0, 0)
    win.keypad(True)
    win.timeout(50)

    search_q: queue.Queue[str] = queue.Queue()
    results_q: queue.Queue[tuple[list[str], list[tuple[str, float]]]] = queue.Queue()

    threading.Thread(
        target=search_daemon,
        args=(conn, cfg, search_q, results_q),
        daemon=True,
    ).start()

    try:
        query = ""
        results: list[str] = []
        timings: list[tuple[str, float]] = []
        redraw(stdscr, win, query, results, timings)
        while True:
            while not results_q.empty():
                results, timings = results_q.get_nowait()
                redraw(stdscr, win, query, results, timings)
            try:
                key = win.get_wch()
            except curses.error:
                continue
            if key == "\x1b":
                break
            elif key in ("\x7f", "\b", curses.KEY_BACKSPACE):
                query = query[:-1]
            elif isinstance(key, str) and (key.isascii() and key.isalnum() or key in "_-@") and len(query) < 32:
                query += key.lower()
            else:
                continue
            search_q.put(query)
            redraw(stdscr, win, query, results, timings)
    finally:
        conn.close()


def main() -> None:
    cfg = parse_args()
    conn = psycopg.connect(cfg.db_connection_string)
    check_indexes(conn, cfg)
    curses.wrapper(interactive, conn, cfg)


if __name__ == "__main__":
    main()
