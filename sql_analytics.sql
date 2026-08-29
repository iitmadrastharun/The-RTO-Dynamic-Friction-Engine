-- =============================================================================
-- sql_analytics.sql
-- Advanced SQL Analytics — RTO & Dynamic Friction Engine
-- Target: PostgreSQL 13+, table `orders` (loaded from ecommerce_orders.csv)
-- =============================================================================
-- Assumed schema (adjust types to taste on load):
--
-- CREATE TABLE orders (
--     order_id                 TEXT PRIMARY KEY,
--     user_id                  TEXT,
--     order_date                TIMESTAMP,
--     city_tier                 TEXT,
--     pincode                   INT,
--     order_value_inr           NUMERIC,
--     cart_value                NUMERIC,
--     discount_amount           NUMERIC,
--     shipping_fee              NUMERIC,
--     item_category              TEXT,
--     payment_method             TEXT,   -- 'CoD' | 'Prepaid'
--     time_of_day                TEXT,   -- 'Morning'|'Afternoon'|'Evening'|'Late_Night'
--     device_os                  TEXT,
--     app_version                TEXT,
--     seller_type                TEXT,
--     warehouse_zone              TEXT,
--     delivery_distance_km        NUMERIC,
--     delivery_attempts           INT,
--     user_tenure_days            INT,
--     historical_orders           INT,
--     historical_rto_count        INT,
--     historical_rto_rate         NUMERIC,
--     customer_lifetime_value      NUMERIC,
--     previous_cod_orders          INT,
--     previous_cod_rtos            INT,
--     is_first_order                BOOLEAN,
--     new_vs_returning_user          TEXT,
--     rto_status                     BOOLEAN
-- );
-- =============================================================================


-- =============================================================================
-- QUERY 1: TOP 5 HIGHEST-RISK COHORTS (statistically significant, n >= 1000)
-- -----------------------------------------------------------------------------
-- Business question: "Where do we lose the most money, per order, and is the
-- sample large enough that we trust the rate rather than dismissing it as noise?"
--
-- Approach: build a cohort key from the levers Product can actually act on
-- (payment method x city tier x category x time-of-day), then rank cohorts by
-- RTO rate subject to a minimum volume floor so a 3-order 100%-RTO cohort
-- doesn't top the list.
-- =============================================================================

WITH cohort_stats AS (
    SELECT
        payment_method,
        city_tier,
        item_category,
        time_of_day,
        COUNT(*)                                   AS order_count,
        SUM(CASE WHEN rto_status THEN 1 ELSE 0 END) AS rto_count,
        ROUND(
            100.0 * SUM(CASE WHEN rto_status THEN 1 ELSE 0 END) / COUNT(*), 2
        )                                            AS rto_rate_pct,
        ROUND(AVG(order_value_inr), 0)                AS avg_order_value,
        -- Flat ₹100 logistics loss assumption per RTO (see Query 3 for detail)
        SUM(CASE WHEN rto_status THEN 1 ELSE 0 END) * 100 AS estimated_rto_loss_inr
    FROM orders
    GROUP BY payment_method, city_tier, item_category, time_of_day
),
significant_cohorts AS (
    -- Statistical-significance floor: without a minimum n, a cohort with
    -- 4 orders and 3 RTOs looks scarier than it is. 1,000 orders keeps the
    -- binomial standard error tight enough (~±1.5pp at 95% CI for a ~15% rate).
    SELECT
        *,
        -- Wilson-ish lower-bound sanity check (approx, for illustration):
        -- rate - 1.96*sqrt(rate*(1-rate)/n) gives a conservative "worst case" rate
        ROUND(
            100.0 * (
                (rto_count::NUMERIC / order_count) -
                1.96 * SQRT((rto_count::NUMERIC / order_count) * (1 - rto_count::NUMERIC / order_count) / order_count)
            ), 2
        ) AS rto_rate_lower_bound_95ci_pct
    FROM cohort_stats
    WHERE order_count >= 1000
)
SELECT
    payment_method,
    city_tier,
    item_category,
    time_of_day,
    order_count,
    rto_count,
    rto_rate_pct,
    rto_rate_lower_bound_95ci_pct,
    avg_order_value,
    estimated_rto_loss_inr
FROM significant_cohorts
ORDER BY rto_rate_pct DESC
LIMIT 5;


-- =============================================================================
-- QUERY 2: ROLLING 7-DAY RTO RATE BY CITY TIER (WINDOW FUNCTIONS)
-- -----------------------------------------------------------------------------
-- Business question: "Is RTO risk trending up or down for each city tier, and
-- are there any sudden spikes worth investigating (e.g. a logistics-partner
-- issue in Tier 3 last week)?"
--
-- Approach: aggregate to daily grain per city tier first (cheap), then apply a
-- window-framed rolling average over the trailing 7 calendar days. Using a
-- RANGE-like day-count window via ROWS is fine here because the daily grain is
-- already gap-free per tier (every tier has orders every day at 500k volume);
-- for sparser data, generate a calendar spine first.
-- =============================================================================

