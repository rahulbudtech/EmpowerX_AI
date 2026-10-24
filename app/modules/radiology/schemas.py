"""
Radiology Pydantic models.

Preprocess-related models live here so API / service / repository layers
share one typed contract for DICOM → PNG metadata (no PHI).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field



class WindowSource(str, Enum):
    """How display windowing was chosen during preprocessing."""

    DICOM = "dicom"
    PERCENTILE_1_99 = "percentile_1_99"
    NONE = "none"


class PreprocessingInfo(BaseModel):
    """
    Technical record of the pixel pipeline (safe to store / send to LLM).

    Captures raw ranges, photometric handling, and which window was applied
    so downstream steps can audit or reproduce display settings.
    """

    model_config = ConfigDict(extra="forbid")

    raw_shape: list[int]
    raw_dtype: str
    raw_min: float
    raw_max: float
    scaled_min: float
    scaled_max: float
    photometric_interpretation: str | None = None
    monochrome1_inverted: bool = False
    window_source: WindowSource
    window_center: float | None = None
    window_width: float | None = None
    output_dtype: str = "uint8"
    output_range: list[int] = Field(default_factory=lambda: [0, 255])


class SafeDicomMetadata(BaseModel):
    """
    De-identified DICOM sidecar metadata.

    Intentionally excludes PHI (patient name, ID, birth date, accession,
    institution, etc.). UIDs are optional for local tracing only.
    """

    model_config = ConfigDict(extra="forbid")

    case_id: str | None = None
    source_file: str | None = None
    modality: str | None = None
    body_part_examined: str | None = None
    protocol_name: str | None = None
    study_description: str | None = None
    series_description: str | None = None
    image_type: list[str] | None = None
    rows: int | None = None
    columns: int | None = None
    number_of_frames: str = "1"
    bits_allocated: int | None = None
    bits_stored: int | None = None
    pixel_spacing_mm: list[float] | list[str] | None = None
    slice_thickness_mm: float | None = None
    spacing_between_slices_mm: float | None = None
    rescale_slope: float | None = 1.0
    rescale_intercept: float | None = 0.0
    kvp: float | None = None
    exposure: str | None = None
    manufacturer: str | None = None
    transfer_syntax_uid: str | None = None
    study_instance_uid: str | None = None
    series_instance_uid: str | None = None
    sop_instance_uid: str | None = None
    preprocessing: PreprocessingInfo
    phi_stripped: bool = True
    note: str = (
        "Patient identity fields (name, ID, birth date, accession, institution, etc.) "
        "are intentionally excluded from this sidecar."
    )

    def to_llm_safe_dict(self) -> dict[str, Any]:
        """
        Subset useful for LLM context: technical tags only, no UIDs / filenames.

        Keeps a trimmed preprocessing block (window + photometric).
        """
        keep_keys = [
            "modality",
            "body_part_examined",
            "protocol_name",
            "study_description",
            "series_description",
            "image_type",
            "rows",
            "columns",
            "number_of_frames",
            "pixel_spacing_mm",
            "slice_thickness_mm",
            "spacing_between_slices_mm",
            "kvp",
            "exposure",
        ]
        data = self.model_dump()
        out: dict[str, Any] = {
            key: data[key] for key in keep_keys if data.get(key) is not None
        }
        p = self.preprocessing
        out["preprocessing"] = {
            "photometric_interpretation": p.photometric_interpretation,
            "monochrome1_inverted": p.monochrome1_inverted,
            "window_source": p.window_source.value,
            "window_center": p.window_center,
            "window_width": p.window_width,
        }
        return out


# ---------------------------------------------------------------------------
# LLM findings report (exact schema expected from the model)
# ---------------------------------------------------------------------------


class ViewPosition(str, Enum):
    PA = "PA"
    AP = "AP"
    LAT = "LAT"
    UNKNOWN = "UNKNOWN"


class ExposureQuality(str, Enum):
    ADEQUATE = "Adequate"
    SUBOPTIMAL = "Suboptimal"
    INADEQUATE = "Inadequate"
    UNDETERMINED = "Undetermined"


class Likelihood(str, Enum):
    DEFINITE = "Definite"
    PROBABLE = "Probable"
    POSSIBLE = "Possible"
    LESS_LIKELY = "Less likely"
    UNLIKELY = "Unlikely"


class TechnicalMetadata(BaseModel):
    """Copied from the DICOM METADATA block — model must not invent these."""

    model_config = ConfigDict(extra="ignore")

    view_position: ViewPosition = ViewPosition.UNKNOWN
    exposure_quality: ExposureQuality = ExposureQuality.UNDETERMINED
    quality_notes: str | None = None


class FindingItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    observation: str


class DiagnosisItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    icd11_code: str | None = None
    likelihood: Likelihood | None = None


class ClinicalImpression(BaseModel):
    model_config = ConfigDict(extra="ignore")

    probable_diagnosis: DiagnosisItem | None = None
    differential_diagnoses: list[DiagnosisItem] = Field(default_factory=list)
    uncertainty_or_limitations: str | None = None


class RecommendationItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    modality: str
    rationale: str | None = None


class RadiologyReport(BaseModel):
    """
    Structured JSON report returned by the findings LLM.

    Canonical key order (API + DB storage):
      1. technical_metadata
      2. findings
      3. clinical_impression
      4. recommendations
    """

    model_config = ConfigDict(extra="ignore")

    technical_metadata: TechnicalMetadata
    findings: list[FindingItem] = Field(default_factory=list)
    clinical_impression: ClinicalImpression | None = None
    recommendations: list[RecommendationItem] = Field(default_factory=list)

    def to_storage_dict(self) -> dict[str, Any]:
        """Serialize with fixed top-level key order for DB / API payloads."""
        data = self.model_dump(mode="json")
        return {
            "technical_metadata": data["technical_metadata"],
            "findings": data["findings"],
            "clinical_impression": data["clinical_impression"],
            "recommendations": data["recommendations"],
        }


# ---------------------------------------------------------------------------
# HTTP API response (temporary — DB / case schema TBD)
# ---------------------------------------------------------------------------


class DicomAnalyzeQueuedResponse(BaseModel):
    """One enqueued DICOM job (poll GET /radiology/findings/{id})."""

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    case_id: str | None = None
    source_file: str | None = None
    provider: str
    status: str


class DicomAnalyzeBatchQueuedResponse(BaseModel):
    """Response for multi-DICOM upload (1–3 files → one job each)."""

    model_config = ConfigDict(extra="forbid")

    jobs: list[DicomAnalyzeQueuedResponse]


class FindingsDetailResponse(BaseModel):
    """Response for GET /radiology/findings/{id} (includes in-progress jobs)."""

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    case_id: str | None = None
    source_file: str | None = None
    modality: str | None = None
    status: str
    provider: str
    report: RadiologyReport | None = None
    preprocess_image_url: str | None = None
    error_message: str | None = None
    created_at: datetime
    updated_at: datetime
