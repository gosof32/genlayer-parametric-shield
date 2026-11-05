"""
Direct-mode tests for Parametric Shield.

Run with:  pytest -q
Web sources and LLM answers are mocked, so the suite is deterministic and
needs no GenLayer node.
"""
import json

import pytest

from conftest import (
    COVER_ARGS, GEN, SOURCES, BULLETIN_URL, mock_sources, bulletin_page, hexaddr,
)

ARGS = list(COVER_ARGS.values())
HEAVY_QUOTE = "The heaviest daily rainfall this month was 132 mm"


def args(**over):
    d = dict(COVER_ARGS)
    d.update(over)
    return list(d.values())


def buy(vm, shield, who, cid, coverage):
    premium = shield.quote_premium(cid, coverage)
    with vm.prank(who):
        vm.value = premium
        shield.buy_cover(cid, coverage)
        vm.value = 0
    return premium


def after_window(vm):
    vm.warp("2027-01-02T00:00:00Z")


# ---------------------------------------------------------------- creation


class TestCreate:
    def test_cover_is_open_with_capital(self, shield, cover, direct_alice):
        c = shield.get_cover(cover)
        assert cover == 1
        assert c["status"] == "OPEN"
        assert c["capital"] == 1000 * GEN
        assert c["threshold"] == "100"
        assert c["sources"][0]["reducer"] == "max"
        assert c["sources"][1]["reducer"] == "max"  # default for JSON sources
        assert c["sources"][2] == {"url": BULLETIN_URL, "path": "", "reducer": ""}
        assert c["resolve_deadline"].startswith("2027-01-07")
        assert c["underwriter"].lower() == hexaddr(direct_alice).lower()

    @pytest.mark.parametrize("over,msg", [
        ({"comparator": "eq"}, "comparator"),
        ({"threshold": "1e3"}, "plain numbers"),
        ({"quorum": 4}, "quorum"),
        ({"tolerance_bp": 5000}, "tolerance_bp"),
        ({"premium_bp": 0}, "premium_bp"),
        ({"grace_days": 0}, "grace_days"),
        ({"window_end": "2026-11-30T00:00:00Z"}, "after window_start"),
        ({"window_start": "2026-11-01T00:00:00Z"}, "in the future"),
        ({"window_start": "2026-12-01T00:00:00"}, "timezone"),
        ({"sources_json": "[]"}, "1 to 5 sources"),
        ({"sources_json": json.dumps([{"url": "http://insecure.example/x"}])}, "https"),
        ({"sources_json": json.dumps([{"url": "https://a.example/x", "path": "a..b"}])}, "dotted keys"),
        ({"sources_json": json.dumps([{"url": "https://a.example/x", "path": "a", "reducer": "median"}])}, "reducer"),
        ({"sources_json": json.dumps([{"url": "https://a.example/x"}, {"url": "https://a.example/x"}]), "quorum": 1}, "duplicate"),
    ])
    def test_validation(self, direct_vm, shield, direct_alice, over, msg):
        with direct_vm.prank(direct_alice):
            direct_vm.value = GEN
            with direct_vm.expect_revert(msg):
                shield.create_cover(*args(**over))

    def test_requires_capital(self, direct_vm, shield, direct_alice):
        with direct_vm.prank(direct_alice):
            with direct_vm.expect_revert("send capital"):
                shield.create_cover(*ARGS)


# ---------------------------------------------------------------- buying


class TestBuy:
    def test_premium_and_position(self, direct_vm, shield, cover, direct_bob):
        assert shield.quote_premium(cover, 100 * GEN) == 8 * GEN
        buy(direct_vm, shield, direct_bob, cover, 100 * GEN)
        buy(direct_vm, shield, direct_bob, cover, 50 * GEN)
        pos = shield.get_position(cover, hexaddr(direct_bob))
        assert pos == {"coverage": 150 * GEN, "premium": 12 * GEN, "claimed": False}
        c = shield.get_cover(cover)
        assert c["sold"] == 150 * GEN and c["premiums"] == 12 * GEN and c["holders"] == 1

    def test_exact_premium_required(self, direct_vm, shield, cover, direct_bob):
        with direct_vm.prank(direct_bob):
            direct_vm.value = GEN
            with direct_vm.expect_revert("premium for this cover is exactly"):
                shield.buy_cover(cover, 100 * GEN)

    def test_fully_collateralized(self, direct_vm, shield, cover, direct_bob):
        # 1000 capital + 8% premium lets one buyer take at most ~1086 GEN of cover.
        with direct_vm.expect_revert("CAPACITY"):
            buy(direct_vm, shield, direct_bob, cover, 1100 * GEN)
        buy(direct_vm, shield, direct_bob, cover, 1080 * GEN)
        assert shield.get_cover(cover)["capacity_left"] >= 0

    def test_sales_close_at_window_start(self, direct_vm, shield, cover, direct_bob):
        direct_vm.warp("2026-12-01T00:00:01Z")
        with direct_vm.expect_revert("sales close"):
            buy(direct_vm, shield, direct_bob, cover, GEN)

    def test_add_capital_only_underwriter(self, direct_vm, shield, cover, direct_alice, direct_bob):
        with direct_vm.prank(direct_bob):
            direct_vm.value = GEN
            with direct_vm.expect_revert("only the underwriter"):
                shield.add_capital(cover)
        with direct_vm.prank(direct_alice):
            direct_vm.value = 500 * GEN
            assert shield.add_capital(cover) == 1500 * GEN
            direct_vm.value = 0


