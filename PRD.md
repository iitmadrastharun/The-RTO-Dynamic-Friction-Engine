# PRD: Dynamic CoD Friction Engine

**Owner:** Product Manager, Trust & Payments
**Status:** Draft for Review
**Related docs:** `risk_engine.py`, `intervention_engine.py`, `AB_TEST_DESIGN.md`, `DECISION_MEMO.md`

---

## 1. Problem Statement

Cash-on-Delivery (CoD) orders convert better than Prepaid in India's price-sensitive, trust-scarce Tier-2/3 markets — but they carry a structurally higher Return-to-Origin (RTO) rate. Our synthetic-but-realistic analysis shows an overall CoD RTO rate of **16.2%** vs **5.7%** for Prepaid, and specific cohorts (CoD + Tier-3 city + Electronics + Late-Night order) spike to **~66%**.

Every RTO destroys unit economics twice: once on forward logistics (already spent) and again on reverse logistics, restocking, and often a written-down or unsellable item. At scale, this is not noise — it's a systematic tax on the P&L that concentrates in identifiable, predictable pockets of the order book.

The naive fix — a blanket CoD booking fee — is not obviously correct. Our own unit-economics simulation shows that applying a flat ₹50 fee to the two highest-risk cohorts, uniformly, **reduces total expected profit by ~2%**, because the conversion loss it causes outweighs the RTO savings for a meaningful share of those orders. The problem isn't "should we add friction" — it's "*for whom, when, and how much*."

## 2. Target Persona

**Primary: The High-Risk, Low-Switching-Cost CoD Shopper**
- Orders from Tier-2/3 cities, frequently from Electronics/Mobiles/Fashion.
- Skews first-time or low-tenure ("testing the platform," low commitment).
- Places a disproportionate share of orders late at night (impulse/browsing behavior with lower purchase conviction).
- Low historical order count, so the platform has little behavioral trust signal on them yet.

**Explicitly NOT the target: The High-LTV Occasional CoD User**
- A returning, high-lifetime-value customer who happens to fall into a risky *cohort* (e.g., orders Electronics from a Tier-3 pincode) but has a clean personal RTO history. Misapplying friction here is a retention risk, not a fraud-prevention win — this persona is explicitly protected by a guardrail (Section 5).

## 3. Proposed Solution & User Flow

Replace the single blunt lever ("charge everyone ₹50 CoD fee" or "do nothing") with a **risk-scored, per-order intervention decision** made at checkout:

1. At checkout, the order context (user history, cart, category, city tier, time, device) is sent to the **RTO Risk Engine**, which returns a 0–100 risk score and band (Low/Medium/High/Very High) in <150ms.
2. The **Intervention Engine** looks up the score, the customer's LTV tier, and current experiment allocation, and selects one of:
   - **No friction** — proceed as normal (default for Low/Medium risk).
   - **₹20 CoD booking fee** — light nudge, refunded/adjusted against the order if delivered.
   - **₹50 CoD booking fee** — stronger nudge, for High/Very-High risk, non-refundable, communicated clearly.
   - **Prepaid incentive** — a small discount (e.g., ₹30 off or free express delivery) for switching to Prepaid instead of a punitive fee. Preferred lever wherever it clears the same profit bar, since it's a carrot rather than a stick.
   - **CoD restriction** — CoD option hidden entirely; reserved for Very-High-risk + repeat-offender history only, gated behind manual policy review, not pure model output.
