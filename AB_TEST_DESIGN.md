# A/B Test & Experiment Design: Dynamic CoD Friction

**Related:** `PRD.md` (feature spec), `intervention_engine.py` (EV model this experiment validates), `DECISION_MEMO.md` (final call)

---

## 0. Why we're testing instead of just launching

Our offline EV simulation says the Dynamic Intervention Engine lifts expected profit **+5.55%** vs. no intervention, and that a naive static fee actually **destroys** ~2% of profit. But that simulation is built on:

- A **generative model of RTO probability** that is our best estimate, not ground truth.
- **Assumed** conversion-drop elasticities (10% drop for a ₹50 fee, etc.) that are informed guesses, not measured behavior.
- **Correlational** relationships between order attributes and RTO — e.g., CoD orders correlate with higher RTO, but CoD is also correlated with lower-income, higher-distrust geographies that might RTO for unrelated reasons (financial stress, joint-family order disputes, etc.). **Correlation ≠ causation**: we cannot claim the fee itself *causes* lower RTO, or that removing CoD *causes* the RTO reduction we see in the data, without an experiment that actually manipulates the treatment and holds everything else constant.

Confounders we're explicitly worried about: city-tier-level logistics-partner quality (some pincodes just have worse couriers, independent of payment method), seasonality (festive-season impulse buying), and self-selection (risk-averse customers may already prefer Prepaid, so the CoD population is pre-selected for different behavior, not merely "made risky by CoD").

An RCT is the only way to isolate the causal effect of the *intervention itself* from these confounders.

## 1. Hypothesis

> Applying a risk-scored dynamic friction intervention (fee or prepaid incentive) to High/Very-High RTO-risk CoD checkouts **causally reduces net RTO-driven losses** by more than it costs in lost conversion, resulting in a **statistically significant increase in net profit per eligible order**, without a statistically significant increase in checkout abandonment or a directional increase in app uninstalls, beyond pre-specified guardrail thresholds.

## 2. Randomization Unit & Population

- **Randomization unit:** `user_id` (not order/session). The treatment is meant to shape a customer's payment behavior and trust with the platform over time, and it must not be possible for the same person to see both fee and no-fee experiences across sessions (which would confuse them and pollute conversion measurement). User-level assignment also lets us measure retention/uninstall guardrails coherently at the person level.
- **Eligible population:** checkout sessions where `risk_band ∈ {High, Very High}` (this is roughly the top ~1–2% of order volume in our simulation — the segment the intervention is actually designed to touch). Low/Medium-risk orders are excluded from the experiment population entirely since neither arm treats them differently; including them would just dilute the effect and inflate the required sample size for no reason.
- **Sticky bucketing:** once a `user_id` is assigned to a variant, they stay in it for the duration of the experiment, even if their risk score fluctuates session to session.

## 3. Control vs. Variant

