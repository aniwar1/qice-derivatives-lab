#!/usr/bin/env python3
"""Archive and independently replay Sina contract-page tick evidence.

An HTTP-body hash is only useful when the bytes being hashed are preserved.
This module therefore keeps a bounded, gzip-compressed copy of the response and
replays the identity-bound table cell before downstream use.  Legacy schema
names containing ``exact`` mean exact replay of what that specific page showed;
they do not turn a third-party page into an exchange-signed specification.
"""

from __future__ import annotations

import base64
import binascii
from decimal import Decimal, InvalidOperation
import gzip
import hashlib
from html.parser import HTMLParser
import re
from typing import Any, Mapping
import zlib

try:
    from scripts.runtime_universe import (
        allowed_products,
        canonical_delivery_contract,
        product_prefix,
        provider_exchange_identity,
        sina_provider_symbol_for_contract,
    )
except ModuleNotFoundError:  # Direct execution puts scripts/ on sys.path.
    from runtime_universe import (
        allowed_products,
        canonical_delivery_contract,
        product_prefix,
        provider_exchange_identity,
        sina_provider_symbol_for_contract,
    )


CONTRACT_DETAIL_URL = "https://finance.sina.com.cn/futures/quotes/{symbol}.shtml"
ARCHIVE_ENCODING = "gzip+base64"
MAX_HTTP_BODY_BYTES = 8 * 1024 * 1024
MAX_COMPRESSED_BYTES = 4 * 1024 * 1024
ARCHIVE_FIELDS = frozenset(
    {
        "response_final_url",
        "response_status_code",
        "response_content_length",
        "response_archive_encoding",
        "response_body_gzip_base64",
    }
)
EXACT_TICK_EVIDENCE_FIELDS = frozenset(
    {
        "kind",
        "contract",
        "provider_symbol",
        "source_url",
        "retrieved_at",
        "content_sha256",
        "hash_scope",
        "locator",
        "raw_item",
        "raw_value",
        "tick_size",
        "exact",
    }
) | ARCHIVE_FIELDS


class TickEvidenceError(ValueError):
    """Raised when archived contract-page tick evidence cannot be replayed."""


def archived_http_response_fields(
    content: bytes,
    *,
    final_url: str,
    status_code: int,
) -> dict[str, Any]:
    """Return deterministic, JSON-safe fields for one complete HTTP body."""

    if not isinstance(content, bytes) or not content:
        raise TickEvidenceError("HTTP response body must be non-empty bytes")
    if len(content) > MAX_HTTP_BODY_BYTES:
        raise TickEvidenceError("HTTP response body exceeds archive size limit")
    if not isinstance(final_url, str) or not final_url:
        raise TickEvidenceError("HTTP response final URL is missing")
    if isinstance(status_code, bool) or not isinstance(status_code, int):
        raise TickEvidenceError("HTTP response status code is invalid")
    compressed = gzip.compress(content, compresslevel=9, mtime=0)
    if len(compressed) > MAX_COMPRESSED_BYTES:
        raise TickEvidenceError("compressed HTTP response exceeds archive size limit")
    return {
        "content_sha256": hashlib.sha256(content).hexdigest(),
        "response_final_url": final_url,
        "response_status_code": status_code,
        "response_content_length": len(content),
        "response_archive_encoding": ARCHIVE_ENCODING,
        "response_body_gzip_base64": base64.b64encode(compressed).decode("ascii"),
    }


