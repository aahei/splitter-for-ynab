"""Configuration, discovery, execution, and reporting."""

import argparse
import json
import os
import sys
import tomllib
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from uuid import UUID

import ynab

import ynab_io
from splitter import (
    DEFAULT_FLAGS,
    Conflict,
    Flags,
    Participant,
    Reconciliation,
    Transaction,
    categories,
    currency_unit,
    expected_adjustments,
    reconcile,
    skip_reason,
    source_fingerprint,
    suggest_category,
    verify,
)
from ynab_io import APIError, Session

FULL_HISTORY = "0001-01-01"
COUNTERS = (
    "scanned",
    "pending",
    "successful",
    "recovered",
    "skipped",
    "conflicts",
    "api_errors",
)


def validate_config(config: dict) -> dict:
    for section in (
        "participants",
        "sync",
        "categories",
        "flags",
    ):
        if section in config and not isinstance(config[section], dict):
            raise ValueError(f"{section} must be a TOML table")
    participants = config.get("participants", {})
    if set(participants) != {"a", "b"}:
        raise ValueError("Configure exactly two participants: a and b")
    for name, participant in participants.items():
        if not isinstance(participant, dict):
            raise ValueError(f"{name} must be a participant table")
        for key in ("plan_id", "settlement_account_id"):
            if not isinstance(participant.get(key), str):
                raise ValueError(f"Missing or invalid {name}.{key}")
            if str(UUID(participant[key])) != participant[key]:
                raise ValueError(f"Use a canonical UUID for {name}.{key}")
    if len({p["plan_id"] for p in participants.values()}) != 2:
        raise ValueError("Participants must use separate plans")
    settings = {"lookback_days": 14, **config.get("sync", {})}
    days = settings["lookback_days"]
    if type(days) is not int or not 1 <= days <= 36500:
        raise ValueError("lookback_days must be a positive integer, at most 36500")
    overrides = config.get("categories", {}).get("overrides", {})
    directions = {"a_to_b", "b_to_a"}
    if not isinstance(overrides, dict) or any(
        direction not in directions or not isinstance(mapping, dict)
        for direction, mapping in overrides.items()
    ):
        raise ValueError("Category overrides must be directional mapping tables")
    return {
        **config,
        "sync": settings,
        "flags": vars(Flags(**config.get("flags", {}))),
        "categories": {"overrides": {"a_to_b": {}, "b_to_a": {}, **overrides}},
    }


def fetch_transactions(
    client: Session, plan_id: str, since: str, account_id: str | None = None
) -> tuple[Transaction, ...]:
    api = ynab.TransactionsApi(client.api)
    operation = api.get_transactions_by_account if account_id else api.get_transactions
    args = (plan_id, account_id) if account_id else (plan_id,)
    response = client.read(operation, *args, since_date=date.fromisoformat(since))
    return tuple(ynab_io.transaction(tx) for tx in response.data.transactions)


def fetch_transaction(client: Session, plan_id: str, txid: str) -> Transaction:
    response = client.read(
        ynab.TransactionsApi(client.api).get_transaction_by_id, plan_id, txid
    )
    return ynab_io.transaction(response.data.transaction)


def history(client: Session, participant: Participant) -> tuple[Transaction, ...]:
    return fetch_transactions(
        client, participant.plan_id, FULL_HISTORY, participant.settlement_account_id
    )


