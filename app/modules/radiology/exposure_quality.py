"""
Chest X-ray exposure quality: pixel heuristics + AI fusion.

Heuristic catches gross under/over-exposure (clipping, flat contrast, EI/DI).
AI judges visual/clinical usability. Service fuses both into the report.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from app.modules.radiology.schemas import (
    ExposureHeuristicAssessment,
    ExposureHeuristicConfidence,
    ExposureQuality,
    TechnicalMetadata,
)

# Fraction of pixels at extreme ends of the scaled intensity range.
_INADEQUATE_CLIP_PCT = 12.0
_SUBOPTIMAL_CLIP_PCT = 4.0
# (p99 - p1) / (max - min + eps); low = washed / crushed contrast.
_INADEQUATE_DYNAMIC_RATIO = 0.25
_SUBOPTIMAL_DYNAMIC_RATIO = 0.45
# Mean of scaled image normalized to [0, 1].
_DARK_MEAN = 0.18
_BRIGHT_MEAN = 0.82
# DICOM Deviation Index (when present): |DI| > 3 ≈ notable exposure miss.
_DI_INADEQUATE = 3.0
_DI_SUBOPTIMAL = 1.5


def assess_exposure_heuristic(
    scaled: np.ndarray,
    image_u8: np.ndarray,
    *,
    exposure_index: float | None = None,
    deviation_index: float | None = None,
    target_exposure_index: float | None = None,
) -> ExposureHeuristicAssessment:
    """
    Grade exposure from pre-window scaled pixels (+ optional EI/DI tags).

    Uses ``scaled`` (after RescaleSlope/Intercept) so DICOM windowing does not
    hide clipping. ``image_u8`` is a secondary display-level check.
    """
    scaled_f = np.asarray(scaled, dtype=np.float64)
    if scaled_f.size == 0:
        return ExposureHeuristicAssessment(
            suggested_grade=ExposureQuality.UNDETERMINED,
            confidence=ExposureHeuristicConfidence.LOW,
            reasons=["empty_pixel_array"],
        )

    s_min = float(np.min(scaled_f))
    s_max = float(np.max(scaled_f))
    span = s_max - s_min
    if span <= 0:
        return ExposureHeuristicAssessment(
            suggested_grade=ExposureQuality.INADEQUATE,
            confidence=ExposureHeuristicConfidence.HIGH,
            reasons=["zero_dynamic_range"],
            black_clip_pct=100.0,
            white_clip_pct=100.0,
            dynamic_range_ratio=0.0,
            mean_normalized=0.0,
            exposure_index=exposure_index,
            deviation_index=deviation_index,
            target_exposure_index=target_exposure_index,
        )

    # Edge bins: bottom/top 1% of the intensity span.
    edge = max(span * 0.01, np.finfo(np.float64).eps)
    black_clip_pct = float(np.mean(scaled_f <= (s_min + edge)) * 100.0)
    white_clip_pct = float(np.mean(scaled_f >= (s_max - edge)) * 100.0)

    p1, p99 = np.percentile(scaled_f, [1.0, 99.0])
    dynamic_range_ratio = float((p99 - p1) / span)
    mean_normalized = float((float(np.mean(scaled_f)) - s_min) / span)

    u8 = np.asarray(image_u8)
    u8_black_pct = float(np.mean(u8 <= 2) * 100.0) if u8.size else 0.0
    u8_white_pct = float(np.mean(u8 >= 253) * 100.0) if u8.size else 0.0

    reasons: list[str] = []
    grade = ExposureQuality.ADEQUATE
    confidence = ExposureHeuristicConfidence.LOW

    if deviation_index is not None:
        abs_di = abs(deviation_index)
        if abs_di >= _DI_INADEQUATE:
            grade = ExposureQuality.INADEQUATE
            confidence = ExposureHeuristicConfidence.HIGH
            reasons.append(f"deviation_index={deviation_index:.2f}")
        elif abs_di >= _DI_SUBOPTIMAL:
            grade = ExposureQuality.SUBOPTIMAL
            confidence = ExposureHeuristicConfidence.MEDIUM
            reasons.append(f"deviation_index={deviation_index:.2f}")

    max_clip = max(black_clip_pct, white_clip_pct, u8_black_pct, u8_white_pct)
    if max_clip >= _INADEQUATE_CLIP_PCT or dynamic_range_ratio <= _INADEQUATE_DYNAMIC_RATIO:
        grade = ExposureQuality.INADEQUATE
        confidence = ExposureHeuristicConfidence.HIGH
        if black_clip_pct >= _INADEQUATE_CLIP_PCT or u8_black_pct >= _INADEQUATE_CLIP_PCT:
            reasons.append("severe_black_clipping")
        if white_clip_pct >= _INADEQUATE_CLIP_PCT or u8_white_pct >= _INADEQUATE_CLIP_PCT:
            reasons.append("severe_white_clipping")
        if dynamic_range_ratio <= _INADEQUATE_DYNAMIC_RATIO:
            reasons.append("crushed_dynamic_range")
    elif (
        grade != ExposureQuality.INADEQUATE
        and (
            max_clip >= _SUBOPTIMAL_CLIP_PCT
            or dynamic_range_ratio <= _SUBOPTIMAL_DYNAMIC_RATIO
            or mean_normalized <= _DARK_MEAN
            or mean_normalized >= _BRIGHT_MEAN
        )
    ):
        if grade == ExposureQuality.ADEQUATE:
            grade = ExposureQuality.SUBOPTIMAL
            confidence = ExposureHeuristicConfidence.MEDIUM
        if black_clip_pct >= _SUBOPTIMAL_CLIP_PCT or u8_black_pct >= _SUBOPTIMAL_CLIP_PCT:
            reasons.append("mild_black_clipping")
        if white_clip_pct >= _SUBOPTIMAL_CLIP_PCT or u8_white_pct >= _SUBOPTIMAL_CLIP_PCT:
            reasons.append("mild_white_clipping")
        if dynamic_range_ratio <= _SUBOPTIMAL_DYNAMIC_RATIO:
            reasons.append("limited_contrast")
        if mean_normalized <= _DARK_MEAN:
            reasons.append("globally_dark")
        if mean_normalized >= _BRIGHT_MEAN:
            reasons.append("globally_bright")

    if not reasons and grade == ExposureQuality.ADEQUATE:
        reasons.append("histogram_within_expected_range")
        confidence = ExposureHeuristicConfidence.LOW

    # Deduplicate while preserving order.
    seen: set[str] = set()
    unique_reasons: list[str] = []
    for reason in reasons:
        if reason not in seen:
            seen.add(reason)
            unique_reasons.append(reason)

    return ExposureHeuristicAssessment(
        suggested_grade=grade,
        confidence=confidence,
        reasons=unique_reasons,
        black_clip_pct=round(black_clip_pct, 3),
        white_clip_pct=round(white_clip_pct, 3),
        dynamic_range_ratio=round(dynamic_range_ratio, 4),
        mean_normalized=round(mean_normalized, 4),
        exposure_index=exposure_index,
        deviation_index=deviation_index,
        target_exposure_index=target_exposure_index,
    )


def _rank(grade: ExposureQuality) -> int:
    return {
        ExposureQuality.ADEQUATE: 0,
        ExposureQuality.UNDETERMINED: 1,
        ExposureQuality.SUBOPTIMAL: 2,
        ExposureQuality.INADEQUATE: 3,
    }.get(grade, 1)


def _merge_notes(*parts: str | None) -> str | None:
    cleaned = [p.strip() for p in parts if p and p.strip()]
    if not cleaned:
        return None
    # Preserve order, drop exact duplicates.
    seen: set[str] = set()
    out: list[str] = []
    for part in cleaned:
        if part not in seen:
            seen.add(part)
            out.append(part)
    return " | ".join(out)


def fuse_exposure_quality(
    heuristic: ExposureHeuristicAssessment | None,
    ai_technical: TechnicalMetadata,
) -> TechnicalMetadata:
    """
    Combine heuristic + AI technical_metadata into a final exposure grade.

    Prefer high-confidence Inadequate from metrics; otherwise lean on AI for
    subtler visual limits; on conflict prefer the more conservative grade.
    """
    ai_grade = ai_technical.exposure_quality
    ai_notes = ai_technical.quality_notes

    if heuristic is None:
        return ai_technical.model_copy(
            update={
                "exposure_quality": ai_grade,
                "quality_notes": ai_notes,
            }
        )

    h_grade = heuristic.suggested_grade
    h_conf = heuristic.confidence
    h_note = (
        f"Heuristic: {h_grade.value} ({h_conf.value})"
        + (f" - {', '.join(heuristic.reasons)}" if heuristic.reasons else "")
    )
    ai_note_part = f"AI: {ai_grade.value}" + (f" - {ai_notes}" if ai_notes else "")

    # Extreme metric fail wins over AI (windowed images can look "ok" to the model).
    if (
        h_grade == ExposureQuality.INADEQUATE
        and h_conf == ExposureHeuristicConfidence.HIGH
    ):
        final_grade = ExposureQuality.INADEQUATE
        notes = _merge_notes(h_note, ai_note_part)
    elif h_grade == ai_grade:
        final_grade = h_grade
        notes = _merge_notes(ai_notes, h_note if h_grade != ExposureQuality.ADEQUATE else None)
    elif h_conf == ExposureHeuristicConfidence.LOW and ai_grade != ExposureQuality.UNDETERMINED:
        # Metrics uncertain → trust visual AI (positioning, inspiration, mild issues).
        final_grade = ai_grade
        notes = _merge_notes(ai_notes, h_note if h_grade != ExposureQuality.ADEQUATE else None)
    else:
        # Conflict: pick the worse (more conservative) grade.
        final_grade = h_grade if _rank(h_grade) >= _rank(ai_grade) else ai_grade
        notes = _merge_notes(h_note, ai_note_part)

    if final_grade == ExposureQuality.ADEQUATE and not (ai_notes and ai_notes.strip()):
        notes = None

    return ai_technical.model_copy(
        update={
            "exposure_quality": final_grade,
            "quality_notes": notes,
        }
    )


def heuristic_to_llm_hint(assessment: ExposureHeuristicAssessment | None) -> dict[str, Any] | None:
    """Compact hint block for the findings user message (not forced copy values)."""
    if assessment is None:
        return None
    return {
        "suggested_grade": assessment.suggested_grade.value,
        "confidence": assessment.confidence.value,
        "reasons": list(assessment.reasons),
        "black_clip_pct": assessment.black_clip_pct,
        "white_clip_pct": assessment.white_clip_pct,
        "dynamic_range_ratio": assessment.dynamic_range_ratio,
        "mean_normalized": assessment.mean_normalized,
        "exposure_index": assessment.exposure_index,
        "deviation_index": assessment.deviation_index,
        "target_exposure_index": assessment.target_exposure_index,
        "note": (
            "Pixel/DICOM heuristic only — use as a hint. Judge final "
            "exposure_quality and quality_notes from the image; service will fuse."
        ),
    }
