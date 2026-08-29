# Decision Memo: Should We Launch Dynamic CoD Friction?

**To:** VP Product, Head of Payments, Head of Risk
**From:** Product Analyst / APM, Trust & Payments
**Re:** Dynamic CoD Friction Engine — Launch Recommendation

---

## Recommendation: **ITERATE**

Not a GO, not a NO-GO. The underlying opportunity is real and the offline economics are attractive — but we are recommending a **scoped experiment before a full launch**, and explicitly recommending **against** the version of this idea most stakeholders initially propose (a flat, cohort-level fee).

## Why not a flat "yes, launch the fee"

The single most important finding in this analysis is that **the obvious version of this idea loses money**. A static ₹50 CoD fee applied uniformly to the two highest-RTO-rate cohorts reduces total expected profit by **~2.1%** in our simulation — the conversion loss it causes outweighs the RTO savings for a meaningful share of orders inside those cohorts, because "cohort-level high risk" and "order-level high risk" are not the same thing. Many orders inside a "risky cohort" are, individually, perfectly safe.

## Why not a flat "no"

A **risk-scored, order-level dynamic policy** — using a calibrated model (ROC-AUC 0.72, PR-AUC 0.27 against an 11% base rate) with an explicit LTV guardrail — lifts total expected profit by **+5.55%** in the same simulation, while applying zero harsh interventions (fee-50 or CoD restriction) to any top-quartile-LTV customer (vs. 851 such customers hit under the naive static policy). The mechanism is real: RTO risk is genuinely concentrated (CoD+Tier-3+Electronics+Late-Night orders RTO at ~66% vs. an 11% baseline), and a targeted lever pointed at that concentration, with a carrot (`Prepaid_Incentive`, chosen for **64%** of flagged orders) preferred over a stick, does not have to trade away conversion the way a blunt fee does.

## Why not a full launch yet

Everything above is **modeled, not measured**. Our RTO-reduction and conversion-elasticity assumptions (e.g., "₹50 fee cuts RTO 30%, cuts conversion 10%") are informed estimates, not observed customer behavior — and payment-method choice is confounded with geography, income, and pre-existing trust in ways a single regression can't fully separate (see `AB_TEST_DESIGN.md §0`). We should not roll out a customer-facing pricing change to the full high-risk population on the strength of a simulation alone.

## Supporting Evidence

| Metric | No Intervention | Static ₹50 Fee (naive) | Dynamic Engine |
|---|---:|---:|---:|
| Total expected profit (sample) | ₹33.65M | ₹32.95M | ₹35.52M |
| Δ vs. baseline | — | **−2.07%** | **+5.55%** |
| Profit / order | ₹448.69 | ₹439.38 | ₹473.58 |
| High-LTV customers hit with harsh friction | 0 | 851 | **0** (guardrail) |

- **RTO reduction:** Dynamic policy applies `Prepaid_Incentive` (64.3% of flagged orders) or a fee (35.7%) — a deliberate skew toward the carrot, which our model estimates cuts RTO propensity by ~55% for the orders that switch to Prepaid.
- **Conversion impact:** Not yet causally known — this is precisely what the A/B test measures.
- **Customer impact / fairness:** the LTV guardrail is the load-bearing fairness control in this design; it is the direct answer to "what if the model penalizes a good customer" and it is testable (guardrail-trigger rate is a tracked metric).
- **Operational complexity:** Medium — requires real-time scoring at checkout (<150ms budget), a new checkout UI state, and a reason-code pipeline for fee refunds on courier-fault RTOs. Not trivial, but within normal scope for a payments team.
- **Model risk:** PR-AUC of 0.27 means most "positive" predictions are still false positives in absolute count — this is normal for a rare-event problem, but it means the *threshold and guardrail design carry as much weight as the model itself*. We are not comfortable making customer-facing decisions on raw model output without the guardrail layer.

## What Happens Next

1. Ship the Dynamic Intervention Engine to the **experiment population only** (High/Very-High risk band, ≈1–2% of checkout volume) per `AB_TEST_DESIGN.md`.
2. Run for **~3 weeks** (≈7,500 eligible orders total) to detect a 5% relative lift in profit/order at 80% power.
3. Re-evaluate against the pre-registered primary metric and guardrails (abandonment, uninstalls, support tickets).
4. If the causal effect holds: **graduate to GO**, expand eligibility gradually (risk-band by risk-band), keep the LTV guardrail permanently in the production policy, and monitor for model drift.
5. If the causal effect doesn't hold, or a guardrail trips: **NO-GO on this mechanism as designed** — return to the Opportunity Solution Tree (`PRD.md §4`) and evaluate the non-friction levers (delivery-confirmation calls, seller-side packaging fixes) instead of forcing a pricing lever that the data doesn't support.

**Bottom line:** the data supports *building and testing* dynamic friction, not *launching* it blind, and it actively argues against the naive static-fee version that a first pass at this problem usually proposes.