def execute(
    source: Transaction,
    owner: Participant,
    participants: dict,
    clients: dict,
    reconciliation: Reconciliation,
    flags: Flags = DEFAULT_FLAGS,
) -> None:
    # Discovery remains valid during the run: callers guarantee exclusive access.
    for adjustment in reconciliation.missing:
        client = clients[adjustment.plan_id]
        participant = participants[adjustment.plan_id]
        try:
            result = client.write(
                ynab.TransactionsApi(client.api).create_transaction,
                adjustment.plan_id,
                ynab.PostTransactionsWrapper(
                    transaction=ynab.NewTransaction(**adjustment.payload())
                ),
            )
            if result.data.transaction is None:
                raise APIError("Creation response has no transaction")
            returned = ynab_io.transaction(result.data.transaction)
        except APIError:
            # A timeout or 409 can conceal a successful write. Never retry POST.
            recovered = history(client, participant)
            if reconcile((adjustment,), {adjustment.plan_id: recovered}).missing:
                raise
        else:
            verify(adjustment, returned, fresh=True)
            readback = fetch_transaction(client, adjustment.plan_id, returned.id)
            verify(adjustment, readback, fresh=True)

    client, pid = clients[owner.plan_id], owner.plan_id
    completed_source = replace(source, flag_color=flags.complete)
    try:
        client.write(
            ynab.TransactionsApi(client.api).update_transaction,
            pid,
            source.id,
            ynab.PutTransactionWrapper(
                transaction=ynab.ExistingTransaction(
                    flag_color=ynab.TransactionFlagColor(flags.complete)
                )
            ),
        )
    except APIError:
        # A flag write can also succeed before the response is lost.
        if source_fingerprint(
            fetch_transaction(client, pid, source.id)
        ) != source_fingerprint(completed_source):
            raise
    final = fetch_transaction(client, pid, source.id)
    if source_fingerprint(final) != source_fingerprint(completed_source):
        raise Conflict("Source flag update could not be verified")


def sync(
    config: dict,
    clients: dict[str, Session],
    since: date,
    *,
    dry_run: bool,
    today: date | None = None,
    summary: dict[str, int | bool] | None = None,
) -> dict[str, int | bool]:
    config = validate_config(config)
    participants = {
        name: Participant.from_config(values)
        for name, values in config["participants"].items()
    }
    flags = Flags(**config["flags"])
    today = today or date.today()
    summary = summary if summary is not None else dict.fromkeys(COUNTERS, 0)
    by_plan = {p.plan_id: p for p in participants.values()}
    pending = []
    for name, owner in participants.items():
        transactions = fetch_transactions(
            clients[owner.plan_id], owner.plan_id, since.isoformat()
        )
        summary["scanned"] += len(transactions)
        pending.extend(
            (name, owner, tx)
            for tx in transactions
            if not tx.deleted and tx.flag_color in (flags.half, flags.full)
        )
        summary["pending"] = len(pending)
    currencies = []
    for pid in by_plan:
        client = clients[pid]
        response = client.read(ynab.PlansApi(client.api).get_plan_settings_by_id, pid)
        currency = response.data.settings.currency_format
        currencies.append(currency_unit(currency.iso_code, currency.decimal_digits))
    if currencies[0] != currencies[1]:
        raise ValueError("Both plans must use the same currency and precision")
    account_maps, category_maps = {}, {}
    for pid, participant in by_plan.items():
        client = clients[pid]
        response = client.read(ynab.AccountsApi(client.api).get_accounts, pid)
        accounts = {a.id: a for a in ynab_io.accounts(response.data.accounts)}
        settlement = accounts.get(participant.settlement_account_id)
        if (
            not settlement
            or settlement.type not in ("checking", "cash")
            or not settlement.on_budget
            or settlement.closed
            or settlement.deleted
            or settlement.linked
        ):
            raise ValueError(
                "Settlement account must be open, unlinked, on-budget cash/checking"
            )
        account_maps[pid] = accounts
        if not pending:
            continue
        response = client.read(ynab.CategoriesApi(client.api).get_categories, pid)
        category_maps[pid] = categories(
            ynab_io.categories(response.data.category_groups)
        )
    unit = currencies[0][1]
    observed = None
    for name, owner, source in pending:
        pid, txid = owner.plan_id, source.id
        recipient_name = next(n for n in participants if n != name)
        recipient = participants[recipient_name]
        reason = skip_reason(
            source,
            account_maps[pid].get(source.account_id),
            owner.settlement_account_id,
            category_maps[pid],
            since,
            today,
            unit,
            flags,
        )
        if reason:
            summary["skipped"] += 1
            print(f"SKIP {name}/{txid}: {reason}")
            continue
        try:
            source_category = next(
                c for c in category_maps[pid] if c.id == source.category_id
            )
            overrides = config["categories"]["overrides"][f"{name}_to_{recipient_name}"]
            category = suggest_category(
                source_category, category_maps[recipient.plan_id], overrides
            )
            expected = expected_adjustments(
                source, owner, recipient, unit, category, flags
            )
            if observed is None or not dry_run:
                observed = {
                    p: history(clients[p], participant)
                    for p, participant in by_plan.items()
                }
            reconciliation = reconcile(expected, observed)
            roles = ", ".join(a.role for a in reconciliation.missing) or "none"
            print(
                f"{'PLAN' if dry_run else 'PROCESS'} {name}/{txid}: "
                f"create {roles}; then {flags.complete}"
            )
            if dry_run:
                continue
            execute(source, owner, by_plan, clients, reconciliation, flags)
            summary["successful"] += 1
            summary["recovered"] += bool(reconciliation.verified)
        except (ValueError, APIError) as error:
            counter = "api_errors" if isinstance(error, APIError) else "conflicts"
            label = (
                "CONFLICT" if isinstance(error, Conflict) else "CONFIGURATION CONFLICT"
            )
            if isinstance(error, APIError):
                label = "API ERROR"
            summary[counter] += 1
            print(f"{label} {name}/{txid}: {error}")
    if not dry_run and not (summary["conflicts"] or summary["api_errors"]):
        summary["balances_match"] = check_balances(config, clients)
    return summary


