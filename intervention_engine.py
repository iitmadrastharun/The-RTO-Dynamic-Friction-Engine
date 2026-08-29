"""
intervention_engine.py
=======================
Dynamic Friction / Intervention Engine + Unit Economics Model.

Given a risk-scored order (see risk_engine.py), this module:
  1. Defines 5 candidate interventions.
  2. Computes Expected Profit for each intervention, per order.
  3. Picks the profit-maximizing intervention subject to a CX guardrail
     (never apply the harshest interventions to High-LTV customers based on
     a risk score alone).
  4. Aggregates results into customer segments and compares three policies:
     No Intervention vs Static ₹50 Fee (top-2 risk cohorts) vs Dynamic Policy.

Run:
    python intervention_engine.py
Requires:
    ecommerce_orders.csv (from generate_data.py)
Optional:
    scored_orders_sample.csv (from risk_engine.py) — if absent, this script
    falls back to a lightweight heuristic risk score so it still runs standalone.
"""

import numpy as np
import pandas as pd

RANDOM_STATE = 42
rng = np.random.default_rng(RANDOM_STATE)

# ---------------------------------------------------------------------------
# Business assumptions (documented, not hidden in code)
# ---------------------------------------------------------------------------
GROSS_MARGIN_RATE = 0.18          # contribution margin on order_value_inr, pre-logistics
BASE_LOGISTICS_COST_RATE = 0.06   # forward logistics as % of order value
RTO_REVERSE_LOGISTICS_FLAT = 100  # ₹ flat loss on a confirmed RTO (matches SQL Query 3)
RTO_RESTOCK_LOSS_RATE = 0.04      # additional damage/restocking loss as % of order value on RTO

FEE_20 = 20
FEE_50 = 50
CONVERSION_DROP_FEE_20 = 0.05     # mild friction -> smaller conversion hit
CONVERSION_DROP_FEE_50 = 0.10     # matches the brief's stated 10% for ₹50 fee
CONVERSION_DROP_PREPAID_INCENTIVE = -0.03   # incentive INCREASES conversion slightly
CONVERSION_DROP_COD_RESTRICTION = 0.35      # blocking CoD entirely is a severe conversion hit

RTO_REDUCTION_FEE_20 = 0.15       # fee-based friction reduces RTO propensity
RTO_REDUCTION_FEE_50 = 0.30       # matches the brief's stated 30% for ₹50 fee
RTO_REDUCTION_PREPAID_INCENTIVE = 0.55      # prepaid orders essentially can't RTO for non-payment reasons
RTO_REDUCTION_COD_RESTRICTION = 1.00        # no CoD = no CoD-RTO, but see conversion cost above

INTERVENTIONS = [
    "No_Friction", "Fee_20_COD", "Fee_50_COD", "Prepaid_Incentive", "COD_Restriction",
]


# ---------------------------------------------------------------------------
# 1. Load data (with or without ML risk scores)
# ---------------------------------------------------------------------------
def load_scored_data():
    try:
        df = pd.read_csv("scored_orders_sample.csv")
        df["rto_prob"] = df["rto_risk_score"] / 100.0
        print(f"Loaded {len(df):,} ML-scored orders from risk_engine.py output.")
        # customer_lifetime_value already present
        return df
    except FileNotFoundError:
        print("scored_orders_sample.csv not found — falling back to full dataset "
              "with a lightweight heuristic risk score (run risk_engine.py for the ML version).")
        df = pd.read_csv("ecommerce_orders.csv", parse_dates=["order_date"])
        df["order_hour"] = df["order_date"].dt.hour
        # crude heuristic score, just so this script is runnable standalone
        score = (
            0.30 * (df["payment_method"] == "CoD")
            + 0.20 * (df["city_tier"] == "Tier 3")
            + 0.15 * (df["item_category"].isin(["Electronics", "Mobiles"]))
            + 0.15 * (df["order_hour"] >= 22).astype(int)
            + 0.10 * df["is_first_order"].astype(int)
            + 0.10 * df["historical_rto_rate"].fillna(0)
        )
        df["rto_prob"] = np.clip(score / score.max(), 0.01, 0.9)
        return df


