"""Pure-standard-library implementations of the frozen contrast and motion math."""

import math
from dataclasses import dataclass
from typing import Iterable, Sequence

from .util import OracleError

def _solve(matrix: list[list[float]], vector: list[float]) -> list[float]:
    """Frozen lower-elimination/upper-backsub solve with partial pivoting."""
    size = len(vector)
    coefficients = [row[:] for row in matrix]
    rhs = vector[:]
    for column in range(size):
        pivot = column
        pivot_abs = abs(coefficients[column][column])
        for row in range(column + 1, size):
            candidate = abs(coefficients[row][column])
            if candidate > pivot_abs:
                pivot = row
                pivot_abs = candidate
        if pivot_abs <= 1e-12 or not math.isfinite(pivot_abs):
            raise OracleError("singular least-squares system")
        if pivot != column:
            coefficients[column], coefficients[pivot] = (
                coefficients[pivot],
                coefficients[column],
            )
            rhs[column], rhs[pivot] = rhs[pivot], rhs[column]
        divisor = coefficients[column][column]
        for item in range(column, size):
            coefficients[column][item] = coefficients[column][item] / divisor
        rhs[column] = rhs[column] / divisor
        for row in range(column + 1, size):
            factor = coefficients[row][column]
            for item in range(column, size):
                coefficients[row][item] = (
                    coefficients[row][item] - factor * coefficients[column][item]
                )
            rhs[row] = rhs[row] - factor * rhs[column]
    output = [0.0] * size
    for row in range(size - 1, -1, -1):
        value = rhs[row]
        for column in range(row + 1, size):
            value = value - coefficients[row][column] * output[column]
        output[row] = value
    if not all(math.isfinite(value) for value in output):
        raise OracleError("non-finite least-squares coefficient")
    return output


def weighted_least_squares(
    rows: Sequence[Sequence[float]],
    values: Sequence[float],
    weights: Sequence[float],
) -> list[float]:
    if not rows or len(rows) != len(values) or len(values) != len(weights):
        raise OracleError("invalid weighted least-squares dimensions")
    width = len(rows[0])
    normal = [[0.0] * width for _ in range(width)]
    rhs = [0.0] * width
    for row, value, weight in zip(rows, values, weights):
        if len(row) != width or weight < 0:
            raise OracleError("invalid weighted least-squares row")
        for first in range(width):
            rhs[first] = rhs[first] + (weight * row[first]) * value
            for second in range(width):
                normal[first][second] = (
                    normal[first][second] + (weight * row[first]) * row[second]
                )
    return _solve(normal, rhs)


def pearson(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right) or len(left) < 2:
        raise OracleError("invalid Pearson vectors")
    l_mean = sum(left) / len(left)
    r_mean = sum(right) / len(right)
    numerator = sum((a - l_mean) * (b - r_mean) for a, b in zip(left, right))
    l_energy = sum((a - l_mean) ** 2 for a in left)
    r_energy = sum((b - r_mean) ** 2 for b in right)
    if l_energy == 0 or r_energy == 0:
        raise OracleError("Pearson correlation is undefined for constant input")
    return numerator / math.sqrt(l_energy * r_energy)


def detrend(values: Sequence[float]) -> list[float]:
    rows = [[1.0, float(index)] for index in range(len(values))]
    fit = weighted_least_squares(rows, list(values), [1.0] * len(values))
    return [
        value - fit[0] - fit[1] * index for index, value in enumerate(values)
    ]


def normalized_autocorrelation(values: Sequence[float], lag: int) -> float:
    if lag <= 0 or lag >= len(values):
        raise OracleError("autocorrelation lag outside series")
    left = values[:-lag]
    right = values[lag:]
    numerator = sum(a * b for a, b in zip(left, right))
    denominator = math.sqrt(sum(a * a for a in left) * sum(b * b for b in right))
    if denominator == 0:
        raise OracleError("zero autocorrelation denominator")
    return numerator / denominator


