"""Official SDK boundary: request safety and immutable domain conversion."""

import time
from collections.abc import Callable

import ynab
from pydantic import ValidationError
from urllib3.exceptions import HTTPError
from urllib3.util.retry import Retry
from ynab.exceptions import ApiTypeError, ApiValueError

from splitter import Account, Category, Transaction


class APIError(RuntimeError):
    """An SDK operation failed; messages never include response/request bodies."""


class Session:
    """Share SDK lifetime, request budget, and read/write safety across API classes."""

    def __init__(self, token: str, *, writes: bool = False):
        configuration = ynab.Configuration(access_token=token)
        # SDK transport retries must not replay an uncertain write or redirect tokens.
        configuration.retries = Retry(total=0, redirect=0)
        self.api = ynab.ApiClient(configuration)
        self.writes = writes
        self.requests = 0
        self.started = time.monotonic()

    def close(self) -> None:
        self.api.rest_client.pool_manager.clear()

    def read[T](self, operation: Callable[..., T], *args, **kwargs) -> T:
        return self._call(operation, args, kwargs, read=True)

    def write[T](self, operation: Callable[..., T], *args, **kwargs) -> T:
        if not self.writes:
            raise APIError("Writes disabled: read-only session")
        return self._call(operation, args, kwargs, read=False)

    def _call(self, operation, args, kwargs, *, read):
        for attempt in range(3 if read else 1):
            if time.monotonic() - self.started >= 3600:
                self.requests, self.started = 0, time.monotonic()
            if self.requests >= 180:
                raise APIError("Local request budget exhausted; rerun after an hour")
            self.requests += 1
            try:
                return operation(*args, **kwargs, _request_timeout=(10.0, 30.0))
            except (
                ynab.ApiException,
                ApiTypeError,
                ApiValueError,
                HTTPError,
                ValidationError,
            ) as error:
                status = getattr(error, "status", 0) or 0
                delay = 2**attempt
                headers = getattr(error, "headers", None) or {}
                try:
                    delay = max(
                        delay,
                        float(headers.get("Retry-After", 0)),
                    )
                except ValueError:
                    pass
                if (
                    read
                    and status in (0, 429, 500, 502, 503, 504)
                    and attempt < 2
                    and delay <= 30
                ):
                    time.sleep(delay)
                    continue
                raise APIError(
                    f"SDK {'read' if read else 'write'} failed (status {status})"
                ) from None


def transaction(value: ynab.TransactionDetail) -> Transaction:
    def optional_id(value):
        return str(value) if value is not None else None

    return Transaction(
        id=value.id,
        date=value.var_date.isoformat(),
        amount=value.amount,
        account_id=str(value.account_id),
        category_id=optional_id(value.category_id),
        flag_color=value.flag_color.value if value.flag_color else None,
        approved=value.approved,
        memo=value.memo,
        import_id=value.import_id,
        payee_name=value.payee_name,
        transfer_account_id=optional_id(value.transfer_account_id),
        transfer_transaction_id=value.transfer_transaction_id,
        matched_transaction_id=value.matched_transaction_id,
        debt_transaction_type=value.debt_transaction_type,
        deleted=value.deleted,
        split=bool(value.subtransactions),
    )


def accounts(values: list[ynab.Account]) -> tuple[Account, ...]:
    return tuple(
        Account(
            str(a.id),
            a.type.value,
            a.on_budget,
            a.closed,
            a.deleted,
            bool(a.direct_import_linked),
        )
        for a in values
    )


def categories(values: list[ynab.CategoryGroupWithCategories]) -> tuple[Category, ...]:
    return tuple(
        Category(
            str(c.id),
            g.name,
            c.name,
            g.hidden or c.hidden,
            g.deleted or c.deleted,
            g.internal or c.internal,
        )
        for g in values
        for c in g.categories
    )
