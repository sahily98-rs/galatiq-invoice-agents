# Invoice Processing Automation — Galatiq Technical Assessment

A multi-agent system that automates the accounts-payable invoice pipeline:
**ingest → validate → approve → pay**, with a reflection loop and a human-review
escape hatch at every stage.

**Author:** Sahil · **Submission for:** Galatiq AI technical assessment

## Quickstart

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt

# Sample PDFs are binary and not committed; fetch the 3 originals:
python data/fetch_pdfs.py

python setup_inventory.py        # create the mock inventory DB
python main.py --invoice_path=data/invoices/invoice_1001.txt
python demo.py --report          # run all 20 samples + write report.html
python -m pytest tests/ -q       # 26 tests, incl. a golden file
```

Set `XAI_API_KEY` to use Grok as the reasoning engine; without it the
deterministic local engine runs everything offline.

## Architecture

```
                    ┌─────────────┐
                    │ Orchestrator │  run_id, crash→HOLD, duplicate guard,
                    └──────┬──────┘  structured JSONL log (runs.jsonl)
                           │
        ┌──────────────────┼──────────────────┐
        ▼                  ▼                  ▼
 ┌─────────────┐   ┌──────────────┐   ┌───────────────┐
 │ Ingestion   │   │ Validation   │   │ Approval      │
 │ Agent       │──▶│ Agent        │──▶│ Agent         │
 └─────────────┘   └──────────────┘   └───────┬───────┘
        │                                     │
        │ repair pass (v2 algorithm)          │ critic (raw doc)
        │ on missing fields                   │ can divert → HOLD
        ▼                                     ▼
 ┌─────────────┐                     ┌───────────────┐
 │ structured  │                     │ Payment Agent │──▶ ledger.jsonl
 │ parse / LLM │                     └───────────────┘  (invoice #, run_id,
 └─────────────┘                                       outcome — always written)