@dataclass(frozen=True)
class PeriodMeasurement:
    integer_lag: int
    delta: float
    refined_lag: float
    period_seconds: float
    peak_correlation: float
    correlations: dict[int, float]


def frozen_period(
    values: Sequence[float],
    *,
    fps: float = 5.0,
    minimum_lag: int = 10,
    maximum_lag: int = 30,
) -> PeriodMeasurement:
    if len(values) != 60 or fps != 5.0:
        raise OracleError("normal-motion period requires 60 samples at exactly 5.000 fps")
    series = detrend(values)
    correlations = {
        lag: normalized_autocorrelation(series, lag)
        for lag in range(minimum_lag - 1, maximum_lag + 2)
    }
    maxima = [
        lag
        for lag in range(minimum_lag, maximum_lag + 1)
        if correlations[lag] > correlations[lag - 1]
        and correlations[lag] > correlations[lag + 1]
    ]
    if not maxima:
        raise OracleError("no strict local autocorrelation maximum")
    best_value = max(correlations[lag] for lag in maxima)
    best = [lag for lag in maxima if correlations[lag] == best_value]
    if len(best) != 1:
        raise OracleError("tied autocorrelation maximum")
    lag = best[0]
    if lag in (minimum_lag, maximum_lag):
        raise OracleError("autocorrelation peak is at search boundary")
    denominator = (
        correlations[lag - 1]
        - 2.0 * correlations[lag]
        + correlations[lag + 1]
    )
    if denominator == 0:
        raise OracleError("zero parabolic-refinement denominator")
    delta = 0.5 * (correlations[lag - 1] - correlations[lag + 1]) / denominator
    if abs(delta) > 1.0 or correlations[lag] < 0.60:
        raise OracleError("invalid refined autocorrelation peak")
    refined = lag + delta
    return PeriodMeasurement(
        lag, delta, refined, refined / fps, correlations[lag], correlations
    )


def _median(values: Sequence[float]) -> float:
    ordered = sorted(values)
    midpoint = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[midpoint]
    return (ordered[midpoint - 1] + ordered[midpoint]) / 2.0


def amplitude_percent(
    measurements: Sequence[tuple[float, float, float, float]]
) -> float:
    if not measurements:
        raise OracleError("silhouette measurements are empty")
    widths = [item[0] for item in measurements]
    heights = [item[1] for item in measurements]
    xs = [item[2] for item in measurements]
    ys = [item[3] for item in measurements]
    width_median, height_median = _median(widths), _median(heights)
    x_median, y_median = _median(xs), _median(ys)
    if width_median <= 0 or height_median <= 0:
        raise OracleError("invalid silhouette dimensions")
    return 100.0 * max(
        max(abs(value - width_median) / width_median for value in widths),
        max(abs(value - height_median) / height_median for value in heights),
        max(abs(value - x_median) / width_median for value in xs),
        max(abs(value - y_median) / height_median for value in ys),
    )


@dataclass(frozen=True)
class CanonicalSvdFirst:
    score: list[float]
    singular_values: list[float]
    first_right_vector: list[float]
    canonical_loading_index: int
    explained_variance: float


