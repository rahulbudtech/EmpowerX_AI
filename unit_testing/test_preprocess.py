"""
Temporary preprocess smoke test (not formal pytest).

Input:  a .dcm file (CLI path, or first file in unit_testing/input/)
Output: unit_testing_results/<stem>/
          - <stem>.png   → display X-ray image
          - <stem>.json  → safe metadata (structured)
          - <stem>.txt   → short human-readable summary

Usage (from repo root, venv active):
    python unit_testing/test_preprocess.py
    python unit_testing/test_preprocess.py path/to/file.dcm
    python unit_testing/test_preprocess.py path/to/file.dcm --no-window
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

# Repo root on sys.path so `app.*` imports work when run as a script.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.modules.radiology.preprocess import (  # noqa: E402
    DicomPreprocessError,
    preprocess_dicom_path,
    save_preprocessed,
)

INPUT_DIR = ROOT / "unit_testing" / "input"
RESULTS_DIR = ROOT / "unit_testing_results"


def _find_default_dicom() -> Path | None:
    """Pick the first .dcm / .dicom under unit_testing/input/."""
    if not INPUT_DIR.exists():
        return None
    candidates = sorted(
        p
        for p in INPUT_DIR.rglob("*")
        if p.is_file() and p.suffix.lower() in {".dcm", ".dicom"}
    )
    return candidates[0] if candidates else None


def _write_summary_txt(path: Path, *, dicom_path: Path, result) -> None:
    """Plain-text summary so you can skim results without opening JSON."""
    meta = result.metadata
    info = meta.preprocessing
    lines = [
        "Empowerx — temporary preprocess test result",
        f"generated_at : {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}",
        f"source_dicom : {dicom_path}",
        f"case_id      : {meta.case_id}",
        f"modality     : {meta.modality}",
        f"body_part    : {meta.body_part_examined}",
        f"rows x cols  : {meta.rows} x {meta.columns}",
        f"image_shape  : {result.image_u8.shape} dtype={result.image_u8.dtype}",
        f"window_source: {info.window_source.value}",
        f"window_center: {info.window_center}",
        f"window_width : {info.window_width}",
        f"photometric  : {info.photometric_interpretation}",
        f"mono1_invert : {info.monochrome1_inverted}",
        f"phi_stripped : {meta.phi_stripped}",
        "",
        "Full safe metadata JSON is saved next to this file.",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def run(dicom_path: Path, *, use_dicom_window: bool) -> int:
    if not dicom_path.exists():
        print(f"ERROR: DICOM not found: {dicom_path}")
        return 1

    print(f"Input : {dicom_path}")
    try:
        result = preprocess_dicom_path(
            dicom_path,
            use_dicom_window=use_dicom_window,
        )
    except (DicomPreprocessError, FileNotFoundError, ValueError) as exc:
        print(f"FAIL  : {exc}")
        return 2

    # One subfolder per run stem keeps results tidy / easy to delete.
    stem = dicom_path.stem
    out_dir = RESULTS_DIR / stem
    out_dir.mkdir(parents=True, exist_ok=True)

    png_path, json_path = save_preprocessed(result, out_dir, stem=stem)
    txt_path = out_dir / f"{stem}.txt"
    _write_summary_txt(txt_path, dicom_path=dicom_path, result=result)

    print("OK")
    print(f"  image : {png_path}")
    print(f"  json  : {json_path}")
    print(f"  text  : {txt_path}")
    print(f"  meta  : modality={result.metadata.modality} shape={result.image_u8.shape}")
    # Tiny peek so the console also shows text without opening the file.
    print("--- summary ---")
    print(txt_path.read_text(encoding="utf-8"))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Temporary DICOM preprocess test → PNG + JSON + TXT in unit_testing_results/"
    )
    parser.add_argument(
        "dicom",
        nargs="?",
        type=Path,
        default=None,
        help="Path to a .dcm file (default: first file in unit_testing/input/)",
    )
    parser.add_argument(
        "--no-window",
        action="store_true",
        help="Ignore DICOM WindowCenter/Width; use percentile windowing",
    )
    args = parser.parse_args()

    dicom_path = args.dicom or _find_default_dicom()
    if dicom_path is None:
        print(
            "ERROR: No DICOM provided.\n"
            f"  Put a .dcm under: {INPUT_DIR}\n"
            "  Or pass a path:   python unit_testing/test_preprocess.py path/to/file.dcm"
        )
        return 1

    return run(dicom_path, use_dicom_window=not args.no_window)


if __name__ == "__main__":
    raise SystemExit(main())
