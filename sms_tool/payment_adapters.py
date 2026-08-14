"""Typed adapter seam for protocol payment extraction."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol

from .payment_contracts import PaymentRequest, PaymentResult


class PaymentAdapter(Protocol):
    key: str
    methods: tuple[str, ...]

    def run(self, request: PaymentRequest) -> PaymentResult: ...


@dataclass(frozen=True)
class FunctionPaymentAdapter:
    key: str
    methods: tuple[str, ...]
    runner: Callable[..., Mapping[str, Any] | PaymentResult]

    def run(self, request: PaymentRequest) -> PaymentResult:
        raw = self.runner(
            access_token=request.access_token,
            proxy=request.proxy,
            auth_context=dict(request.auth_context),
            payment_method=request.payment_method,
            runtime_config=dict(request.runtime_config),
            **dict(request.options),
        )
        if isinstance(raw, PaymentResult):
            return raw
        return PaymentResult.from_mapping(raw, payment_method=request.payment_method)

    def run_mapping(self, request: PaymentRequest) -> Mapping[str, Any]:
        raw = self.runner(
            access_token=request.access_token,
            proxy=request.proxy,
            auth_context=dict(request.auth_context),
            payment_method=request.payment_method,
            runtime_config=dict(request.runtime_config),
            **dict(request.options),
        )
        return raw.to_dict() if isinstance(raw, PaymentResult) else dict(raw or {})


class PaymentAdapterRegistry:
    def __init__(self) -> None:
        self._by_method: dict[str, PaymentAdapter] = {}

    def register(self, adapter: PaymentAdapter) -> None:
        for method in adapter.methods:
            if method in self._by_method and self._by_method[method] is not adapter:
                raise ValueError(f"payment adapter already registered for {method}")
            self._by_method[method] = adapter

    def get(self, method: str) -> PaymentAdapter:
        try:
            return self._by_method[method]
        except KeyError as exc:
            raise KeyError(f"no payment adapter registered for {method}") from exc

    def methods(self) -> tuple[str, ...]:
        return tuple(self._by_method)

    def execute(self, request: PaymentRequest) -> PaymentResult:
        if not request.access_token.strip():
            raise ValueError("access_token is required")
        return self.get(request.payment_method).run(request)

    def execute_mapping(self, request: PaymentRequest) -> Mapping[str, Any]:
        if not request.access_token.strip():
            raise ValueError("access_token is required")
        adapter = self.get(request.payment_method)
        if isinstance(adapter, FunctionPaymentAdapter):
            return adapter.run_mapping(request)
        return adapter.run(request).to_dict()

    def validate_methods(self, expected: set[str]) -> None:
        registered = set(self._by_method)
        missing = expected - registered
        extra = registered - expected
        if missing or extra:
            details = []
            if missing:
                details.append(f"missing={','.join(sorted(missing))}")
            if extra:
                details.append(f"extra={','.join(sorted(extra))}")
            raise ValueError(f"payment adapter registry mismatch: {'; '.join(details)}")
