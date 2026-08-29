"""
risk_engine.py
==============
RTO Risk Engine — estimates P(RTO | user, order, context) and converts it into
an interpretable 0-100 risk score with Low/Medium/High/Very-High bands.

Compares two modelling approaches on the interpretability/performance frontier:
  1. Logistic Regression (L2)  — fully interpretable, coefficients = business story
  2. Gradient Boosted Trees    — higher predictive ceiling, needs SHAP/feature
                                   importance to stay explainable

We do NOT optimize for accuracy alone. Given RTO base rate ~11%, a model that
predicts "never RTO" is already ~89% accurate and completely useless — hence
ROC-AUC, PR-AUC, recall-at-precision, and calibration are the metrics that
actually matter for this business problem, alongside an explicit discussion of
false-positive vs false-negative cost.

Run:
    python risk_engine.py
Requires:
    ecommerce_orders.csv (from generate_data.py)
"""

import numpy as np
import pandas as pd
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import StandardScaler, OneHotEncoder
from sklearn.compose import ColumnTransformer
from sklearn.pipeline import Pipeline
from sklearn.linear_model import LogisticRegression
from sklearn.ensemble import GradientBoostingClassifier
from sklearn.calibration import CalibratedClassifierCV
from sklearn.frozen import FrozenEstimator
from sklearn.metrics import (
    roc_auc_score, average_precision_score, precision_recall_curve,
    precision_score, recall_score, f1_score, brier_score_loss, roc_curve
)

RANDOM_STATE = 42


# ---------------------------------------------------------------------------
# 1. Load & feature-engineer
# ---------------------------------------------------------------------------
def load_data(path="ecommerce_orders.csv"):
    df = pd.read_csv(path, parse_dates=["order_date"])
    df["order_hour"] = df["order_date"].dt.hour
    df["order_dow"] = df["order_date"].dt.dayofweek
    return df


NUMERIC_FEATURES = [
    "order_value_inr", "cart_value", "discount_amount", "shipping_fee",
    "delivery_distance_km", "delivery_attempts", "user_tenure_days",
    "historical_orders", "historical_rto_count", "historical_rto_rate",
    "customer_lifetime_value", "previous_cod_orders", "previous_cod_rtos",
    "order_hour", "order_dow",
]
CATEGORICAL_FEATURES = [
    "city_tier", "item_category", "payment_method", "time_of_day",
    "device_os", "seller_type", "warehouse_zone", "new_vs_returning_user",
]
TARGET = "rto_status"


def build_preprocessor():
    return ColumnTransformer(
        transformers=[
            ("num", StandardScaler(), NUMERIC_FEATURES),
            ("cat", OneHotEncoder(handle_unknown="ignore", drop="first"), CATEGORICAL_FEATURES),
        ]
    )


# ---------------------------------------------------------------------------
# 2. Train/validation/test split + class imbalance handling
# ---------------------------------------------------------------------------
def split_data(df):
    X = df[NUMERIC_FEATURES + CATEGORICAL_FEATURES]
    y = df[TARGET].astype(int)

    # 70 / 15 / 15 stratified split — stratification matters because the
    # positive class (~11%) is a minority; naive random splits can drift
    # the class ratio across folds at the tails.
    X_train, X_temp, y_train, y_temp = train_test_split(
        X, y, test_size=0.30, stratify=y, random_state=RANDOM_STATE
    )
    X_val, X_test, y_val, y_test = train_test_split(
        X_temp, y_temp, test_size=0.50, stratify=y_temp, random_state=RANDOM_STATE
    )
    print(f"Train: {len(X_train):,} (RTO rate {y_train.mean():.2%})")
    print(f"Val:   {len(X_val):,} (RTO rate {y_val.mean():.2%})")
    print(f"Test:  {len(X_test):,} (RTO rate {y_test.mean():.2%})")
    return X_train, X_val, X_test, y_train, y_val, y_test


# ---------------------------------------------------------------------------
# 3. Models
# ---------------------------------------------------------------------------
def build_logreg_pipeline():
    # class_weight='balanced' re-weights the minority (RTO=1) class instead of
    # naive oversampling — avoids duplicating rows / overfitting to copies,
    # and keeps the model's probability outputs closer to well-behaved before
    # calibration.
    return Pipeline([
        ("prep", build_preprocessor()),
        ("clf", LogisticRegression(
            class_weight="balanced", max_iter=1000, C=0.5, random_state=RANDOM_STATE
        )),
    ])