# ---------------------------------------------------------------------------
# 2. Expected value math
#    Expected Profit = Expected Revenue - Expected Logistics - Expected RTO
#                       cost - Friction cost - Lost conversion contribution
# ---------------------------------------------------------------------------
def expected_profit_per_order(order_value, base_rto_prob, intervention, ltv):
    """
    Returns expected profit (INR) for ONE order under a given intervention,
    marginalized over: (a) whether the customer converts/completes checkout
    at all, and (b) whether the resulting order RTOs.
    """
    if intervention == "No_Friction":
        conv_delta, rto_delta, fee = 0.0, 0.0, 0
    elif intervention == "Fee_20_COD":
        conv_delta, rto_delta, fee = CONVERSION_DROP_FEE_20, RTO_REDUCTION_FEE_20, FEE_20
    elif intervention == "Fee_50_COD":
        conv_delta, rto_delta, fee = CONVERSION_DROP_FEE_50, RTO_REDUCTION_FEE_50, FEE_50
    elif intervention == "Prepaid_Incentive":
        conv_delta, rto_delta, fee = CONVERSION_DROP_PREPAID_INCENTIVE, RTO_REDUCTION_PREPAID_INCENTIVE, 0
    elif intervention == "COD_Restriction":
        conv_delta, rto_delta, fee = CONVERSION_DROP_COD_RESTRICTION, RTO_REDUCTION_COD_RESTRICTION, 0
    else:
        raise ValueError(intervention)

    conversion_prob = np.clip(1 - conv_delta, 0.01, 1.0)
    adj_rto_prob = np.clip(base_rto_prob * (1 - rto_delta), 0.0, 0.98)

    gross_margin = order_value * GROSS_MARGIN_RATE
    forward_logistics = order_value * BASE_LOGISTICS_COST_RATE
    rto_cost = adj_rto_prob * (RTO_REVERSE_LOGISTICS_FLAT + order_value * RTO_RESTOCK_LOSS_RATE)
    friction_revenue = fee * conversion_prob  # fee only collected if customer still checks out
    lost_contribution = (1 - conversion_prob) * gross_margin  # contribution we forgo on non-converts

    expected_profit = (
        conversion_prob * (gross_margin - forward_logistics - rto_cost + friction_revenue)
        - lost_contribution
    )
    return expected_profit, adj_rto_prob, conversion_prob


def cx_guardrail_allowed(intervention, ltv, ltv_high_threshold):
    """
    Explicit CX guardrail per the brief: 'What if the model incorrectly
    penalizes a valuable customer?' -> High-LTV customers are NEVER routed to
    the harshest interventions (COD_Restriction, and Fee_50) purely on a risk
    score; they can at most receive the mildest paid nudge (Fee_20) or the
    Prepaid_Incentive (which is a carrot, not a stick).
    """
    if ltv >= ltv_high_threshold and intervention in ("COD_Restriction", "Fee_50_COD"):
        return False
    return True


def choose_best_intervention(row, ltv_high_threshold):
    best_int, best_profit = "No_Friction", -np.inf
    details = {}
    for intervention in INTERVENTIONS:
        if not cx_guardrail_allowed(intervention, row["customer_lifetime_value"], ltv_high_threshold):
            continue
        ep, adj_rto, conv = expected_profit_per_order(
            row["order_value_inr"], row["rto_prob"], intervention, row["customer_lifetime_value"]
        )
        details[intervention] = ep
        if ep > best_profit:
            best_profit, best_int = ep, intervention
    return best_int, best_profit, details


