"""Pure accounting and reconciliation. All amounts are integer milliunits."""

import hashlib
import re
from dataclasses import dataclass, replace
from datetime import date


class Conflict(ValueError):
    """Observed financial state requires human review."""


@dataclass(frozen=True)
class Participant:
    plan_id: str
    settlement_account_id: str

    @classmethod
    def from_config(cls, values: dict) -> "Participant":
        return cls(values["plan_id"], values["settlement_account_id"])


@dataclass(frozen=True)
class Category:
    id: str
    group: str
    name: str
    hidden: bool = False
    deleted: bool = False
    internal: bool = False


@dataclass(frozen=True)
class Account:
    id: str
    type: str
    on_budget: bool
    closed: bool = False
    deleted: bool = False
    linked: bool = False


@dataclass(frozen=True)
class Transaction:
    id: str
    date: str
    amount: int
    account_id: str
    category_id: str | None = None
    flag_color: str | None = None
    approved: bool = False
    memo: str | None = None
    import_id: str | None = None
    payee_name: str | None = None
    transfer_account_id: str | None = None
    transfer_transaction_id: str | None = None
    matched_transaction_id: str | None = None
    debt_transaction_type: str | None = None
    deleted: bool = False
    split: bool = False


def memo_identity(memo: str | None) -> tuple[str, str, str] | None:
    match = re.search(
        r"(?:^| )\[splitter-for-ynab\|src=([\w-]+):([\w-]+)"
        r"\|role=(owner|recipient)\]\Z",
        memo or "",
    )
    return match.groups() if match else None


def adjustment_import_id(source_plan: str, source_id: str, role: str) -> str:
    identity = f"{source_plan}:{source_id}:{role}"
    return "SFY:" + hashlib.sha256(identity.encode()).hexdigest()[:32]


@dataclass(frozen=True)
class Adjustment:
    plan_id: str
    account_id: str
    source_plan: str
    source_id: str
    role: str
    date: str
    amount: int
    category_id: str | None
    payee_name: str | None = None
    source_memo: str | None = None

    @property
    def memo(self) -> str:
        marker = (
            f"[splitter-for-ynab|src={self.source_plan}:{self.source_id}"
            f"|role={self.role}]"
        )
        if not self.source_memo:
            return marker
        return f"{self.source_memo[: 500 - len(marker) - 1]} {marker}"

    @property
    def import_id(self) -> str:
        return adjustment_import_id(self.source_plan, self.source_id, self.role)

    def payload(self) -> dict:
        return {
            "account_id": self.account_id,
            "date": self.date,
            "amount": self.amount,
            "category_id": self.category_id,
            "payee_name": self.payee_name,
            "memo": self.memo,
            "import_id": self.import_id,
            "approved": self.role == "owner",
            "cleared": "uncleared",
        }


@dataclass(frozen=True)
class Reconciliation:
    missing: tuple[Adjustment, ...]
    verified: tuple[Adjustment, ...]

    @property
    def complete(self) -> bool:
        return not self.missing


def currency_unit(code: str, digits: int) -> tuple[str, int]:
    supported = {"USD": 2, "EUR": 2, "GBP": 2, "CAD": 2, "AUD": 2, "NZD": 2, "JPY": 0}
    if code not in supported or type(digits) is not int or digits != supported[code]:
        raise ValueError("Unsupported currency or currency precision")
    return code, 10 ** (3 - digits)


@dataclass(frozen=True)
class Flags:
    half: str = "blue"
    full: str = "purple"
    complete: str = "green"

    def __post_init__(self):
        colors = (self.half, self.full, self.complete)
        supported = ("red", "orange", "yellow", "green", "blue", "purple")
        if any(color not in supported for color in colors):
            raise ValueError("Flags must be YNAB color names")
        if len(set(colors)) != 3:
            raise ValueError("Half, full, and completion flags must be distinct")


DEFAULT_FLAGS = Flags()