def _restore_http_response_body(evidence: Mapping[str, Any]) -> bytes:
    if evidence.get("response_archive_encoding") != ARCHIVE_ENCODING:
        raise TickEvidenceError("HTTP response archive encoding is invalid")
    encoded = evidence.get("response_body_gzip_base64")
    if not isinstance(encoded, str) or not encoded:
        raise TickEvidenceError("HTTP response archive is missing")
    if len(encoded) > ((MAX_COMPRESSED_BYTES + 2) // 3) * 4:
        raise TickEvidenceError("HTTP response archive exceeds compressed size limit")
    try:
        compressed = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise TickEvidenceError("HTTP response archive is not strict base64") from exc
    if not compressed or len(compressed) > MAX_COMPRESSED_BYTES:
        raise TickEvidenceError("HTTP response archive has invalid compressed size")

    inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        content = inflater.decompress(compressed, MAX_HTTP_BODY_BYTES + 1)
    except zlib.error as exc:
        raise TickEvidenceError("HTTP response archive is not valid gzip") from exc
    if (
        len(content) > MAX_HTTP_BODY_BYTES
        or not inflater.eof
        or inflater.unconsumed_tail
        or inflater.unused_data
    ):
        raise TickEvidenceError("HTTP response archive is truncated, concatenated, or oversized")
    try:
        content += inflater.flush(MAX_HTTP_BODY_BYTES + 1 - len(content))
    except zlib.error as exc:
        raise TickEvidenceError("HTTP response archive cannot be finalized") from exc
    if not content or len(content) > MAX_HTTP_BODY_BYTES:
        raise TickEvidenceError("HTTP response body has invalid restored size")
    return content


def _numeric_value(value: Any) -> Decimal | None:
    match = re.search(
        r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)",
        value if isinstance(value, str) else str(value),
    )
    if match is None:
        return None
    try:
        number = Decimal(match.group(0))
    except InvalidOperation:
        return None
    return number if number.is_finite() and number > 0 else None


def _compact_text(parts: list[str]) -> str:
    return re.sub(r"\s+", " ", "".join(parts)).strip()


