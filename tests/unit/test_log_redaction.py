"""Task 19 — enqueue/complete logs must not include case_id or filesystem paths."""

from __future__ import annotations

import inspect

from app.api.routes import radiology as radiology_routes
from app.modules.radiology import service as radiology_service


def test_analyze_route_log_format_omits_case_id() -> None:
    source = inspect.getsource(radiology_routes.analyze_dicom)
    assert "case_id=%s" not in source
    assert "user_id=%s facility_id=%s" in source


def test_enqueue_and_complete_logs_omit_case_id_and_paths() -> None:
    enqueue_src = inspect.getsource(radiology_service.enqueue_dicom_analyze)
    complete_src = inspect.getsource(radiology_service.process_queued_dicom)
    persist_src = inspect.getsource(radiology_service._persist_token_usage_best_effort)

    assert "case_id=%s" not in enqueue_src
    assert "path=%s" not in enqueue_src
    assert "finding_id=%s" in enqueue_src

    assert "case_id=%s" not in complete_src
    assert "image=%s" not in complete_src

    assert "case_id=%s" not in persist_src
