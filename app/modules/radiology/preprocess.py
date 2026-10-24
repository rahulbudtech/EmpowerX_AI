"""
DICOM preprocessing for radiology (phase 1: single-frame grayscale).

Pipeline (per file / bytes):
  1. Read DICOM (pydicom)
  2. Decode pixels
  3. Apply RescaleSlope / RescaleIntercept
  4. Apply window/level (DICOM tags, or percentile fallback)
  5. Handle MONOCHROME1 (invert so PNG is display-normal)
  6. Convert to 8-bit uint8
  7. Build PHI-stripped SafeDicomMetadata (Pydantic)

Public entry points:
  - preprocess_dicom_path(...)  → PreprocessResult (in-memory image + metadata)
  - preprocess_dicom_bytes(...) → same, from upload bytes
  - save_preprocessed(...)      → optional PNG + JSON sidecar on disk
"""

from __future__ import annotations

import json
import uuid
import warnings
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any, BinaryIO

import numpy as np
import pydicom
from PIL import Image
from pydicom.dataset import Dataset
from pydicom.misc import is_dicom

from app.modules.radiology.schemas import (
    PreprocessingInfo,
    SafeDicomMetadata,
    WindowSource,
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class DicomPreprocessError(Exception):
    """Raised when a DICOM cannot be decoded or converted for LLM use."""


# ---------------------------------------------------------------------------
# Result container (ndarray stays outside Pydantic)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class PreprocessResult:
    """In-memory preprocess output: display image + typed safe metadata."""

    image_u8: np.ndarray  # HxW, dtype=uint8
    metadata: SafeDicomMetadata

    @property
    def height(self) -> int:
        return int(self.image_u8.shape[0])

    @property
    def width(self) -> int:
        return int(self.image_u8.shape[1])


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _as_float(value: Any, default: float | None = None) -> float | None:
    """Coerce DICOM numeric / MultiValue tags to a single float."""
    if value is None:
        return default
    try:
        # WindowCenter/Width can be MultiValue — take the first.
        if hasattr(value, "__iter__") and not isinstance(value, (str, bytes)):
            value = list(value)[0]
        return float(value)
    except (TypeError, ValueError, IndexError):
        return default


def _safe_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _load_dataset_from_path(path: Path) -> Dataset:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return pydicom.dcmread(str(path), force=True)


def _load_dataset_from_bytes(data: bytes) -> Dataset:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return pydicom.dcmread(BytesIO(data), force=True)


def _ensure_dicom_path(path: Path) -> None:
    if not path.exists():
        raise FileNotFoundError(f"Path does not exist: {path}")
    if not is_dicom(str(path)):
        raise DicomPreprocessError(f"Not a valid DICOM file: {path}")


# ---------------------------------------------------------------------------
# Pixel pipeline
# ---------------------------------------------------------------------------


def get_pixel_array(ds: Dataset) -> np.ndarray:
    """
    Decode pixel data to a NumPy array.

    Multi-frame volumes: first frame only in v1 (DX is usually single-frame).
    RGB DICOM is rejected for now (grayscale only).
    """
    try:
        arr = ds.pixel_array
    except Exception as exc:  # noqa: BLE001 — surface decode tips to caller
        raise DicomPreprocessError(
            f"Could not decode pixel_array: {exc}. "
            "Tip: pip install pylibjpeg pylibjpeg-libjpeg pylibjpeg-openjpeg python-gdcm"
        ) from exc

    arr = np.asarray(arr)

    # Multi-frame stack → first frame (not HWC RGB).
    if arr.ndim == 3 and arr.shape[0] > 1 and arr.shape[-1] not in (3, 4):
        arr = arr[0]

    if arr.ndim != 2:
        raise DicomPreprocessError(
            f"Unsupported pixel shape {arr.shape}; expected 2D grayscale for v1."
        )
    return arr


def apply_rescale(arr: np.ndarray, ds: Dataset) -> np.ndarray:
    """Apply RescaleSlope / RescaleIntercept → float64 HU/display values."""
    slope = _as_float(ds.get("RescaleSlope"), 1.0) or 1.0
    intercept = _as_float(ds.get("RescaleIntercept"), 0.0) or 0.0
    if slope == 1.0 and intercept == 0.0:
        return arr.astype(np.float64, copy=False)
    return arr.astype(np.float64) * slope + intercept


def resolve_window(ds: Dataset) -> tuple[float, float] | None:
    """Return (center, width) from DICOM tags, or None if missing/invalid."""
    center = _as_float(ds.get("WindowCenter"))
    width = _as_float(ds.get("WindowWidth"))
    if center is None or width is None or width <= 0:
        return None
    return center, width


def apply_window(arr: np.ndarray, center: float, width: float) -> np.ndarray:
    """Linear window/level → values in [0, 1]."""
    lo = center - width / 2.0
    hi = center + width / 2.0
    clipped = np.clip(arr, lo, hi)
    if hi == lo:
        return np.zeros_like(clipped, dtype=np.float64)
    return (clipped - lo) / (hi - lo)


def apply_percentile_window(
    arr: np.ndarray,
    low: float = 1.0,
    high: float = 99.0,
) -> np.ndarray:
    """Fallback window using percentiles when DICOM W/L is absent."""
    lo = float(np.percentile(arr, low))
    hi = float(np.percentile(arr, high))
    if hi <= lo:
        lo = float(arr.min())
        hi = float(arr.max())
    if hi <= lo:
        return np.zeros_like(arr, dtype=np.float64)
    clipped = np.clip(arr, lo, hi)
    return (clipped - lo) / (hi - lo)


def handle_photometric(arr01: np.ndarray, photometric: str | None) -> np.ndarray:
    """
    Normalize so higher value = brighter in the PNG.

    MONOCHROME1: min pixel = white → invert after windowing.
    MONOCHROME2 / missing: min = black → keep.
    """
    if photometric and photometric.upper() == "MONOCHROME1":
        return 1.0 - arr01
    return arr01


def to_uint8(arr01: np.ndarray) -> np.ndarray:
    return np.clip(np.round(arr01 * 255.0), 0, 255).astype(np.uint8)


def preprocess_pixels(
    ds: Dataset,
    *,
    use_dicom_window: bool = True,
) -> tuple[np.ndarray, PreprocessingInfo]:
    """
    Run the full pixel pipeline on an open Dataset.

    Returns (uint8 HxW image, PreprocessingInfo).
    """
    raw = get_pixel_array(ds)
    scaled = apply_rescale(raw, ds)
    photometric = _safe_str(ds.get("PhotometricInterpretation"))

    window_center: float | None = None
    window_width: float | None = None

    if use_dicom_window:
        resolved = resolve_window(ds)
        if resolved is not None:
            window_center, window_width = resolved
            arr01 = apply_window(scaled, window_center, window_width)
            window_source = WindowSource.DICOM
        else:
            arr01 = apply_percentile_window(scaled)
            window_source = WindowSource.PERCENTILE_1_99
    else:
        arr01 = apply_percentile_window(scaled)
        window_source = WindowSource.PERCENTILE_1_99

    arr01 = handle_photometric(arr01, photometric)
    image_u8 = to_uint8(arr01)

    info = PreprocessingInfo(
        raw_shape=list(raw.shape),
        raw_dtype=str(raw.dtype),
        raw_min=float(np.min(raw)),
        raw_max=float(np.max(raw)),
        scaled_min=float(np.min(scaled)),
        scaled_max=float(np.max(scaled)),
        photometric_interpretation=photometric,
        monochrome1_inverted=bool(
            photometric and photometric.upper() == "MONOCHROME1"
        ),
        window_source=window_source,
        window_center=window_center,
        window_width=window_width,
    )
    return image_u8, info


# ---------------------------------------------------------------------------
# Safe metadata (no PHI)
# ---------------------------------------------------------------------------


def _looks_like_uuid(value: str) -> bool:
    """True if value is a UUID string (e.g. upload staging folder name)."""
    try:
        uuid.UUID(value)
        return True
    except (ValueError, AttributeError, TypeError):
        return False


def derive_case_id_from_dataset(ds: Dataset) -> str | None:
    """
    Resolve case key from DICOM tags.

    Demo/dataset case labels like ``DX001097`` live in AccessionNumber
    (and often PatientID), not in StudyInstanceUID.

    Order:
      AccessionNumber → PatientID → StudyID →
      StudyInstanceUID → SeriesInstanceUID → SOPInstanceUID
    """
    for tag in (
        "AccessionNumber",
        "PatientID",
        "StudyID",
        "StudyInstanceUID",
        "SeriesInstanceUID",
        "SOPInstanceUID",
    ):
        value = _safe_str(ds.get(tag))
        if value:
            return value
    return None


def resolve_case_id(
    ds: Dataset,
    *,
    explicit_case_id: str | None = None,
    fallback: str | None = None,
) -> str | None:
    """
    Prefer identity from the DICOM itself; then explicit/API override;
    then a folder-name fallback (ignored when it is a UUID upload dir).
    """
    safe_fallback = fallback
    if safe_fallback and _looks_like_uuid(safe_fallback):
        # uploads/{finding_id}/file.dcm — parent name must not become case_id
        safe_fallback = None
    return derive_case_id_from_dataset(ds) or explicit_case_id or safe_fallback


def extract_safe_metadata(
    ds: Dataset,
    *,
    source_name: str | None,
    process_info: PreprocessingInfo,
    case_id: str | None = None,
) -> SafeDicomMetadata:
    """Build PHI-stripped metadata from DICOM tags + preprocess info."""
    pixel_spacing = ds.get("PixelSpacing") or ds.get("ImagerPixelSpacing")
    parsed_spacing: list[float] | list[str] | None = None
    if pixel_spacing is not None:
        try:
            parsed_spacing = [float(pixel_spacing[0]), float(pixel_spacing[1])]
        except Exception:  # noqa: BLE001
            parsed_spacing = [str(pixel_spacing[0]), str(pixel_spacing[1])]

    transfer_syntax = None
    if hasattr(ds, "file_meta") and ds.file_meta is not None:
        transfer_syntax = _safe_str(getattr(ds.file_meta, "TransferSyntaxUID", None))

    image_type_raw = ds.get("ImageType", [])
    image_type = [str(x) for x in image_type_raw] or None

    return SafeDicomMetadata(
        case_id=case_id,
        source_file=source_name,
        modality=_safe_str(ds.get("Modality")),
        body_part_examined=_safe_str(ds.get("BodyPartExamined")),
        protocol_name=_safe_str(ds.get("ProtocolName")),
        study_description=_safe_str(ds.get("StudyDescription")),
        series_description=_safe_str(ds.get("SeriesDescription")),
        image_type=image_type,
        rows=int(ds.Rows) if getattr(ds, "Rows", None) is not None else None,
        columns=int(ds.Columns) if getattr(ds, "Columns", None) is not None else None,
        number_of_frames=_safe_str(ds.get("NumberOfFrames")) or "1",
        bits_allocated=(
            int(ds.BitsAllocated) if getattr(ds, "BitsAllocated", None) is not None else None
        ),
        bits_stored=(
            int(ds.BitsStored) if getattr(ds, "BitsStored", None) is not None else None
        ),
        pixel_spacing_mm=parsed_spacing,
        slice_thickness_mm=_as_float(ds.get("SliceThickness")),
        spacing_between_slices_mm=_as_float(ds.get("SpacingBetweenSlices")),
        rescale_slope=_as_float(ds.get("RescaleSlope"), 1.0),
        rescale_intercept=_as_float(ds.get("RescaleIntercept"), 0.0),
        kvp=_as_float(ds.get("KVP")),
        exposure=_safe_str(ds.get("Exposure")),
        manufacturer=_safe_str(ds.get("Manufacturer")),
        transfer_syntax_uid=transfer_syntax,
        study_instance_uid=_safe_str(ds.get("StudyInstanceUID")),
        series_instance_uid=_safe_str(ds.get("SeriesInstanceUID")),
        sop_instance_uid=_safe_str(ds.get("SOPInstanceUID")),
        preprocessing=process_info,
    )


def _build_result(
    ds: Dataset,
    *,
    source_name: str | None,
    case_id: str | None,
    use_dicom_window: bool,
) -> PreprocessResult:
    image_u8, process_info = preprocess_pixels(ds, use_dicom_window=use_dicom_window)
    metadata = extract_safe_metadata(
        ds,
        source_name=source_name,
        process_info=process_info,
        case_id=case_id,
    )
    return PreprocessResult(image_u8=image_u8, metadata=metadata)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def preprocess_dicom_path(
    path: str | Path,
    *,
    use_dicom_window: bool = True,
    case_id: str | None = None,
) -> PreprocessResult:
    """
    Preprocess a DICOM file from disk.

    ``case_id`` in metadata prefers StudyInstanceUID from the DICOM.
    Explicit ``case_id`` / parent folder name are fallbacks only.
    """
    path = Path(path)
    _ensure_dicom_path(path)
    ds = _load_dataset_from_path(path)
    resolved_case = resolve_case_id(
        ds,
        explicit_case_id=case_id,
        fallback=path.parent.name,
    )
    return _build_result(
        ds,
        source_name=path.name,
        case_id=resolved_case,
        use_dicom_window=use_dicom_window,
    )


def preprocess_dicom_bytes(
    data: bytes,
    *,
    source_name: str | None = None,
    use_dicom_window: bool = True,
    case_id: str | None = None,
) -> PreprocessResult:
    """
    Preprocess DICOM bytes (e.g. FastAPI UploadFile.read()).

    Prefer this path in the API layer so uploads never need a temp write
    unless you explicitly want a sidecar on disk.
    """
    if not data:
        raise DicomPreprocessError("Empty DICOM payload.")
    # Soft check: pydicom force=True still tries; surface clearer errors later.
    ds = _load_dataset_from_bytes(data)
    resolved_case = resolve_case_id(ds, explicit_case_id=case_id)
    return _build_result(
        ds,
        source_name=source_name,
        case_id=resolved_case,
        use_dicom_window=use_dicom_window,
    )


def preprocess_dicom_fileobj(
    fileobj: BinaryIO,
    *,
    source_name: str | None = None,
    use_dicom_window: bool = True,
    case_id: str | None = None,
) -> PreprocessResult:
    """Preprocess from any binary file-like object."""
    data = fileobj.read()
    return preprocess_dicom_bytes(
        data,
        source_name=source_name,
        use_dicom_window=use_dicom_window,
        case_id=case_id,
    )


# ---------------------------------------------------------------------------
# Optional disk persistence (PNG + JSON sidecar)
# ---------------------------------------------------------------------------


def save_preprocessed(
    result: PreprocessResult,
    out_dir: str | Path,
    *,
    stem: str | None = None,
) -> tuple[Path, Path]:
    """
    Write lossless PNG + metadata JSON next to each other.

    Returns (png_path, json_path). Safe for local debugging; the service
    layer may skip this and keep everything in memory / DB later.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    name = stem or (result.metadata.case_id or "dicom")
    if result.metadata.source_file:
        # Prefer original stem when available.
        src_stem = Path(result.metadata.source_file).stem
        name = stem or src_stem

    png_path = out_dir / f"{name}.png"
    json_path = out_dir / f"{name}.json"

    # Avoid clobbering when stem collides.
    if png_path.exists() and stem is None and result.metadata.case_id:
        name = f"{result.metadata.case_id}__{Path(result.metadata.source_file or 'dicom').stem}"
        png_path = out_dir / f"{name}.png"
        json_path = out_dir / f"{name}.json"

    Image.fromarray(result.image_u8, mode="L").save(png_path, format="PNG", optimize=False)
    json_path.write_text(
        json.dumps(result.metadata.model_dump(mode="json"), indent=2),
        encoding="utf-8",
    )
    return png_path, json_path
