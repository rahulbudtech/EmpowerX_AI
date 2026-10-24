"""
Temporary Celery multi-request queue test.

Prerequisites (2 terminals):
  1) uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
  2) .\\run_worker.ps1

Fires N parallel POST /radiology/dicom/analyze, then polls each job until
completed/failed. Writes a summary JSON under unit_testing_results/.

Usage (from repo root, venv active):
  python unit_testing/test_celery_queue.py
  python unit_testing/test_celery_queue.py path/to/file.dcm --count 5
  python unit_testing/test_celery_queue.py --base-url http://127.0.0.1:8000 --count 3
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
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


def _enqueue(base_url: str, dicom_path: Path, case_id: str | None, index: int) -> dict:
    url = f"{base_url.rstrip('/')}/radiology/dicom/analyze"
    # Do not invent case_id — worker sets it from DICOM AccessionNumber.
    data = {"case_id": case_id} if case_id else None
    with dicom_path.open("rb") as fh:
        # Field name must be ``files`` (API accepts 1–3 parts with this name).
        upload = {"files": (dicom_path.name, fh, "application/dicom")}
        with httpx.Client(timeout=60.0) as client:
            resp = client.post(url, data=data, files=upload)
    payload = {
        "index": index,
        "http_status": resp.status_code,
        "enqueue_ok": resp.status_code == 202,
    }
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        body = {"raw": resp.text[:500]}
    payload["body"] = body
    # Batch response: { "jobs": [ { id, status, ... }, ... ] }
    if isinstance(body, dict):
        jobs = body.get("jobs") or []
        if jobs and isinstance(jobs[0], dict) and jobs[0].get("id"):
            payload["finding_id"] = jobs[0]["id"]
            payload["status"] = jobs[0].get("status")
    return payload


def _poll_one(
    base_url: str,
    finding_id: str,
    *,
    timeout_s: float,
    interval_s: float,
) -> dict:
    url = f"{base_url.rstrip('/')}/radiology/findings/{finding_id}"
    deadline = time.monotonic() + timeout_s
    last: dict = {}
    with httpx.Client(timeout=30.0) as client:
        while time.monotonic() < deadline:
            resp = client.get(url)
            if resp.status_code != 200:
                return {
                    "finding_id": finding_id,
                    "poll_http_status": resp.status_code,
                    "final_status": "poll_error",
                    "body": resp.text[:500],
                }
            last = resp.json()
            status = last.get("status")
            if status in {"completed", "failed"}:
                return {
                    "finding_id": finding_id,
                    "final_status": status,
                    "has_report": last.get("report") is not None,
                    "error_message": last.get("error_message"),
                    "elapsed_hint": "done",
                }
            time.sleep(interval_s)
    return {
        "finding_id": finding_id,
        "final_status": "timeout",
        "last_status": last.get("status"),
        "has_report": last.get("report") is not None if last else False,
    }


def run(
    *,
    dicom_path: Path,
    base_url: str,
    count: int,
    timeout_s: float,
    interval_s: float,
    case_id: str | None,
) -> int:
    print(f"DICOM   : {dicom_path}")
    print(f"Base URL: {base_url}")
    print(f"Jobs    : {count} (parallel enqueue)")
    print("Make sure API + Celery worker are running.\n")

    t0 = time.monotonic()
    enqueued: list[dict] = []
    with ThreadPoolExecutor(max_workers=min(count, 8)) as pool:
        futures = [
            pool.submit(_enqueue, base_url, dicom_path, case_id, i)
            for i in range(1, count + 1)
        ]
        for fut in as_completed(futures):
            item = fut.result()
            enqueued.append(item)
            print(
                f"  enqueue[{item['index']}] http={item['http_status']} "
                f"id={item.get('finding_id')} status={item.get('status')}"
            )

    enqueued.sort(key=lambda x: x["index"])
    ok_enqueue = [e for e in enqueued if e.get("enqueue_ok") and e.get("finding_id")]
    if not ok_enqueue:
        print("FAIL: no jobs accepted (is the API up? worker not required for 202).")
        return 2

    print(f"\nPolling {len(ok_enqueue)} job(s) ...")
    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=min(len(ok_enqueue), 8)) as pool:
        futures = {
            pool.submit(
                _poll_one,
                base_url,
                e["finding_id"],
                timeout_s=timeout_s,
                interval_s=interval_s,
            ): e
            for e in ok_enqueue
        }
        for fut in as_completed(futures):
            item = fut.result()
            results.append(item)
            print(
                f"  poll {item['finding_id']} -> {item.get('final_status')} "
                f"report={item.get('has_report')}"
            )

    elapsed = time.monotonic() - t0
    completed = sum(1 for r in results if r.get("final_status") == "completed")
    failed = sum(1 for r in results if r.get("final_status") == "failed")
    timed_out = sum(1 for r in results if r.get("final_status") == "timeout")

    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dicom": str(dicom_path),
        "base_url": base_url,
        "count_requested": count,
        "enqueue_accepted": len(ok_enqueue),
        "completed": completed,
        "failed": failed,
        "timeout": timed_out,
        "elapsed_seconds": round(elapsed, 2),
        "enqueued": enqueued,
        "results": results,
    }

    out_dir = RESULTS_DIR / "celery_queue"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = out_dir / f"queue_test_{stamp}.json"
    out_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    print("\n--- summary ---")
    print(f"accepted  : {len(ok_enqueue)}/{count}")
    print(f"completed : {completed}")
    print(f"failed    : {failed}")
    print(f"timeout   : {timed_out}")
    print(f"elapsed   : {elapsed:.1f}s")
    print(f"saved     : {out_path}")

    # Success = all accepted jobs finished completed (queue handled them).
    if completed == len(ok_enqueue) and timed_out == 0:
        print("OK: queue handled all jobs.")
        return 0
    print("FAIL: some jobs did not complete successfully.")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fire N parallel DICOM analyze jobs and poll Celery queue results."
    )
    parser.add_argument(
        "dicom",
        nargs="?",
        type=Path,
        default=None,
        help="Path to .dcm (default: first file in unit_testing/input/)",
    )
    parser.add_argument("--count", type=int, default=3, help="Number of parallel jobs (default: 3)")
    parser.add_argument(
        "--base-url",
        default="http://127.0.0.1:8000",
        help="API base URL (default: http://127.0.0.1:8000)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=300.0,
        help="Per-job poll timeout seconds (default: 300)",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=2.0,
        help="Poll interval seconds (default: 2)",
    )
    parser.add_argument("--case-id", default=None, help="Optional case_id prefix")
    args = parser.parse_args()

    dicom_path = args.dicom or _find_default_dicom()
    if dicom_path is None or not dicom_path.exists():
        print(
            "ERROR: No DICOM provided.\n"
            f"  Put a .dcm under: {INPUT_DIR}\n"
            "  Or: python unit_testing/test_celery_queue.py path/to/file.dcm --count 3"
        )
        return 1

    if args.count < 1:
        print("ERROR: --count must be >= 1")
        return 1

    return run(
        dicom_path=dicom_path,
        base_url=args.base_url,
        count=args.count,
        timeout_s=args.timeout,
        interval_s=args.interval,
        case_id=args.case_id,
    )


if __name__ == "__main__":
    raise SystemExit(main())
