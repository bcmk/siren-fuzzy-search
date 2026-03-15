# siren-fuzzy-search

Companion repo for [SIREN](https://github.com/bcmk/siren),
a Telegram bot that notifies users
when their favourite streamers go online.

Contains an interactive CLI tool
for testing fuzzy search against a PostgreSQL database,
and the blog post
[Fast Fuzzy Search on Millions of Rows in PostgreSQL](docs/search.md)
describing the multi-leg search technique
that keeps queries fast across all input shapes.

![Search testing tool](docs/ui-screenshot.png)

## Usage

```sh
pip install -r requirements.txt
./search.py --db-connection-string "host=... dbname=..." --table people --field nickname
```

Or create a `config.ini`:

```ini
db-connection-string = host=localhost dbname=mydb
table = people
field = nickname
```

Then just run `./search.py`.

### Options

- `--db-connection-string` — PostgreSQL connection string
  (also reads `DB_CONNECTION_STRING` env var)
- `--table` — table to search
- `--field` — field to search
- `--debounce-ms` — input debounce in milliseconds (default: 300)
- `--leg-times` — show per-leg timings instead of total only
