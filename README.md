# Parametric Shield

**A fully collateralized parametric insurance primitive for GenLayer Intelligent Contracts.**

Parametric insurance pays a fixed amount when a measurable event crosses a threshold (rain ≥ 100 mm in a day, temperature ≤ 0 °C, river level ≥ 4 m), with no claims adjuster. The hard part has always been the oracle: who reads the number, from where, and why should anyone trust it?

Parametric Shield makes that oracle a GenLayer consensus problem. Validators independently read several public sources, structured APIs and human-written bulletins alike, and agree on the reading within a stated tolerance before any money moves.

Example: a farmer cooperative in Riau buys cover that pays out if the heaviest single-day rainfall in Pekanbaru reaches 100 mm in December. The trigger is checked against Open-Meteo, a second weather API, and the regional weather bulletin.

---

## Why this is a primitive, not a demo

| Concern | How the contract handles it |
|---|---|
| Any measurable event | The trigger is data, not code: `metric`, `comparator` (`gte` / `lte`), `threshold`, plus 1 to 5 sources. |
| Structured and unstructured sources | JSON sources are read **deterministically** (dotted path + reducer). Text sources use an LLM, but every reading must carry a **verbatim quote** that contract code finds on the page. |
| Validators see slightly different data | Custom validator with **relative + absolute tolerance** per cover. |
| A result that flips the payout | Validators must land on the **same side of the threshold**. Drift is tolerated, a different decision is not. |
| One bad or manipulated source | Decision uses the **median** of sources with a **quorum**. With three or more sources, one outlier can't move it. |
| Sources down | `NO_QUORUM` changes nothing and can be retried. After the resolve deadline the cover is `VOID` and everyone is refunded. |
| Solvency | Fully collateralized: cover sold ≤ capital + premiums, enforced at purchase. |
| Prompt injection in pages | Page text is fenced as untrusted data, `<` `>` neutralized, and an injected number fails the quote check. |
| Stuck funds | Every terminal state (`TRIGGERED`, `NOT_TRIGGERED`, `VOID`) has a defined payout for both sides. |

Reusable pieces you can lift into other contracts: `_aggregate` (median + quorum + decision), `_within` (tolerance check), `_select_path` / `_reduce` (deterministic JSON extraction), and the quote-anchored `_extract_from_text`.

---

## Lifecycle

```
create_cover (underwriter, sends capital)
      │
      ▼
    OPEN ── buy_cover (buyers, exact premium) until window_start
      │     add_capital (underwriter) until window_start
      │
      │  window_end passes
      ▼
   resolve(cover_id)  ── anyone can call
      │
      ├─ NO_QUORUM ──► stays OPEN, retry later
      ├─ TRIGGERED ──► claim(): holders get their coverage
      │                withdraw_underwriter(): capital + premiums − cover sold
      └─ NOT_TRIGGERED ► withdraw_underwriter(): capital + premiums
      
   resolve_deadline passes while still OPEN
      └─ expire() ──► VOID: claim() refunds premiums, underwriter gets capital back
```

## How consensus is used

`resolve` calls `gl.vm.run_nondet_unsafe(leader_fn, validator_fn)`.

**Leader** (`_read_all`), for each source:
- **JSON source** (`path` set): `gl.nondet.web.get`, parse, follow the dotted path (`daily.precipitation_sum`), fold a series with the reducer (`max`, `min`, `sum`, `mean`, `last`). No LLM involved.
- **Text source** (no `path`): `gl.nondet.web.render(mode="text")`, then `gl.nondet.exec_prompt` asks for `{"value", "quote"}`. Contract code rejects the reading unless the quote appears verbatim in the page and contains the number.

It returns `{"values": [milli-int or null per source], "quotes": [...]}`. All numbers are integer thousandths, so there are no floats in consensus.

**Validator** repeats the reads and votes **yes** only if all of these hold:
1. Shape is exact: one value per source, integers or null, no booleans or floats.
2. Every source both sides read agrees within `max(abs_tolerance, tolerance_bp × value)`.
3. Every leader quote from a text source appears in the validator's own render of that page.
4. Both sides reach the same quorum result.
5. Both medians are on the same side of the threshold and within tolerance of each other.

A source the leader could read but the validator couldn't (or the other way round) is not a disagreement on its own. Only the aggregate decision matters, so a flaky endpoint doesn't stall consensus.

**After consensus** (deterministic): median of non-null values → compare with threshold → state change. A SHA-256 `evidence_hash` of the agreed readings is stored for audit.

Near the threshold, honest validators can land on different sides. They then disagree and GenLayer's leader rotation / appeal process takes over, rather than a coin-flip payout. Pick `tolerance_bp` and `abs_tolerance` to match how noisy your sources are.

---

## Contract API