# ---------------------------------------------------------------- resolution


class TestResolve:
    def test_not_before_window_end(self, direct_vm, shield, cover):
        direct_vm.warp("2026-12-15T00:00:00Z")
        with direct_vm.expect_revert("NOT_READY"):
            shield.resolve(cover)

    def test_triggered_uses_median_of_three_sources(self, direct_vm, shield, cover):
        after_window(direct_vm)
        mock_sources(direct_vm, meteo=[12.0, 130.4, 40.1, None], backup=128, bulletin=bulletin_page(132),
                     llm=(132, HEAVY_QUOTE))
        res = shield.resolve(cover)
        assert res == {"status": "TRIGGERED", "observed": "130.4", "threshold": "100", "sources_ok": 3}
        assert direct_vm.run_validator() is True
        c = shield.get_cover(cover)
        assert c["source_values"] == ["130.4", "128", "132"]
        assert len(c["evidence_hash"]) == 64 and c["attempts"] == 1
        with direct_vm.expect_revert("already resolved"):
            shield.resolve(cover)

    def test_not_triggered(self, direct_vm, shield, cover):
        after_window(direct_vm)
        quote = "The heaviest daily rainfall this month was 61 mm"
        mock_sources(direct_vm, meteo=[20.0, 60.2], backup=59.8, bulletin=bulletin_page(61), llm=(61, quote))
        res = shield.resolve(cover)
        assert res["status"] == "NOT_TRIGGERED" and res["observed"] == "60.2"
        assert direct_vm.run_validator() is True

    def test_one_outlier_source_cannot_flip_the_median(self, direct_vm, shield, cover):
        after_window(direct_vm)
        mock_sources(direct_vm, meteo=[60.0], backup=999, bulletin=bulletin_page(61),
                     llm=(61, "The heaviest daily rainfall this month was 61 mm"))
        assert shield.resolve(cover)["status"] == "NOT_TRIGGERED"

    def test_hallucinated_quote_is_discarded(self, direct_vm, shield, cover):
        after_window(direct_vm)
        mock_sources(direct_vm, meteo=[130.0], backup=None, bulletin=bulletin_page(132),
                     llm=(150, "Rainfall reached 150 mm"))
        res = shield.resolve(cover)
        # Bulletin reading rejected and backup down: 1 of 2 required sources.
        assert res == {"status": "NO_QUORUM", "sources_ok": 1, "quorum": 2}
        assert shield.get_cover(cover)["status"] == "OPEN"

    def test_no_quorum_then_retry_succeeds(self, direct_vm, shield, cover):
        after_window(direct_vm)
        mock_sources(direct_vm)
        assert shield.resolve(cover)["status"] == "NO_QUORUM"
        direct_vm.clear_mocks()
        mock_sources(direct_vm, meteo=[101.0], backup=104)
        res = shield.resolve(cover)
        assert res["status"] == "TRIGGERED" and res["observed"] == "102.5"
        assert shield.get_cover(cover)["attempts"] == 2


# ---------------------------------------------------------------- validator


class TestValidator:
    def _resolve_triggered(self, direct_vm, shield, cover):
        after_window(direct_vm)
        mock_sources(direct_vm, meteo=[130.4], backup=128, bulletin=bulletin_page(132), llm=(132, HEAVY_QUOTE))
        shield.resolve(cover)

    def test_accepts_small_snapshot_drift(self, direct_vm, shield, cover):
        self._resolve_triggered(direct_vm, shield, cover)
        direct_vm.clear_mocks()
        mock_sources(direct_vm, meteo=[131.9], backup=127, bulletin=bulletin_page(132), llm=(132, HEAVY_QUOTE))
        assert direct_vm.run_validator() is True

    def test_rejects_fabricated_value(self, direct_vm, shield, cover):
        self._resolve_triggered(direct_vm, shield, cover)
        assert direct_vm.run_validator(
            leader_result={"values": [130400, 200000, 132000], "quotes": ["", "", HEAVY_QUOTE]}
        ) is False

    def test_rejects_quote_missing_from_page(self, direct_vm, shield, cover):
        self._resolve_triggered(direct_vm, shield, cover)
        assert direct_vm.run_validator(
            leader_result={"values": [130400, 128000, 132000], "quotes": ["", "", "made up text 132 mm"]}
        ) is False

    def test_rejects_flipped_outcome_near_threshold(self, direct_vm, shield, cover):
        after_window(direct_vm)
        mock_sources(direct_vm, meteo=[100.5], backup=100.2)
        assert shield.resolve(cover)["status"] == "TRIGGERED"
        direct_vm.clear_mocks()
        # Within tolerance per source, but this validator's median lands below 100.
        mock_sources(direct_vm, meteo=[99.6], backup=99.8)
        assert direct_vm.run_validator() is False

    def test_rejects_quorum_disagreement(self, direct_vm, shield, cover):
        after_window(direct_vm)
        mock_sources(direct_vm, meteo=[130.0], backup=129)
        shield.resolve(cover)
        assert direct_vm.run_validator(
            leader_result={"values": [130000, None, None], "quotes": ["", "", ""]}
        ) is False

    @pytest.mark.parametrize("bad", [
        "not a dict",
        {"values": [1, 2]},
        {"values": [1, 2, 3], "quotes": ["", ""]},
        {"values": [True, 1, 1], "quotes": ["", "", ""]},
        {"values": [1.5, 1, 1], "quotes": ["", "", ""]},
    ])
    def test_rejects_malformed_leader_output(self, direct_vm, shield, cover, bad):
        self._resolve_triggered(direct_vm, shield, cover)
        assert direct_vm.run_validator(leader_result=bad) is False

    def test_rejects_leader_error(self, direct_vm, shield, cover):
        self._resolve_triggered(direct_vm, shield, cover)
        assert direct_vm.run_validator(leader_error=Exception("boom")) is False


