#!/usr/bin/env python3
"""Replayable identity evidence for Sina's symbol-less DCE daily response.

``InnerFuturesNewService.getDailyKLine`` returns only date/OHLCV/OI/settlement
rows.  Its callback name is chosen by the requester and is not instrument
identity.  A response therefore earns a contract identity only when its
terminal completed bar matches a separately fetched, identity-bearing Sina
direct-quote endpoint for the exact contract across OHLCV and open interest.
This cross-endpoint anchor prevents adapter relabelling; it is still third-party
evidence and is not exchange certification.
"""

from __future__ import annotations

import base64
import binascii
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
import gzip
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any, Mapping
import zlib

import requests

try:
    from scripts.runtime_universe import canonical_delivery_contract
except ModuleNotFoundError:  # Direct execution puts scripts/ on sys.path.
    from runtime_universe import canonical_delivery_contract


DAILY_CALLBACK = "_DCE_DAILY_ARCHIVE"
DAILY_BASE_URL = (
    "https://stock2.finance.sina.com.cn/futures/api/jsonp.php/"
    f"var%20{DAILY_CALLBACK}=/InnerFuturesNewService.getDailyKLine"
)
DAILY_TYPE = "2021_04_12"
QUOTE_URL = "https://hq.sinajs.cn/list=nf_{symbol}"
HEADERS = {
    "User-Agent": "Mozilla/5.0 Chrome/124.0 Safari/537.36",
    "Referer": "https://finance.sina.com.cn/",
}
ARCHIVE_ENCODING = "gzip+base64"
MAX_HTTP_BODY_BYTES = 8 * 1024 * 1024
MAX_COMPRESSED_BYTES = 4 * 1024 * 1024
CONTRACT_EVIDENCE_SCHEMA = "dce_sina_daily_contract_identity.v1"
BUNDLE_SCHEMA = "dce_sina_daily_identity_bundle.v1"
INDEX_SCHEMA = "dce_sina_daily_identity_index.v1"
HTTP_ARCHIVE_FIELDS = frozenset(
    {
        "requested_url",
        "request_method",
        "final_url",
        "status_code",
        "content_length",
        "content_sha256",
        "archive_encoding",
        "body_gzip_base64",
    }
)
CONTRACT_EVIDENCE_FIELDS = frozenset(
    {
        "schema_version",
        "contract",
        "identity_status",
        "daily_response",
        "direct_quote_response",
        "daily_row_count",
        "daily_date_range",
        "daily_rows_sha256",
        "terminal_binding",
    }
)
BUNDLE_FIELDS = frozenset(
    {
        "schema_version",
        "published_dates",
        "entries",
        "coverage_row_count",
        "coverage_sha256",
    }
)
DESCRIPTOR_FIELDS = frozenset(
    {
        "file",
        "sha256",
        "schema_version",
        "published_dates",
        "coverage_row_count",
        "coverage_sha256",
    }
)


class DceDailyIdentityError(RuntimeError):
    """Raised when a symbol-less daily response cannot prove its target."""


def _canonical_contract(value: str) -> str:
    try:
        canonical = canonical_delivery_contract(str(value).strip().upper(), exchange="DCE")
    except ValueError as exc:
        raise DceDailyIdentityError("DCE daily identity contract is invalid") from exc
    return canonical


def _prepared_daily_url(contract: str) -> str:
    prepared = requests.Request(
        "GET",
        DAILY_BASE_URL,
        params={"symbol": contract, "type": DAILY_TYPE},
    ).prepare()
    if not isinstance(prepared.url, str):
        raise DceDailyIdentityError("cannot prepare Sina DCE daily URL")
    return prepared.url


