# The RTO & Dynamic Friction Engine
### An end-to-end Product Analyst / APM case study

> "Should an e-commerce company introduce dynamic friction for high-risk Cash-on-Delivery orders — and if so, for whom, when, and at what price?"

This repo is not a dashboard. It's the full analytical chain a Product Analyst would actually run to answer that question: **problem discovery → data → risk modeling → segmentation → intervention design → unit economics → causal experiment design → a defensible product decision.**

## TL;DR result

- The obvious fix (a flat ₹50 CoD fee on the riskiest cohorts) **loses ~2% of profit** — conversion loss outweighs RTO savings when applied bluntly at the cohort level.
- A risk-scored, order-level dynamic policy with an LTV guardrail **gains ~5.5% profit**, mostly by nudging risky orders to Prepaid with an incentive rather than punishing them with a fee.
- **Recommendation: ITERATE** — run the A/B test in `AB_TEST_DESIGN.md` before a full launch. See `DECISION_MEMO.md` for the full reasoning.

## Repo structure

```
├── generate_data.py           # Task 1: synthetic 500k-order dataset generator
├── ecommerce_orders_sample.csv # 5k-row preview (run generate_data.py for the full 500k)
├── sql_analytics.sql          # Task 2: 3 interview-grade PostgreSQL queries
├── risk_engine.py             # RTO risk model: LogReg vs GBM, calibration, risk bands
├── intervention_engine.py     # Task 3: EV-based dynamic intervention + unit economics
├── PRD.md                     # Task 4: Product Requirements Document
├── AB_TEST_DESIGN.md          # Task 5: A/B test & experiment design
└── DECISION_MEMO.md           # Final GO / NO-GO / ITERATE executive memo
```

## Quick start

```bash
pip install pandas numpy faker scikit-learn

# 1. Generate the 500k-row synthetic dataset (~86MB, not committed to git)
python generate_data.py

# 2. (Optional) Load ecommerce_orders.csv into Postgres, then run sql_analytics.sql

# 3. Train & evaluate the RTO risk model (LogReg vs GBM, calibration, risk bands)
python risk_engine.py
# -> writes scored_orders_sample.csv (used by step 4)

# 4. Run the intervention engine / unit economics comparison
python intervention_engine.py
# -> writes intervention_results.csv, prints the policy comparison table
```

`intervention_engine.py` also runs standalone (with a heuristic fallback score) if you skip step 3 — useful for a quick unit-economics sanity check without training a model first.

## Why the dataset isn't trivial

`generate_data.py` doesn't just correlate `payment_method` with `rto_status` in isolation. RTO probability is generated from a logistic model with **main effects + explicit interaction terms** (e.g. CoD × Tier-3 × Electronics × Late-Night → a sharp, non-additive risk spike) plus irreducible noise, and per-user latent traits that create realistic historical/behavioral features. A model — or an analyst — that only looks at single-variable rates will systematically under-explain the outcome, which is the point: this is meant to reward real segmentation and modeling work, not a one-line `GROUP BY`.

## Model performance, honestly reported

The risk model (Gradient Boosting, isotonic-calibrated) scores **ROC-AUC 0.72, PR-AUC 0.27** against an 11% base rate on a held-out test set. That's a realistic, useful-but-imperfect result for a rare-event consumer-behavior problem — not an inflated 0.95 that would signal target leakage. Risk bands are calibration-checked against actual outcomes (Low band: 8.6% actual RTO rate; Very High band: 84.1%) rather than just asserted.

## What's deliberately *not* claimed

- We do not claim the fee *causes* RTO reduction — only that it's correlated with it in the (synthetic) data and modeled as causal for the EV simulation. `AB_TEST_DESIGN.md` exists precisely to convert this correlational estimate into a causal one before any full launch.
- We do not present the static-fee result as a win. Task 3 originally asked to "prove that adding friction increases margin" — the honest simulation output shows the naive version of that claim is false, and that's the more useful finding for a real business.