# ---------------------------------------------------------------- settlement


class TestSettlement:
    def test_triggered_pays_holders_and_rest_to_underwriter(self, direct_vm, shield, cover, direct_alice,
                                                            direct_bob, direct_charlie):
        buy(direct_vm, shield, direct_bob, cover, 300 * GEN)
        buy(direct_vm, shield, direct_charlie, cover, 200 * GEN)
        after_window(direct_vm)
        mock_sources(direct_vm, meteo=[130.0], backup=129)
        shield.resolve(cover)
        with direct_vm.prank(direct_bob):
            assert shield.claim(cover) == 300 * GEN
            with direct_vm.expect_revert("already claimed"):
                shield.claim(cover)
        with direct_vm.prank(direct_charlie):
            assert shield.claim(cover) == 200 * GEN
        with direct_vm.prank(direct_alice):
            assert shield.withdraw_underwriter(cover) == 1000 * GEN + 40 * GEN - 500 * GEN
            with direct_vm.expect_revert("already withdrawn"):
                shield.withdraw_underwriter(cover)

    def test_not_triggered_returns_everything_to_underwriter(self, direct_vm, shield, cover, direct_alice, direct_bob):
        buy(direct_vm, shield, direct_bob, cover, 100 * GEN)
        after_window(direct_vm)
        mock_sources(direct_vm, meteo=[30.0], backup=31)
        shield.resolve(cover)
        with direct_vm.prank(direct_bob):
            with direct_vm.expect_revert("did not trigger"):
                shield.claim(cover)
        with direct_vm.prank(direct_alice):
            assert shield.withdraw_underwriter(cover) == 1008 * GEN

    def test_void_after_deadline_refunds_both_sides(self, direct_vm, shield, cover, direct_alice, direct_bob):
        buy(direct_vm, shield, direct_bob, cover, 100 * GEN)
        after_window(direct_vm)
        with direct_vm.expect_revert("deadline has not passed"):
            shield.expire(cover)
        direct_vm.warp("2027-01-08T00:00:01Z")
        with direct_vm.expect_revert("EXPIRED"):
            shield.resolve(cover)
        assert shield.expire(cover) == "VOID"
        with direct_vm.prank(direct_bob):
            assert shield.claim(cover) == 8 * GEN
        with direct_vm.prank(direct_alice):
            assert shield.withdraw_underwriter(cover) == 1000 * GEN

    def test_underwriter_cannot_withdraw_while_open(self, direct_vm, shield, cover, direct_alice):
        with direct_vm.prank(direct_alice):
            with direct_vm.expect_revert("not resolved"):
                shield.withdraw_underwriter(cover)

    def test_only_underwriter_withdraws(self, direct_vm, shield, cover, direct_bob):
        with direct_vm.prank(direct_bob):
            with direct_vm.expect_revert("only the underwriter"):
                shield.withdraw_underwriter(cover)


# ---------------------------------------------------------------- pure helpers


class TestHelpers:
    def test_milli_and_median(self, module):
        assert module._to_milli("130.4") == 130400
        assert module._to_milli(-2.5) == -2500
        assert module._fmt_milli(-2500) == "-2.5"
        assert module._median([1, 9, 5]) == 5
        assert module._median([100000, 105000]) == 102500

    def test_reducers(self, module):
        series = [1, None, 4, 2]
        assert module._reduce(series, "max") == 4000
        assert module._reduce(series, "min") == 1000
        assert module._reduce(series, "sum") == 7000
        assert module._reduce(series, "mean") == 2333
        assert module._reduce(series, "last") == 2000

    def test_tolerance(self, module):
        assert module._within(100000, 104000, 500, 0)  # 5% of 104 = 5.2
        assert not module._within(100000, 110000, 500, 0)
        assert module._within(1000, 2500, 500, 2000)  # absolute floor for small values