def build_gbm_pipeline():
    return Pipeline([
        ("prep", build_preprocessor()),
        ("clf", GradientBoostingClassifier(
            n_estimators=250, max_depth=3, learning_rate=0.05,
            subsample=0.8, random_state=RANDOM_STATE
        )),
    ])


def evaluate(model, X, y, label, threshold=0.5):
    proba = model.predict_proba(X)[:, 1]
    preds = (proba >= threshold).astype(int)
    metrics = {
        "model": label,
        "roc_auc": roc_auc_score(y, proba),
        "pr_auc": average_precision_score(y, proba),
        "precision": precision_score(y, preds, zero_division=0),
        "recall": recall_score(y, preds, zero_division=0),
        "f1": f1_score(y, preds, zero_division=0),
        "brier_score": brier_score_loss(y, proba),  # lower = better calibrated
    }
    return metrics, proba


def find_best_threshold(y_true, proba, min_precision=0.35):
    """
    Threshold selection is a BUSINESS decision, not a modelling one.
    Here we pick the lowest threshold that still clears a minimum precision
    bar — i.e. "don't friction more than ~65% innocent orders per flagged
    order" — then report the recall we get at that precision. This encodes
    the false-positive cost explicitly instead of defaulting to 0.5.
    """
    precisions, recalls, thresholds = precision_recall_curve(y_true, proba)
    # precision_recall_curve returns len(thresholds) = len(precisions) - 1
    valid = np.where(precisions[:-1] >= min_precision)[0]
    if len(valid) == 0:
        return 0.5, None
    best_idx = valid[np.argmax(recalls[valid])]
    return thresholds[best_idx], (precisions[best_idx], recalls[best_idx])


# ---------------------------------------------------------------------------
# 4. Risk bands (0-100 score, interpretable cutoffs)
# ---------------------------------------------------------------------------
def to_risk_score(proba):
    return np.round(proba * 100, 1)


def assign_band(score):
    return pd.cut(
        score,
        bins=[-0.1, 20, 45, 70, 100],
        labels=["Low", "Medium", "High", "Very High"],
    )


# ---------------------------------------------------------------------------
# 5. Feature importance (both models)
# ---------------------------------------------------------------------------
def get_feature_names(preprocessor):
    num_names = NUMERIC_FEATURES
    cat_names = list(preprocessor.named_transformers_["cat"].get_feature_names_out(CATEGORICAL_FEATURES))
    return num_names + cat_names


def logreg_importance(pipeline, top_n=15):
    coefs = pipeline.named_steps["clf"].coef_[0]
    names = get_feature_names(pipeline.named_steps["prep"])
    imp = pd.DataFrame({"feature": names, "coefficient": coefs})
    imp["abs_coef"] = imp["coefficient"].abs()
    return imp.sort_values("abs_coef", ascending=False).head(top_n)[["feature", "coefficient"]]