### Write
| Method | Who | Notes |
|---|---|---|
| `create_cover(title, metric, comparator, threshold, sources_json, quorum, tolerance_bp, abs_tolerance, window_start, window_end, grace_days, premium_bp)` *payable* | underwriter | `msg.value` is the capital. Window must start in the future. Returns `cover_id`. |
| `add_capital(cover_id)` *payable* | underwriter | Before the window opens. |
| `buy_cover(cover_id, coverage)` *payable* | anyone | `msg.value` must equal `quote_premium`. Sales close at `window_start`. |
| `resolve(cover_id)` | anyone | Between `window_end` and `resolve_deadline`. |
| `expire(cover_id)` | anyone | After `resolve_deadline` if still open → `VOID`. |
| `claim(cover_id)` | holder | Coverage if `TRIGGERED`, premium refund if `VOID`. |
| `withdraw_underwriter(cover_id)` | underwriter | Once, after resolution. |

### View
`get_cover`, `list_covers`, `quote_premium`, `get_position`, `get_holders`.

### Parameters
| Field | Example | Meaning |
|---|---|---|
| `metric` | `highest single-day rainfall in Pekanbaru, in millimetres` | Plain-language definition, also given to the LLM for text sources. |
| `comparator` / `threshold` | `gte` / `"100"` | Trigger when the median reading ≥ 100. |
| `sources_json` | see below | 1 to 5 sources, https only. |
| `quorum` | `2` | Minimum sources that must return a value. |
| `tolerance_bp` / `abs_tolerance` | `500` / `"2"` | Validators agree if readings are within 5% or 2 units, whichever is larger. |
| `grace_days` | `7` | Time after `window_end` to reach quorum before the cover voids. |
| `premium_bp` | `800` | Premium = 8% of coverage, rounded up. |

```json
[
  {"url": "https://archive-api.open-meteo.com/v1/archive?latitude=0.51&longitude=101.45&start_date=2026-12-01&end_date=2026-12-31&daily=precipitation_sum&timezone=UTC",
   "path": "daily.precipitation_sum", "reducer": "max"},
  {"url": "https://api.weather-backup.example/pekanbaru/2026-12.json", "path": "data.max_daily_mm"},
  {"url": "https://bmkg.example.go.id/bulletin/riau-december-2026"}
]
```

---

## Other covers you can build without changing code

- **Crop drought:** `sum` of daily precipitation over a season `lte` 200 mm.
- **Frost:** `min` of `daily.temperature_2m_min` `lte` 0.
- **Flight / port disruption:** a text source (an airport or port authority notice) reporting hours of closure `gte` 24.
- **Heatwave for outdoor workers:** `max` of `daily.temperature_2m_max` `gte` 38.
- **River flood:** a hydrology bulletin's peak gauge reading `gte` a flood stage.

---

## Live on Studionet

Deployed and run end to end on GenLayer Studionet against **live Open-Meteo data** for Pekanbaru, October 2026. Every transaction finalized with `MAJORITY_AGREE`.

Contract: `0x62D6040b907DBeDCCC77a5C7956AFca61c06C6f7`

| Cover | Trigger | Source readings (archive default, archive ERA5, historical forecast) | Median | Result |
|---|---|---|---|---|
| #1 | highest daily rainfall ≥ 15 mm | 16.6, 24.3, 16.6 | 16.6 | **TRIGGERED**: farmer claimed 100 units, underwriter got back 908 |
| #2 | monthly total ≥ 300 mm | 226.3, 184.6, 226.3 | 226.3 | **NOT_TRIGGERED**: underwriter got back 1,008 |

The ERA5 model disagrees with the other two sources on both covers. The median absorbs it, which is exactly the outlier case the design is for.

Negative paths were checked on-chain too: wrong premium, resolving twice, claiming a cover that didn't trigger, and claiming without a position all revert.

All 14 transaction hashes, receipts, and the final contract state are in [`deployments/studionet.json`](deployments/studionet.json). To reproduce with throwaway keys:

```bash
npm install
npm run lifecycle:studionet
```

---

## Tests

46 direct-mode tests with mocked web and LLM, so they need no node and are deterministic:

```bash
pip install -r requirements-dev.txt
pytest
```

They cover:
- creation validation (14 cases), premiums, the collateral cap, the sales window
- resolution: triggered, not triggered, median vs. an outlier source, a hallucinated LLM quote being discarded, `NO_QUORUM` then retry
- the validator: accepts snapshot drift; rejects a fabricated value, a fabricated quote, an outcome flipped near the threshold, a quorum mismatch, malformed leader output, and a leader error
- settlement for all three outcomes, double-claim and access control
- pure helpers (fixed-point math, reducers, tolerance)

## Deploy

1. Open [GenLayer Studio](https://studio.genlayer.com) and paste `contracts/parametric_shield.py`.
2. Deploy (no constructor arguments).
3. Call `create_cover` with a value attached, using a window that starts in the future.

## Limits and design choices

- Binary payout (all or nothing per position). Graded payouts can be layered on by deploying several covers at stepped thresholds.
- At most 5 sources and 100 holders per cover, to bound gas and validator work.
- Resolution only after the window ends, so `max`/`sum`/`min` series are complete and there is no early-trigger race.
- The contract does not pick sources for you. Choose sources the buyer and underwriter both accept, and prefer JSON APIs where they exist.

## License

MIT
