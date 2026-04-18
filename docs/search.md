# Fast Fuzzy Search on Millions of Nicknames in PostgreSQL

There are plenty of articles about fuzzy search in PostgreSQL.
However, when I tried these approaches on a table with a few million rows,
some search queries turned out to be hundreds of times slower than others.
So let's build fuzzy search that stays fast regardless of inputs.

![Search testing tool](ui-screenshot.png)

## Problem

Let's say we have a table `people` with ~3 million rows.
Each row has a `nickname` field consisting of lowercase `a-z`, `0-9`, and `_`.
We want to fuzzy search these nicknames.

Some approaches can be ruled out right away.
Soundex relies on how English words sound,
but nicknames like `xx_42z` have no pronunciation.
Full-text search splits input into dictionary words and stems them —
nicknames are not natural language words, so there is nothing to stem.

## The Textbook Approach

Naturally we may want to just sort our nicknames
by some similarity measure to our search term
and pick the best matches.
This is exactly the approach described in PostgreSQL's `pg_trgm` extension
[documentation](https://www.postgresql.org/docs/current/pgtrgm.html).
The extension provides trigram-based similarity,
so why don't we use it to sort?

```sql
create extension if not exists pg_trgm;

create index on people using gist (nickname gist_trgm_ops);

select nickname, nickname <-> 'lemberg_caviar' as dist
from people
order by dist
limit 10;
```

And it works well, but unfortunately not at scale.
In my testing on a DigitalOcean managed database (1 vCPU, 1 GB RAM)
it became unusable at around 1 million rows —
latency went up to 800 ms (90th percentile),
with outliers reaching 5 seconds.

This uses a GiST index — the only index type in `pg_trgm`
that can accelerate the `<->` (trigram distance) operator.

GiST is a tree index.
Each leaf stores a lossy signature (a fixed-size bitfield) of a nickname's trigrams.
Each trigram hashes to a bit position in the signature,
but different trigrams can collide —
hypothetically, `"aaa"`, `"bbb"`, and `"ccc"` might all set bit 5.

Internal tree nodes store the bitwise OR of all their children's signatures.
This lets the index explore only subtrees where the trigram bit is present.

GiST signature length can be tuned with `siglen`,
which can reduce false positives at the cost of a larger index,
but in our tests it did not close the gap enough on multi-million-row tables.

## The Next Option: GIN

The other index type `pg_trgm` offers is GIN.
GIN maps each trigram to the rows that contain it.
For example, to find the `"lemberg_caviar"` substring
(`LIKE '%lemberg\_caviar%'` predicate),
GIN looks up the rows for each trigram
(`"  l"`, `" le"`, `"lem"`, `"emb"`, `"mbe"`, `"ber"`, `"erg"`, `"rg "`,
`"  c"`, `" ca"`, `"cav"`, `"avi"`, `"via"`, `"iar"`, `"ar "`)
and returns only rows where all of them are present.
This scales much better than GiST on large tables.
But it has its own blind spots.

## When GIN Trigrams Break Down

**Short search terms** are slow because the index must build huge bitmaps first.
For example, `"ab"` produces space-padded trigrams like `"  a"` and `" ab"`,
which are much more common than 3-letter trigrams and thus match far more rows.
A GIN trigram index on 3.3 million rows
returns 1.7 million candidate rows for `"ab"` —
worse yet, GIN must build the entire bitmap before returning any row,
which takes up to 8 seconds in my tests.

**Repeated characters** cause too many false positives.
For example, let's work through a condition `LIKE '%aaaaaa%'`.
`pg_trgm` does not count how many times a trigram occurs,
it only checks its presence.
So `"aaa"` and `"aaaaaaa"` produce the same trigrams:

```sql
select show_trgm('aaa');      -- ["  a", " aa", "aaa", "aa "]
select show_trgm('aaaaaaa');  -- ["  a", " aa", "aaa", "aa "]
```

PostgreSQL needs these space-padded trigrams to match word boundaries,
but since our pattern `%aaaaaa%` has wildcards on both sides,
it really uses only `"aaa"` for this query.
So GIN returns every row containing `"aaa"` as a candidate,
and PostgreSQL must recheck them against `LIKE '%aaaaaa%'` —
obviously most don't match.
This makes the search painfully slow.

**Non-alphanumeric characters** are invisible to `pg_trgm`.
`select show_trgm('_____')` returns an empty array.
A username made of underscores
cannot be narrowed by any PostgreSQL trigram index.

No single index or operator covers all of these cases.
The key insight is: don't try to find one.

## Solution: Multi-Leg Search

GIN works well for most queries
but each edge case above needs its own index and query.
Instead of handling all inputs with one query,
let's split the search into multiple legs
and pick the ones that are fast for the specific search term at hand.
We decide which legs to run on the application side,
then deduplicate their results and choose best matches (sort by distance).
Each leg needs its own `SET LOCAL` planner overrides (more on this below),
so we collect results in a transaction-scoped temp table rather than `UNION` CTEs.

### Prerequisites

We need two extensions:

```sql
create extension if not exists pg_trgm;
create extension if not exists fuzzystrmatch;
```

### Example

```sql
begin;

-- leg 1: exact match
create temp table _search_results on commit drop as
select nickname from people
where nickname = 'lemberg_caviar'; -- search term

-- leg 2: similarity
set local enable_seqscan = off;
set local enable_indexscan = off;
set local enable_indexonlyscan = off;
set local enable_bitmapscan = on;
set local pg_trgm.word_similarity_threshold = 0.5;
insert into _search_results
select nickname from people
where nickname %> 'lemberg_caviar' -- search term
limit 100;

-- other legs: substring, repeated characters,
-- non-alphanumeric runs, prefix (details below)

-- deduplicate + sort
select nickname from (
    select distinct nickname from _search_results
) sub
order by
    2 * (nickname <-> 'lemberg_caviar') -- search term
    + levenshtein(left(nickname, 255), left('lemberg_caviar', 255))::float
        / greatest(length(nickname), length('lemberg_caviar'), 1)
limit 10;

commit;
```

This example runs all legs unconditionally.
In practice, we only activate legs that will be fast
for the given search term — more on this below.

Ideally each leg would sort by distance first and then return closest matches,
but such ordering is too expensive on large tables.
So each "fuzzy" leg just collects 100 arbitrary nicknames that match its filter —
which turns out to be good enough in practice —
and the final sort ranks only those candidates.

## The Legs

### 1. Exact Match Leg

```sql
create temp table _search_results on commit drop as
select nickname from people
where nickname = 'lemberg_caviar'; -- search term
```

Uses a B-tree index to check
if the search term matches an existing record exactly.
This is cheap and ensures an exact match is always included if one exists.

**Activate when:** always.

**Prerequisites:** a B-tree index with `text_pattern_ops`, which supports the equality operator (`=`) as well.
A plain B-tree index (without `text_pattern_ops`) may work too;
more on this in leg 6.

```sql
create index on people (nickname text_pattern_ops);
```

### 2. Main Substring Leg (accelerated by GIN trigram index)

```sql
-- force GIN bitmap scan (planner sometimes prefers a slower btree scan)
set local enable_seqscan = off;
set local enable_indexscan = off;
set local enable_indexonlyscan = off;
set local enable_bitmapscan = on;

insert into _search_results
select nickname from people
where nickname like '%lemberg\_caviar%' -- LIKE-escaped search term
limit 100;
```

`'lemberg\_caviar'` is the search term with `LIKE` metacharacters escaped:

```
_  ⟶  \_
%  ⟶  \%
\  ⟶  \\
```

This is our workhorse leg.
`pg_trgm` produces candidate rows that contain trigrams from the search term,
which are then rechecked against the `LIKE` predicate.

**Activate when:**

- the longest alphanumeric run in the search term is at least 3 characters
  (shorter runs can produce trigrams that match too many rows,
  so PostgreSQL must build a huge bitmap before returning data)
- the longest repeated-character run in the search term is below 5 characters
  (`pg_trgm` does not distinguish `'aaa'` from `'aaaaaaa'` in terms of trigrams,
  so the index returns too many false positives)

**Prerequisites:** a GIN trigram index.

```sql
create index on people using gin (nickname gin_trgm_ops);
```

### 3. Main Fuzzy Leg (word similarity)

```sql
-- force GIN bitmap scan (planner sometimes prefers a slower btree scan)
set local enable_seqscan = off;
set local enable_indexscan = off;
set local enable_indexonlyscan = off;
set local enable_bitmapscan = on;
set local pg_trgm.word_similarity_threshold = 0.5;

insert into _search_results
select nickname from people
where nickname %> 'lemberg_caviar' -- search term
limit 100;
```

This is where all the fuzziness comes from.
The `%>` operator (word similarity) slides the search term
across every position in the target string and takes the best similarity score.
It catches typos, transpositions, and partial matches that `LIKE` would miss.
Despite the name, word similarity in `pg_trgm` is still trigram-based;
it does not depend on dictionaries or linguistic tokenization.
For nicknames, its usefulness comes from allowing
a good substring-level match inside a longer value.

We raise the threshold from the default 0.3 to 0.5
to avoid flooding the result set with poor matches.

**Activate when:**

- the search term has at least 2 alphanumeric characters
- the search term is at least 4 characters long

**Prerequisites:** the same GIN trigram index as leg 2.

**Why `%>` and not `%`:**
the `%` operator computes similarity over the entire strings.
A short search term like `"kate"` has low whole-string similarity
against a longer value like `"katesmithxyz"`,
so most GIN candidates fail the recheck.
In practice, `%` has to scan thousands of heap blocks
(GIN does not support `INCLUDE` as of PostgreSQL 18)
to find 100 matches, while `%>` finds them almost immediately
because substring-level matching is much more generous.

### 4. Repeated-Run Rescue Leg (accelerated by longest repeated character index and GIN)

```sql
-- force bitmap scan for BitmapAnd
-- between max_repeated_alnum_run btree index and GIN
set local enable_seqscan = off;
set local enable_indexscan = off;
set local enable_indexonlyscan = off;
set local enable_bitmapscan = on;

insert into _search_results
select nickname from people
where max_repeated_alnum_run(nickname)
    -- precompute during analysis in prod
    >= max_repeated_alnum_run('lemberg_caviar')
  and nickname like '%lemberg\_caviar%' -- LIKE-escaped search term
limit 100;
```

Some queries defeat `pg_trgm` entirely —
`"aaa"` and `"aaaaaaa"` produce the same trigrams,
so the index can't tell a short run from a long one.
A functional index on the longest repeated run
helps narrow the search (via BitmapAnd).

**Activate when:** the longest repeated-character run
in the search term is at least 5 characters.

**Prerequisites:** a custom SQL function and a partial index.

```sql
create function max_repeated_alnum_run(text)
returns int as $fn$
    select coalesce(max(length(m[1])), 0)
    from regexp_matches($1, '(([a-z0-9])\2*)', 'g') as m
$fn$ language sql immutable;

create index on people (
    max_repeated_alnum_run(nickname)
) where max_repeated_alnum_run(nickname) >= 5;
```

The regex assumes lowercase ASCII nicknames (`[a-z0-9]`),
matching the alphabet defined in the Problem section.
The partial index is tiny for typical nickname data.

### 5. Non-Alnum Rescue Leg (accelerated by longest non-alnum run index)

```sql
-- force index-only scan on the max_nonalnum_run index;
-- GIN produces no trigrams for nonalnum characters
-- and can worsen timings by orders of magnitude
set local enable_seqscan = off;
set local enable_indexscan = off;
set local enable_indexonlyscan = on;
set local enable_bitmapscan = off;

insert into _search_results
select nickname from people
where max_nonalnum_run(nickname)
    -- precompute during analysis in prod
    >= max_nonalnum_run('lemberg_caviar')
  and nickname like '%lemberg\_caviar%' -- LIKE-escaped search term
limit 100;
```

The same technique with a function
that measures the longest non-alphanumeric run.
Since `pg_trgm` produces zero trigrams
for these characters (`select show_trgm('_____')` returns `{}`),
this leg is an effective way to find such patterns.

**Activate when:** the longest non-alphanumeric run
in the search term is at least 3 characters.

**Prerequisites:** same technique as leg 4.

```sql
create function max_nonalnum_run(text)
returns int as $fn$
    select coalesce(max(length(m[1])), 0)
    from regexp_matches($1, '([^a-z0-9]+)', 'g') as m
$fn$ language sql immutable;

create index on people (
    max_nonalnum_run(nickname)
) include (nickname)
where max_nonalnum_run(nickname) >= 3;
```

The partial index is tiny for typical nickname data and `INCLUDE` enables index-only scans.

### 6. Prefix Fallback Leg

```sql
-- force index-only scan on text_pattern_ops btree
set local enable_seqscan = off;
set local enable_indexscan = off;
set local enable_indexonlyscan = on;
set local enable_bitmapscan = off;

insert into _search_results
select nickname from people
where nickname like 'lemberg\_caviar%' -- LIKE-escaped search term
limit 100;
```

This is a useful fallback for very short or low-information inputs.
Prefix matching is fast but only finds nicknames starting with the search term.
It still provides useful results for short queries
where substring or similarity would match almost arbitrary strings.

**Activate when:** no other content-based leg qualifies:

- longest alphanumeric run in the search term below 3 characters
- longest non-alphanumeric run in the search term below 3 characters

**Prerequisites:** the same `text_pattern_ops` B-tree index as leg 1.
This operator class (`text_pattern_ops`) allows prefix search e.g. on UTF-8 strings
by effectively applying `collate "C"` just to the index.

**A plain B-tree vs. `text_pattern_ops`:**
a plain B-tree index (without `text_pattern_ops`) may work too,
if the field has "C" collation.
Under "C" collation PostgreSQL compares bytes rather than characters, ignoring linguistic rules —
and that's also what enables prefix search acceleration.
This works for our nicknames problem — they are ASCII-only.

So if you already have a plain B-tree index and use it for e.g. `ORDER BY`,
sometimes you can reuse it rather than creating an almost identical separate `text_pattern_ops` index.
Such a B-tree index supports both prefix search and regular `ORDER BY`, just beware of "C" collation ordering limitations.
You would need to specify the field `collate "C"` explicitly unless the database default is already `C`.

You might wonder why not use `text_pattern_ops` for both `ORDER BY` and prefix search.
The answer is you can't — it uses different comparison operators (`~<~`, `~>~`)
and regular `ORDER BY` won't use it.

## Which Legs to Run

To decide which legs to activate (see **Activate when** above),
we analyse the search term on the application side:

| Metric            | What it measures                                 |
| ----------------- | ------------------------------------------------ |
| Alnum count       | Total alphanumeric characters                    |
| Max alnum run     | Longest consecutive `[a-z0-9]` substring         |
| Max repeated run  | Longest run of the _same_ alphanumeric character |
| Max non-alnum run | Longest consecutive non-alphanumeric substring   |

These four numbers determine which legs will produce useful results
in a reasonable time and which would thrash and slow down the index.
For example, a search term like `"aaaaaa"` has a max repeated run of 6 —
it should skip the GIN substring leg and use a specialised index instead.
A search term like `"___"` has a max non-alphanumeric run of 3
and zero alphanumeric characters — trigram indexes are useless here.

Here is a summary of the activation rules:

| Leg                 | Purpose                     | Run when                                   |
| ------------------- | --------------------------- | ------------------------------------------ |
| Exact match         | Include exact hit           | Always                                     |
| Main substring      | Main substring filter       | Max alnum run ≥ 3 and max repeated run < 5 |
| Main fuzzy          | Fuzzy/typo matching         | Alnum count ≥ 2 and length ≥ 4             |
| Repeated-run rescue | Handle inputs like `aaaaaa` | Max repeated run ≥ 5                       |
| Non-alnum rescue    | Handle inputs like `___`    | Max non-alnum run ≥ 3                      |
| Prefix fallback     | Low-information fallback    | No stronger content-based leg qualifies    |

## Final Sorting

After all legs have contributed their candidates, deduplicate, sort by distance, and return the best matches.
The candidate set is at most a few hundred rows — no index is needed here.

```sql
select nickname from (
    select distinct nickname from _search_results
) sub
order by
    2 * (nickname <-> 'lemberg_caviar') -- search term
    + levenshtein(left(nickname, 255), left('lemberg_caviar', 255))::float
        / greatest(length(nickname), length('lemberg_caviar'), 1)
limit 10;
```

We blend two distance metrics with complementary strengths.

### Trigram Distance

The `<->` operator has useful properties.
For example, `'lemberg_caviar'` is not far from `'caviar_lemberg'`,
so you still find it if you forgot the word order.
It works on unordered sets of trigrams
and computes `1 − count(shared trigrams) / count(all unique trigrams from both strings)` (Jaccard distance).

### Levenshtein Distance

```sql
create extension if not exists fuzzystrmatch;
```

The `levenshtein` function from this extension
counts the minimum number of single-character edits
(insertions, deletions, substitutions)
needed to transform one string into another.
It helps distinguish strings that differ only in non-alphanumeric characters,
where trigram distance sees no difference:

```sql
select 'x' <-> 'x_';  -- 0 (underscores are ignored by pg_trgm)
select levenshtein('x', 'x_');  -- 1
```

We normalise it by dividing by the longer string's length
to get a 0–1 ratio comparable to trigram distance.
Note that `levenshtein` has a 255-character input limit;
we cap both inputs with `left(..., 255)`.

Trigram distance is weighted 2:1 over normalised Levenshtein — chosen empirically.

## Planner Control

PostgreSQL's query planner picks indexes automatically,
but the wrong choice can be two orders of magnitude slower.
Since we validated each leg with `EXPLAIN ANALYZE` and benchmarking,
we know exactly which index to use and force it
with `SET LOCAL` before each leg:

```sql
set local enable_seqscan = off;
set local enable_indexscan = off;
set local enable_indexonlyscan = off;
set local enable_bitmapscan = on;
-- now the planner must use bitmap scan (GIN)
```

`SET LOCAL` scopes changes to the current transaction, so nothing leaks.
It is usually not recommended to override planner settings.
However, in our testing PostgreSQL chose the wrong path quite often,
so we don't really have a choice here.

### Prepared Statements

If your database driver uses prepared statements, beware of generic plans.
PostgreSQL uses custom plans for the first few executions,
taking current `SET LOCAL` settings into account.
After several executions it may switch to a generic plan
that was optimised without the planner overrides —
undoing the `SET LOCAL` settings entirely.

We have to use unprepared (simple-protocol) execution
for leg queries to ensure the planner respects the overrides every time.

## Performance Techniques

Use pipeline mode if your driver supports it (libpq, pgx) —
all legs go in one batch, reducing round trips to a single flight.

Keep everything in one transaction so `SET LOCAL` settings don't leak,
the temp table lives for all legs, and you get a consistent snapshot.

## Results

We benchmarked 178 search terms on a 3.3-million-row table
on a DigitalOcean managed database (1 vCPU, 1 GB RAM).
Here are 20 randomly selected results:

| Search term                         | ms  |
| ----------------------------------- | --- |
| `______________________________`    | 3   |
| `__a__b__`                          | 40  |
| `a________________________________` | 2   |
| `b___`                              | 5   |
| `ee`                                | 5   |
| `emma`                              | 30  |
| `ethan`                             | 21  |
| `finn`                              | 12  |
| `lily_grace_park_nash`              | 89  |
| `mason`                             | 29  |
| `max`                               | 5   |
| `mia_lynn_jones_hall`               | 88  |
| `noah_james_kim`                    | 72  |
| `nora`                              | 16  |
| `owen`                              | 8   |
| `owen_rose_park_cruz`               | 47  |
| `qjonpbvhpysdhr`                    | 3   |
| `ryan_lynn_bell`                    | 38  |
| `xyz`                               | 3   |
| `zoe`                               | 4   |

Over all 178 search terms:
median = 31 ms, 90th percentile = 51 ms, maximum = 131 ms.

The raw `nickname` data is about 39 MB. Index sizes relative to the data:

| Index                               | Size   | % of nickname data |
| ----------------------------------- | ------ | ------------------ |
| GIN trigram                         | 122 MB | 313%               |
| B-tree `text_pattern_ops`           | 100 MB | 256%               |
| max_nonalnum_run (partial, INCLUDE) | 248 kB | 0.6%               |
| max_repeated_alnum_run (partial)    | 112 kB | 0.3%               |

The exact numbers depend on your data distribution, hardware, and query patterns,
but the technique consistently keeps searches fast
across the full range of input shapes.

The key takeaway: don't search for one perfect index strategy.
Classify your inputs, run the right leg for each class,
and let the union plus blended sorting produce a cohesive result.

This design deliberately optimises for low and stable latency,
not exhaustive recall.
Each leg contributes a bounded number of candidates,
and the final ranking happens only within that union.

## Write-Side Cost of GIN

The GIN trigram index is the largest index in the table in our case
and also the most expensive to maintain during writes.

The problem is, normally for every row you need to update just one index record.
But GIN maps each trigram to every row containing it,
so for each new nickname GIN must add a record for every trigram in the nickname.

We benchmarked 200-row operations on a 3.3-million-row table
(same DigitalOcean setup as the read benchmarks).
Each result averaged over 10 iterations:

| Operation                          | Without GIN | With GIN | Slower by |
| ---------------------------------- | ----------- | -------- | --------- |
| Pure `INSERT`                      | 227 ms      | 425 ms   | ~87%      |
| Pure `UPDATE`                      | 333 ms      | 569 ms   | ~71%      |
| `INSERT ... ON CONFLICT DO UPDATE` | 443 ms      | 898 ms   | ~103%     |

Even `UPDATE` statements that do not modify `nickname`
can still pay the GIN cost.
PostgreSQL writes a new heap tuple version for every update.
If the update cannot be done as a HOT (Heap-Only Tuple) update —
because there is not enough free space on the same page,
or because an indexed column changed —
PostgreSQL must create new index entries
for the new tuple version, including in the GIN index.

The upsert path (`INSERT ... ON CONFLICT DO UPDATE`) is the most expensive
because it attempts an insert first, tentatively touching GIN,
before falling back to an update on conflict.

For a read-heavy workload like search, this is an acceptable trade-off —
the GIN index makes reads orders of magnitude faster.
But for tables with frequent writes, the overhead is worth monitoring.
We did not tune GIN maintenance parameters here;
the write benchmarks reflect default managed-Postgres settings.

If your table has frequent writes but the fuzzy search field rarely changes,
it may help to duplicate it into a separate table
where the fuzzy search indexes are defined.
This way writes to the main table don't touch GIN at all.
We use this approach and it made our updates much faster.

## Testing Tool

The [companion repository](https://github.com/bcmk/siren-fuzzy-search)
includes an interactive terminal tool
for testing search against a live database.
It connects to PostgreSQL directly, runs all the legs described above,
and shows results with per-leg timings as you type.

The tool helped catch slow edge cases early — you can try any input pattern
and immediately see which legs fire and how long each one takes.

## Where We Use This

This technique powers the streamer search
in [SIREN](https://github.com/bcmk/siren),
a Telegram bot for webcast alerts.
