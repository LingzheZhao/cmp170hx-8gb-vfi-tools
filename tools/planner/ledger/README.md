# ledger/

One append-only JSONL file per card, named by the card config's `ledger` field (`cards/e.json`:
`ledger/card-e.jsonl`). `card-e.jsonl` ships empty: the first `ingest.py` or `hunt.py --execute` run appends to it.

- One JSON object per line, one line per attempted shot, written with flush + fsync.
- Records are never edited or deleted. A wrong or duplicate record is withdrawn by appending
  `{"phase": "VOID", "attempt_id": "<id>", "reason": "..."}` (`ledger_tools.py void` / `dedupe --apply`).
- ARMED records (a shot sent, outcome not yet logged) resolve to `pending`, then `unknown`; never to `under`.
- Field list and outcome classes: see `../README.md`, section Ledger. `../samples/sample-ledger.jsonl` holds 14
  synthetic records in this format.

Keep your own ledgers out of version control if they name your bench or cards.
