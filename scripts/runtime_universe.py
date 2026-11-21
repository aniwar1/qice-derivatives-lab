"""Apply the user's positive product scope before runtime market requests."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
import re
from typing import Any


UNIVERSE_PATH = Path(__file__).resolve().parents[1] / "config/trading_universe.json"
PROVIDER_EXCHANGE_ALIASES = {
    "DCE": {"DCE", "大连商品交易所", "大商所"},
    "SHFE": {"SHFE", "上海期货交易所", "上期所"},
    "CZCE": {"CZCE", "郑州商品交易所", "郑商所"},
    "GFEX": {"GFEX", "广州期货交易所", "广期所"},
    "INE": {"INE", "上海国际能源交易中心", "能源中心", "SHFE"},
}


def _declared_product_count(payload: dict[str, Any]) -> int:
    value = payload.get("product_count")
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("允许品种配置缺少有效 product_count")
    return value


def configured_product_count() -> int:
    """Return the explicit count bound to the current universe file."""
    payload = json.loads(UNIVERSE_PATH.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("允许品种配置顶层必须是对象")
    return _declared_product_count(payload)


def allowed_products() -> dict[str, dict[str, str]]:
    """Read the current permission table; a missing or invalid table fails closed."""
    payload = json.loads(UNIVERSE_PATH.read_text(encoding="utf-8"))
    expected_count = _declared_product_count(payload)
    scope = payload["permission_scope"]
    permitted_exchanges = set(scope["allowed_exchanges"])
    products: dict[str, dict[str, str]] = {}
    nodes: set[str] = set()
    for item in scope["allowed_products"]:
        symbol, exchange, node = item["symbol"], item["exchange"], item["sina_node"]
        if (
            not re.fullmatch(r"[A-Z]{1,3}", symbol)
            or exchange not in permitted_exchanges
            or not node
            or symbol in products
            or node in nodes
        ):
            raise ValueError("允许品种配置无效")
        products[symbol] = dict(item)
        nodes.add(node)
    if len(products) != expected_count:
        raise ValueError(
            "允许品种配置必须恰好包含"
            f"{expected_count}个唯一品种；实际为{len(products)}个"
        )
    return products


def allowed_product_symbols() -> tuple[str, ...]:
    """Return the one centrally validated current product set."""
    return tuple(sorted(allowed_products()))


def require_exact_allowed_products(
    observed: list[str] | tuple[str, ...] | set[str], *, label: str
) -> tuple[str, ...]:
    """Fail closed unless ``observed`` is exactly the configured product set."""
    normalized = [str(value).strip().upper() for value in observed]
    duplicates = sorted(
        {value for value in normalized if normalized.count(value) > 1}
    )
    expected = allowed_product_symbols()
    expected_count = len(expected)
    missing = sorted(set(expected) - set(normalized))
    unexpected = sorted(set(normalized) - set(expected))
    if duplicates or missing or unexpected or len(normalized) != len(expected):
        details = []
        if missing:
            details.append("missing=" + ",".join(missing))
        if unexpected:
            details.append("unexpected=" + ",".join(unexpected))
        if duplicates:
            details.append("duplicates=" + ",".join(duplicates))
        raise ValueError(
            f"{label}必须与当前{expected_count}品种范围完全一致"
            + ("；" + "；".join(details) if details else "")
        )
    return expected


def product_prefix(value: str) -> str | None:
    symbol = str(value).strip().upper().removeprefix("NF_")
    match = re.fullmatch(r"([A-Z]{1,3})(?:\d{1,4})?", symbol)
    return match[1] if match else None


def canonical_delivery_contract(
    value: str,
    *,
    exchange: str | None = None,
    reference_date: date | None = None,
    allow_czce_three_digit: bool = False,
) -> str:
    """Validate a concrete delivery month and return canonical ``PRODUCTYYMM``.

    Provider-native CZCE symbols may carry one year digit.  They are accepted
    only at an explicitly marked provider boundary with a reference trading
    date, then expanded immediately; every internal artifact uses four digits.
    Continuous/index aliases (0/1/88/888/8888/9999) never enter this path.
    """

    normalized = str(value).strip().upper().removeprefix("NF_")
    matched = re.fullmatch(r"([A-Z]{1,3})(\d{3,4})", normalized)
    if matched is None:
        raise ValueError("actual delivery contract must use PRODUCTYYMM")
    product, digits = matched.groups()
    configured = allowed_products().get(product)
    if configured is None:
        raise ValueError("delivery contract product is outside the allowed universe")
    configured_exchange = str(configured["exchange"]).upper()
    if exchange is not None and str(exchange).upper() != configured_exchange:
        raise ValueError("delivery contract exchange conflicts with the universe")
    if digits in {"000", "001", "088", "888", "0000", "0001", "0088", "0888", "8888", "9999"}:
        raise ValueError("continuous or synthetic contract aliases are not delivery months")

    if len(digits) == 3:
        if (
            configured_exchange != "CZCE"
            or not allow_czce_three_digit
            or reference_date is None
        ):
            raise ValueError("three-digit CZCE code is allowed only at a dated provider boundary")
        year_digit, month = int(digits[0]), int(digits[1:])
        if not 1 <= month <= 12:
            raise ValueError("delivery contract month must be 01 through 12")
        candidates = []
        for year in range(reference_date.year, reference_date.year + 5):
            distance_months = (
                year * 12
                + month
                - (reference_date.year * 12 + reference_date.month)
            )
            if year % 10 == year_digit and 0 <= distance_months <= 48:
                candidates.append(year)
        if len(candidates) != 1:
            raise ValueError("three-digit CZCE delivery year cannot be resolved uniquely")
        digits = f"{candidates[0] % 100:02d}{month:02d}"

    month = int(digits[-2:])
    if not 1 <= month <= 12:
        raise ValueError("delivery contract month must be 01 through 12")
    if reference_date is not None:
        delivery_year = 2000 + int(digits[:2])
        distance_months = (
            delivery_year * 12
            + month
            - (reference_date.year * 12 + reference_date.month)
        )
        if distance_months < 0 or distance_months > 48:
            raise ValueError(
                "delivery contract is outside the provider-observable 0..48 month horizon"
            )
    return f"{product}{digits}"


def sina_provider_symbol_for_contract(value: str) -> str:
    """Return Sina's current unambiguous four-digit delivery symbol.

    Sina's quote, detail, and daily endpoints currently accept canonical CZCE
    ``PRODUCTYYMM`` symbols.  Historical three-digit CZCE codes are an inbound
    compatibility concern only: callers must expand them with an explicit
    observation date through :func:`canonical_delivery_contract` and must never
    emit them by guessing a decade at this outbound boundary.
    """

    canonical = canonical_delivery_contract(value)
    return canonical


def provider_exchange_identity(
    configured_exchange: str, provider_exchange: Any
) -> str:
    """Classify raw provider exchange identity without replacing it by config."""

    configured = str(configured_exchange).strip().upper()
    if configured not in PROVIDER_EXCHANGE_ALIASES:
        return "configured_exchange_invalid"
    if provider_exchange is None:
        return "provider_exchange_unobservable"
    observed = str(provider_exchange).strip().upper()
    if not observed:
        return "provider_exchange_unobservable"
    if observed == configured:
        return "verified_exact"
    if configured == "INE" and observed == "SHFE":
        return "verified_known_ine_under_shfe_alias"
    if observed in {value.upper() for value in PROVIDER_EXCHANGE_ALIASES[configured]}:
        return "verified_named_alias"
    return "provider_exchange_conflict"


def is_allowed_product(value: str, exchange: str | None = None) -> bool:
    product = allowed_products().get(product_prefix(value))
    return bool(
        product
        and (exchange is None or product["exchange"] == str(exchange).upper())
    )


def require_allowed_product(value: str, exchange: str | None = None) -> str:
    prefix = product_prefix(value)
    if prefix is None or not is_allowed_product(value, exchange):
        raise ValueError("品种不在当前允许范围；请求未发送")
    return prefix


def filter_sina_catalog(catalog: dict[str, Any]) -> list[tuple[str, str, str]]:
    """Resolve permitted provider nodes before requesting any contract chain.

    Sina groups energy-center nodes under Shanghai in this public catalog.
    Their exchange identity remains the configured energy-center exchange.
    """
    by_node = {item["sina_node"]: item for item in allowed_products().values()}
    eligible: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for provider_exchange, entries in catalog.items():
        if not isinstance(entries, list):
            continue
        for entry in entries[1:]:
            if not isinstance(entry, (list, tuple)) or len(entry) < 2:
                continue
            name, node = entry[:2]
            product = by_node.get(node)
            if product is None or node in seen:
                continue
            exchange = product["exchange"]
            source_exchange = str(provider_exchange).upper()
            if source_exchange != exchange and not (
                exchange == "INE" and source_exchange == "SHFE"
            ):
                continue
            eligible.append((exchange.lower(), str(name), node))
            seen.add(node)
    return eligible