def _archive_response(content: bytes, *, requested_url: str, response: Any) -> dict[str, Any]:
    if not isinstance(content, bytes) or not content:
        raise DceDailyIdentityError("Sina HTTP response body is empty")
    if len(content) > MAX_HTTP_BODY_BYTES:
        raise DceDailyIdentityError("Sina HTTP response body exceeds archive limit")
    final_url = str(getattr(response, "url", "") or "")
    status_code = getattr(response, "status_code", None)
    request = getattr(response, "request", None)
    actual_requested_url = str(getattr(request, "url", "") or "")
    request_method = str(getattr(request, "method", "") or "").upper()
    if actual_requested_url != requested_url or final_url != requested_url:
        raise DceDailyIdentityError(
            "Sina HTTP actual requested/final URL differs from expected URL"
        )
    if request_method != "GET":
        raise DceDailyIdentityError("Sina HTTP request method is not GET")
    if type(status_code) is not int or status_code != 200:
        raise DceDailyIdentityError("Sina HTTP response status is not 200")
    compressed = gzip.compress(content, compresslevel=9, mtime=0)
    if len(compressed) > MAX_COMPRESSED_BYTES:
        raise DceDailyIdentityError("Sina compressed response exceeds archive limit")
    return {
        "requested_url": requested_url,
        "request_method": request_method,
        "final_url": final_url,
        "status_code": status_code,
        "content_length": len(content),
        "content_sha256": hashlib.sha256(content).hexdigest(),
        "archive_encoding": ARCHIVE_ENCODING,
        "body_gzip_base64": base64.b64encode(compressed).decode("ascii"),
    }


