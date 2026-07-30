"""Deterministic A08/A09 measurements over decoded lossless frame fixtures."""

import math
from dataclasses import asdict
from typing import Any, Sequence

from .math_oracle import (
    amplitude_percent,
    canonical_svd_first,
    detrend,
    frozen_period,
    pearson,
    weighted_least_squares,
)
from .numeric_reference import (
    linear_rgb,
    measure_text_contrast,
    srgb_lookup,
    validate_numeric_reference,
)
from .png import PngImage
from .util import OracleError

Box = tuple[int, int, int, int]


def measure_character_contrast(
    image: PngImage,
    box: Box,
    all_character_boxes: Sequence[Box],
) -> dict[str, Any]:
    try:
        target_index = list(all_character_boxes).index(box)
    except ValueError as error:
        raise OracleError("target character box is not in the complete box set") from error
    return measure_text_contrast(image, all_character_boxes)["characters"][
        target_index
    ]


def measure_text_node_contrast(
    image: PngImage, character_box_document: dict[str, Any]
) -> dict[str, Any]:
    raw_boxes = character_box_document.get("non_whitespace_boxes")
    if not isinstance(raw_boxes, list) or not raw_boxes:
        raise OracleError("text node has no non-whitespace character boxes")
    boxes: list[tuple[float, float, float, float]] = []
    for item in raw_boxes:
        boxes.append(
            (
                item["left"],
                item["top"],
                item["right"],
                item["bottom"],
            )
        )
    numeric = measure_text_contrast(image, boxes)
    characters = []
    for item, measurement in zip(raw_boxes, numeric["characters"]):
        characters.append(
            {
                "code_point_index": item["code_point_index"],
                "code_point": item["code_point"],
                "pixel_box": measurement["pixel_box"],
                "measurement": measurement,
            }
        )
    minimum = min(
        item["measurement"]["minimum_contrast_ratio"] for item in characters
    )
    return {
        "schema_version": 1,
        "text": character_box_document.get("text"),
        "character_count": len(characters),
        "characters": characters,
        "numeric_reference": numeric["numeric_reference"],
        "input_rgb_sha256": numeric["input_rgb_sha256"],
        "srgb_q_to_linear_raw_bits": numeric["srgb_q_to_linear_raw_bits"],
        "minimum_contrast_ratio": minimum,
        "result": "pass" if minimum >= 4.5 else "fail",
    }