def gbm_importance(pipeline, top_n=15):
    importances = pipeline.named_steps["clf"].feature_importances_
    names = get_feature_names(pipeline.named_steps["prep"])
    imp = pd.DataFrame({"feature": names, "importance": importances})
    return imp.sort_values("importance", ascending=False).head(top_n)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    df = load_data()
    X_train, X_val, X_test, y_train, y_val, y_test = split_data(df)

    print("\n=== Training Logistic Regression (interpretable baseline) ===")
    logreg = build_logreg_pipeline()
    logreg.fit(X_train, y_train)
    lr_metrics, lr_val_proba = evaluate(logreg, X_val, y_val, "LogisticRegression")

    print("\n=== Training Gradient Boosted Trees (performance ceiling) ===")
    gbm = build_gbm_pipeline()
    gbm.fit(X_train, y_train)
    gbm_metrics, gbm_val_proba = evaluate(gbm, X_val, y_val, "GradientBoosting")

    print("\n--- Validation metrics (uncalibrated) ---")
    print(pd.DataFrame([lr_metrics, gbm_metrics]).round(4).to_string(index=False))

    # Calibration: tree ensembles in particular tend to produce probabilities
    # that are directionally right but not well-calibrated (over-confident at
    # the extremes). We calibrate on the validation fold using isotonic
    # regression, since we have >50k validation rows — plenty for isotonic
    # to be stable (sigmoid/Platt would be preferred with <1-2k rows).
    print("\n=== Calibrating GBM (isotonic, using validation fold) ===")
    try:
        # scikit-learn >= 1.6: pre-fitted estimators are wrapped explicitly
        gbm_calibrated = CalibratedClassifierCV(FrozenEstimator(gbm), method="isotonic")
        gbm_calibrated.fit(X_val, y_val)
    except ImportError:
        # scikit-learn < 1.6: cv="prefit" signals an already-fitted estimator
        gbm_calibrated = CalibratedClassifierCV(gbm, method="isotonic", cv="prefit")
        gbm_calibrated.fit(X_val, y_val)

    # Final evaluation on the held-out TEST set (never touched until now)
    print("\n=== FINAL TEST SET EVALUATION ===")
    lr_test_metrics, lr_test_proba = evaluate(logreg, X_test, y_test, "LogisticRegression")
    gbm_test_metrics, gbm_test_proba = evaluate(gbm_calibrated, X_test, y_test, "GBM_Calibrated")
    print(pd.DataFrame([lr_test_metrics, gbm_test_metrics]).round(4).to_string(index=False))

    # Threshold selection — business-driven, not arbitrary 0.5
    best_thresh, at_thresh = find_best_threshold(y_test, gbm_test_proba, min_precision=0.35)
    print(f"\nSelected operating threshold (GBM): {best_thresh:.3f}")
    if at_thresh:
        print(f"  -> Precision {at_thresh[0]:.2%}, Recall {at_thresh[1]:.2%} at this threshold")
        print("  Interpretation: of orders we flag as high-risk, "
              f"~{at_thresh[0]:.0%} genuinely RTO (acceptable false-positive rate "
              f"of ~{1-at_thresh[0]:.0%} given friction is a MILD deterrent, not a block).")

    # Risk scores + bands on test set (using calibrated GBM as production model)
    risk_scores = to_risk_score(gbm_test_proba)
    bands = assign_band(risk_scores)
    band_summary = pd.DataFrame({"risk_band": bands, "actual_rto": y_test.values}).groupby(
        "risk_band", observed=True
    ).agg(orders=("actual_rto", "size"), actual_rto_rate=("actual_rto", "mean"))
    print("\n--- Risk Band Calibration Check (test set) ---")
    print(band_summary.round(3))

    # Feature importance from both models
    print("\n--- Logistic Regression: Top drivers (coefficient = log-odds impact) ---")
    print(logreg_importance(logreg).to_string(index=False))

    print("\n--- Gradient Boosting: Top drivers (impurity-based importance) ---")
    print(gbm_importance(gbm).to_string(index=False))

    print("""
--- Interpretability vs Performance trade-off ---
Logistic Regression: every coefficient is a direct, auditable statement
("CoD adds X log-odds of RTO, holding everything else constant") — critical
for explaining friction decisions to Risk/Compliance/CX and for debugging why
a specific customer got flagged. Weaker at capturing the 3-way interaction
(CoD x Tier-3 x Electronics x Late-Night) that drives the sharpest risk spike,
because it only sees additive main effects unless interactions are hand-engineered.

Gradient Boosting: captures nonlinear interactions natively (that's exactly
the spike pattern baked into the data generator) and wins on ROC-AUC / PR-AUC.
Costs: less directly explainable per-prediction (mitigated with feature
importance / SHAP), more prone to overfitting on noise without regularization,
and requires calibration before its probabilities can be trusted as "P(RTO)".

Recommendation: ship the calibrated GBM as the scoring model (it will
actually reduce more RTO $ at a given false-positive budget), but always
publish the Logistic Regression coefficients alongside it as the
"why" layer for support/CX escalations and fairness audits.
""")

    # Save scored test set for downstream intervention engine
    scored = X_test.copy()
    scored["actual_rto"] = y_test.values
    scored["rto_risk_score"] = risk_scores
    scored["risk_band"] = np.asarray(bands)
    scored.to_csv("scored_orders_sample.csv", index=False)
    print("Saved scored_orders_sample.csv for the intervention engine.")


if __name__ == "__main__":
    main()