# ---------------------------------------------------------------------------
# 3. Segmentation
# ---------------------------------------------------------------------------
def build_segments(df):
    rto_high = df["rto_prob"] >= df["rto_prob"].quantile(0.75)
    ltv_high = df["customer_lifetime_value"] >= df["customer_lifetime_value"].quantile(0.75)

    conditions = [
        rto_high & ltv_high,
        rto_high & ~ltv_high,
        ~rto_high & ltv_high,
        df["historical_orders"] <= 1,
        (df["historical_orders"] > 1) & (~rto_high) & (~ltv_high),
    ]
    choices = [
        "High RTO / High LTV", "High RTO / Low LTV", "Low RTO / High LTV",
        "New Users", "Returning / Core",
    ]
    df["segment"] = np.select(conditions, choices, default="Other")
    return df


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    df = load_scored_data()
    df = build_segments(df)
    ltv_high_threshold = df["customer_lifetime_value"].quantile(0.75)

    # ------------------------------------------------------------------
    # Policy A: No Intervention (current state)
    # ------------------------------------------------------------------
    ep_a, rto_a, conv_a = expected_profit_per_order(
        df["order_value_inr"], df["rto_prob"], "No_Friction", df["customer_lifetime_value"]
    )
    df["profit_no_intervention"] = ep_a

    # ------------------------------------------------------------------
    # Policy B: Static ₹50 fee applied to the TOP-2 highest-risk cohorts
    # (mirrors Task 3's literal instruction: blunt, cohort-level rule)
    # ------------------------------------------------------------------
    cohort_risk = (
        df.groupby(["payment_method", "city_tier", "item_category"])["rto_prob"]
        .mean()
        .sort_values(ascending=False)
    )
    top2_cohorts = set(cohort_risk.head(2).index)
    df["_cohort_key"] = list(zip(df["payment_method"], df["city_tier"], df["item_category"]))
    df["in_top2_cohort"] = df["_cohort_key"].isin(top2_cohorts)

    def static_policy_profit(row):
        intervention = "Fee_50_COD" if (row["in_top2_cohort"] and row["payment_method"] == "CoD") else "No_Friction"
        ep, _, _ = expected_profit_per_order(
            row["order_value_inr"], row["rto_prob"], intervention, row["customer_lifetime_value"]
        )
        return ep

    df["profit_static_fee50"] = df.apply(static_policy_profit, axis=1)

    # ------------------------------------------------------------------
    # Policy C: Dynamic Intervention Engine (order-level EV-maximizing choice
    # subject to the CX guardrail)
    # ------------------------------------------------------------------
    print("Scoring dynamic interventions (this evaluates 5 options per order)...")
    chosen, profits = [], []
    for _, row in df.iterrows():
        c, p, _ = choose_best_intervention(row, ltv_high_threshold)
        chosen.append(c)
        profits.append(p)
    df["dynamic_intervention"] = chosen
    df["profit_dynamic"] = profits

    # ------------------------------------------------------------------
    # Compare policies
    # ------------------------------------------------------------------
    summary = pd.DataFrame({
        "policy": ["No Intervention", "Static ₹50 Fee (Top-2 Cohorts)", "Dynamic Intervention Engine"],
        "total_expected_profit_inr": [
            df["profit_no_intervention"].sum(),
            df["profit_static_fee50"].sum(),
            df["profit_dynamic"].sum(),
        ],
        "profit_per_order_inr": [
            df["profit_no_intervention"].mean(),
            df["profit_static_fee50"].mean(),
            df["profit_dynamic"].mean(),
        ],
    })
    summary["incremental_profit_vs_baseline_inr"] = (
        summary["total_expected_profit_inr"] - summary["total_expected_profit_inr"].iloc[0]
    )
    summary["incremental_pct"] = (
        100 * summary["incremental_profit_vs_baseline_inr"] / summary["total_expected_profit_inr"].iloc[0]
    )

    print("\n=== POLICY COMPARISON: No Intervention vs Static Fee vs Dynamic Engine ===")
    print(summary.round(2).to_string(index=False))

    # ------------------------------------------------------------------
    # Dynamic intervention mix
    # ------------------------------------------------------------------
    print("\n=== Dynamic Engine: Intervention Mix ===")
    print(df["dynamic_intervention"].value_counts(normalize=True).mul(100).round(1).astype(str) + "%")

    # ------------------------------------------------------------------
    # Segment-level breakdown
    # ------------------------------------------------------------------
    seg_summary = df.groupby("segment").agg(
        orders=("order_id", "count") if "order_id" in df.columns else ("rto_prob", "size"),
        avg_rto_prob=("rto_prob", "mean"),
        avg_ltv=("customer_lifetime_value", "mean"),
        profit_no_intervention=("profit_no_intervention", "mean"),
        profit_dynamic=("profit_dynamic", "mean"),
    )
    seg_summary["most_common_intervention"] = df.groupby("segment")["dynamic_intervention"].agg(
        lambda s: s.value_counts().idxmax()
    )
    seg_summary["profit_lift_per_order"] = (
        seg_summary["profit_dynamic"] - seg_summary["profit_no_intervention"]
    )
    print("\n=== Segment-Level Summary ===")
    print(seg_summary.round(2).to_string())

    # ------------------------------------------------------------------
    # Guardrail sanity check: how many High-LTV customers would the NAIVE
    # static policy have hit vs. how many the dynamic engine protects
    # ------------------------------------------------------------------
    high_ltv_mask = df["customer_lifetime_value"] >= ltv_high_threshold
    naive_hits = df.loc[high_ltv_mask & df["in_top2_cohort"] & (df["payment_method"] == "CoD")]
    dynamic_harsh_hits = df.loc[
        high_ltv_mask & df["dynamic_intervention"].isin(["Fee_50_COD", "COD_Restriction"])
    ]
    print(f"\n=== CX Guardrail Check: 'What if we penalize a valuable customer?' ===")
    print(f"High-LTV customers hit with ₹50 fee under STATIC policy: {len(naive_hits):,}")
    print(f"High-LTV customers hit with harsh intervention under DYNAMIC policy: {len(dynamic_harsh_hits):,}")
    print("(Dynamic engine's guardrail caps harsh interventions for top-quartile LTV "
          "customers regardless of risk score — protecting revenue relationships "
          "that a blunt cohort rule would damage.)")

    df.drop(columns=["_cohort_key"]).to_csv("intervention_results.csv", index=False)
    print("\nSaved intervention_results.csv")


if __name__ == "__main__":
    main()
