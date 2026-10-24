"""
Temporary findings-LLM smoke test (preprocess → Gemini → JSON report).

Input:  a .dcm file (CLI path, or first file in unit_testing/input/)
Output: unit_testing_results/<stem>_findings/
          - <stem>.png              → preprocessed X-ray
          - <stem>_metadata.json    → safe DICOM metadata
          - <stem>_report.json      → structured findings report (API shape)

Requires GEMINI_API_KEY in .env

Usage (from repo root, venv active):
    python unit_testing/test_findings_llm.py
    python unit_testing/test_findings_llm.py path/to/file.dcm
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.config import get_settings  # noqa: E402
from app.modules.radiology.preprocess import save_preprocessed  # noqa: E402
from app.modules.radiology.service import (  # noqa: E402
    RadiologyServiceError,
    analyze_dicom_path,
)

INPUT_DIR = ROOT / "unit_testing" / "input"
RESULTS_DIR = ROOT / "unit_testing_results"


def _find_default_dicom() -> Path | None:
    if not INPUT_DIR.exists():
        return None
    candidates = sorted(
        p
        for p in INPUT_DIR.rglob("*")
        if p.is_file() and p.suffix.lower() in {".dcm", ".dicom"}
    )
    return candidates[0] if candidates else None


def run(dicom_path: Path, *, use_dicom_window: bool) -> int:
    settings = get_settings()
    if not settings.resolved_gemini_api_key:
        print("ERROR: GEMINI_API_KEY is empty. Set it in .env first.")
        return 1

    print(f"Input   : {dicom_path}")
    print(f"Provider: {settings.llm_provider}")
    print(f"Model   : {settings.llm_model}")

    try:
        result = analyze_dicom_path(
            dicom_path,
            use_dicom_window=use_dicom_window,
            settings=settings,
        )
    except RadiologyServiceError as exc:
        print(f"FAIL: {exc}")
        return 2

    stem = dicom_path.stem
    out_dir = RESULTS_DIR / f"{stem}_findings"
    out_dir.mkdir(parents=True, exist_ok=True)

    png_path, _meta_sidecar = save_preprocessed(result.preprocess, out_dir, stem=stem)

    meta_path = out_dir / f"{stem}_metadata.json"
    report_path = out_dir / f"{stem}_report.json"
    meta_path.write_text(
        json.dumps(result.metadata.model_dump(mode="json"), indent=2),
        encoding="utf-8",
    )
    report_path.write_text(
        json.dumps(result.report.model_dump(mode="json"), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print("OK")
    print(f"  generated_at : {stamp}")
    print(f"  image        : {png_path}")
    print(f"  metadata     : {meta_path}")
    print(f"  report       : {report_path}")
    print(f"  findings_n   : {len(result.report.findings)}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Temporary findings LLM test → PNG + report JSON in unit_testing_results/"
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
            "  Or pass a path:   python unit_testing/test_findings_llm.py path/to/file.dcm"
        )
        return 1

    return run(dicom_path, use_dicom_window=not args.no_window)


if __name__ == "__main__":
    raise SystemExit(main())