```

**Outcomes** — `PAID`, `REJECTED_VALIDATION`, `REJECTED_FRAUD`,
`REJECTED_DUPLICATE`, `HOLD_REVIEW`. Anything the system can't decide
confidently (missing payee/total, low extraction confidence, foreign
currency, vendor identity questions, amounts above the auto-pay ceiling,
crashes) goes to **HOLD_REVIEW** for a human instead of being auto-paid or
crashing. Outcomes are structured fields set by the approval agent — never
inferred from reason text.

**Agents and their tools** (every tool call is traced to `runs.jsonl`):

| Agent | Tools | What it does |
|---|---|---|
| Ingestion | `read_document`, `parse_structured`, `llm.extract` | Native JSON/CSV/XML parse; free text via Grok or the local engine. If critical fields are missing, a *different* algorithm (line-oriented labeled scan + price-anchored item detection) runs as the repair pass. Dates normalized to ISO, currency detected, payee names sanitized against PDF column bleed. |
| Validation | `inventory.lookup` | Exact SKU matching (no fuzzy substring). Every line is resolved to its canonical SKU **first**, then quantities are **aggregated per SKU** before stock checks. Negative line quantities are always errors. Unit prices beyond 3x (or under 1/3x) the list price are errors. The line-item sum is **always** checked against the stated subtotal (a vendor who inflates both together does not pass); then expected = subtotal + tax + shipping is checked against the total. Unexplained mismatch > $1.00 is an error. Missing total is an error → HOLD. |
| Approval | `risk.analyze`, `llm.critique` | Policy: critical fraud signals (wire-transfer pressure, bank-detail changes, backdated due date) → reject as fraud; errors → reject; missing data / identity questions / FX / low confidence / >$50K auto-pay ceiling → hold. A critic with access to the **raw document** reviews the draft and can divert *either* direction to human review. In Grok mode the critic runs a ReAct loop — Grok itself chooses between `inventory_lookup`, `ledger_search`, and `risk_analyze`, and its reasoning trace is recorded in `runs.jsonl`. |
| Payment | `mock_payment` | Executes or withholds payment; **always** writes a ledger entry with invoice number + run ID. |

**Self-correction that actually fires** (check `runs.jsonl` / console):
- Ingestion repair pass uses a different algorithm when fields are missing.
- Validation does variant-aware SKU lookup (`"WidgetA (rush order)"` → `WidgetA`) with a logged warning.
- The critic catches garbled payees from two-column PDFs and weak rejections (unknown item close to a known SKU → human verifies instead of auto-reject).
- The orchestrator converts *any* crash into `HOLD_REVIEW` + ledger entry — an invoice never vanishes from the audit trail.
- Duplicate invoices are keyed on **(vendor, invoice number)** — never pay the same vendor twice for one number. A *revised* invoice (new revision marker) is held with the difference calculated instead of rejected.

## Human review queue

Held invoices need a person. `python review.py --list` shows open holds;
`python review.py --approve INV-1014 --note "..."` / `--reject ...` records
the decision back into the ledger. `demo.py --report` generates `report.html`
with a dollar summary ($X paid, $Y blocked, $Z overbilling caught, $W held)
and the exact review commands for each held invoice.

The demo writes to its own `demo_ledger.jsonl` — it never touches the
production `ledger.jsonl`, so the brief's example commands keep working
after a demo run.

## Business impact

- **Throughput:** 20 invoices processed end-to-end in seconds; clean ones paid automatically.
- **Loss prevention:** per-SKU stock aggregation, duplicate-invoice refusal, overbilling detection (stated vs. computed totals), and wire-fraud pressure signals.
- **Auditability:** every decision carries a run ID, reasoning, risk flags, and a ledger entry; structured JSONL logs capture each stage and tool call.
- **Human leverage:** HOLD_REVIEW concentrates reviewer time on the ambiguous cases, instead of all-or-nothing automation.

## Evaluation scenarios (all 20 samples, `tests/golden.json`)

| Invoice | Outcome | Why |
|---|---|---|
| 1001, 1006, 1010, 1011.pdf, 1015 | **PAID** | Clean; totals verified against subtotal+tax+shipping |
| 1002, 1005, 1007, 1008, 1013 | **REJECTED** | Stock over-request (aggregated per SKU), unknown SKUs |
| 1003 | **REJECTED_FRAUD** | Wire-transfer pressure + backdated due date + zero-stock item |
| 1004_revised | **HOLD_REVIEW** | Revision R1 of an already-paid invoice — held with the $4,050 difference calculated |
| 1011.txt | **REJECTED_DUPLICATE** | Same (vendor, number) already paid |
| 1009, 1012, 1014, 1016 | **HOLD_REVIEW** | Missing payee; vendor name change (BEC pattern); EUR (no FX); unknown SKU close to a real one |

Notable catches: 1015's total is $6,500 (not the $2,500 line total a naive CSV parse takes);
1013's per-line quantities look fine but aggregate to 22/18/9 against stock of 15/10/5;
1010's $7,185 total is *correct* once tax ($335) and shipping ($150) are modeled.

## UI

`python demo.py --report` writes `report.html` — a styled run report with a
dollar summary ($X paid, $Y blocked, $Z overbilling caught, $W held),
outcome badges, and expandable per-invoice detail. Held invoices show the
exact `review.py` commands to approve or reject them, closing the loop on
the UI/UX criterion.

## Red-teaming

`tests/test_redteam.py` contains invoices designed to cheat the pipeline:
inflated subtotals, variant-name stock evasion, absurd unit prices, negative
lines netting overstock, vendor/number duplicate edge cases, and a
bank-detail-change BEC attempt. Each is an attack with the expected outcome
asserted.

## LLM integration

`src/llm.py` — Grok via `xai-sdk` (`XAI_API_KEY`), used for free-text extraction
and the critic's second pass, with strict JSON parsing. **Failures are loud**:
a broken key or SDK error is logged as an error and flagged (`degraded`), never
silently treated as success. Without a key, the deterministic local engine runs
the same interfaces offline.

## Future improvements

- FX conversion / multi-currency settlement instead of holding foreign invoices.
- Human-review queue UI with approve/reject actions feeding back into the ledger.
- Learned extraction confidence from reviewer corrections.
- Vendor master-data matching (beyond the name-change signal) for BEC defense.