def _restore_response(archive: Mapping[str, Any], *, expected_url: str) -> bytes:
    if not isinstance(archive, Mapping) or set(archive) != HTTP_ARCHIVE_FIELDS:
        raise DceDailyIdentityError("Sina HTTP archive fields are incomplete")
    if (
        archive.get("requested_url") != expected_url
        or archive.get("request_method") != "GET"
        or archive.get("final_url") != expected_url
    ):
        raise DceDailyIdentityError("Sina HTTP archive URL binding is invalid")
    if (
        type(archive.get("status_code")) is not int
        or archive.get("status_code") != 200
        or archive.get("archive_encoding") != ARCHIVE_ENCODING
    ):
        raise DceDailyIdentityError("Sina HTTP archive status or encoding is invalid")
    encoded = archive.get("body_gzip_base64")
    if not isinstance(encoded, str) or not encoded:
        raise DceDailyIdentityError("Sina HTTP archive body is missing")
    if len(encoded) > ((MAX_COMPRESSED_BYTES + 2) // 3) * 4:
        raise DceDailyIdentityError("Sina HTTP archive exceeds compressed limit")
    try:
        compressed = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise DceDailyIdentityError("Sina HTTP archive is not strict base64") from exc
    if not compressed or len(compressed) > MAX_COMPRESSED_BYTES:
        raise DceDailyIdentityError("Sina HTTP archive compressed size is invalid")
    inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        content = inflater.decompress(compressed, MAX_HTTP_BODY_BYTES + 1)
    except zlib.error as exc:
        raise DceDailyIdentityError("Sina HTTP archive is not valid gzip") from exc
    if (
        len(content) > MAX_HTTP_BODY_BYTES
        or not inflater.eof
        or inflater.unconsumed_tail
        or inflater.unused_data
    ):
        raise DceDailyIdentityError("Sina HTTP archive is truncated or oversized")
    try:
        content += inflater.flush(MAX_HTTP_BODY_BYTES + 1 - len(content))
    except zlib.error as exc:
        raise DceDailyIdentityError("Sina HTTP archive cannot be finalized") from exc
    if not content or len(content) > MAX_HTTP_BODY_BYTES:
        raise DceDailyIdentityError("restored Sina HTTP body size is invalid")
    declared_length = archive.get("content_length")
    if (
        isinstance(declared_length, bool)
        or not isinstance(declared_length, int)
        or declared_length != len(content)
    ):
        raise DceDailyIdentityError("Sina HTTP archive content length differs")
    digest = archive.get("content_sha256")
    if not isinstance(digest, str) or hashlib.sha256(content).hexdigest() != digest:
        raise DceDailyIdentityError("Sina HTTP archive SHA-256 differs")
    return content


def _decimal_number(value: Any, *, positive: bool) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise DceDailyIdentityError("Sina numeric field is invalid") from exc
    if not parsed.is_finite() or (parsed <= 0 if positive else parsed < 0):
        raise DceDailyIdentityError("Sina numeric field is outside its valid domain")
    return parsed


def _number(value: Any, *, positive: bool) -> float:
    parsed = _decimal_number(value, positive=positive)
    converted = float(parsed)
    if not math.isfinite(converted):
        raise DceDailyIdentityError("Sina numeric field cannot be represented safely")
    return converted


def _valid_iso_date(value: str) -> bool:
    try:
        return date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def _daily_payload(content: bytes) -> list[dict[str, Any]]:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DceDailyIdentityError("Sina daily response is not UTF-8") from exc
    match = re.fullmatch(
        rf"\s*(?:/\*.*?\*/\s*)?var\s+{re.escape(DAILY_CALLBACK)}\s*=\s*\((.*)\);\s*",
        text,
        flags=re.DOTALL,
    )
    if match is None:
        raise DceDailyIdentityError("Sina daily JSONP envelope is invalid")
    try:
        payload = json.loads(
            match.group(1),
            parse_float=Decimal,
            parse_int=Decimal,
        )
    except json.JSONDecodeError as exc:
        raise DceDailyIdentityError("Sina daily JSON payload is invalid") from exc
    if not isinstance(payload, list) or not payload:
        raise DceDailyIdentityError("Sina daily response has no rows")
    return payload


def parse_daily_response(content: bytes) -> list[dict[str, Any]]:
    """Parse rows without treating the caller-controlled callback as identity."""

    payload = _daily_payload(content)
    rows: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict) or set(item) != {
            "d",
            "o",
            "h",
            "l",
            "c",
            "v",
            "p",
            "s",
        }:
            raise DceDailyIdentityError("Sina daily row fields are invalid")
        try:
            trade_day = date.fromisoformat(str(item["d"]))
        except ValueError as exc:
            raise DceDailyIdentityError("Sina daily row date is invalid") from exc
        # The response can contain old zero-volume/settlement-only rows.  They
        # are preserved in the archived series and filtered by the publisher;
        # rejecting the complete response here would discard valid later
        # bars.  Negative or non-finite values remain invalid.
        row = {
            "date": trade_day.isoformat(),
            "open": _number(item["o"], positive=False),
            "high": _number(item["h"], positive=False),
            "low": _number(item["l"], positive=False),
            "close": _number(item["c"], positive=False),
            "volume": _number(item["v"], positive=False),
            "open_interest": _number(item["p"], positive=False),
            "settle": _number(item["s"], positive=False),
        }
        if row["high"] < max(row["open"], row["low"], row["close"]) or row[
            "low"
        ] > min(row["open"], row["high"], row["close"]):
            raise DceDailyIdentityError("Sina daily row OHLC is inconsistent")
        rows.append(row)
    dates = [row["date"] for row in rows]
    if dates != sorted(set(dates)):
        raise DceDailyIdentityError("Sina daily rows are duplicated or not increasing")
    return rows


def _daily_exact_values(content: bytes, *, completed_through: str) -> dict[str, Decimal]:
    """Return terminal source decimals without a binary-float round trip."""

    matches = [
        item
        for item in _daily_payload(content)
        if isinstance(item, dict) and str(item.get("d") or "") == completed_through
    ]
    if len(matches) != 1:
        raise DceDailyIdentityError("Sina daily exact terminal row is not unique")
    item = matches[0]
    source_fields = {
        "open": "o",
        "high": "h",
        "low": "l",
        "close": "c",
        "volume": "v",
        "open_interest": "p",
    }
    return {
        field: _decimal_number(item[source], positive=field != "open_interest")
        for field, source in source_fields.items()
    }