def measure_screen_text_contrast(
    image: PngImage,
    node_documents: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    """Measure all visible text together so every other glyph is background-excluded."""
    flattened: list[tuple[float, float, float, float]] = []
    spans = []
    for document in node_documents:
        raw_boxes = document.get("non_whitespace_boxes")
        if not isinstance(raw_boxes, list) or not raw_boxes:
            raise OracleError("visible text node has no non-whitespace character boxes")
        start = len(flattened)
        for item in raw_boxes:
            flattened.append(
                (
                    item["left"],
                    item["top"],
                    item["right"],
                    item["bottom"],
                )
            )
        spans.append((start, len(flattened), document, raw_boxes))
    if not flattened:
        raise OracleError("screen has no measurable visible text")
    numeric = measure_text_contrast(image, flattened)
    nodes = []
    for start, end, document, raw_boxes in spans:
        measured = numeric["characters"][start:end]
        characters = [
            {
                "code_point_index": raw["code_point_index"],
                "code_point": raw["code_point"],
                "pixel_box": result["pixel_box"],
                "measurement": result,
            }
            for raw, result in zip(raw_boxes, measured)
        ]
        minimum = min(
            item["measurement"]["minimum_contrast_ratio"] for item in characters
        )
        nodes.append(
            {
                "node_path": document.get("node_path"),
                "text": document.get("text"),
                "character_count": len(characters),
                "characters": characters,
                "minimum_contrast_ratio": minimum,
                "result": "pass" if minimum >= 4.5 else "fail",
            }
        )
    minimum = min(item["minimum_contrast_ratio"] for item in nodes)
    return {
        "schema_version": 1,
        "complete_visible_character_box_count": len(flattened),
        "all_visible_text_boxes_excluded_from_every_background_fit": True,
        "nodes": nodes,
        "minimum_contrast_ratio": minimum,
        "numeric_reference": numeric["numeric_reference"],
        "input_rgb_sha256": numeric["input_rgb_sha256"],
        "srgb_q_to_linear_raw_bits": numeric["srgb_q_to_linear_raw_bits"],
        "result": "pass" if minimum >= 4.5 else "fail",
    }


def validate_frame_timestamps(
    timestamps_ms: Sequence[float],
    *,
    count: int,
    interval_ms: float,
    tolerance_ms: float = 10.0,
) -> None:
    if len(timestamps_ms) != count:
        raise OracleError(f"expected exactly {count} frames")
    origin = timestamps_ms[0]
    for index, timestamp in enumerate(timestamps_ms):
        if abs((timestamp - origin) - index * interval_ms) > tolerance_ms:
            raise OracleError(
                f"frame timestamp error exceeds {tolerance_ms:g}ms"
            )


def motion_pixel_signal(
    frames: Sequence[PngImage],
    hero_roi: Box,
) -> dict[str, Any]:
    if len(frames) != 60:
        raise OracleError("normal motion requires exactly 60 frames")
    dimensions = {(frame.width, frame.height) for frame in frames}
    if len(dimensions) != 1:
        raise OracleError("frame dimensions changed")
    left, top, right, bottom = hero_roi
    features: list[list[float]] = [[] for _ in frames]
    changing_points = set()
    for y in range(top, bottom):
        for x in range(left, right):
            pixels = [frame.pixels[y][x] for frame in frames]
            if max(max(pixel[channel] for pixel in pixels) - min(pixel[channel] for pixel in pixels) for channel in range(3)) < 2:
                continue
            changing_points.add((x, y))
            linear = [linear_rgb(pixel) for pixel in pixels]
            for channel in range(3):
                mean = sum(pixel[channel] for pixel in linear) / len(linear)
                for index, pixel in enumerate(linear):
                    features[index].append(pixel[channel] - mean)
    if not changing_points:
        raise OracleError("normal-motion changing-pixel matrix is empty")
    decomposition = canonical_svd_first(features)
    raw_score = decomposition.score
    score = detrend(raw_score)
    singular1 = decomposition.singular_values[0]
    singular2 = (
        decomposition.singular_values[1]
        if len(decomposition.singular_values) > 1
        else 0.0
    )
    explained = decomposition.explained_variance
    if explained < 0.60:
        raise OracleError("first motion component explains under 60 percent")
    period = frozen_period(raw_score)
    _, lookup_bits = srgb_lookup()
    return {
        "numeric_reference": validate_numeric_reference(),
        "srgb_q_to_linear_raw_bits": list(lookup_bits),
        "changing_pixel_count": len(changing_points),
        "singular_values": decomposition.singular_values,
        "first_right_vector": decomposition.first_right_vector,
        "canonical_loading_index": decomposition.canonical_loading_index,
        "explained_variance": explained,
        "raw_pixel_score": raw_score,
        "pixel_signal": score,
        "period": asdict(period),
        "changing_points": sorted(changing_points),
    }


def _oklab(rgb: Sequence[float]) -> tuple[float, float, float]:
    red, green, blue = (min(1.0, max(0.0, value)) for value in rgb)
    l = 0.4122214708 * red + 0.5363325363 * green + 0.0514459929 * blue
    m = 0.2119034982 * red + 0.6806995451 * green + 0.1073969566 * blue
    s = 0.0883024619 * red + 0.2817188376 * green + 0.6299787005 * blue

    def cube_root(value: float) -> float:
        return math.copysign(abs(value) ** (1.0 / 3.0), value)

    lp, mp, sp = cube_root(l), cube_root(m), cube_root(s)
    return (
        0.2104542553 * lp + 0.7936177850 * mp - 0.0040720468 * sp,
        1.9779984951 * lp - 2.4285922050 * mp + 0.4505937099 * sp,
        0.0259040371 * lp + 0.7827717662 * mp - 0.8086757660 * sp,
    )


def _distance(first: Sequence[float], second: Sequence[float]) -> float:
    return math.sqrt(sum((left - right) ** 2 for left, right in zip(first, second)))


def _components(points: set[tuple[int, int]]) -> list[set[tuple[int, int]]]:
    remaining = set(points)
    output = []
    while remaining:
        seed = min(remaining, key=lambda item: (item[1], item[0]))
        component = {seed}
        queue = [seed]
        remaining.remove(seed)
        for point in queue:
            x, y = point
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    neighbor = (x + dx, y + dy)
                    if neighbor in remaining:
                        remaining.remove(neighbor)
                        component.add(neighbor)
                        queue.append(neighbor)
        output.append(component)
    return output


def _fit_srgb_background(
    frame: PngImage, hero_roi: Box
) -> tuple[list[list[float]], list[tuple[int, int]]]:
    left, top, right, bottom = hero_roi
    if right - left <= 16 or bottom - top <= 16:
        raise OracleError("hero ROI is too small for an eight-pixel perimeter")
    perimeter = [
        (x, y)
        for y in range(top, bottom)
        for x in range(left, right)
        if x < left + 8
        or x >= right - 8
        or y < top + 8
        or y >= bottom - 8
    ]

    def row(x: int, y: int) -> list[float]:
        xn = (2.0 * (x - left) / (right - left - 1)) - 1.0
        yn = (2.0 * (y - top) / (bottom - top - 1)) - 1.0
        return [1.0, xn, yn, xn * yn]

    design = [row(x, y) for x, y in perimeter]
    coefficients = []
    for channel in range(3):
        values = [frame.pixels[y][x][channel] / 255.0 for x, y in perimeter]
        weights = [1.0] * len(values)
        fit = weighted_least_squares(design, values, weights)
        delta = 2.0 / 255.0
        for _ in range(10):
            residuals = [
                abs(value - sum(coefficient * term for coefficient, term in zip(fit, terms)))
                for value, terms in zip(values, design)
            ]
            weights = [
                1.0 if residual <= delta else delta / residual
                for residual in residuals
            ]
            fit = weighted_least_squares(design, values, weights)
        coefficients.append(fit)
    return coefficients, perimeter


def _silhouette_mask(
    frame: PngImage, hero_roi: Box
) -> tuple[set[tuple[int, int]], dict[str, Any]]:
    left, top, right, bottom = hero_roi
    coefficients, perimeter = _fit_srgb_background(frame, hero_roi)

    def design(x: int, y: int) -> tuple[float, float, float, float]:
        xn = (2.0 * (x - left) / (right - left - 1)) - 1.0
        yn = (2.0 * (y - top) / (bottom - top - 1)) - 1.0
        return 1.0, xn, yn, xn * yn

    candidate = set()
    for y in range(top, bottom):
        for x in range(left, right):
            row = design(x, y)
            background = [
                min(1.0, max(0.0, sum(value * term for value, term in zip(fit, row))))
                for fit in coefficients
            ]
            actual = [channel / 255.0 for channel in frame.pixels[y][x]]
            if _distance(_oklab(actual), _oklab(background)) >= 0.08:
                candidate.add((x, y))
    minimum = max(1, math.ceil(0.0005 * (right - left) * (bottom - top)))
    candidate = set().union(
        *[
            component
            for component in _components(candidate)
            if len(component) >= minimum
        ]
    ) if candidate else set()
    if not candidate:
        raise OracleError("silhouette candidate mask is empty")

    universe = {
        (x, y) for y in range(top, bottom) for x in range(left, right)
    }
    dilated = {
        (x + dx, y + dy)
        for x, y in candidate
        for dx in (-1, 0, 1)
        for dy in (-1, 0, 1)
        if (x + dx, y + dy) in universe
    }
    closed = {
        (x, y)
        for x, y in universe
        if all(
            (x + dx, y + dy) in dilated
            for dx in (-1, 0, 1)
            for dy in (-1, 0, 1)
            if (x + dx, y + dy) in universe
        )
    }
    complement = universe - closed
    exterior = set()
    queue = [
        point
        for point in complement
        if point[0] in (left, right - 1) or point[1] in (top, bottom - 1)
    ]
    for point in queue:
        exterior.add(point)
    for x, y in queue:
        for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            neighbor = (x + dx, y + dy)
            if neighbor in complement and neighbor not in exterior:
                exterior.add(neighbor)
                queue.append(neighbor)
    filled = universe - exterior
    center_left = left + int(0.4 * (right - left))
    center_right = left + math.ceil(0.6 * (right - left))
    center_top = top + int(0.4 * (bottom - top))
    center_bottom = top + math.ceil(0.6 * (bottom - top))
    center = {
        (x, y)
        for y in range(center_top, center_bottom)
        for x in range(center_left, center_right)
    }
    eligible = []
    for component in _components(filled):
        if not component & center:
            continue
        white_seed = False
        for x, y in component:
            lab = _oklab([channel / 255.0 for channel in frame.pixels[y][x]])
            chroma = math.sqrt(lab[1] * lab[1] + lab[2] * lab[2])
            if lab[0] >= 0.70 and chroma <= 0.12:
                white_seed = True
                break
        if white_seed:
            eligible.append(component)
    if not eligible:
        raise OracleError("silhouette lacks central low-chroma white-hair component")
    eligible.sort(
        key=lambda component: (
            -len(component),
            min((y, x) for x, y in component),
        )
    )
    mask = eligible[0]
    if any(
        x in (left, right - 1) or y in (top, bottom - 1)
        for x, y in mask
    ):
        raise OracleError("silhouette mask is edge-clipped")
    xs = [point[0] for point in mask]
    ys = [point[1] for point in mask]
    measurement = {
        "width": max(xs) - min(xs) + 1,
        "height": max(ys) - min(ys) + 1,
        "area": len(mask),
        "centroid_x": sum(xs) / len(xs),
        "centroid_y": sum(ys) / len(ys),
        "background_coefficients": coefficients,
        "perimeter_pixel_count": len(perimeter),
        "candidate_pixel_count": len(candidate),
        "mask_pixel_count": len(mask),
        "mask_points": sorted(mask, key=lambda point: (point[1], point[0])),
    }
    return mask, measurement


def _dilate(points: set[tuple[int, int]], radius: int) -> set[tuple[int, int]]:
    return {
        (x + dx, y + dy)
        for x, y in points
        for dx in range(-radius, radius + 1)
        for dy in range(-radius, radius + 1)
    }


def validate_inside_hero_attribution(
    frames: Sequence[PngImage],
    changing_points: set[tuple[int, int]],
    masks: Sequence[set[tuple[int, int]]],
    pixel_signal: Sequence[float],
) -> dict[str, Any]:
    swept = set().union(*masks)
    unexplained = changing_points - swept
    dilation = _dilate(swept, 8)
    accepted_glow = set()
    for component in _components(unexplained):
        if not component & dilation:
            raise OracleError("changing component is detached from swept character")
        temporal = []
        for frame in frames:
            values = [
                sum(linear_rgb(frame.pixels[y][x])) / 3.0
                for x, y in component
            ]
            temporal.append(sum(values) / len(values))
        correlation = abs(pearson(detrend(temporal), pixel_signal))
        if correlation < 0.80:
            raise OracleError("attached glow correlation is below 0.80")
        accepted_glow.update(component)
    return {
        "swept_character_pixel_count": len(swept),
        "attached_glow_pixel_count": len(accepted_glow),
        "all_changing_pixels_attributed": True,
    }


def measure_normal_motion(
    frames: Sequence[PngImage],
    timestamps_ms: Sequence[float],
    hero_roi: Box,
) -> dict[str, Any]:
    validate_frame_timestamps(
        timestamps_ms, count=60, interval_ms=200.0, tolerance_ms=25.0
    )
    validate_outside_hero_static(frames, hero_roi)
    pixel = motion_pixel_signal(frames, hero_roi)
    masks = []
    mask_reports = []
    measurements = []
    for frame in frames:
        mask, report = _silhouette_mask(frame, hero_roi)
        masks.append(mask)
        mask_reports.append(report)
        measurements.append(
            (
                report["width"],
                report["height"],
                report["centroid_x"],
                report["centroid_y"],
            )
        )
    silhouette = validate_silhouette_measurements(
        measurements, pixel["pixel_signal"]
    )
    if not silhouette["pass"]:
        raise OracleError("normal-motion period is outside 4.5..5.1 seconds")
    attribution = validate_inside_hero_attribution(
        frames,
        set(tuple(point) for point in pixel["changing_points"]),
        masks,
        pixel["pixel_signal"],
    )
    palette = {
        color: sum(
            1 for row in frames[0].pixels for pixel_value in row if pixel_value == rgb
        )
        for color, rgb in {
            "#0B0714": (0x0B, 0x07, 0x14),
            "#171126": (0x17, 0x11, 0x26),
            "#B69CFF": (0xB6, 0x9C, 0xFF),
            "#78E8FF": (0x78, 0xE8, 0xFF),
        }.items()
    }
    if any(count < 100 for count in palette.values()):
        raise OracleError(f"frozen palette pixel coverage failed: {palette}")
    return {
        "schema_version": 1,
        "frame_count": 60,
        "hero_roi": list(hero_roi),
        "palette_counts": palette,
        "pixel_measurement": pixel,
        "silhouette_measurement": silhouette,
        "per_frame_silhouettes": mask_reports,
        "attribution": attribution,
        "outside_hero_static": True,
        "result": "pass",
    }


def validate_outside_hero_static(frames: Sequence[PngImage], hero_roi: Box) -> None:
    first = frames[0]
    left, top, right, bottom = hero_roi
    for index, frame in enumerate(frames[1:], 1):
        for y in range(first.height):
            for x in range(first.width):
                if left <= x < right and top <= y < bottom:
                    continue
                if frame.pixels[y][x] != first.pixels[y][x]:
                    raise OracleError(
                        f"pixel outside hero ROI changed at frame {index}, ({x},{y})"
                    )


def validate_reduced_motion(frames: Sequence[PngImage], timestamps_ms: Sequence[float]) -> None:
    if len(frames) == 26:
        validate_frame_timestamps(timestamps_ms, count=26, interval_ms=200.0)
        first = frames[0].pixels
        if any(frame.pixels != first for frame in frames[1:]):
            raise OracleError("reduced-motion idle frames are not byte-identical pixels")
    elif len(frames) == 13:
        validate_frame_timestamps(timestamps_ms, count=13, interval_ms=100.0)
    else:
        raise OracleError("reduced-motion capture must contain 13 or 26 frames")


def validate_silhouette_measurements(
    values: Sequence[tuple[float, float, float, float]],
    pixel_signal: Sequence[float],
) -> dict[str, Any]:
    if len(values) != 60 or len(pixel_signal) != 60:
        raise OracleError("silhouette report requires 60 frames")
    widths = [item[0] for item in values]
    heights = [item[1] for item in values]
    xs = [item[2] for item in values]
    ys = [item[3] for item in values]

    def median(items: Sequence[float]) -> float:
        ordered = sorted(items)
        middle = len(ordered) // 2
        return (ordered[middle - 1] + ordered[middle]) / 2.0

    width_median = median(widths)
    height_median = median(heights)
    x_median = median(xs)
    y_median = median(ys)
    raw_rows = [
        [
            width / width_median,
            height / height_median,
            (x - x_median) / width_median,
            (y - y_median) / height_median,
        ]
        for width, height, x, y in values
    ]
    retained_columns = [
        column
        for column in range(4)
        if any(
            row[column] != raw_rows[0][column] for row in raw_rows[1:]
        )
    ]
    if not retained_columns:
        raise OracleError("silhouette matrix has no non-constant column")
    means = [
        sum(row[column] for row in raw_rows) / len(raw_rows)
        for column in retained_columns
    ]
    matrix = [
        [
            row[column] - means[index]
            for index, column in enumerate(retained_columns)
        ]
        for row in raw_rows
    ]
    decomposition = canonical_svd_first(matrix)
    raw_signal = decomposition.score
    silhouette_signal = detrend(raw_signal)
    correlation = pearson(silhouette_signal, pixel_signal)
    if abs(correlation) < 0.50:
        raise OracleError("silhouette/pixel correlation is below 0.50")
    period = frozen_period(raw_signal)
    pixel_period = frozen_period(pixel_signal)
    if abs(period.period_seconds - pixel_period.period_seconds) > 0.2:
        raise OracleError("silhouette and pixel periods disagree by more than 0.2s")
    amplitude = amplitude_percent(values)
    if amplitude > 1.5:
        raise OracleError("silhouette amplitude exceeds 1.5 percent")
    return {
        "retained_columns": retained_columns,
        "singular_values": decomposition.singular_values,
        "first_right_vector": decomposition.first_right_vector,
        "canonical_loading_index": decomposition.canonical_loading_index,
        "raw_silhouette_score": raw_signal,
        "silhouette_signal": silhouette_signal,
        "absolute_correlation": abs(correlation),
        "period": asdict(period),
        "pixel_period": asdict(pixel_period),
        "amplitude_percent": amplitude,
        "pass": (
            4.5 <= period.period_seconds <= 5.1
            and 4.5 <= pixel_period.period_seconds <= 5.1
        ),
    }
