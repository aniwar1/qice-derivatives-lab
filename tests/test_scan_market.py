from __future__ import annotations

import hashlib
import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from runtime_universe import (  # noqa: E402
    allowed_products,
    allowed_product_symbols,
    sina_provider_symbol_for_contract,
)
from scan_market import (  # noqa: E402
    SCHEMA_VERSION,
    _default_fetcher,
    _recompute_actual_main,
    build_scan_manifest,
    canonical_json_bytes,
    canonical_sha256,
    write_json_atomic,
)
from scripts import futures_main_report  # noqa: E402
from tick_evidence import archived_http_response_fields  # noqa: E402


PRODUCT_TICKS = {
    str(item["symbol"]).upper(): item["tick_size"]
    for item in json.loads(
        (ROOT / "config/product_contract_specs.json").read_text(encoding="utf-8")
    )["products"]
}
EXCHANGE_DISPLAY_NAMES = {
    "DCE": "大连商品交易所",
    "SHFE": "上海期货交易所",
    "CZCE": "郑州商品交易所",
    "GFEX": "广州期货交易所",
    "INE": "上海国际能源交易中心",
}


def fake_fetch(symbol: str) -> dict:
    contract = f"{symbol}2701"
    tick_size = PRODUCT_TICKS[symbol]
    exchange = allowed_products()[symbol]["exchange"]
    provider_symbol = sina_provider_symbol_for_contract(contract)
    source_url = (
        "https://finance.sina.com.cn/futures/quotes/"
        f"{provider_symbol}.shtml"
    )
    tick_body = (
        "<html><head>"
        f"<title>fixture({provider_symbol})期货行情_新浪财经</title>"
        "</head><body><script>var futures_app = {};"
        f'futures_app.code = "{provider_symbol}";'
        "</script>"
        '<div class="futures-title"><h1 class="title">fixture</h1>'
        f'<span class="code">({provider_symbol})</span></div>'
        '<table id="table-futures-basic-data"><tbody>'
        f"<tr><th>最小变动价位</th><td>{tick_size}</td></tr>"
        f"<tr><th>交易代码</th><td>{symbol}</td>"
        f"<th>上市交易所</th><td>{EXCHANGE_DISPLAY_NAMES[exchange]}</td></tr>"
        "</tbody></table></body></html>"
    ).encode("gb18030")
    chain = [
        {
            "symbol": contract,
            "open_interest": 1000,
            "volume": 500,
            "bid": 100,
            "ask": 101,
            "quote_time": "2026-09-13 09:00:00",
            "provider_quote_time": "2026-09-13 09:00:00",
            "quote_time_normalization": "provider_calendar_time_unchanged",
            "exchange": exchange,
            "provider_exchange": exchange.lower(),
            "exchange_identity_status": "verified_exact",
        },
        {
            "symbol": f"{symbol}2703",
            "open_interest": 500,
            "volume": 300,
            "bid": 100,
            "ask": 101,
            "quote_time": "2026-09-13 09:00:00",
            "provider_quote_time": "2026-09-13 09:00:00",
            "quote_time_normalization": "provider_calendar_time_unchanged",
            "exchange": exchange,
            "provider_exchange": exchange.lower(),
            "exchange_identity_status": "verified_exact",
        },
    ]
    return {
        "success": True,
        "requested_symbol": symbol,
        "request_started_at": "2026-09-13T08:59:59+08:00",
        "fetch_time": "2026-09-13T09:00:00+08:00",
        "main_contract": {"symbol": contract},
        "quote": {"quote_time": "2026-09-13 09:00:00"},
        "actual_contract_chain": chain,
        "main_selection": {
            "source_record_count": 2,
            "available_contract_count": 2,
            "rankable_contract_count": 2,
            "chain_complete_for_product": True,
            "pagination_complete": True,
            "selection_claim": "verified_actual_main",
            "main_liquidity_status": "observable_trade",
            "actual_contract_chain_sha256": canonical_sha256(chain),
            "chain_time_evidence": {
                "status": "same_snapshot",
                "min_observed_at": "2026-09-13T09:00:00+08:00",
                "max_observed_at": "2026-09-13T09:00:00+08:00",
                "span_seconds": 0.0,
                "invalid_time_contracts": [],
                "future_time_contracts": [],
            },
        },
        "technical": {
            "symbol": contract,
            "tick_size": tick_size,
            "tick_size_contract": contract,
            "tick_size_source": "fixture actual-contract specification",
            "tick_size_exact": True,
            "tick_size_precision": "exact_actual_contract",
            "tick_size_evidence": {
                "kind": "actual_contract_http_detail",
                "contract": contract,
                "provider_symbol": provider_symbol,
                "source_url": source_url,
                "retrieved_at": "2026-09-13T09:00:00+08:00",
                **archived_http_response_fields(
                    tick_body,
                    final_url=source_url,
                    status_code=200,
                ),
                "hash_scope": "http_response_body",
                "locator": "contract detail table item=最小变动价位",
                "raw_item": "最小变动价位",
                "raw_value": str(tick_size),
                "tick_size": tick_size,
                "exact": True,
            },
            "daily_data_evidence": {"status": "verified"},
            "completed_daily_rows": [
                {
                    "date": day,
                    "open": close * tick_size,
                    "high": (close + 2) * tick_size,
                    "low": (close - 2) * tick_size,
                    "close": close * tick_size,
                    "volume": 100 + index,
                    "hold": 1000 + index,
                    "completed": True,
                }
                for index, (day, close) in enumerate(
                    zip(
                        (
                            "2026-08-29", "2026-08-30", "2026-08-31",
                            "2026-09-01", "2026-09-02", "2026-09-03",
                            "2026-09-04", "2026-09-05", "2026-09-06",
                            "2026-09-07", "2026-09-08", "2026-09-09",
                            "2026-09-10", "2026-09-11",
                        ),
                        (100, 104, 108, 103, 98, 102, 107, 101, 96, 100, 106, 102, 99, 105),
                    )
                )
            ],
        },
    }