def check_balances(config: dict, clients: dict) -> bool:
    total = 0
    for participant in config["participants"].values():
        pid = participant["plan_id"]
        client = clients[pid]
        response = client.read(
            ynab.AccountsApi(client.api).get_account_by_id,
            pid,
            participant["settlement_account_id"],
        )
        total += response.data.account.balance
    return total == 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", type=Path, default=Path(__file__).with_name("config.toml")
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    clients, pooled = {}, {}
    summary = dict.fromkeys(COUNTERS, 0)
    status = 1
    try:
        with args.config.open("rb") as stream:
            config = tomllib.load(stream)
        config = validate_config(config)
        for name, participant in config["participants"].items():
            token = os.environ.get(f"YNAB_TOKEN_{name.upper()}")
            if not token:
                raise ValueError(f"Missing YNAB_TOKEN_{name.upper()}")
            if token not in pooled:
                pooled[token] = Session(token, writes=not args.dry_run)
            clients[participant["plan_id"]] = pooled[token]
        since = date.today() - timedelta(days=config["sync"]["lookback_days"] - 1)
        sync(config, clients, since, dry_run=args.dry_run, summary=summary)
        status = int(
            bool(
                summary["conflicts"]
                or summary["api_errors"]
                or not summary.get("balances_match", True)
            )
        )
    except (OSError, ValueError, KeyError, TypeError, APIError) as error:
        if isinstance(error, APIError):
            summary["api_errors"] += 1
        print(f"ERROR: {error}", file=sys.stderr)
    finally:
        for client in pooled.values():
            client.close()
        print(json.dumps(summary, sort_keys=True))
        if path := os.environ.get("GITHUB_STEP_SUMMARY"):
            report = "\n".join(f"- {key}: {value}" for key, value in summary.items())
            mode_name = "dry run" if args.dry_run else "sync"
            with Path(path).open("a") as stream:
                stream.write(
                    f"## Splitter for YNAB {mode_name}\n\n"
                    f"{report}\n\nExit status: {status}\n"
                )
    return status


if __name__ == "__main__":
    raise SystemExit(main())
