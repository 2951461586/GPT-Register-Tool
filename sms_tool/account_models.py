"""Typed account/session boundary used by persistence and workflow results."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping

from .sanitizer import sanitize


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else MappingProxyType({})


def _text(value: Any) -> str:
    return "" if value is None else str(value)


@dataclass(frozen=True)
class SessionCredentials:
    access_token: str = field(default="", repr=False)
    refresh_token: str = field(default="", repr=False)
    oauth_refresh_token: str = field(default="", repr=False)
    id_token: str = field(default="", repr=False)
    session_token: str = field(default="", repr=False)
    cookie_header: str = field(default="", repr=False)
    totp_secret: str = field(default="", repr=False)


@dataclass(frozen=True)
class MailboxSnapshot:
    email: str = ""
    provider: str = ""
    source: str = ""
    token: str = field(default="", repr=False)
    purchase_id: str = ""
    project_name: str = ""
    price: str = ""
    purchase_total_cost: str = ""
    balance_after: str = ""


@dataclass(frozen=True)
class PaymentSnapshot:
    method: str = ""
    ok: bool = False
    status: str = ""
    url: str = field(default="", repr=False)
    cs_id: str = ""
    pm_id: str = field(default="", repr=False)
    currency: str = ""
    amount_due: int = 0
    has_paypal: bool = False
    error: str = ""


@dataclass(frozen=True)
class AccountSessionModel:
    email: str
    success: bool | None
    status: str
    error: str
    source: str = ""
    register_method: str = "unknown"
    session_type: str = "unknown"
    plan_type: str = "unknown"
    password: str = field(default="", repr=False)
    device_id: str = ""
    credentials: SessionCredentials = field(default_factory=SessionCredentials, repr=False)
    mailbox: MailboxSnapshot = field(default_factory=MailboxSnapshot, repr=False)
    payment: PaymentSnapshot = field(default_factory=PaymentSnapshot, repr=False)
    auth_session: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}), repr=False)
    timing: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    pipeline_timing: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    quota: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    workspace: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    _raw: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}), repr=False)

    @classmethod
    def from_value(cls, value: "AccountSessionModel | Mapping[str, Any]") -> "AccountSessionModel":
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("account/session payload must be a mapping or AccountSessionModel")
        mailbox = _mapping(value.get("mailbox"))
        purchase = _mapping(value.get("purchase"))
        payment = _mapping(value.get("paypal") or value.get("payment"))
        auth_session = _mapping(value.get("auth_session"))
        success = value.get("success") if "success" in value else None
        credentials = SessionCredentials(
            access_token=_text(value.get("access_token")),
            refresh_token=_text(value.get("refresh_token")),
            oauth_refresh_token=_text(value.get("oauth_refresh_token")),
            id_token=_text(value.get("id_token")),
            session_token=_text(value.get("session_token")),
            cookie_header=_text(value.get("cookie_header")),
            totp_secret=_text(value.get("totp_secret")),
        )
        mailbox_model = MailboxSnapshot(
            email=_text(mailbox.get("email")),
            provider=_text(mailbox.get("provider") or purchase.get("provider")),
            source=_text(mailbox.get("source") or purchase.get("source")),
            token=_text(mailbox.get("token")),
            purchase_id=_text(mailbox.get("purchase_id") or purchase.get("purchase_id")),
            project_name=_text(mailbox.get("project_name") or purchase.get("project_name")),
            price=_text(mailbox.get("price") or purchase.get("price")),
            purchase_total_cost=_text(mailbox.get("purchase_total_cost") or purchase.get("total_cost")),
            balance_after=_text(mailbox.get("balance_after") or purchase.get("balance_after")),
        )
        try:
            amount_due = int(payment.get("amount_due") or payment.get("due") or 0)
        except (TypeError, ValueError):
            amount_due = 0
        payment_model = PaymentSnapshot(
            method=_text(value.get("payment_method") or payment.get("payment_method") or payment.get("method")),
            ok=bool(payment.get("ok")),
            status=_text(value.get("paypal_status") or payment.get("status")),
            url=_text(payment.get("url")),
            cs_id=_text(payment.get("cs_id")),
            pm_id=_text(payment.get("pm_id")),
            currency=_text(payment.get("currency")),
            amount_due=amount_due,
            has_paypal=bool(payment.get("has_paypal")),
            error=_text(payment.get("error")),
        )
        return cls(
            email=_text(value.get("email") or mailbox_model.email),
            success=bool(success) if success is not None else None,
            status=_text(value.get("status")),
            error=_text(value.get("error")),
            source=_text(value.get("source") or value.get("account_source")),
            register_method=_text(value.get("register_method") or "unknown").lower(),
            session_type=_text(value.get("session_type") or "unknown").lower(),
            plan_type=_text(value.get("plan_type") or value.get("account_type") or "unknown").lower(),
            password=_text(value.get("password")),
            device_id=_text(value.get("device_id")),
            credentials=credentials,
            mailbox=mailbox_model,
            payment=payment_model,
            auth_session=MappingProxyType(dict(auth_session)),
            timing=MappingProxyType(dict(_mapping(value.get("timing")))),
            pipeline_timing=MappingProxyType(dict(_mapping(value.get("pipeline_timing")))),
            quota=MappingProxyType(dict(_mapping(value.get("quota")))),
            workspace=MappingProxyType(dict(_mapping(value.get("workspace_scan")))),
            _raw=MappingProxyType(dict(value)),
        )

    def to_storage_mapping(self) -> Mapping[str, Any]:
        """Return a detached compatibility mapping only at the repository seam."""
        value = dict(self._raw)
        value.update({
            "email": self.email,
            "success": self.success,
            "status": self.status,
            "error": self.error,
            "source": self.source,
            "register_method": self.register_method,
            "session_type": self.session_type,
            "plan_type": self.plan_type,
            "password": self.password,
            "device_id": self.device_id,
            "access_token": self.credentials.access_token,
            "refresh_token": self.credentials.refresh_token,
            "oauth_refresh_token": self.credentials.oauth_refresh_token,
            "id_token": self.credentials.id_token,
            "session_token": self.credentials.session_token,
            "cookie_header": self.credentials.cookie_header,
            "totp_secret": self.credentials.totp_secret,
        })
        return value

    def safe_snapshot(self) -> dict[str, Any]:
        """Return the canonical token-free representation allowed in raw JSON/audits."""
        value = {
            "email": self.email,
            "success": self.success,
            "status": self.status,
            "error": self.error,
            "device_id": self.device_id,
            "source": self.source,
            "register_method": self.register_method,
            "session_type": self.session_type,
            "plan_type": self.plan_type,
            "mailbox": {
                "email": self.mailbox.email,
                "provider": self.mailbox.provider,
                "source": self.mailbox.source,
                "purchase_id": self.mailbox.purchase_id,
                "project_name": self.mailbox.project_name,
                "price": self.mailbox.price,
                "purchase_total_cost": self.mailbox.purchase_total_cost,
                "balance_after": self.mailbox.balance_after,
            },
            "payment": {
                "method": self.payment.method,
                "ok": self.payment.ok,
                "status": self.payment.status,
                "cs_id": self.payment.cs_id,
                "currency": self.payment.currency,
                "amount_due": self.payment.amount_due,
                "has_paypal": self.payment.has_paypal,
                "error": self.payment.error,
            },
            "timing": dict(self.timing),
            "pipeline_timing": dict(self.pipeline_timing),
            "quota": dict(self.quota),
            "workspace_scan": dict(self.workspace),
        }
        return sanitize(value)
