# Claude Code Guidelines

## Git

- Don't use heredocs for commit messages, use `git commit -m "message"`

## Code Style

- Use lowercase SQL keywords in code blocks,
  uppercase in prose (e.g. `SELECT`, `LIKE`, `SET LOCAL`)
- Write SQL result comments in JSON format, not raw Postgres array text
  (e.g. `-- ["a", "b"]`, not `-- {a,b}`)

## Checks

- Run `mypy` after changes
- Run `ruff check --fix` after changes
- Run `ruff format` after changes
- Run `prettier --write` on markdown files after changes
- Run `scripts/genpdf` after changing `docs/search.md`