def fake_analyzer(raw: dict) -> dict:
    from scripts.market_structure import _compact_fetch_structure

    return _compact_fetch_structure(raw)


def replay_fixture_daily_evidence(technical: dict, *, contract: str) -> dict:
    """Isolate scanner unit tests from the published configured-product cache.

    Production always uses ``verified_daily_loader``.  These tests exercise the
    scanner's status/identity derivations with synthetic bars, while dedicated
    loader tests exercise the real manifest, gzip and slice replay boundary.
    """

    evidence = technical.get("daily_data_evidence")
    if not isinstance(evidence, dict) or evidence.get("status") != "verified":
        raise ValueError("fixture daily evidence is not verified")
    if technical.get("symbol") != contract:
        raise ValueError("fixture daily evidence contract mismatch")
    return evidence


class ScanMarketTests(unittest.TestCase):
    def setUp(self):
        # ``scan_market`` is imported both as a top-level script module and as a
        # package module by the report replay path; patch both explicit test
        # seams so a passing unit fixture never creates a production bypass.
        for target in (
            "scan_market.replay_verified_daily_evidence",
            "scripts.scan_market.replay_verified_daily_evidence",
        ):
            patcher = patch(target, side_effect=replay_fixture_daily_evidence)
            patcher.start()
            self.addCleanup(patcher.stop)

    def build(self, fetcher=fake_fetch, analyzer=fake_analyzer):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        manifest = build_scan_manifest(
            fetcher=fetcher,
            analyzer=analyzer,
            evidence_dir=root / "evidence",
            manifest_dir=root,
            generated_at="2026-09-13T09:00:00+08:00",
        )
        return root, manifest

    def test_default_fetcher_enforces_a_whole_product_process_deadline(self):
        fetcher = _default_fetcher(timeout=3, limit=12)
        with patch(
            "scan_market.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["fetch"], timeout=5),
        ):
            with self.assertRaisesRegex(TimeoutError, "3-second product deadline"):
                fetcher("M")

    def test_default_fetcher_uses_configured_research_tick_policy(self):
        fetcher = _default_fetcher(timeout=3, limit=12)
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout='{"success": true}', stderr=""
        )
        with patch("scan_market.subprocess.run", return_value=completed) as runner:
            self.assertEqual(fetcher("M"), {"success": True})

        command = runner.call_args.args[0]
        policy_index = command.index("--tick-evidence-policy")
        self.assertEqual(command[policy_index + 1], "configured_research")

    def test_actual_main_contracts_are_bounded_to_fetch_time_delivery_horizon(self):
        forged = fake_fetch("CU")
        forged["actual_contract_chain"][0]["symbol"] = "CU9912"
        forged["actual_contract_chain"][1]["symbol"] = "CU9911"
        forged["main_selection"]["actual_contract_chain_sha256"] = canonical_sha256(
            forged["actual_contract_chain"]
        )
        leader, status, gaps = _recompute_actual_main(forged, "CU")
        self.assertIsNone(leader)
        self.assertEqual(status, "unobservable")
        self.assertIn("actual_main_chain_contract_invalid:0", gaps)
        self.assertIn("actual_main_chain_contract_invalid:1", gaps)

        boundary = fake_fetch("CU")
        boundary["actual_contract_chain"][0]["symbol"] = "CU3009"
        boundary["main_selection"]["actual_contract_chain_sha256"] = canonical_sha256(
            boundary["actual_contract_chain"]
        )
        leader, status, gaps = _recompute_actual_main(boundary, "CU")
        self.assertEqual((leader, status, gaps), ("CU3009", "complete", []))

    def test_weekend_night_timestamp_cannot_masquerade_as_calendar_normalization(self):
        raw = fake_fetch("M")
        raw["quote"]["quote_time"] = "2026-09-12 21:00:00"
        for row in raw["actual_contract_chain"]:
            row["provider_quote_time"] = "2026-09-14 21:00:00"
            row["quote_time"] = "2026-09-12 21:00:00"
            row["quote_time_normalization"] = (
                "derived_calendar_time_from_direct_quote_for_next_trade_day_night_session"
            )
        raw["main_selection"]["actual_contract_chain_sha256"] = canonical_sha256(
            raw["actual_contract_chain"]
        )
        raw["main_selection"]["chain_time_evidence"] = {
            "status": "same_snapshot",
            "min_observed_at": "2026-09-12T21:00:00+08:00",
            "max_observed_at": "2026-09-12T21:00:00+08:00",
            "span_seconds": 0.0,
            "invalid_time_contracts": [],
            "future_time_contracts": [],
        }

        leader, status, gaps = _recompute_actual_main(raw, "M")

        self.assertEqual(leader, "M2701")
        self.assertEqual(status, "partial")
        self.assertTrue(
            any("time_normalization_conflict" in gap for gap in gaps),
            gaps,
        )

    def test_scan_replays_product_specific_night_close(self):
        for product, clock, expected_status in (
            ("M", "02:00:00", "partial"),
            ("CU", "00:30:00", "complete"),
            ("CU", "01:30:00", "partial"),
            ("CU", "02:00:00", "partial"),
        ):
            with self.subTest(product=product, clock=clock):
                raw = fake_fetch(product)
                raw["quote"]["quote_time"] = f"2026-09-12 {clock}"
                for row in raw["actual_contract_chain"]:
                    row["provider_quote_time"] = f"2026-09-14 {clock}"
                    row["quote_time"] = f"2026-09-12 {clock}"
                    row["quote_time_normalization"] = (
                        "derived_calendar_time_from_direct_quote_for_next_trade_day_night_session"
                    )
                raw["main_selection"]["actual_contract_chain_sha256"] = canonical_sha256(
                    raw["actual_contract_chain"]
                )
                raw["main_selection"]["chain_time_evidence"] = {
                    "status": "same_snapshot",
                    "min_observed_at": f"2026-09-12T{clock}+08:00",
                    "max_observed_at": f"2026-09-12T{clock}+08:00",
                    "span_seconds": 0.0,
                    "invalid_time_contracts": [],
                    "future_time_contracts": [],
                }

                _, status, gaps = _recompute_actual_main(raw, product)

                self.assertEqual(status, expected_status, gaps)

    def test_unchanged_provider_clock_must_match_same_request_direct_quote(self):
        raw = fake_fetch("M")
        raw["quote"]["quote_time"] = "2026-09-11 15:00:00"
        for row in raw["actual_contract_chain"]:
            row["provider_quote_time"] = "2026-09-11 10:00:00"
            row["quote_time"] = "2026-09-11 10:00:00"
            row["quote_time_normalization"] = "provider_calendar_time_unchanged"
        raw["main_selection"]["actual_contract_chain_sha256"] = canonical_sha256(
            raw["actual_contract_chain"]
        )
        raw["main_selection"]["chain_time_evidence"] = {
            "status": "same_snapshot",
            "min_observed_at": "2026-09-11T10:00:00+08:00",
            "max_observed_at": "2026-09-11T10:00:00+08:00",
            "span_seconds": 0.0,
            "invalid_time_contracts": [],
            "future_time_contracts": [],
        }

        _, status, gaps = _recompute_actual_main(raw, "M")

        self.assertEqual(status, "partial")
        self.assertTrue(
            any("time_normalization_conflict" in gap for gap in gaps), gaps
        )

    def test_complete_manifest_has_each_allowed_product_exactly_once(self):
        root, manifest = self.build()
        expected = list(allowed_product_symbols())
        expected_count = len(expected)

        self.assertEqual(manifest["schema_version"], SCHEMA_VERSION)
        self.assertEqual(manifest["expected_count"], expected_count)
        self.assertEqual(manifest["attempted_count"], expected_count)
        self.assertEqual(manifest["successful_count"], expected_count)
        self.assertEqual(manifest["partial_count"], 0)
        self.assertEqual(manifest["unobservable_count"], 0)
        self.assertEqual(manifest["expected_symbols"], expected)
        self.assertEqual(manifest["attempted_symbols"], expected)
        self.assertEqual(manifest["successful_symbols"], expected)
        self.assertEqual(manifest["partial_symbols"], [])
        self.assertEqual(manifest["unobservable_symbols"], [])
        self.assertEqual(
            len({item["symbol"] for item in manifest["products"]}), expected_count
        )

        for item in manifest["products"]:
            self.assertEqual(item["scan_status"], "success")
            self.assertEqual(item["actual_main_contract"], f"{item['symbol']}2701")
            self.assertEqual(item["analysis_contract"], item["actual_main_contract"])
            self.assertEqual(item["as_of"], "2026-09-11")
            for path_key, hash_key in (
                ("raw_snapshot_path", "raw_snapshot_sha256"),
                ("structure_snapshot_path", "structure_snapshot_sha256"),
            ):
                evidence_path = root / item[path_key]
                self.assertTrue(evidence_path.is_file())
                self.assertEqual(
                    hashlib.sha256(evidence_path.read_bytes()).hexdigest(),
                    item[hash_key],
                )
            structure_artifact = json.loads(
                (root / item["structure_snapshot_path"]).read_text(encoding="utf-8")
            )
            self.assertEqual(
                structure_artifact["input_snapshot_sha256"],
                item["raw_snapshot_sha256"],
            )
            self.assertEqual(
                item["structure_input_snapshot_sha256"],
                item["raw_snapshot_sha256"],
            )
            self.assertEqual(
                item["structure_schema_version"],
                "cn_futures_market_structure.v1",
            )
            self.assertEqual(
                item["structure_algorithm_version"],
                "causal_pivots_zones_theil_sen_tick_aligned.v1",
            )

    def test_new_scan_artifacts_cannot_invalidate_last_atomic_manifest(self):
        root, first = self.build()
        manifest_path = root / "latest-scan.json"
        write_json_atomic(manifest_path, first)
        old_artifacts = {
            row[path_key]: row[hash_key]
            for row in first["products"]
            for path_key, hash_key in (
                ("raw_snapshot_path", "raw_snapshot_sha256"),
                ("structure_snapshot_path", "structure_snapshot_sha256"),
            )
        }

        def changed_fetch(symbol: str) -> dict:
            raw = fake_fetch(symbol)
            raw["warnings"] = ["second batch bytes"]
            return raw

        second = build_scan_manifest(
            fetcher=changed_fetch,
            analyzer=fake_analyzer,
            evidence_dir=root / "evidence",
            manifest_dir=root,
            generated_at="2026-09-13T09:00:30+08:00",
        )

        self.assertEqual(json.loads(manifest_path.read_text(encoding="utf-8")), first)
        self.assertNotEqual(
            second["products"][0]["raw_snapshot_path"],
            first["products"][0]["raw_snapshot_path"],
        )
        for relative_path, digest in old_artifacts.items():
            content = (root / relative_path).read_bytes()
            self.assertEqual(hashlib.sha256(content).hexdigest(), digest)

        focus = first["products"][0]
        result = futures_main_report._validate_scan_manifest_v3(
            {
                "scan_scope": "all_market",
                "scan_manifest": first,
                "scan_manifest_sha256": canonical_sha256(first),
            },
            datetime.fromisoformat("2026-09-13T09:01:00+08:00"),
            focus["symbol"],
            focus["analysis_contract"],
            focus["actual_main_contract"],
            policy_sha256=hashlib.sha256(
                (ROOT / "config/futures_main_report_policy.json").read_bytes()
            ).hexdigest(),
            artifact_root=root,
        )
        self.assertEqual(result["artifact_hashes_verified"], 2 * len(first["products"]))

    def test_failures_remain_one_row_and_partition_success_from_unobservable(self):
        symbols = list(allowed_product_symbols())
        fetch_failure = symbols[0]
        structure_failure = symbols[1]

        def fetcher(symbol: str) -> dict:
            if symbol == fetch_failure:
                raise RuntimeError("provider offline")
            return fake_fetch(symbol)

        def analyzer(raw: dict) -> dict:
            if raw["requested_symbol"] == structure_failure:
                return {"success": False, "error": "not enough completed bars"}
            return fake_analyzer(raw)

        _, manifest = self.build(fetcher, analyzer)
        rows = {item["symbol"]: item for item in manifest["products"]}

        self.assertEqual(manifest["attempted_count"], len(symbols))
        self.assertEqual(manifest["successful_count"], len(symbols) - 2)
        self.assertEqual(manifest["partial_count"], 0)
        self.assertEqual(manifest["unobservable_count"], 2)
        self.assertEqual(
            sorted(manifest["successful_symbols"] + manifest["unobservable_symbols"]),
            symbols,
        )
        self.assertEqual(rows[fetch_failure]["data_status"], "unobservable")
        self.assertEqual(rows[structure_failure]["structure_status"], "unobservable")
        self.assertTrue(rows[fetch_failure]["raw_snapshot_sha256"])
        self.assertTrue(rows[structure_failure]["structure_snapshot_sha256"])

    def test_product_scan_rejects_specific_contract_override(self):
        target = allowed_product_symbols()[0]

        def fetcher(symbol: str) -> dict:
            result = fake_fetch(symbol)
            if symbol == target:
                result["actual_main"] = {"symbol": f"{symbol}2701"}
                result["analysis_contract"] = {"symbol": f"{symbol}2611"}
                result["analysis_mode"] = "requested_specific_contract"
            return result

        _, manifest = self.build(fetcher, fake_analyzer)
        row = next(item for item in manifest["products"] if item["symbol"] == target)
        self.assertEqual(row["actual_main_contract"], f"{target}2701")
        self.assertEqual(row["analysis_contract"], f"{target}2611")
        self.assertEqual(row["scan_status"], "unobservable")
        self.assertIn(
            "analysis_contract_not_actual_main_for_product_scan:"
            f"actual={target}2701:analysis={target}2611",
            row["evidence_gaps"],
        )
        self.assertTrue(
            any(
                gap.startswith("structure_failed:ValueError:technical.symbol")
                for gap in row["evidence_gaps"]
            )
        )

    def test_conflicting_analyzer_input_hash_is_not_silently_overwritten(self):
        target = allowed_product_symbols()[0]

        def analyzer(raw: dict) -> dict:
            result = fake_analyzer(raw)
            if raw["requested_symbol"] == target:
                result["input_snapshot_sha256"] = "0" * 64
            return result

        root, manifest = self.build(fake_fetch, analyzer)
        row = next(item for item in manifest["products"] if item["symbol"] == target)
        artifact = json.loads(
            (root / row["structure_snapshot_path"]).read_text(encoding="utf-8")
        )

        self.assertEqual(row["scan_status"], "unobservable")
        self.assertIn("structure_input_snapshot_sha256_conflict", row["evidence_gaps"])
        self.assertEqual(artifact["input_snapshot_sha256"], row["raw_snapshot_sha256"])
        self.assertEqual(
            artifact["analyzer_declared_input_snapshot_sha256"], "0" * 64
        )

    def test_structure_scope_and_tick_must_match_raw_snapshot(self):
        target = allowed_product_symbols()[0]

        def analyzer(raw: dict) -> dict:
            result = fake_analyzer(raw)
            if raw["requested_symbol"] == target:
                result["actual_main"] = f"{target}2705"
                result["analysis_contract"] = f"{target}2709"
                result["as_of"] = "2026-09-10"
                result["tick_size"] = 2
                result["tick_size_exact"] = False
                result["tick_size_actionability"] = "research_only_not_for_exact_order"
            return result

        _, manifest = self.build(fake_fetch, analyzer)
        row = next(item for item in manifest["products"] if item["symbol"] == target)

        self.assertEqual(row["scan_status"], "partial")
        self.assertEqual(row["structure_status"], "partial")
        self.assertTrue(
            any(gap.startswith("structure_actual_main_conflict") for gap in row["evidence_gaps"])
        )
        self.assertTrue(
            any(
                gap.startswith("structure_analysis_contract_conflict")
                for gap in row["evidence_gaps"]
            )
        )
        self.assertTrue(
            any(
                gap.startswith("structure_completed_as_of_conflict")
                for gap in row["evidence_gaps"]
            )
        )
        self.assertTrue(
            any(gap.startswith("structure_tick_size_conflict") for gap in row["evidence_gaps"])
        )
        self.assertIn(
            "structure_tick_size_exact_conflict", row["evidence_gaps"]
        )
        self.assertIn(
            "structure_tick_not_exact_order_compatible", row["evidence_gaps"]
        )

    def test_raw_and_structure_cannot_self_attest_a_wrong_configured_tick(self):
        target = allowed_product_symbols()[0]

        def fetcher(symbol: str) -> dict:
            result = fake_fetch(symbol)
            if symbol == target:
                result["technical"]["tick_size"] = 7
            return result

        def analyzer(raw: dict) -> dict:
            result = fake_analyzer(raw)
            if raw["requested_symbol"] == target:
                result["tick_size"] = 7
                result["support"]["lower"] = 99.5
                result["trend"]["value"] = 100.5
            return result

        _, manifest = self.build(fetcher, analyzer)
        row = next(item for item in manifest["products"] if item["symbol"] == target)

        self.assertEqual(row["scan_status"], "unobservable")
        self.assertTrue(
            any(
                gap.startswith(
                    "structure_failed:ValueError:tick evidence value conflicts"
                )
                for gap in row["evidence_gaps"]
            )
        )
        self.assertIsNone(row["machine_hint"]["technical_direction"])

    def test_actual_main_is_recomputed_from_full_raw_chain(self):
        target = allowed_product_symbols()[0]

        def fetcher(symbol: str) -> dict:
            result = fake_fetch(symbol)
            if symbol == target:
                result["main_contract"] = {"symbol": f"{symbol}2701"}
                result["actual_contract_chain"][1]["open_interest"] = 2000
                result["main_selection"]["actual_contract_chain_sha256"] = canonical_sha256(
                    result["actual_contract_chain"]
                )
            return result

        _, manifest = self.build(fetcher, fake_analyzer)
        row = next(item for item in manifest["products"] if item["symbol"] == target)

        self.assertEqual(row["actual_main_contract"], f"{target}2703")
        self.assertEqual(row["declared_actual_main_contract"], f"{target}2701")
        self.assertEqual(row["scan_status"], "partial")
        self.assertTrue(
            any(gap.startswith("actual_main_claim_conflict") for gap in row["evidence_gaps"])
        )

    def test_unknown_oi_contract_makes_main_chain_partial(self):
        target = allowed_product_symbols()[0]

        def fetcher(symbol: str) -> dict:
            result = fake_fetch(symbol)
            if symbol == target:
                result["actual_contract_chain"].append(
                    {
                        "symbol": f"{symbol}2705",
                        "open_interest": None,
                        "volume": 900,
                        "quote_time": "2026-09-13 09:00:00",
                        "provider_quote_time": "2026-09-13 09:00:00",
                        "quote_time_normalization": "provider_calendar_time_unchanged",
                        "exchange": allowed_products()[symbol]["exchange"],
                        "provider_exchange": allowed_products()[symbol]["exchange"].lower(),
                        "exchange_identity_status": "verified_exact",
                    }
                )
                selection = result["main_selection"]
                selection["source_record_count"] = 3
                selection["available_contract_count"] = 2
                selection["actual_contract_chain_sha256"] = canonical_sha256(
                    result["actual_contract_chain"]
                )
                selection["chain_complete_for_product"] = False
                selection["pagination_complete"] = False
                selection["selection_claim"] = "provisional_leader_of_observed_oi_rows"
                selection["chain_completeness_reason"] = "one contract has unknown OI"
            return result

        _, manifest = self.build(fetcher, fake_analyzer)
        row = next(item for item in manifest["products"] if item["symbol"] == target)

        self.assertEqual(row["actual_main_chain_status"], "partial")
        self.assertEqual(row["data_status"], "partial")
        self.assertEqual(row["scan_status"], "partial")
        self.assertTrue(
            any(
                gap.startswith("actual_main_chain_unknown_open_interest")
                for gap in row["evidence_gaps"]
            )
        )

    def test_zero_volume_oi_leader_is_named_but_not_treated_as_executable(self):
        target = allowed_product_symbols()[0]

        def fetcher(symbol: str) -> dict:
            result = fake_fetch(symbol)
            if symbol == target:
                leader = result["actual_contract_chain"][0]
                leader.update({"volume": 0, "bid": 0, "ask": 0})
                selection = result["main_selection"]
                selection["actual_contract_chain_sha256"] = canonical_sha256(
                    result["actual_contract_chain"]
                )
                selection["main_liquidity_status"] = "no_trade"
            return result

        _, manifest = self.build(fetcher, fake_analyzer)
        row = next(item for item in manifest["products"] if item["symbol"] == target)

        self.assertEqual(row["actual_main_contract"], f"{target}2701")
        self.assertEqual(row["actual_main_liquidity_status"], "no_trade")
        self.assertEqual(row["scan_status"], "partial")
        self.assertIn("actual_main_liquidity_no_trade", row["evidence_gaps"])

    def test_provider_main_claim_is_not_promoted_when_no_oi_row_is_rankable(self):
        target = allowed_product_symbols()[0]

        def fetcher(symbol: str) -> dict:
            result = fake_fetch(symbol)
            if symbol == target:
                for chain_row in result["actual_contract_chain"]:
                    chain_row["open_interest"] = None
                selection = result["main_selection"]
                selection["available_contract_count"] = 0
                selection["rankable_contract_count"] = 0
                selection["chain_complete_for_product"] = False
                selection["pagination_complete"] = False
                selection["selection_claim"] = "provisional_leader_of_observed_oi_rows"
                selection["chain_completeness_reason"] = "all OI rows are unknown"
                selection["actual_contract_chain_sha256"] = canonical_sha256(
                    result["actual_contract_chain"]
                )
            return result

        _, manifest = self.build(fetcher, fake_analyzer)
        row = next(item for item in manifest["products"] if item["symbol"] == target)

        self.assertIsNone(row["actual_main_contract"])
        self.assertEqual(row["declared_actual_main_contract"], f"{target}2701")
        self.assertEqual(row["actual_main_chain_status"], "unobservable")
        self.assertEqual(row["scan_status"], "partial")
        self.assertIn("actual_main_chain_no_rankable_row", row["evidence_gaps"])

    def test_cross_timestamp_oi_rows_cannot_form_a_verified_leader(self):
        target = allowed_product_symbols()[0]

        def fetcher(symbol: str) -> dict:
            result = fake_fetch(symbol)
            if symbol == target:
                result["actual_contract_chain"][1]["quote_time"] = (
                    "2026-09-13 08:55:00"
                )
                selection = result["main_selection"]
                selection["actual_contract_chain_sha256"] = canonical_sha256(
                    result["actual_contract_chain"]
                )
                selection["chain_complete_for_product"] = False
                selection["pagination_complete"] = False
                selection["selection_claim"] = "provisional_leader_of_observed_oi_rows"
                selection["chain_completeness_reason"] = "rows span five minutes"
                selection["chain_time_evidence"] = {
                    "status": "mixed_or_unobservable_time",
                    "min_observed_at": "2026-09-13T08:55:00+08:00",
                    "max_observed_at": "2026-09-13T09:00:00+08:00",
                    "span_seconds": 300.0,
                    "invalid_time_contracts": [],
                    "future_time_contracts": [],
                }
            return result

        _, manifest = self.build(fetcher, fake_analyzer)
        row = next(item for item in manifest["products"] if item["symbol"] == target)

        self.assertEqual(row["actual_main_chain_status"], "partial")
        self.assertEqual(row["scan_status"], "partial")
        self.assertIn("actual_main_chain_mixed_snapshot_time", row["evidence_gaps"])

    def test_provider_clock_skew_inside_declared_five_second_window_is_consistent(self):
        target = allowed_product_symbols()[0]

        def fetcher(symbol: str) -> dict:
            result = fake_fetch(symbol)
            if symbol == target:
                for chain_row in result["actual_contract_chain"]:
                    chain_row["quote_time"] = "2026-09-13 09:00:03"
                    chain_row["provider_quote_time"] = "2026-09-13 09:00:03"
                selection = result["main_selection"]
                selection["actual_contract_chain_sha256"] = canonical_sha256(
                    result["actual_contract_chain"]
                )
                selection["chain_time_evidence"] = {
                    "status": "same_snapshot",
                    "min_observed_at": "2026-09-13T09:00:03+08:00",
                    "max_observed_at": "2026-09-13T09:00:03+08:00",
                    "span_seconds": 0.0,
                    "invalid_time_contracts": [],
                    "future_time_contracts": [],
                }
            return result

        _, manifest = self.build(fetcher, fake_analyzer)
        row = next(item for item in manifest["products"] if item["symbol"] == target)

        self.assertEqual(row["scan_status"], "success")
        self.assertNotIn("actual_main_chain_time_after_fetch", row["evidence_gaps"])

    def test_degraded_daily_source_is_disclosed_as_partial_evidence(self):
        target = allowed_product_symbols()[0]

        def fetcher(symbol: str) -> dict:
            result = fake_fetch(symbol)
            if symbol == target:
                result["technical"]["daily_data_evidence"] = {
                    "status": "degraded"
                }
            return result

        def analyzer(raw: dict) -> dict:
            result = fake_analyzer(raw)
            if raw["requested_symbol"] == target:
                result["daily_data_evidence"] = {"status": "degraded"}
            return result

        _, manifest = self.build(fetcher, analyzer)
        row = next(item for item in manifest["products"] if item["symbol"] == target)

        self.assertEqual(row["daily_data_evidence_status"], "degraded")
        self.assertEqual(row["data_status"], "partial")
        self.assertEqual(row["structure_status"], "partial")
        self.assertEqual(row["scan_status"], "partial")
        self.assertIn(
            "daily_data_evidence_not_verified:degraded", row["evidence_gaps"]
        )

    def test_success_without_complete_structure_geometry_is_not_available(self):
        target = allowed_product_symbols()[0]

        def analyzer(raw: dict) -> dict:
            result = fake_analyzer(raw)
            if raw["requested_symbol"] == target:
                result["support"] = None
                result["resistance"] = {"lower": 102, "upper": None}
                result["trend"] = {"direction": "up", "value": None}
            return result

        _, manifest = self.build(fake_fetch, analyzer)
        row = next(item for item in manifest["products"] if item["symbol"] == target)

        self.assertEqual(row["data_status"], "available")
        self.assertEqual(row["structure_status"], "partial")
        self.assertEqual(row["scan_status"], "partial")
        self.assertIn("missing_support", row["evidence_gaps"])
        self.assertIn("missing_resistance_upper", row["evidence_gaps"])
        self.assertIn("missing_trend_value", row["evidence_gaps"])

    def test_counts_are_derived_and_machine_hints_never_choose_an_action(self):
        _, manifest = self.build()
        expected_count = len(allowed_product_symbols())
        self.assertEqual(
            manifest["counts"],
            {
                "data_status": {
                    "available": expected_count,
                    "partial": 0,
                    "unobservable": 0,
                },
                "structure_status": {
                    "available": expected_count,
                    "partial": 0,
                    "unobservable": 0,
                },
                "with_evidence_gaps": 0,
            },
        )
        forbidden_keys = {"action", "trade_action", "recommendation", "rank", "score"}

        def visit(value):
            if isinstance(value, dict):
                self.assertTrue(forbidden_keys.isdisjoint(value))
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)

        visit(manifest)

    def test_embedded_manifest_hash_uses_canonical_json_without_newline(self):
        _, manifest = self.build()
        expected = hashlib.sha256(canonical_json_bytes(manifest)).hexdigest()
        self.assertEqual(canonical_sha256(manifest), expected)
        self.assertEqual(len(manifest["universe_sha256"]), 64)
        self.assertEqual(len(manifest["policy_sha256"]), 64)
        self.assertEqual(
            manifest["universe_sha256"],
            hashlib.sha256((ROOT / "config/trading_universe.json").read_bytes()).hexdigest(),
        )
        self.assertEqual(
            manifest["policy_sha256"],
            hashlib.sha256(
                (ROOT / "config/futures_main_report_policy.json").read_bytes()
            ).hexdigest(),
        )
        self.assertEqual(
            manifest["product_specs_sha256"],
            hashlib.sha256(
                (ROOT / "config/product_contract_specs.json").read_bytes()
            ).hexdigest(),
        )
        json.loads(canonical_json_bytes(manifest).decode("utf-8"))

    def test_manifest_is_consumed_by_v3_report_validator(self):
        root, manifest = self.build()
        focus = manifest["products"][0]
        expected_count = len(allowed_product_symbols())
        report = {
            "scan_scope": "all_market",
            "scan_manifest": manifest,
            "scan_manifest_sha256": canonical_sha256(manifest),
        }

        result = futures_main_report._validate_scan_manifest_v3(
            report,
            datetime.fromisoformat("2026-09-13T09:01:00+08:00"),
            focus["symbol"],
            focus["analysis_contract"],
            focus["actual_main_contract"],
            policy_sha256=hashlib.sha256(
                (ROOT / "config/futures_main_report_policy.json").read_bytes()
            ).hexdigest(),
            artifact_root=root,
        )

        self.assertEqual(result["successful_count"], expected_count)
        self.assertEqual(result["partial_count"], 0)
        self.assertEqual(result["unobservable_count"], 0)
        self.assertEqual(result["focus_scan_status"], "success")
        self.assertEqual(result["artifact_hashes_verified"], 2 * expected_count)
        self.assertEqual(result["focus"], focus)

    def test_v3_report_validator_rejects_stale_scan_manifest(self):
        root, manifest = self.build()
        focus = manifest["products"][0]
        report = {
            "scan_scope": "all_market",
            "scan_manifest": manifest,
            "scan_manifest_sha256": canonical_sha256(manifest),
        }

        with self.assertRaises(futures_main_report.FuturesMainReportError):
            futures_main_report._validate_scan_manifest_v3(
                report,
                datetime.fromisoformat("2026-09-13T11:00:01+08:00"),
                focus["symbol"],
                focus["analysis_contract"],
                focus["actual_main_contract"],
                policy_sha256=hashlib.sha256(
                    (ROOT / "config/futures_main_report_policy.json").read_bytes()
                ).hexdigest(),
                artifact_root=root,
            )


if __name__ == "__main__":
    unittest.main()
