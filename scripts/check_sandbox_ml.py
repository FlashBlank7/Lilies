"""Exercise the installed ML runtime with synthetic data, without project inputs."""
import importlib.metadata
import json
from pathlib import Path
import tempfile

import joblib
import numpy as np
from sklearn.impute import SimpleImputer
from sklearn.linear_model import Ridge
from sklearn.model_selection import TimeSeriesSplit, cross_val_score
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler


def main():
    x = np.arange(40, dtype=float).reshape(-1, 1)
    y = 2 * x[:, 0] + 3
    pipeline = make_pipeline(SimpleImputer(), StandardScaler(), Ridge(alpha=0.001))
    errors = -cross_val_score(
        pipeline, x[:32], y[:32], cv=TimeSeriesSplit(n_splits=3),
        scoring="neg_mean_absolute_error", error_score="raise",
    )
    pipeline.fit(x[:32], y[:32])
    predicted = pipeline.predict(x[32:])
    with tempfile.TemporaryDirectory(prefix="platform-ml-check-") as directory:
        artifact = Path(directory) / "pipeline.joblib"
        joblib.dump(pipeline, artifact)
        restored = joblib.load(artifact).predict(x[32:])
    checks = {
        "finite_temporal_scores": bool(np.isfinite(errors).all()),
        "synthetic_prediction": bool(np.max(np.abs(predicted - y[32:])) < 0.01),
        "model_roundtrip": bool(np.array_equal(predicted, restored)),
    }
    result = {
        "passed": all(checks.values()), "scope": "synthetic_environment_check",
        "checks": checks,
        "versions": {name: importlib.metadata.version(name) for name in
                     ["numpy", "scipy", "pandas", "scikit-learn", "joblib", "threadpoolctl"]},
    }
    print(json.dumps(result, allow_nan=False))
    if not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
