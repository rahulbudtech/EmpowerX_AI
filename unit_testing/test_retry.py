"""
Retry policy unit tests (permanent vs transient) — no live Gemini required.

Covers:
  1) LLM transient classifier heuristics
  2) Bad DICOM / missing file → PermanentPipelineError
  3) process_queued_dicom: permanent → mark failed; transient → leave processing
  4) Celery task: permanent → no retry; transient → self.retry; exhausted → failed

Usage (from repo root, venv active):
  python unit_testing/test_retry.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from celery.exceptions import MaxRetriesExceededError, Retry  # noqa: E402

from app.modules.radiology.llm import (  # noqa: E402
    FindingsLLMError,
    FindingsLLMTransientError,
    _is_transient_provider_error,
)
from app.modules.radiology.models import FindingsStatus  # noqa: E402
from app.modules.radiology.service import (  # noqa: E402
    PermanentPipelineError,
    TransientPipelineError,
    analyze_dicom_path,
    process_queued_dicom,
)
from app.workers.tasks import radiology_tasks as tasks  # noqa: E402

RESULTS_DIR = ROOT / "unit_testing_results"


class _Check:
    def __init__(self, name: str) -> None:
        self.name = name
        self.ok = False
        self.detail = ""

    def pass_(self, detail: str = "") -> None:
        self.ok = True
        self.detail = detail

    def fail(self, detail: str) -> None:
        self.ok = False
        self.detail = detail


def _test_classifier() -> _Check:
    check = _Check("classifier_transient_vs_permanent")
    try:
        assert _is_transient_provider_error(TimeoutError("timed out"))
        assert _is_transient_provider_error(ConnectionError("reset"))
        assert _is_transient_provider_error(Exception("429 rate limit"))
        assert _is_transient_provider_error(Exception("503 unavailable"))
        assert _is_transient_provider_error(Exception("resource exhausted"))
        assert not _is_transient_provider_error(Exception("invalid api key"))
        assert not _is_transient_provider_error(Exception("Report schema validation failed"))
        assert issubclass(FindingsLLMTransientError, FindingsLLMError)
        check.pass_("timeout/429/5xx=transient; key/schema=permanent")
    except AssertionError as exc:
        check.fail(str(exc))
    return check


def _test_permanent_missing_file() -> _Check:
    check = _Check("analyze_missing_file_is_permanent")
    missing = ROOT / "unit_testing" / "input" / "__does_not_exist__.dcm"
    try:
        analyze_dicom_path(missing)
        check.fail("expected PermanentPipelineError")
    except PermanentPipelineError as exc:
        check.pass_(str(exc)[:200])
    except Exception as exc:  # noqa: BLE001
        check.fail(f"wrong exception type: {type(exc).__name__}: {exc}")
    return check


def _test_permanent_corrupt_dicom() -> _Check:
    check = _Check("analyze_corrupt_bytes_is_permanent")
    try:
        with tempfile.NamedTemporaryFile(suffix=".dcm", delete=False) as tmp:
            tmp.write(b"NOT_A_DICOM_FILE")
            path = Path(tmp.name)
        try:
            analyze_dicom_path(path)
            check.fail("expected PermanentPipelineError")
        except PermanentPipelineError as exc:
            check.pass_(str(exc)[:200])
        except Exception as exc:  # noqa: BLE001
            check.fail(f"wrong exception type: {type(exc).__name__}: {exc}")
        finally:
            path.unlink(missing_ok=True)
    except Exception as exc:  # noqa: BLE001
        check.fail(str(exc))
    return check


def _test_process_permanent_marks_failed() -> _Check:
    check = _Check("process_permanent_marks_failed")
    finding_id = uuid.uuid4()
    db = MagicMock()
    row = MagicMock()
    row.source_file = "x.dcm"

    with (
        patch("app.modules.radiology.service.mark_finding_processing", return_value=row),
        patch(
            "app.modules.radiology.service.analyze_dicom_path",
            side_effect=PermanentPipelineError("bad dicom"),
        ) as analyze,
        patch("app.modules.radiology.service.mark_finding_failed") as mark_failed,
        patch("app.modules.radiology.service.mark_finding_completed") as mark_completed,
    ):
        try:
            process_queued_dicom(db, finding_id=finding_id, file_path="x.dcm")
            check.fail("expected PermanentPipelineError")
            return check
        except PermanentPipelineError:
            pass

        if mark_failed.call_count != 1:
            check.fail(f"mark_finding_failed called {mark_failed.call_count} times")
        elif mark_completed.call_count != 0:
            check.fail("mark_finding_completed should not run")
        elif analyze.call_count != 1:
            check.fail("analyze should run once")
        else:
            check.pass_("failed once, no completed")
    return check


def _test_process_transient_keeps_processing() -> _Check:
    check = _Check("process_transient_does_not_mark_failed")
    finding_id = uuid.uuid4()
    db = MagicMock()
    row = MagicMock()
    row.source_file = "x.dcm"

    with (
        patch("app.modules.radiology.service.mark_finding_processing", return_value=row),
        patch(
            "app.modules.radiology.service.analyze_dicom_path",
            side_effect=TransientPipelineError("429 rate limit"),
        ),
        patch("app.modules.radiology.service.mark_finding_failed") as mark_failed,
        patch("app.modules.radiology.service.mark_finding_completed") as mark_completed,
    ):
        try:
            process_queued_dicom(db, finding_id=finding_id, file_path="x.dcm")
            check.fail("expected TransientPipelineError")
            return check
        except TransientPipelineError:
            pass

        if mark_failed.call_count != 0:
            check.fail("must not mark failed on transient (Celery will retry)")
        elif mark_completed.call_count != 0:
            check.fail("mark_finding_completed should not run")
        else:
            check.pass_("stays processing for retry")
    return check


def _test_task_permanent_no_retry() -> _Check:
    check = _Check("task_permanent_returns_failed_no_retry")
    finding_id = str(uuid.uuid4())
    task = tasks.analyze_dicom_task
    db = MagicMock()

    with (
        patch.object(tasks, "SessionLocal", return_value=db),
        patch.object(
            tasks,
            "process_queued_dicom",
            side_effect=PermanentPipelineError("corrupt"),
        ),
        patch.object(
            task,
            "retry",
            side_effect=AssertionError("retry must not be called"),
        ) as retry_mock,
    ):
        task.push_request(retries=0)
        try:
            result = task.run(finding_id, r"C:\fake\x.dcm", None)
        finally:
            task.pop_request()

    if result.get("status") != "failed":
        check.fail(f"expected failed, got {result}")
    elif retry_mock.called:
        check.fail("retry was called for permanent error")
    else:
        check.pass_(str(result.get("error", ""))[:120])
    return check


def _test_task_transient_calls_retry() -> _Check:
    check = _Check("task_transient_calls_celery_retry")
    finding_id = str(uuid.uuid4())
    task = tasks.analyze_dicom_task
    db = MagicMock()

    with (
        patch.object(tasks, "SessionLocal", return_value=db),
        patch.object(
            tasks,
            "process_queued_dicom",
            side_effect=TransientPipelineError("timeout"),
        ),
        patch.object(task, "retry", side_effect=Retry(message="retry scheduled")) as retry_mock,
        patch("app.modules.radiology.repository.mark_finding_failed") as mark_failed,
    ):
        task.push_request(retries=0)
        try:
            try:
                task.run(finding_id, r"C:\fake\x.dcm", None)
                check.fail("expected Retry to propagate")
                return check
            except Retry:
                pass
        finally:
            task.pop_request()

        if not retry_mock.called:
            check.fail("self.retry was not called")
        elif mark_failed.call_count != 0:
            check.fail("must not mark failed when retrying")
        else:
            check.pass_(f"retry called; status stays {FindingsStatus.PROCESSING}")
    return check


def _test_task_retries_exhausted_marks_failed() -> _Check:
    check = _Check("task_retries_exhausted_marks_failed")
    finding_id = str(uuid.uuid4())
    fid = uuid.UUID(finding_id)
    task = tasks.analyze_dicom_task
    db = MagicMock()

    with (
        patch.object(tasks, "SessionLocal", return_value=db),
        patch.object(
            tasks,
            "process_queued_dicom",
            side_effect=TransientPipelineError("still 429"),
        ),
        patch.object(
            task,
            "retry",
            side_effect=MaxRetriesExceededError("done"),
        ),
        patch("app.modules.radiology.repository.mark_finding_failed") as mark_failed,
    ):
        task.push_request(retries=tasks._MAX_RETRIES)
        try:
            result = task.run(finding_id, r"C:\fake\x.dcm", None)
        finally:
            task.pop_request()

    if result.get("status") != "failed":
        check.fail(f"expected failed after exhaustion, got {result}")
    elif mark_failed.call_count != 1:
        check.fail(f"mark_finding_failed called {mark_failed.call_count} times")
    else:
        called_fid = mark_failed.call_args.args[1]
        if called_fid != fid:
            check.fail(f"wrong finding id marked failed: {called_fid}")
        else:
            check.pass_(
                f"exhausted {tasks._MAX_RETRIES + 1} attempts / "
                f"{tasks._RETRY_DELAY_S}s delay: {str(result.get('error', ''))[:100]}"
            )
    return check


def _test_llm_maps_transient() -> _Check:
    """_run_findings maps FindingsLLMTransientError → TransientPipelineError."""
    check = _Check("llm_transient_maps_to_pipeline_transient")
    import numpy as np

    from app.modules.radiology.preprocess import PreprocessResult
    from app.modules.radiology.schemas import PreprocessingInfo, SafeDicomMetadata, WindowSource
    from app.modules.radiology.service import _run_findings

    pre = PreprocessResult(
        image_u8=np.zeros((8, 8), dtype=np.uint8),
        metadata=SafeDicomMetadata(
            modality="CR",
            preprocessing=PreprocessingInfo(
                raw_shape=[8, 8],
                raw_dtype="uint8",
                raw_min=0.0,
                raw_max=0.0,
                scaled_min=0.0,
                scaled_max=0.0,
                window_source=WindowSource.NONE,
            ),
        ),
    )
    settings = MagicMock()
    settings.llm_provider = "gemini"

    with patch(
        "app.modules.radiology.service.generate_findings_report",
        side_effect=FindingsLLMTransientError("429"),
    ):
        try:
            _run_findings(pre, settings=settings)
            check.fail("expected TransientPipelineError")
        except TransientPipelineError as exc:
            check.pass_(str(exc)[:120])
        except Exception as exc:  # noqa: BLE001
            check.fail(f"{type(exc).__name__}: {exc}")
    return check


def main() -> int:
    import logging

    # Expected failures log at warning/error — keep console readable for this script.
    logging.disable(logging.CRITICAL)

    print("Empowerx - retry policy unit tests")
    print(
        f"max_retries={tasks._MAX_RETRIES} delay_s={tasks._RETRY_DELAY_S} "
        f"(total attempts={tasks._MAX_RETRIES + 1})\n"
    )

    checks = [
        _test_classifier(),
        _test_permanent_missing_file(),
        _test_permanent_corrupt_dicom(),
        _test_llm_maps_transient(),
        _test_process_permanent_marks_failed(),
        _test_process_transient_keeps_processing(),
        _test_task_permanent_no_retry(),
        _test_task_transient_calls_retry(),
        _test_task_retries_exhausted_marks_failed(),
    ]

    passed = sum(1 for c in checks if c.ok)
    failed = len(checks) - passed
    for c in checks:
        mark = "PASS" if c.ok else "FAIL"
        print(f"  [{mark}] {c.name}")
        if c.detail:
            print(f"         {c.detail}")

    print(f"\n{passed}/{len(checks)} passed")

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out = RESULTS_DIR / "retry_policy_test.json"
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "max_retries": tasks._MAX_RETRIES,
        "retry_delay_s": tasks._RETRY_DELAY_S,
        "total_attempts": tasks._MAX_RETRIES + 1,
        "passed": passed,
        "failed": failed,
        "checks": [{"name": c.name, "ok": c.ok, "detail": c.detail} for c in checks],
    }
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"Wrote {out}")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