def _jacobi_symmetric_eigen(
    matrix: Sequence[Sequence[float]],
) -> tuple[list[float], list[list[float]]]:
    """Deterministic binary64 Jacobi reference for the small row Gram matrix."""
    size = len(matrix)
    if not size or any(len(row) != size for row in matrix):
        raise OracleError("Jacobi eigen input must be non-empty and square")
    values = [list(map(float, row)) for row in matrix]
    vectors = [
        [1.0 if row == column else 0.0 for column in range(size)]
        for row in range(size)
    ]
    limit = max(64, 100 * size * size)
    for _ in range(limit):
        first, second = 0, 1 if size > 1 else 0
        largest = 0.0
        for row in range(size):
            for column in range(row + 1, size):
                candidate = abs(values[row][column])
                if candidate > largest:
                    largest = candidate
                    first, second = row, column
        scale = max(1.0, max(abs(values[index][index]) for index in range(size)))
        if largest <= 1e-14 * scale:
            break
        app = values[first][first]
        aqq = values[second][second]
        apq = values[first][second]
        tau = (aqq - app) / (2.0 * apq)
        sign = 1.0 if tau >= 0.0 else -1.0
        tangent = sign / (abs(tau) + math.sqrt(1.0 + tau * tau))
        cosine = 1.0 / math.sqrt(1.0 + tangent * tangent)
        sine = tangent * cosine
        for index in range(size):
            if index in (first, second):
                continue
            aip = values[index][first]
            aiq = values[index][second]
            new_first = cosine * aip - sine * aiq
            new_second = sine * aip + cosine * aiq
            values[index][first] = new_first
            values[first][index] = new_first
            values[index][second] = new_second
            values[second][index] = new_second
        values[first][first] = (
            cosine * cosine * app
            - 2.0 * sine * cosine * apq
            + sine * sine * aqq
        )
        values[second][second] = (
            sine * sine * app
            + 2.0 * sine * cosine * apq
            + cosine * cosine * aqq
        )
        values[first][second] = 0.0
        values[second][first] = 0.0
        for row in range(size):
            vip = vectors[row][first]
            viq = vectors[row][second]
            vectors[row][first] = cosine * vip - sine * viq
            vectors[row][second] = sine * vip + cosine * viq
    else:
        raise OracleError("Jacobi eigen reference did not converge")
    order = sorted(
        range(size), key=lambda index: (-values[index][index], index)
    )
    eigenvalues = []
    eigenvectors = []
    for index in order:
        eigenvalue = values[index][index]
        if eigenvalue < 0.0 and abs(eigenvalue) <= 1e-12 * scale:
            eigenvalue = 0.0
        if eigenvalue < 0.0 or not math.isfinite(eigenvalue):
            raise OracleError("invalid symmetric Gram eigenvalue")
        eigenvalues.append(eigenvalue)
        eigenvectors.append([vectors[row][index] for row in range(size)])
    return eigenvalues, eigenvectors


def canonical_svd_first(rows: Sequence[Sequence[float]]) -> CanonicalSvdFirst:
    """Thin-SVD-equivalent reference with the frozen right-loading sign rule."""
    if not rows or not rows[0]:
        raise OracleError("SVD matrix is empty")
    width = len(rows[0])
    if any(len(row) != width for row in rows):
        raise OracleError("SVD matrix is ragged")
    if any(not math.isfinite(value) for row in rows for value in row):
        raise OracleError("SVD matrix contains non-finite input")
    gram = []
    for left in rows:
        gram_row = []
        for right in rows:
            total = 0.0
            for first, second in zip(left, right):
                total = total + first * second
            gram_row.append(total)
        gram.append(gram_row)
    eigenvalues, left_vectors = _jacobi_symmetric_eigen(gram)
    singular_values = [math.sqrt(value) for value in eigenvalues]
    first = singular_values[0]
    second = singular_values[1] if len(singular_values) > 1 else 0.0
    if first == 0.0:
        raise OracleError("largest singular value is zero")
    if abs(first - second) <= 1e-9 * first:
        raise OracleError("largest two singular values are not distinct")
    left = left_vectors[0]
    right = []
    for column in range(width):
        loading = 0.0
        for row_index in range(len(rows)):
            loading = loading + rows[row_index][column] * left[row_index]
        right.append(loading / first)
    maximum = max(abs(value) for value in right)
    loading_index = next(
        index for index, value in enumerate(right) if abs(value) == maximum
    )
    if right[loading_index] < 0.0:
        left = [-value for value in left]
        right = [-value for value in right]
    score = [value * first for value in left]
    total_energy = 0.0
    for row in rows:
        for value in row:
            total_energy = total_energy + value * value
    explained = first * first / total_energy
    return CanonicalSvdFirst(
        score=score,
        singular_values=singular_values,
        first_right_vector=right,
        canonical_loading_index=loading_index,
        explained_variance=explained,
    )
