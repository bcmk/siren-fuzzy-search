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