def recipient_share(
    amount: int, flag: str, unit: int, flags: Flags = DEFAULT_FLAGS
) -> int:
    if type(amount) is not int or type(unit) is not int or unit <= 0 or amount % unit:
        raise ValueError("Amount is not an integer number of spendable currency units")
    if flag == flags.full:
        return amount
    if flag != flags.half:
        raise ValueError("Expected a configured split flag")
    return (1 if amount >= 0 else -1) * (abs(amount) // (2 * unit)) * unit


def categories(items: tuple[Category, ...]) -> tuple[Category, ...]:
    return tuple(
        c
        for c in items
        if not (
            c.deleted or c.hidden or c.internal or c.group == "Credit Card Payments"
        )
    )


def suggest_category(
    source: Category, targets: tuple[Category, ...], overrides: dict[str, str]
) -> str | None:
    if source.id in overrides:
        target = overrides[source.id]
        if target not in {c.id for c in targets}:
            raise ValueError("Category override points to an unavailable category")
        return target
    normalize = str.casefold
    matches = [c for c in targets if normalize(c.name) == normalize(source.name)]
    exact = [c for c in matches if normalize(c.group) == normalize(source.group)]
    candidates = exact or matches
    return candidates[0].id if len(candidates) == 1 else None


def skip_reason(
    tx: Transaction,
    account: Account | None,
    settlement_id: str,
    available: tuple[Category, ...],
    since: date,
    today: date,
    unit: int,
    flags: Flags = DEFAULT_FLAGS,
) -> str | None:
    if tx.flag_color not in (flags.half, flags.full):
        return "Flag is not a configured split flag"
    if tx.deleted:
        return "Deleted transaction"
    if not since <= date.fromisoformat(tx.date) <= today:
        return "Outside processing date window"
    if tx.account_id == settlement_id:
        return "Settlement account transaction"
    if (tx.import_id or "").startswith("SFY:") or memo_identity(tx.memo):
        return "Generated adjustment"
    if (
        not account
        or account.type not in ("checking", "savings", "cash", "creditCard")
        or (not account.on_budget or account.deleted or account.closed)
    ):
        return "Unsupported source account"
    if tx.transfer_account_id or tx.transfer_transaction_id:
        return "Transfer transaction"
    if tx.split:
        return "Multi-category split transaction"
    if tx.debt_transaction_type:
        return "Loan or debt operation"
    if tx.category_id not in {c.id for c in available}:
        return "Unsupported category (including uncategorized or Ready to Assign)"
    if not tx.amount:
        return "Zero amount"
    try:
        share = recipient_share(tx.amount, tx.flag_color, unit, flags)
    except ValueError as error:
        return str(error)
    return None if share else "Recipient allocation rounds to zero"


def expected_adjustments(
    tx: Transaction,
    owner: Participant,
    recipient: Participant,
    unit: int,
    recipient_category: str | None,
    flags: Flags = DEFAULT_FLAGS,
) -> tuple[Adjustment, Adjustment]:
    share = recipient_share(tx.amount, tx.flag_color, unit, flags)
    return tuple(
        Adjustment(
            participant.plan_id,
            participant.settlement_account_id,
            owner.plan_id,
            tx.id,
            role,
            tx.date,
            amount,
            category,
            tx.payee_name,
            tx.memo,
        )
        for participant, role, amount, category in (
            (owner, "owner", -share, tx.category_id),
            (recipient, "recipient", share, recipient_category),
        )
    )


def verify(adjustment: Adjustment, tx: Transaction, *, fresh: bool = False) -> None:
    fields = ("account_id", "date", "amount", "memo", "import_id")
    expected = adjustment.payload()
    if any(getattr(tx, k) != expected[k] for k in fields) or any(
        getattr(tx, k)
        for k in (
            "deleted",
            "matched_transaction_id",
            "transfer_account_id",
            "transfer_transaction_id",
            "split",
            "debt_transaction_type",
        )
    ):
        raise Conflict(
            f"Conflicting {adjustment.role} adjustment for {adjustment.source_id}"
        )
    # Category and approval belong to the recipient after initial creation.
    if (fresh or adjustment.role == "owner") and any(
        getattr(tx, k) != expected[k] for k in ("category_id", "approved")
    ):
        raise Conflict(f"Unexpected category/approval for {adjustment.role} adjustment")


def reconcile(
    expected: tuple[Adjustment, ...], existing: dict[str, tuple[Transaction, ...]]
) -> Reconciliation:
    missing, verified = [], []
    for adjustment in expected:
        matches = [
            tx
            for tx in existing[adjustment.plan_id]
            if tx.import_id == adjustment.import_id
            or memo_identity(tx.memo)
            == (adjustment.source_plan, adjustment.source_id, adjustment.role)
        ]
        if len(matches) > 1:
            raise Conflict(f"Ambiguous {adjustment.role} adjustments")
        if matches:
            verify(adjustment, matches[0])
            verified.append(adjustment)
        else:
            missing.append(adjustment)
    for adjustment in missing:
        if possible_import_match(adjustment, existing[adjustment.plan_id]):
            raise Conflict(
                "Potential match to a manually entered settlement transaction"
            )
    return Reconciliation(tuple(missing), tuple(verified))


def possible_import_match(
    adjustment: Adjustment, history: tuple[Transaction, ...]
) -> bool:
    return any(
        not tx.deleted
        and not tx.import_id
        and tx.amount == adjustment.amount
        and abs(
            (date.fromisoformat(tx.date) - date.fromisoformat(adjustment.date)).days
        )
        <= 10
        for tx in history
    )


def source_fingerprint(tx: Transaction) -> Transaction:
    return replace(tx, approved=False, memo=None)
