# Invoice Processing Automation — Multi-Agent System

Submission for the Galatiq technical assessment: a working multi-agent prototype that automates Acme Corp's end-to-end invoice workflow — ingestion → validation → approval → payment.

Acme Corp (PE-backed manufacturing) loses **~$2M/year** to manual invoice processing: 30% error rates, 5-day delays, frustrated stakeholders. This system attacks exactly that: every invoice is extracted, validated against inventory, reviewed under policy, and either paid or rejected **with logged reasoning** — no human in the loop for the routine cases, and a clear audit trail for the exceptions.

## Architecture

Four specialized agents, orchestrated as a pipeline (`src/orchestrator.py`):

| Agent | Tools | Job |
|---|---|---|
| **IngestionAgent** | `read_document`, `parse_structured`, LLM extraction | Pulls Vendor, Amount, Items, Due Date from PDF/TXT/CSV/JSON/XML. Structured formats are parsed natively (fast, exact); free text goes through the LLM. **Self-correction:** one repair pass when critical fields are missing. |
| **ValidationAgent** | `InventoryDB.lookup` (SQLite) | Flags unknown items, quantity > stock, zero-stock/fraudulent items, negative quantities, and stated-vs-computed total mismatches. **Self-correction:** requests re-extraction when confidence is low and issues look like extraction artefacts. |
| **ApprovalAgent** | policy rules + `detect_fraud_signals` | Rule-based VP review: validation errors → reject; fraud language (urgency, wire-transfer pressure) → reject; >$10K → heightened scrutiny. **Reflection loop:** every draft decision is passed through a critic (LLM auditor, or a policy-grounded local critic) that can uphold or challenge it; challenged decisions are revised once. |
| **PaymentAgent** | `mock_payment` | Executes payment on approval; otherwise logs the rejection **with reasoning** to `ledger.jsonl` — the audit trail finance actually needs. |

**LLM integration** (`src/llm.py`): xAI's Grok is the reasoning engine when `XAI_API_KEY` is set. Without a key, a deterministic local engine takes over — tolerant regex extraction (handles typos like "Vndr", "INVOCE", OCR noise like "2O26"/"$3,500.O0") plus a policy-grounded critic — so the system runs fully offline, per the brief.

## Setup

```bash
pip install -r requirements.txt
python setup_inventory.py        # creates inventory.db (required)
```

## Running

```bash
# Single invoice (human-readable summary)
python main.py --invoice_path=data/invoices/invoice_1001.txt

# Full result as JSON
python main.py --invoice_path=data/invoices/invoice_1002.txt --json

# All 20 sample invoices, summary table
python demo.py
```

With Grok as the reasoning engine:

```bash
export XAI_API_KEY=your_key_here
python main.py --invoice_path=data/invoices/invoice_1003.txt
```

## What it does on the test set

| Scenario | Example | Outcome |
|---|---|---|
| Clean invoice, in stock | INV-1001 | **PAID** |
| Quantity exceeds stock | INV-1002 (20× GadgetX, 5 in stock) | Rejected — `stock_mismatch` |
| Fraudulent vendor + urgency language | INV-1003 (FakeItem, "wire transfer preferred") | Rejected — `zero_stock` + fraud signals |
| Unknown products | INV-1008 (SuperGizmo, MegaSprocket) | Rejected — `unknown_item` |
| Data integrity (negative qty) | INV-1009 | Rejected — `negative_quantity` |
| Messy OCR invoice | INV-1012 ("FROM:", "Widget A", "2O26") | **PAID** after tolerant extraction |
| High-value but clean (>$10K) | INV-1013 ($22.5K) | **PAID** with scrutiny flag |

Every run appends a structured record to `ledger.jsonl`: who, how much, approved/rejected, why.

## Design decisions (and what I'd do with more time)

- **Custom orchestration over a framework.** LangGraph/CrewAI would add weight without changing the demo; the agent boundaries, tool contracts, and correction loops are explicit and testable as plain Python.
- **Deterministic fast path + LLM enhancement.** Native parsing for structured formats; LLM only where it adds value (messy text, critique). This is also why the system works with zero API keys.
- **Rejections are first-class outputs.** A finance automation that silently drops invoices is worse than the manual process — hence the ledger with reasoning on every decision.
- **Next steps:** confidence-scored human-in-the-loop queue for borderline cases, vendor master-data matching, and a small review UI over the ledger.

## Business impact

- **Error rate:** validation catches the exact failure modes behind Acme's 30% error rate (stock mismatches, unknown items, bad data) *before* money moves.
- **Cycle time:** straight-through processing for clean invoices collapses the 5-day email-chain delay to seconds; only exceptions need humans.
- **Fraud:** urgency/wire-transfer language and zero-stock vendors are flagged automatically instead of relying on a tired AP clerk to notice.