def _direct_quote_fields(content: bytes, *, contract: str) -> tuple[str, list[str]]:
    canonical = _canonical_contract(contract)
    try:
        text = content.decode("gb18030")
    except UnicodeDecodeError as exc:
        raise DceDailyIdentityError("Sina direct quote response encoding is invalid") from exc
    match = re.fullmatch(
        rf'\s*var\s+hq_str_nf_{re.escape(canonical)}\s*=\s*"([^\"]*)";\s*',
        text,
    )
    if match is None:
        raise DceDailyIdentityError("Sina direct quote response does not identify contract")
    fields = match.group(1).split(",")
    if len(fields) < 19:
        raise DceDailyIdentityError("Sina direct quote fields are incomplete")
    return canonical, fields


def parse_direct_quote_response(content: bytes, *, contract: str) -> dict[str, Any]:
    canonical, fields = _direct_quote_fields(content, contract=contract)
    try:
        trade_day = date.fromisoformat(fields[17]).isoformat()
    except ValueError as exc:
        raise DceDailyIdentityError("Sina direct quote date is invalid") from exc
    clock = fields[1]
    if re.fullmatch(r"\d{6}", clock) is None:
        raise DceDailyIdentityError("Sina direct quote clock is invalid")
    try:
        quote_clock = time(
            hour=int(clock[:2]),
            minute=int(clock[2:4]),
            second=int(clock[4:]),
        )
    except ValueError as exc:
        raise DceDailyIdentityError("Sina direct quote clock is invalid") from exc
    return {
        "contract": canonical,
        "date": trade_day,
        "quote_time": f"{trade_day} {quote_clock.isoformat()}",
        "open": _number(fields[2], positive=True),
        "high": _number(fields[3], positive=True),
        "low": _number(fields[4], positive=True),
        "close": _number(fields[8], positive=True),
        "open_interest": _number(fields[13], positive=False),
        "volume": _number(fields[14], positive=True),
    }


def _direct_quote_exact_values(content: bytes, *, contract: str) -> dict[str, Decimal]:
    """Return quote decimals exactly as archived by the identity-bearing endpoint."""

    _, fields = _direct_quote_fields(content, contract=contract)
    positions = {
        "open": 2,
        "high": 3,
        "low": 4,
        "close": 8,
        "open_interest": 13,
        "volume": 14,
    }
    return {
        field: _decimal_number(fields[position], positive=field != "open_interest")
        for field, position in positions.items()
    }


