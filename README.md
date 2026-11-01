# Splitter for YNAB

Split expenses between two YNAB plans using transaction flags. No database or
persistent sync state. Uses the official Python SDK.

| Flag (configurable) | Meaning |
| --- | --- |
| Blue | Split 50/50 |
| Purple | Assign the full amount to the other person |
| Green | Completed; never processed again |

A $100 blue purchase becomes $50 spending in each plan. The original stays intact
except for its flag. Categorized refunds and inflows work in reverse.

## Setup

```sh
python -m venv .venv
.venv/bin/python -m pip install .
```

1. In each plan, create a dedicated unlinked cash/checking account with a zero starting balance.
   Positive balance means the other person owes you; negative means you owe them.
2. Edit [config.toml](config.toml): fill in `plan_id` and `settlement_account_id`
   under `participants.a` and `participants.b`. Both plans must use the same currency.
3. Set your access tokens locally:

```sh
export YNAB_TOKEN_A='your-token-for-a'
export YNAB_TOKEN_B='your-token-for-b'
```

## Run

Flag transactions in either plan, preview, then apply:

```sh
.venv/bin/python main.py --dry-run
.venv/bin/python main.py
```

`sync.lookback_days` defaults to 14 calendar days, based on
transaction date. Increase it temporarily to process older transactions.
Configure colors under `[flags]` and optional category-ID mappings under
`[categories.overrides.a_to_b]` / `[categories.overrides.b_to_a]`.

The source adjustment uses its original category. The recipient gets a category
suggestion and an unapproved transaction to review; missing/ambiguous suggestions
are uncategorized. Transfers, multi-category splits, and Ready-to-Assign income
are skipped.

Leave incomplete splits untouched, keep configuration stable during recovery,
and avoid edits, imports, or overlapping runs while syncing. Failed runs can
be rerun; existing adjustments are checked before creating missing ones.

Settlement accounts are virtual and affect budget availability; manage repayment
affordability yourself. Repay using transfers between real and settlement accounts.
To undo an unsettled split, delete its two generated adjustments and **leave the
original green**. Reconciled or already-settled entries need manual accounting.