WITH daily_tier_stats AS (
    SELECT
        city_tier,
        order_date::DATE                             AS order_day,
        COUNT(*)                                      AS daily_orders,
        SUM(CASE WHEN rto_status THEN 1 ELSE 0 END)   AS daily_rtos
    FROM orders
    GROUP BY city_tier, order_date::DATE
),
rolling AS (
    SELECT
        city_tier,
        order_day,
        daily_orders,
        daily_rtos,
        -- Rolling 7-day totals via a ROWS window (partitioned per tier,
        -- ordered chronologically) — correct because each tier has exactly
        -- one row per calendar day (no gaps to skip over).
        SUM(daily_orders) OVER (
            PARTITION BY city_tier
            ORDER BY order_day
            ROWS BETWEEN 6 PRECEDING AND CURRENT ROW
        ) AS rolling_7d_orders,
        SUM(daily_rtos) OVER (
            PARTITION BY city_tier
            ORDER BY order_day
            ROWS BETWEEN 6 PRECEDING AND CURRENT ROW
        ) AS rolling_7d_rtos,
        -- Day-over-day RTO rate for comparison against the smoothed trend
        ROUND(100.0 * daily_rtos / NULLIF(daily_orders, 0), 2) AS daily_rto_rate_pct
    FROM daily_tier_stats
)
SELECT
    city_tier,
    order_day,
    daily_orders,
    daily_rto_rate_pct,
    rolling_7d_orders,
    rolling_7d_rtos,
    ROUND(100.0 * rolling_7d_rtos / NULLIF(rolling_7d_orders, 0), 2) AS rolling_7d_rto_rate_pct,
    -- Flag days where the smoothed rate jumps >3pp vs the prior day's smoothed rate
    ROUND(
        100.0 * rolling_7d_rtos / NULLIF(rolling_7d_orders, 0)
        - LAG(ROUND(100.0 * rolling_7d_rtos / NULLIF(rolling_7d_orders, 0), 2))
            OVER (PARTITION BY city_tier ORDER BY order_day),
        2
    ) AS rolling_rate_change_vs_prior_day_pp
FROM rolling
ORDER BY city_tier, order_day;


-- =============================================================================
-- QUERY 3: TOTAL FINANCIAL LOSS FROM RTOs
-- -----------------------------------------------------------------------------
-- Business question: "In plain rupees, how much did RTOs cost us — overall,
-- by payment method, and by city tier — assuming a flat ₹100 reverse-logistics
-- loss per RTO event?"
--
-- Note: ₹100 flat loss is a simplifying assumption for this exercise; in
-- production this would vary by parcel weight/distance/category (see the
-- unit-economics model, where logistics cost is order-value- and
-- distance-sensitive rather than flat).
-- =============================================================================

WITH loss_base AS (
    SELECT
        *,
        CASE WHEN rto_status THEN 100 ELSE 0 END AS rto_logistics_loss_inr
    FROM orders
)
SELECT
    'Overall' AS grain,
    NULL::TEXT AS grain_value,
    COUNT(*) AS total_orders,
    SUM(CASE WHEN rto_status THEN 1 ELSE 0 END) AS total_rtos,
    ROUND(100.0 * SUM(CASE WHEN rto_status THEN 1 ELSE 0 END) / COUNT(*), 2) AS rto_rate_pct,
    SUM(rto_logistics_loss_inr) AS total_rto_loss_inr,
    ROUND(SUM(rto_logistics_loss_inr)::NUMERIC / COUNT(*), 2) AS rto_loss_per_order_inr
FROM loss_base

UNION ALL

SELECT
    'By Payment Method',
    payment_method,
    COUNT(*),
    SUM(CASE WHEN rto_status THEN 1 ELSE 0 END),
    ROUND(100.0 * SUM(CASE WHEN rto_status THEN 1 ELSE 0 END) / COUNT(*), 2),
    SUM(rto_logistics_loss_inr),
    ROUND(SUM(rto_logistics_loss_inr)::NUMERIC / COUNT(*), 2)
FROM loss_base
GROUP BY payment_method

UNION ALL

SELECT
    'By City Tier',
    city_tier,
    COUNT(*),
    SUM(CASE WHEN rto_status THEN 1 ELSE 0 END),
    ROUND(100.0 * SUM(CASE WHEN rto_status THEN 1 ELSE 0 END) / COUNT(*), 2),
    SUM(rto_logistics_loss_inr),
    ROUND(SUM(rto_logistics_loss_inr)::NUMERIC / COUNT(*), 2)
FROM loss_base
GROUP BY city_tier

ORDER BY grain, total_rto_loss_inr DESC NULLS LAST;