class _SinaContractPageParser(HTMLParser):
    """Collect only the stable identity anchors and basic-data table.

    Sina's page contains many related-contract symbols and unrelated tables.
    Treating any occurrence of the requested symbol as page identity lets a
    correctly hashed wrong-month page masquerade as the requested contract.
    This parser deliberately ignores those ambient strings.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[dict[str, Any]] = []
        self.titles: list[str] = []
        self.futures_title_codes: list[str] = []
        self.inline_scripts: list[str] = []
        self.basic_tables: list[dict[str, Any]] = []

    @staticmethod
    def _attributes(attrs: list[tuple[str, str | None]]) -> dict[str, str]:
        return {
            str(key).lower(): "" if value is None else str(value)
            for key, value in attrs
        }

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        tag = tag.lower()
        attributes = self._attributes(attrs)
        parent = self.stack[-1] if self.stack else {}
        classes = set(attributes.get("class", "").split())
        inside_futures_title = bool(parent.get("inside_futures_title")) or (
            "futures-title" in classes
        )
        table = parent.get("basic_table")
        if tag == "table" and attributes.get("id") == "table-futures-basic-data":
            table = {"rows": []}
            self.basic_tables.append(table)

        entry: dict[str, Any] = {
            "tag": tag,
            "inside_futures_title": inside_futures_title,
            "basic_table": table,
        }
        if tag == "title":
            entry["title_parts"] = []
        if tag == "span" and "code" in classes and inside_futures_title:
            entry["futures_title_code_parts"] = []
        if tag == "script" and "src" not in attributes:
            entry["inline_script_parts"] = []
        if tag == "tr" and table is not None:
            entry["row_cells"] = []
        if tag in {"th", "td"} and table is not None:
            entry["cell_parts"] = []
        self.stack.append(entry)

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        for key in (
            "cell_parts",
            "futures_title_code_parts",
            "title_parts",
            "inline_script_parts",
        ):
            for entry in reversed(self.stack):
                parts = entry.get(key)
                if parts is not None:
                    parts.append(data)
                    break

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        matching_index = next(
            (
                index
                for index in range(len(self.stack) - 1, -1, -1)
                if self.stack[index]["tag"] == tag
            ),
            None,
        )
        if matching_index is None:
            return
        while len(self.stack) > matching_index:
            entry = self.stack.pop()
            if "cell_parts" in entry:
                cell = (entry["tag"], _compact_text(entry["cell_parts"]))
                for ancestor in reversed(self.stack):
                    if (
                        "row_cells" in ancestor
                        and ancestor.get("basic_table") is entry.get("basic_table")
                    ):
                        ancestor["row_cells"].append(cell)
                        break
            if "row_cells" in entry:
                entry["basic_table"]["rows"].append(entry["row_cells"])
            if "title_parts" in entry:
                self.titles.append(_compact_text(entry["title_parts"]))
            if "futures_title_code_parts" in entry:
                self.futures_title_codes.append(
                    _compact_text(entry["futures_title_code_parts"])
                )
            if "inline_script_parts" in entry:
                self.inline_scripts.append("".join(entry["inline_script_parts"]))


def _unique_basic_table_value(
    table: Mapping[str, Any], label: str
) -> str:
    matches: list[str] = []
    for row in table.get("rows", []):
        for index, cell in enumerate(row[:-1]):
            cell_tag, cell_text = cell
            if cell_tag != "th" or cell_text.rstrip("：:") != label:
                continue
            value_tag, value_text = row[index + 1]
            if value_tag != "td":
                raise TickEvidenceError(
                    f"archived basic-data table {label} value cell is invalid"
                )
            matches.append(value_text)
    if len(matches) != 1 or not matches[0]:
        raise TickEvidenceError(
            f"archived basic-data table {label} is missing or not unique"
        )
    return matches[0]


def _page_contract_identities(
    parser: _SinaContractPageParser,
) -> tuple[str, str, str]:
    if len(parser.titles) != 1:
        raise TickEvidenceError("archived contract page title is missing or not unique")
    title_codes = re.findall(r"\(([A-Z]{1,3}\d{4})\)", parser.titles[0])
    if len(title_codes) != 1:
        raise TickEvidenceError("archived page title contract code is missing or not unique")

    if len(parser.futures_title_codes) != 1:
        raise TickEvidenceError(
            "archived .futures-title span.code is missing or not unique"
        )
    title_span_match = re.fullmatch(
        r"\(\s*([A-Z]{1,3}\d{4})\s*\)", parser.futures_title_codes[0]
    )
    if title_span_match is None:
        raise TickEvidenceError("archived .futures-title span.code is invalid")

    app_codes: list[str] = []
    app_code_pattern = re.compile(
        r"(?<![\w$])futures_app\s*\.\s*code\s*=\s*(['\"])([A-Z]{1,3}\d{4})\1\s*;?"
    )
    for script in parser.inline_scripts:
        app_codes.extend(match.group(2) for match in app_code_pattern.finditer(script))
    if len(app_codes) != 1:
        raise TickEvidenceError("archived inline futures_app.code is missing or not unique")
    return title_codes[0], title_span_match.group(1), app_codes[0]


def extract_contract_tick_from_html(
    content: bytes, *, provider_symbol: str
) -> tuple[str, str, Decimal]:
    """Replay one contract page's identity-bound basic-data tick cell."""

    if not isinstance(content, bytes) or not content:
        raise TickEvidenceError("archived contract detail page is empty")
    expected_symbol = str(provider_symbol).strip().upper()
    try:
        canonical = canonical_delivery_contract(expected_symbol)
    except ValueError as exc:
        raise TickEvidenceError("expected provider contract is not canonical") from exc
    if canonical != expected_symbol:
        raise TickEvidenceError("expected provider contract is not canonical")

    text = content.decode("gb18030", errors="replace")
    parser = _SinaContractPageParser()
    parser.feed(text)
    parser.close()
    identities = _page_contract_identities(parser)
    if identities != (expected_symbol, expected_symbol, expected_symbol):
        raise TickEvidenceError(
            "archived page identity anchors do not match the provider contract"
        )
    if len(parser.basic_tables) != 1:
        raise TickEvidenceError(
            "archived table#table-futures-basic-data is missing or not unique"
        )
    table = parser.basic_tables[0]
    raw_value = _unique_basic_table_value(table, "最小变动价位")
    parsed_tick = _numeric_value(raw_value)
    if parsed_tick is None:
        raise TickEvidenceError("archived contract tick is not a positive number")

    product = product_prefix(expected_symbol)
    if product is None:
        raise TickEvidenceError("archived contract product is invalid")
    table_product = _unique_basic_table_value(table, "交易代码").strip().upper()
    if table_product != product:
        raise TickEvidenceError("archived basic-data table product does not match contract")
    configured_exchange = str(allowed_products()[product]["exchange"]).upper()
    table_exchange = _unique_basic_table_value(table, "上市交易所")
    exchange_identity = provider_exchange_identity(configured_exchange, table_exchange)
    if not exchange_identity.startswith("verified_"):
        raise TickEvidenceError(
            "archived basic-data table exchange does not match configured exchange"
        )
    return "最小变动价位", raw_value, parsed_tick


