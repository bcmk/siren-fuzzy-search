# Claude Code Guidelines

## Git

- Don't use heredocs for commit messages, use `git commit -m "message"`

## Code Style

- Use lowercase SQL keywords in code blocks,
  uppercase in prose (e.g. `SELECT`, `LIKE`, `SET LOCAL`)
- Write SQL result comments in JSON format, not raw Postgres array text
  (e.g. `-- ["a", "b"]`, not `-- {a,b}`)
- Don't use em dashes (`—`) inside code blocks in `docs/search.md`;
  rephrase with a separator that survives Medium paste (e.g. `(…)`
  or `;`). Medium silently rewrites `—` to `-` inside code on paste.

## Checks

- Run `mypy` after changes
- Run `ruff check --fix` after changes
- Run `ruff format` after changes
- Run `prettier --write` on markdown files after changes
- Run `scripts/genpdf` after changing `docs/search.md`

## Verifying a Medium Paste

To verify that a saved Medium HTML matches `docs/search.md`,
run `scripts/verify-medium <path-to-saved-medium-html>`.
It reports structural diffs
(including heading-level regressions like md `###` landing as `<h2>` in the paste)
and content diffs.

Known Medium paste regressions worth checking by eye after the script passes:

- **Tables become `<pre>` code blocks** and Medium strips inline
  markdown (backticks, `_italic_`), so column alignment breaks unless
  pre-flattened. `scripts/genmedium` handles this.
- **Blank lines inside code blocks** are sometimes stripped on paste.
  Not fixable from the HTML side; the verifier flags them as `pre`
  content diffs.