| Arm | Definition |
|---|---|
| **Control** | Current state — no friction shown regardless of risk score (`No_Friction` for everyone, matching today's production behavior). |
| **Variant** | Dynamic Intervention Engine live — eligible users see `Fee_20_COD`, `Fee_50_COD`, or `Prepaid_Incentive` per the model + guardrail logic in `intervention_engine.py`. (`COD_Restriction` is excluded from this first experiment — held back for a later, narrower test given its severity.) |

A single Control-vs-Variant split is preferred over testing all 5 interventions head-to-head simultaneously: the offline EV model already found `Prepaid_Incentive` and `Fee_50_COD` dominate for our synthetic elasticities, and splitting traffic 5 ways would multiply the required sample size and timeline. **A follow-up experiment**, once the top-line effect is confirmed, should A/B the *fee vs. incentive* framing against each other specifically.

## 4. Metrics

### Primary Metric
**Net profit per eligible order** (order value × margin − forward logistics − expected RTO cost + fee revenue, realized — not modeled — using actual delivery outcomes joined back via `order_delivery_outcome`).

### Secondary Metrics
- RTO rate among eligible orders (Control vs. Variant)
- Checkout conversion rate among eligible sessions
- Payment-method mix shift (CoD → Prepaid) among eligible users
- Repeat purchase rate at 30/60 days for eligible users (does friction hurt or help retention?)
- Fee refund rate (proxy for false-positive / dispute burden)

### Guardrail / Counter Metrics
(Experiment auto-flags for review, does not necessarily auto-stop, if these move adversely beyond threshold)
- **Checkout abandonment rate** — must not increase by more than 2 percentage points (absolute) in the Variant vs. Control among eligible sessions.
- **App uninstall rate** (7-day post-checkout) — must not show a statistically significant increase.
- **Customer support contact rate** (fee-related tickets) — tracked, capped informally; a spike here is an early qualitative warning even before it's "significant."
- **NPS / in-app rating**, if available for the eligible cohort — directional check only, likely underpowered at this sample size.

## 5. Baseline, MDE, α, Power, Sample Size

Using our simulated eligible population (High/Very-High risk band, ≈1.2% of total checkout volume):

| Parameter | Value | Note |
|---|---|---|
| Baseline mean profit/order (Control) | ≈ ₹940 | From offline simulation on the eligible segment; will be re-baselined on real data before launch |
| Std. dev of profit/order | ≈ ₹725 | High variance — order values span categories from Grocery to Mobiles |
| Minimum Detectable Effect (MDE) | 5% relative lift (≈ ₹47/order) | Smallest lift that would justify the operational complexity of shipping this |
| α (two-sided) | 0.05 | Standard |
| Power (1−β) | 0.80 | Standard |
| **Required sample size** | **≈ 3,734 eligible orders per arm** (≈ 7,470 total) | Two-sample t-test sample size formula for a continuous metric |

**Duration estimate:** at 50,000 daily active checkouts platform-wide and eligible orders ≈1.2% of volume (~590 eligible checkouts/day, split 50/50 across arms → ~295/day/arm), reaching ~3,734 per arm takes:

**≈ 13 calendar days**, rounded up to **2.5–3 weeks** to absorb a day-of-week cycle (weekday vs weekend CoD mix differs) and leave buffer for the ramp period below.

*If actual eligible volume differs materially from the simulation once real risk scores are live, re-run this calculation on Day 3 of the ramp using observed data before committing to a fixed end date.*

## 6. Rollout, Stopping Criteria, and Risk Controls

- **Ramp:** 5% of eligible traffic → variant for 48 hours (sanity/bug check, not for statistical inference) → 50/50 for the full duration if no guardrail trips.
- **No peeking / fixed horizon:** the primary metric is evaluated once, at the pre-registered sample size / duration — not via repeated significance testing as data accrues (which inflates false-positive rate). Guardrails may be monitored continuously for safety, with a defined emergency-stop threshold (e.g., abandonment +5pp absolute) distinct from the analysis-time guardrail check (+2pp).
- **Novelty effects:** because a visible fee/incentive is a new UI element, expect an initial spike in `friction_ui_interacted` (curiosity clicks) and possibly an initial abandonment bump that fades. We will report both the full-window effect and a "post-novelty" effect using only days 8+ of the experiment as a robustness check, not as the primary readout.
- **Contamination risks:**
  - Users who share devices/accounts within a household could see inconsistent experiences — acceptable at this scale, but flagged.
  - Customer support / social media chatter about "some people get charged a fee and some don't" could bias behavior in either arm if it spreads — monitor support tickets and social mentions for signs of this during the ramp.
  - Marketing/CRM campaigns that reference "free CoD" broadly must be paused or scoped to exclude the eligible segment for the experiment duration, or they'll directly contradict what Variant users see.

## 7. Segmentation Analysis (planned, post-hoc)

Pre-registered cuts to check for heterogeneous effects (not for cherry-picking a positive result, but to inform the segment-level policy in the Decision Memo):
- By `risk_band` (High vs. Very High) — does the effect hold at both severities?
- By `new_vs_returning_user` — first-time buyers may react to a fee very differently (more elastic) than returning customers.
- By `city_tier` — Tier-3 users are more price-sensitive; conversion elasticity to the fee may be steeper there even if RTO reduction is also steeper.
- By intervention type actually assigned (`Fee_20`, `Fee_50`, `Prepaid_Incentive`) — even within Variant, isolate which lever is doing the work.

All segment cuts are reported with confidence intervals and explicitly labeled as **exploratory / hypothesis-generating**, not used to override the primary pre-registered result.