def replay_exact_tick_evidence(
    evidence: Mapping[str, Any],
    *,
    contract: str,
    expected_tick_size: Decimal,
) -> bytes:
    """Verify provenance and replay the page-displayed product tick cell."""

    if set(evidence) != EXACT_TICK_EVIDENCE_FIELDS:
        raise TickEvidenceError("exact tick evidence fields are incomplete or unexpected")
    provider_symbol = sina_provider_symbol_for_contract(contract)
    source_url = CONTRACT_DETAIL_URL.format(symbol=provider_symbol)
    if evidence.get("kind") != "actual_contract_http_detail":
        raise TickEvidenceError("exact tick evidence kind is invalid")
    if evidence.get("contract") != contract:
        raise TickEvidenceError("exact tick evidence contract is invalid")
    if evidence.get("provider_symbol") != provider_symbol:
        raise TickEvidenceError("exact tick provider symbol is invalid")
    if evidence.get("source_url") != source_url:
        raise TickEvidenceError("exact tick source URL is invalid")
    if evidence.get("response_final_url") != source_url:
        raise TickEvidenceError("exact tick final URL is invalid")
    status_code = evidence.get("response_status_code")
    if isinstance(status_code, bool) or status_code != 200:
        raise TickEvidenceError("exact tick HTTP status is not 200")
    if evidence.get("hash_scope") != "http_response_body":
        raise TickEvidenceError("exact tick hash scope is invalid")
    if evidence.get("locator") != "contract detail table item=最小变动价位":
        raise TickEvidenceError("exact tick locator is invalid")
    if evidence.get("raw_item") != "最小变动价位":
        raise TickEvidenceError("exact tick raw item is invalid")
    if evidence.get("exact") is not True:
        raise TickEvidenceError("exact tick flag is invalid")

    content = _restore_http_response_body(evidence)
    content_length = evidence.get("response_content_length")
    if isinstance(content_length, bool) or content_length != len(content):
        raise TickEvidenceError("exact tick archived content length does not match")
    digest = evidence.get("content_sha256")
    if not isinstance(digest, str) or hashlib.sha256(content).hexdigest() != digest:
        raise TickEvidenceError("exact tick archived content hash does not match")

    label, raw_value, parsed_tick = extract_contract_tick_from_html(
        content, provider_symbol=provider_symbol
    )
    if evidence.get("raw_item") != label or evidence.get("raw_value") != raw_value:
        raise TickEvidenceError("exact tick raw table cell does not match archived bytes")
    try:
        declared_tick = Decimal(str(evidence.get("tick_size")))
    except InvalidOperation as exc:
        raise TickEvidenceError("exact tick declared value is invalid") from exc
    if (
        not declared_tick.is_finite()
        or declared_tick <= 0
        or parsed_tick != declared_tick
        or declared_tick != expected_tick_size
    ):
        raise TickEvidenceError("exact tick value cannot be replayed from archived bytes")
    return content