def _canonical_rows_sha256(rows: list[dict[str, Any]]) -> str:
    return hashlib.sha256(
        json.dumps(
            rows,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _terminal_binding(
    rows: list[dict[str, Any]],
    quote: Mapping[str, Any],
    *,
    contract: str,
    completed_through: str,
    daily_exact: Mapping[str, Decimal],
    quote_exact: Mapping[str, Decimal],
) -> dict[str, Any]:
    quote_time_text = str(quote.get("quote_time") or "")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", quote_time_text) is None:
        raise DceDailyIdentityError("Sina direct quote timestamp is invalid")
    try:
        quote_timestamp = datetime.strptime(quote_time_text, "%Y-%m-%d %H:%M:%S")
    except ValueError as exc:
        raise DceDailyIdentityError("Sina direct quote timestamp is invalid") from exc
    if quote_timestamp.date().isoformat() != quote.get("date"):
        raise DceDailyIdentityError("Sina direct quote timestamp/date differs")
    # The direct endpoint rolls into the next night session from 21:00.  It can
    # bind a completed daily bar only after the day close and before that roll;
    # exact values alone must not let a coincidentally unchanged night quote
    # certify the prior daily response.
    if not time(15, 0, 0) <= quote_timestamp.time() < time(21, 0, 0):
        raise DceDailyIdentityError(
            "cross_endpoint_anchor_status=outside_daily_close_crosscheck_window; "
            "Sina direct quote is outside 15:00:00..20:59:59 Beijing time"
        )
    matches = [row for row in rows if row["date"] == completed_through]
    if (
        len(matches) != 1
        or rows[-1]["date"] != completed_through
        or quote.get("date") != completed_through
    ):
        raise DceDailyIdentityError(
            "cross_endpoint_anchor_status=mismatch_terminal_date; "
            "Sina daily response terminal completed date cannot bind direct quote"
        )
    terminal = matches[0]
    fields = ("open", "high", "low", "close", "volume", "open_interest")
    if (
        set(daily_exact) != set(fields)
        or set(quote_exact) != set(fields)
        or any(daily_exact[field] != quote_exact[field] for field in fields)
        or any(
            Decimal(str(terminal[field])) != Decimal(str(quote.get(field)))
            for field in fields
        )
    ):
        raise DceDailyIdentityError(
            "cross_endpoint_anchor_status=mismatch_terminal_ohlcv_oi; "
            "Sina daily response terminal OHLCV/OI differs from direct quote"
        )
    return {
        "method": "identity_bound_direct_quote_full_terminal_bar_match",
        "contract": contract,
        "completed_trade_date": completed_through,
        "direct_quote_time": quote["quote_time"],
        "matched_fields": list(fields),
        "matched_values": {field: terminal[field] for field in fields},
        "matched_exact_decimal_values": {
            field: format(daily_exact[field], "f") for field in fields
        },
    }


def fetch_bound_contract_history(
    contract: str,
    *,
    completed_through: date,
    timeout: int = 20,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Bind a symbol-less response through an identity-bearing Sina endpoint."""

    canonical = _canonical_contract(contract)
    daily_response = requests.get(
        DAILY_BASE_URL,
        params={"symbol": canonical, "type": DAILY_TYPE},
        headers=HEADERS,
        timeout=max(1, timeout),
        allow_redirects=False,
    )
    quote_url = QUOTE_URL.format(symbol=canonical)
    quote_response = requests.get(
        quote_url,
        headers=HEADERS,
        timeout=max(1, timeout),
        allow_redirects=False,
    )
    return bind_contract_http_responses(
        canonical,
        completed_through=completed_through,
        daily_response=daily_response,
        quote_response=quote_response,
    )


def bind_contract_http_responses(
    contract: str,
    *,
    completed_through: date,
    daily_response: Any,
    quote_response: Any,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Archive, parse and cross-bind two already obtained HTTP responses."""

    canonical = _canonical_contract(contract)
    daily_url = _prepared_daily_url(canonical)
    daily_content = bytes(getattr(daily_response, "content", b""))
    daily_archive = _archive_response(
        daily_content, requested_url=daily_url, response=daily_response
    )
    rows = parse_daily_response(daily_content)
    quote_url = QUOTE_URL.format(symbol=canonical)
    quote_content = bytes(getattr(quote_response, "content", b""))
    quote_archive = _archive_response(
        quote_content, requested_url=quote_url, response=quote_response
    )
    quote = parse_direct_quote_response(quote_content, contract=canonical)
    cutoff = completed_through.isoformat()
    binding = _terminal_binding(
        rows,
        quote,
        contract=canonical,
        completed_through=cutoff,
        daily_exact=_daily_exact_values(daily_content, completed_through=cutoff),
        quote_exact=_direct_quote_exact_values(quote_content, contract=canonical),
    )
    evidence = {
        "schema_version": CONTRACT_EVIDENCE_SCHEMA,
        "contract": canonical,
        "identity_status": "verified_by_direct_quote_terminal_bar",
        "daily_response": daily_archive,
        "direct_quote_response": quote_archive,
        "daily_row_count": len(rows),
        "daily_date_range": {"start": rows[0]["date"], "end": rows[-1]["date"]},
        "daily_rows_sha256": _canonical_rows_sha256(rows),
        "terminal_binding": binding,
    }
    replay_contract_evidence(evidence)
    return rows, evidence


def replay_contract_evidence(evidence: Mapping[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(evidence, Mapping) or set(evidence) != CONTRACT_EVIDENCE_FIELDS:
        raise DceDailyIdentityError("DCE contract identity evidence fields are invalid")
    if evidence.get("schema_version") != CONTRACT_EVIDENCE_SCHEMA or evidence.get(
        "identity_status"
    ) != "verified_by_direct_quote_terminal_bar":
        raise DceDailyIdentityError("DCE contract identity evidence schema/status is invalid")
    contract = _canonical_contract(str(evidence.get("contract") or ""))
    daily_content = _restore_response(
        evidence.get("daily_response", {}), expected_url=_prepared_daily_url(contract)
    )
    rows = parse_daily_response(daily_content)
    quote_url = QUOTE_URL.format(symbol=contract)
    quote_content = _restore_response(
        evidence.get("direct_quote_response", {}), expected_url=quote_url
    )
    quote = parse_direct_quote_response(quote_content, contract=contract)
    date_range = {"start": rows[0]["date"], "end": rows[-1]["date"]}
    declared_row_count = evidence.get("daily_row_count")
    if (
        isinstance(declared_row_count, bool)
        or not isinstance(declared_row_count, int)
        or declared_row_count != len(rows)
        or evidence.get("daily_date_range") != date_range
    ):
        raise DceDailyIdentityError("DCE daily response row count/range differs")
    if evidence.get("daily_rows_sha256") != _canonical_rows_sha256(rows):
        raise DceDailyIdentityError("DCE daily response parsed-row SHA-256 differs")
    binding = evidence.get("terminal_binding")
    if not isinstance(binding, dict):
        raise DceDailyIdentityError("DCE terminal binding is missing")
    regenerated = _terminal_binding(
        rows,
        quote,
        contract=contract,
        completed_through=str(binding.get("completed_trade_date") or ""),
        daily_exact=_daily_exact_values(
            daily_content,
            completed_through=str(binding.get("completed_trade_date") or ""),
        ),
        quote_exact=_direct_quote_exact_values(quote_content, contract=contract),
    )
    if binding != regenerated:
        raise DceDailyIdentityError("DCE terminal binding differs from archived responses")
    return rows


def _coverage_records(
    entries: list[Mapping[str, Any]], published_dates: list[str]
) -> list[dict[str, Any]]:
    wanted = set(published_dates)
    records: list[dict[str, Any]] = []
    for entry in entries:
        contract = str(entry["contract"])
        for row in replay_contract_evidence(entry):
            if row["date"] in wanted and _publishable_daily_row(row):
                records.append({"contract": contract, **row})
    records.sort(key=lambda item: (item["date"], item["contract"]))
    if len(records) != len({(item["date"], item["contract"]) for item in records}):
        raise DceDailyIdentityError("DCE identity bundle has duplicate contract-date rows")
    return records


def _publishable_daily_row(row: Mapping[str, Any]) -> bool:
    """Mirror the cache's numeric bar gate without trusting caller labels."""

    prices = [Decimal(str(row[field])) for field in ("open", "high", "low", "close")]
    settle = Decimal(str(row["settle"]))
    volume = Decimal(str(row["volume"]))
    open_interest = Decimal(str(row["open_interest"]))
    if any(value <= 0 for value in [*prices, settle, volume]) or open_interest < 0:
        return False
    open_price, high, low, close = prices
    return high >= max(open_price, low, close) and low <= min(
        open_price, high, close
    )


def build_identity_bundle(
    entries: list[Mapping[str, Any]], *, published_dates: list[date]
) -> dict[str, Any]:
    ordered_entries = sorted(
        (dict(item) for item in entries), key=lambda item: item["contract"]
    )
    contracts = [str(item.get("contract") or "") for item in ordered_entries]
    if not contracts or contracts != sorted(set(contracts)):
        raise DceDailyIdentityError("DCE identity bundle contracts are empty or duplicated")
    dates = sorted({item.isoformat() for item in published_dates})
    if not dates:
        raise DceDailyIdentityError("DCE identity bundle published dates are empty")
    records = _coverage_records(ordered_entries, dates)
    if not records:
        raise DceDailyIdentityError("DCE identity bundle covers no rows")
    return {
        "schema_version": BUNDLE_SCHEMA,
        "published_dates": dates,
        "entries": ordered_entries,
        "coverage_row_count": len(records),
        "coverage_sha256": _canonical_rows_sha256(records),
    }


def replay_identity_bundle(bundle: Mapping[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    if (
        not isinstance(bundle, Mapping)
        or set(bundle) != BUNDLE_FIELDS
        or bundle.get("schema_version") != BUNDLE_SCHEMA
    ):
        raise DceDailyIdentityError("DCE identity bundle schema/fields are invalid")
    dates = bundle.get("published_dates")
    entries = bundle.get("entries")
    if (
        not isinstance(dates, list)
        or not dates
        or dates != sorted(set(dates))
        or not all(
            isinstance(value, str)
            and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value)
            and _valid_iso_date(value)
            for value in dates
        )
        or not isinstance(entries, list)
        or not entries
    ):
        raise DceDailyIdentityError("DCE identity bundle dates/entries are invalid")
    contracts = [
        str(item.get("contract") or "")
        for item in entries
        if isinstance(item, dict)
    ]
    if len(contracts) != len(entries) or contracts != sorted(set(contracts)):
        raise DceDailyIdentityError("DCE identity bundle contracts are invalid")
    records = _coverage_records(entries, dates)
    declared_coverage_count = bundle.get("coverage_row_count")
    if (
        isinstance(declared_coverage_count, bool)
        or not isinstance(declared_coverage_count, int)
        or declared_coverage_count != len(records)
        or bundle.get("coverage_sha256") != _canonical_rows_sha256(records)
    ):
        raise DceDailyIdentityError("DCE identity bundle coverage differs")
    return {(item["contract"], item["date"]): item for item in records}


def verify_bundle_covers_cache_rows(
    bundle: Mapping[str, Any], rows: list[Mapping[str, Any]]
) -> dict[tuple[str, str], dict[str, Any]]:
    """Require exact cache rows; never let caller labels replace response data."""

    replayed = replay_identity_bundle(bundle)
    verify_identity_rows_cover_cache_rows(
        replayed,
        rows,
        require_exact_key_set=True,
    )
    return replayed


def verify_identity_rows_cover_cache_rows(
    identity_rows: Mapping[tuple[str, str], Mapping[str, Any]],
    rows: list[Mapping[str, Any]],
    *,
    require_exact_key_set: bool = False,
) -> None:
    """Compare cache values with replayed source rows, field for field."""

    expected: dict[tuple[str, str], dict[str, Any]] = {}
    fields = (
        "open",
        "high",
        "low",
        "close",
        "volume",
        "open_interest",
        "settle",
    )
    for row in rows:
        if row.get("exchange") != "DCE":
            raise DceDailyIdentityError("DCE identity cache row exchange is invalid")
        contract = _canonical_contract(str(row.get("contract") or ""))
        try:
            trade_day = date.fromisoformat(str(row.get("date") or "")).isoformat()
        except ValueError as exc:
            raise DceDailyIdentityError("DCE identity cache row date is invalid") from exc
        record = {
            "contract": contract,
            "date": trade_day,
            **{
                field: _number(
                    row.get(field),
                    positive=field not in {"open_interest"},
                )
                for field in fields
            },
        }
        key = (contract, trade_day)
        if key in expected:
            raise DceDailyIdentityError("DCE identity cache rows contain a duplicate key")
        expected[key] = record
    if not expected:
        raise DceDailyIdentityError("DCE identity cache row selection is empty")
    missing_or_different = [
        key for key, row in expected.items() if identity_rows.get(key) != row
    ]
    extra = set(identity_rows) - set(expected) if require_exact_key_set else set()
    if missing_or_different or extra:
        raise DceDailyIdentityError(
            "DCE identity bundle does not exactly cover publishable cache rows"
        )


def descriptor_for_bundle(*, file: str, sha256: str, bundle: Mapping[str, Any]) -> dict[str, Any]:
    if (
        not isinstance(file, str)
        or Path(file).name != file
        or not isinstance(sha256, str)
        or file != f"dce_daily_identity.sha256-{sha256}.json"
        or re.fullmatch(r"[0-9a-f]{64}", sha256) is None
    ):
        raise DceDailyIdentityError("DCE identity artifact descriptor path/hash is invalid")
    replay_identity_bundle(bundle)
    return {
        "file": file,
        "sha256": sha256,
        "schema_version": BUNDLE_SCHEMA,
        "published_dates": bundle["published_dates"],
        "coverage_row_count": bundle["coverage_row_count"],
        "coverage_sha256": bundle["coverage_sha256"],
    }


def load_manifest_identity_rows(
    manifest: Mapping[str, Any], *, artifact_directory: Path
) -> tuple[dict[tuple[str, str], dict[str, Any]], str]:
    """Return replayed verified rows, separating absent legacy evidence."""

    index = manifest.get("dce_source_identity")
    if index is None:
        return {}, "unverified_legacy_no_response_identity_artifact"
    if not isinstance(index, dict) or set(index) != {
        "schema_version",
        "legacy_unverified_through",
        "evidence_objects",
    } or index.get("schema_version") != INDEX_SCHEMA:
        raise DceDailyIdentityError("DCE source identity index is invalid")
    legacy_boundary = index.get("legacy_unverified_through")
    if (
        not isinstance(legacy_boundary, str)
        or re.fullmatch(r"\d{4}-\d{2}-\d{2}", legacy_boundary) is None
        or not _valid_iso_date(legacy_boundary)
    ):
        raise DceDailyIdentityError("DCE legacy identity boundary is invalid")
    descriptors = index.get("evidence_objects")
    if not isinstance(descriptors, list):
        raise DceDailyIdentityError("DCE identity evidence object list is invalid")
    rows: dict[tuple[str, str], dict[str, Any]] = {}
    seen_files: set[str] = set()
    seen_dates: set[str] = set()
    for descriptor in descriptors:
        if not isinstance(descriptor, dict) or set(descriptor) != DESCRIPTOR_FIELDS:
            raise DceDailyIdentityError("DCE identity artifact descriptor is invalid")
        filename = descriptor.get("file")
        digest = descriptor.get("sha256")
        if (
            not isinstance(filename, str)
            or Path(filename).name != filename
            or not filename.endswith(".json")
            or filename in seen_files
            or not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        ):
            raise DceDailyIdentityError("DCE identity artifact descriptor path/hash is invalid")
        seen_files.add(filename)
        path = artifact_directory / filename
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise DceDailyIdentityError("DCE identity artifact cannot be read") from exc
        if hashlib.sha256(content).hexdigest() != digest:
            raise DceDailyIdentityError("DCE identity artifact SHA-256 differs")
        try:
            bundle = json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DceDailyIdentityError("DCE identity artifact JSON is invalid") from exc
        if not isinstance(bundle, dict):
            raise DceDailyIdentityError("DCE identity artifact must contain an object")
        expected_descriptor = descriptor_for_bundle(
            file=filename, sha256=digest, bundle=bundle
        )
        if descriptor != expected_descriptor:
            raise DceDailyIdentityError("DCE identity artifact descriptor differs from bundle")
        published_dates = set(descriptor["published_dates"])
        if (
            published_dates & seen_dates
            or any(value <= legacy_boundary for value in published_dates)
        ):
            raise DceDailyIdentityError(
                "DCE identity artifact dates overlap or cross the legacy boundary"
            )
        seen_dates.update(published_dates)
        for key, row in replay_identity_bundle(bundle).items():
            if key in rows:
                raise DceDailyIdentityError("DCE identity artifacts duplicate one row")
            rows[key] = row
    return rows, (
        "verified_response_archives_available"
        if rows
        else "unverified_legacy_no_response_identity_artifact"
    )


__all__ = [
    "BUNDLE_SCHEMA",
    "CONTRACT_EVIDENCE_SCHEMA",
    "DAILY_BASE_URL",
    "DAILY_CALLBACK",
    "DAILY_TYPE",
    "DceDailyIdentityError",
    "INDEX_SCHEMA",
    "QUOTE_URL",
    "bind_contract_http_responses",
    "build_identity_bundle",
    "descriptor_for_bundle",
    "fetch_bound_contract_history",
    "load_manifest_identity_rows",
    "parse_daily_response",
    "parse_direct_quote_response",
    "replay_contract_evidence",
    "replay_identity_bundle",
    "verify_bundle_covers_cache_rows",
    "verify_identity_rows_cover_cache_rows",
]
