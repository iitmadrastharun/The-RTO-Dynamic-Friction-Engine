"""
generate_data.py
=================
Synthetic data generator for the RTO (Return-to-Origin) & Dynamic Friction Engine.

Produces `ecommerce_orders.csv` — 500,000 synthetic CoD/Prepaid e-commerce orders
with realistic, NONLINEAR and INTERACTING drivers of RTO risk baked in, so that
downstream SQL/EDA/ML work actually has to dig for the signal instead of reading
it off a single column.

Design principle: no single feature should trivially predict RTO. Risk is driven
by combinations (e.g. CoD + Tier-3 + Electronics + late-night + first-time buyer),
and every risk-raising factor is diluted with noise so the "ground truth" pattern
is recoverable but not obvious.

Run:
    python generate_data.py
Output:
    ecommerce_orders.csv  (~500,000 rows)
"""

import numpy as np
import pandas as pd
from faker import Faker
from datetime import datetime, timedelta

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
N_ORDERS = 500_000
N_USERS = 120_000          # repeat customers -> enables historical/CLV features
RANDOM_SEED = 42
START_DATE = datetime(2024, 1, 1)
END_DATE = datetime(2024, 12, 31)

rng = np.random.default_rng(RANDOM_SEED)
fake = Faker("en_IN")
Faker.seed(RANDOM_SEED)

CITY_TIERS = np.array(["Tier 1", "Tier 2", "Tier 3"])
CITY_TIER_P = np.array([0.35, 0.40, 0.25])

CATEGORIES = np.array(
    ["Electronics", "Fashion", "Home & Kitchen", "Beauty", "Grocery",
     "Mobiles", "Footwear", "Books", "Toys", "Appliances"]
)
CATEGORY_P = np.array([0.14, 0.20, 0.12, 0.10, 0.09, 0.10, 0.09, 0.05, 0.06, 0.05])

PAYMENT_METHODS = np.array(["CoD", "Prepaid"])
DEVICE_OS = np.array(["Android", "iOS"])
DEVICE_OS_P = np.array([0.82, 0.18])          # India skews heavily Android
APP_VERSIONS = np.array(["6.2.0", "6.3.1", "6.4.0", "6.5.2", "7.0.0"])
WAREHOUSE_ZONES = np.array(["North", "South", "East", "West", "Central"])
SELLER_TYPES = np.array(["Marketplace_3P", "Flagship_1P", "Local_SmallSeller"])
SELLER_TYPE_P = np.array([0.55, 0.20, 0.25])


def random_dates(start, end, n):
    delta = (end - start).days
    offsets = rng.integers(0, delta, size=n)
    seconds = rng.integers(0, 24 * 3600, size=n)
    return [start + timedelta(days=int(d), seconds=int(s)) for d, s in zip(offsets, seconds)]


def time_bucket(hour):
    if 5 <= hour < 12:
        return "Morning"
    elif 12 <= hour < 17:
        return "Afternoon"
    elif 17 <= hour < 22:
        return "Evening"
    else:
        return "Late_Night"  # 22:00 - 04:59


