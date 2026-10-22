# ADR-0008: Pluggable state store

- **Status:** Accepted
- **Date:** 2026-10-21

## Context

Actions mode keeps state in GitHub (labels, issues, small JSON files in the hub); server mode needs transactions, a queue and an audit log.

## Decision

State goes through a store interface with implementations per runtime: git files and GitHub issues in Actions mode, SQLite and Postgres in server mode, with compare-and-set writes.

## Consequences

Actions mode needs no database. The server can run a single process on SQLite or scale on Postgres. Today the `StateStore` interface (`squadhq.store`) has SQLite and Postgres implementations; Actions mode still reads and writes its GitHub-based state directly rather than through the interface.

## Alternatives considered

A GitHub-only ledger for server mode was rejected as too slow and too coarse for a job queue.