3. The chosen intervention is rendered at the payment-method selection step: a clearly labeled fee line-item, or a Prepaid-incentive banner, never a silent price change.
4. Customer completes checkout (converts) or abandons (doesn't). Either outcome is logged.
5. Order proceeds; delivery outcome (Delivered / RTO) is later joined back to the original risk score + intervention for model feedback and experiment measurement.

## 4. Opportunity Solution Tree (summary)

```
Outcome: Improve CoD contribution margin without hurting overall conversion
│
├─ Opportunity: Reduce RTO rate on the riskiest CoD orders
│    ├─ Solution: Dynamic risk-based friction (THIS PRD)
│    ├─ Solution: Pre-delivery IVR/WhatsApp order confirmation call
│    └─ Solution: Seller-side packaging/QC improvements for high-RTO categories
│
├─ Opportunity: Shift risky CoD demand to Prepaid without losing the order
│    ├─ Solution: Prepaid incentive at checkout (part of THIS PRD)
│    └─ Solution: UPI-first default payment sheet for repeat high-risk users
│
└─ Opportunity: Reduce false-positive customer friction (protect good customers)
     ├─ Solution: LTV-based guardrail in the Intervention Engine (THIS PRD)
     └─ Solution: Post-hoc appeals/refund flow for wrongly-charged fees
```

This PRD covers the starred solutions; the others are backlog candidates that attack the same opportunities from a different angle and aren't mutually exclusive with this launch.

## 5. Edge Cases

| Edge Case | Handling |
|---|---|
| Model falsely flags a high-LTV, low-personal-risk user as high-risk (cohort effect, not personal history) | **Guardrail:** customers in the top LTV quartile can never receive `Fee_50_COD` or `COD_Restriction` from the model alone, regardless of score. At most `Fee_20_COD` or `Prepaid_Incentive`. |
| New user, zero history, model has nothing to go on | Falls back to cohort-level prior (city tier x category x time) with wider uncertainty bands; default to `Prepaid_Incentive` rather than a fee, since punishing a stranger for their first order is poor first-impression UX. |
| Customer disputes a charged fee after a legitimate failed delivery (not their fault — e.g., courier issue) | Fee is refunded automatically if RTO reason code = logistics/courier fault, not customer refusal. Requires reason-code capture at the courier/NDR (non-delivery report) stage. |
| Risk model is down / times out | Fail open to `No_Friction` — never fail closed and block checkout. Friction is a margin optimization, not a security gate; availability > precision here. |
| App version doesn't support the new checkout fee UI | Server-side feature flag by app version; unsupported versions get `No_Friction` fallback rather than a broken payment screen. |
| Seasonal/flash-sale spikes change the risk distribution (e.g., Diwali sale draws many new, high-value, first-time buyers) | Risk thresholds and band cutoffs are reviewed and can be temporarily loosened ahead of known high-volume, high-new-user events; not a "set and forget" model. |
| Repeat CoD refusers (customer has refused delivery 3+ times) | Escalates beyond fee-based nudging to `COD_Restriction`, but requires the repeat-offense count itself (not just the risk score) — a distinct, harder rule, and is logged for manual Trust & Safety review before permanent restriction. |

## 6. Telemetry & Event Tracking

Every event below must carry `order_session_id`, `user_id` (hashed), `risk_score`, `risk_band`, and `experiment_variant` so downstream analysis can always join back to the decision that was made.

| Event | Fired When | Key Properties |
|---|---|---|
| `rto_risk_scored` | Risk Engine returns a score for a checkout session | `risk_score` (0-100), `risk_band`, `model_version`, `latency_ms`, `top_risk_factors` (array, for internal debugging only — never shown to user) |
| `intervention_assigned` | Intervention Engine selects an action | `intervention_type`, `fee_amount`, `guardrail_triggered` (bool — was a harsh action downgraded due to LTV?), `expected_profit_delta` |
| `friction_ui_shown` | Fee/incentive UI actually renders to the customer | `intervention_type`, `payment_options_shown` |
| `friction_ui_interacted` | Customer taps into the fee/incentive detail (e.g., "why is there a fee?" tooltip) | `interaction_type` |
| `payment_method_selected` | Customer picks CoD or Prepaid at checkout | `payment_method`, `switched_from_default` (bool) |
| `checkout_completed` | Order is placed | `order_id`, `order_value_inr`, `fee_charged_inr`, `final_payment_method` |
| `checkout_abandoned` | Customer exits checkout without completing | `abandonment_step`, `time_on_step_ms` — critical for the conversion-loss guardrail metric |
| `order_delivery_outcome` | Order resolves (Delivered / RTO / Cancelled) — backend event, joined later | `outcome`, `rto_reason_code`, `delivery_attempts` |
| `fee_refund_issued` | A charged fee is refunded (e.g., courier-fault RTO or customer appeal) | `refund_reason`, `refund_amount_inr` |

**Why this matters:** without `risk_score` + `intervention_type` on every event through to `order_delivery_outcome`, we cannot compute realized (not just expected) profit per intervention, cannot audit the guardrail, and cannot run the A/B test described in `AB_TEST_DESIGN.md`.