def main():
    print(f"Generating {N_ORDERS:,} synthetic orders...")

    # ------------------------------------------------------------------
    # 1. User pool — repeat customers so history/CLV features are coherent
    # ------------------------------------------------------------------
    user_ids = np.array([f"U{100000 + i}" for i in range(N_USERS)])
    user_tenure_days_pool = rng.integers(1, 1500, size=N_USERS)
    # Power-law-ish order frequency: most users order rarely, a few order a lot
    user_weight = rng.pareto(a=1.8, size=N_USERS) + 0.3
    user_weight = user_weight / user_weight.sum()
    user_pick_idx = rng.choice(N_USERS, size=N_ORDERS, p=user_weight)

    order_ids = np.array([f"ORD{1000000 + i}" for i in range(N_ORDERS)])
    user_id = user_ids[user_pick_idx]
    user_tenure_days = user_tenure_days_pool[user_pick_idx]

    # ------------------------------------------------------------------
    # 2. Core categorical fields
    # ------------------------------------------------------------------
    city_tier = rng.choice(CITY_TIERS, size=N_ORDERS, p=CITY_TIER_P)
    item_category = rng.choice(CATEGORIES, size=N_ORDERS, p=CATEGORY_P)
    device_os = rng.choice(DEVICE_OS, size=N_ORDERS, p=DEVICE_OS_P)
    app_version = rng.choice(APP_VERSIONS, size=N_ORDERS)
    warehouse_zone = rng.choice(WAREHOUSE_ZONES, size=N_ORDERS)
    seller_type = rng.choice(SELLER_TYPES, size=N_ORDERS, p=SELLER_TYPE_P)

    order_dates = random_dates(START_DATE, END_DATE, N_ORDERS)
    hours = np.array([d.hour for d in order_dates])
    time_of_day = np.array([time_bucket(h) for h in hours])

    # Pincode roughly correlated with city tier (not used for modelling, just realism)
    pincode = rng.integers(110001, 855999, size=N_ORDERS)

    # ------------------------------------------------------------------
    # 3. Payment method — CoD propensity varies by tier & category
    #    (Tier-3 + budget categories lean CoD; Tier-1 + premium leans Prepaid)
    # ------------------------------------------------------------------
    base_cod_prob = np.select(
        [city_tier == "Tier 1", city_tier == "Tier 2", city_tier == "Tier 3"],
        [0.35, 0.55, 0.72],
    )
    category_cod_bump = np.where(
        np.isin(item_category, ["Electronics", "Mobiles", "Appliances"]), 0.05, -0.03
    )
    cod_prob = np.clip(base_cod_prob + category_cod_bump + rng.normal(0, 0.05, N_ORDERS), 0.05, 0.95)
    payment_method = np.where(rng.random(N_ORDERS) < cod_prob, "CoD", "Prepaid")

    # ------------------------------------------------------------------
    # 4. Order value — category-driven lognormal, with cart/discount/shipping
    # ------------------------------------------------------------------
    category_base_value = {
        "Electronics": 6500, "Mobiles": 12000, "Appliances": 9000,
        "Fashion": 1400, "Footwear": 1600, "Home & Kitchen": 2200,
        "Beauty": 900, "Grocery": 650, "Books": 450, "Toys": 800,
    }
    base_vals = np.array([category_base_value[c] for c in item_category])
    order_value_inr = np.round(
        base_vals * rng.lognormal(mean=0.0, sigma=0.55, size=N_ORDERS), -1
    ).clip(150, 150000)

    cart_value = np.round(order_value_inr * rng.uniform(1.0, 1.35, N_ORDERS), -1)
    discount_amount = np.round((cart_value - order_value_inr).clip(0), -1)
    shipping_fee = np.where(
        order_value_inr < 500, rng.choice([0, 40, 60], size=N_ORDERS, p=[0.3, 0.4, 0.3]), 0
    )

    delivery_distance_km = np.round(
        np.select(
            [city_tier == "Tier 1", city_tier == "Tier 2", city_tier == "Tier 3"],
            [rng.gamma(2.0, 4, N_ORDERS), rng.gamma(2.2, 6, N_ORDERS), rng.gamma(2.5, 9, N_ORDERS)],
        ), 1,
    )
    delivery_attempts = rng.choice([1, 2, 3], size=N_ORDERS, p=[0.78, 0.17, 0.05])

    # ------------------------------------------------------------------
    # 5. Historical / behavioural features (per user, then joined to orders)
    #    These create realistic user-level signal without leaking the label.
    # ------------------------------------------------------------------
    user_hist_orders = rng.poisson(lam=np.clip(user_tenure_days_pool / 90, 0.5, 20), size=N_USERS) + 1
    # Latent "flakiness" trait per user (unobserved) drives their true RTO tendency
    user_latent_risk = rng.beta(a=2.0, b=8.0, size=N_USERS)   # most users low-risk, long tail
    user_hist_rto_count = rng.binomial(user_hist_orders, np.clip(user_latent_risk, 0.01, 0.9))
    user_hist_rto_rate = np.round(user_hist_rto_count / user_hist_orders, 3)
    user_clv = np.round(
        user_hist_orders * rng.gamma(2.0, 900, N_USERS) * (1 - user_hist_rto_rate * 0.4), -1
    ).clip(0)
    user_prev_cod_orders = np.round(user_hist_orders * rng.uniform(0.2, 0.9, N_USERS)).astype(int)
    user_prev_cod_rtos = np.minimum(
        user_hist_rto_count, np.round(user_prev_cod_orders * rng.uniform(0.5, 1.0, N_USERS)).astype(int)
    )

    historical_orders = user_hist_orders[user_pick_idx]
    historical_rto_count = user_hist_rto_count[user_pick_idx]
    historical_rto_rate = user_hist_rto_rate[user_pick_idx]
    customer_lifetime_value = user_clv[user_pick_idx]
    previous_cod_orders = user_prev_cod_orders[user_pick_idx]
    previous_cod_rtos = user_prev_cod_rtos[user_pick_idx]
    latent_risk = user_latent_risk[user_pick_idx]   # kept internally to drive label, dropped before save

    is_first_order = (historical_orders <= 1).astype(bool)
    new_vs_returning = np.where(is_first_order, "New", "Returning")

    # ------------------------------------------------------------------
    # 6. RTO PROBABILITY MODEL — nonlinear, interaction-heavy, noisy
    #    This is the "ground truth" generative process. It intentionally
    #    mixes main effects, 2-3 way interactions, and randomness so that
    #    naive single-variable analysis under-explains the outcome.
    # ------------------------------------------------------------------
    logit = np.full(N_ORDERS, -2.6)  # baseline ~7% RTO

    # Main effects
    logit += np.where(payment_method == "CoD", 0.55, -0.35)
    logit += np.select(
        [city_tier == "Tier 1", city_tier == "Tier 2", city_tier == "Tier 3"],
        [-0.25, 0.05, 0.45],
    )
    logit += np.where(item_category == "Electronics", 0.30, 0.0)
    logit += np.where(item_category == "Mobiles", 0.20, 0.0)
    logit += np.where(item_category == "Fashion", 0.10, 0.0)
    logit += np.where(time_of_day == "Late_Night", 0.35, 0.0)
    logit += np.where(is_first_order, 0.30, -0.10)
    logit += 2.2 * (latent_risk - latent_risk.mean())          # unobserved-in-real-life trait
    logit += 0.15 * (delivery_attempts - 1)
    logit += np.where(seller_type == "Local_SmallSeller", 0.20, 0.0)
    logit += np.clip((delivery_distance_km - 10) / 40, -0.2, 0.4)
    logit += np.clip((order_value_inr - 3000) / 15000, -0.15, 0.5)  # pricier -> slightly more RTO
    logit -= np.clip(historical_orders / 25, 0, 0.5)                 # tenure/loyalty dampens risk

    # --- KEY INTERACTION EFFECTS (the "hidden" signal analysts must find) ---
    # CoD + Tier-3 + Electronics + Late-Night => sharp risk spike
    combo_mask = (
        (payment_method == "CoD") & (city_tier == "Tier 3") &
        (item_category == "Electronics") & (time_of_day == "Late_Night")
    )
    logit += np.where(combo_mask, 1.55, 0.0)

    # CoD + first-time buyer + high order value => "testing the waters" fraud/remorse pattern
    combo_mask2 = (payment_method == "CoD") & is_first_order & (order_value_inr > 5000)
    logit += np.where(combo_mask2, 0.65, 0.0)

    # Prepaid dampens almost everything, even the risky combos, but not entirely
    logit -= np.where((payment_method == "Prepaid") & combo_mask, 0.9, 0.0)

    # Random noise (irreducible / unmodelable variance)
    logit += rng.normal(0, 0.55, N_ORDERS)

    rto_prob = 1 / (1 + np.exp(-logit))
    rto_prob = np.clip(rto_prob, 0.01, 0.92)
    rto_status = rng.random(N_ORDERS) < rto_prob

    # ------------------------------------------------------------------
    # 7. Assemble & save
    # ------------------------------------------------------------------
    df = pd.DataFrame({
        "order_id": order_ids,
        "user_id": user_id,
        "order_date": [d.strftime("%Y-%m-%d %H:%M:%S") for d in order_dates],
        "city_tier": city_tier,
        "pincode": pincode,
        "order_value_inr": order_value_inr.astype(int),
        "cart_value": cart_value.astype(int),
        "discount_amount": discount_amount.astype(int),
        "shipping_fee": shipping_fee.astype(int),
        "item_category": item_category,
        "payment_method": payment_method,
        "time_of_day": time_of_day,
        "device_os": device_os,
        "app_version": app_version,
        "seller_type": seller_type,
        "warehouse_zone": warehouse_zone,
        "delivery_distance_km": delivery_distance_km,
        "delivery_attempts": delivery_attempts,
        "user_tenure_days": user_tenure_days,
        "historical_orders": historical_orders,
        "historical_rto_count": historical_rto_count,
        "historical_rto_rate": historical_rto_rate,
        "customer_lifetime_value": customer_lifetime_value.astype(int),
        "previous_cod_orders": previous_cod_orders,
        "previous_cod_rtos": previous_cod_rtos,
        "is_first_order": is_first_order,
        "new_vs_returning_user": new_vs_returning,
        "rto_status": rto_status,
    })

    df.sort_values("order_date", inplace=True)
    df.reset_index(drop=True, inplace=True)

    out_path = "ecommerce_orders.csv"
    df.to_csv(out_path, index=False)

    print(f"Saved {len(df):,} rows to {out_path}")
    print(f"Overall RTO rate: {df['rto_status'].mean():.2%}")
    print(f"CoD RTO rate: {df.loc[df.payment_method=='CoD','rto_status'].mean():.2%}")
    print(f"Prepaid RTO rate: {df.loc[df.payment_method=='Prepaid','rto_status'].mean():.2%}")
    combo = df[
        (df.payment_method == "CoD") & (df.city_tier == "Tier 3") &
        (df.item_category == "Electronics") & (df.time_of_day == "Late_Night")
    ]
    print(f"CoD+Tier3+Electronics+LateNight RTO rate: {combo['rto_status'].mean():.2%} "
          f"(n={len(combo):,})")


if __name__ == "__main__":
    main()
