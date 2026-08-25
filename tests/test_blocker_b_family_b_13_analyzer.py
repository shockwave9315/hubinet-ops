from __future__ import annotations

import hashlib
import json
import socket
import subprocess
from pathlib import Path
from typing import Any

import pytest

from scripts.research.blocker_b_family_b_13_analyzer import (
    ANALYZER_REVISION,
    CAPTURE_FILES,
    CLOCK_CONTRACT_REVISION,
    EXPECTED_B_S1_REVISION,
    EXPECTED_SOURCE_LEDGER,
    GENERATOR_CONTRACT_REVISION,
    PROTOCOL_REVISION,
    SCHEMA_REVISION,
    SUBRUN_CONTRACT_REVISION,
    AnalyzerOutcome,
    analyze_capture,
)


UPID_A = "UPID:fixture:00000001:00000001:00000001:stopall::generator@pve:"
UPID_B = "UPID:fixture:00000002:00000002:00000002:stopall::generator@pve:"
UPID_C = "UPID:fixture:00000003:00000003:00000003:stopall::generator@pve:"
UPID_X = "UPID:othernode:00000004:00000004:00000004:mystery:999:other@pve:"
UPID_OWNER_X = "UPID:fixture:00000005:00000005:00000005:stopall::other@pve:"

INOTIFY_MASKS = {
    "IN_ACCESS": 0x00000001,
    "IN_MODIFY": 0x00000002,
    "IN_ATTRIB": 0x00000004,
    "IN_CLOSE_WRITE": 0x00000008,
    "IN_CLOSE_NOWRITE": 0x00000010,
    "IN_OPEN": 0x00000020,
    "IN_MOVED_FROM": 0x00000040,
    "IN_MOVED_TO": 0x00000080,
    "IN_CREATE": 0x00000100,
    "IN_DELETE": 0x00000200,
    "IN_DELETE_SELF": 0x00000400,
    "IN_MOVE_SELF": 0x00000800,
    "IN_UNMOUNT": 0x00002000,
    "IN_Q_OVERFLOW": 0x00004000,
    "IN_IGNORED": 0x00008000,
    "IN_ISDIR": 0x40000000,
}


def _raw_mask(masks: list[str]) -> int:
    return sum(INOTIFY_MASKS[mask] for mask in masks)


def _active_raw(*upids: str) -> str:
    return "".join(f"{upid} 0\n" for upid in upids)


def _archive_raw(*upids: str) -> str:
    return "".join(f"{upid} 00000001 OK\n" for upid in upids)


def _set_subrun(
    manifest: dict[str, Any], subrun_id: str, phenomena: list[str]
) -> dict[str, str]:
    evidence_ids = {phenomenon: f"{subrun_id.lower()}-{phenomenon}" for phenomenon in phenomena}
    manifest["subrun_id"] = subrun_id
    manifest["generator_contract"]["subrun_id"] = subrun_id
    manifest["subrun_contract"] = {
        "contract_revision": SUBRUN_CONTRACT_REVISION,
        "subrun_id": subrun_id,
        "required_phenomena": phenomena,
        "evidence_ids": evidence_ids,
    }
    return evidence_ids


def _watch_event(
    sequence: int,
    *,
    masks: list[str],
    monotonic_ns: int,
    upid: str | None = None,
    event_type: str = "exact_log_event",
    filename: str | None = None,
    phenomenon_id: str | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "watcher_sequence": sequence,
        "event_type": event_type,
        "mask": masks,
        "watched_path": "tasks/0",
        "filename": filename if filename is not None else (upid or ""),
        "queue_overflow": "IN_Q_OVERFLOW" in masks,
        "watch_descriptor": -1 if "IN_Q_OVERFLOW" in masks else 1,
        "cookie": 0,
        "raw_mask": _raw_mask(masks),
        "raw_order": sequence,
        "monotonic_ns": monotonic_ns,
        "wall_timestamp": "2026-08-25T00:00:03Z",
    }
    if upid is not None:
        record["normalized_upid"] = upid
    if phenomenon_id is not None:
        record["phenomenon_id"] = phenomenon_id
    return record


def _set_surface_raw(
    record: dict[str, Any], raw_evidence: str, upids: list[str]
) -> None:
    record["raw_evidence"] = raw_evidence
    record["sha256"] = hashlib.sha256(raw_evidence.encode("utf-8")).hexdigest()
    record["normalized_upids"] = upids


def _append_surface(
    records: dict[str, list[dict[str, Any]]],
    source: str,
    *,
    capture_start: int,
    capture_end: int,
    raw_evidence: str,
    upids: list[str],
    stat: dict[str, int] | None = None,
) -> dict[str, Any]:
    sequence = len(records["surface_observations"]) + 1
    record: dict[str, Any] = {
        "observation_sequence": sequence,
        "source": source,
        "capture_start_monotonic_ns": capture_start,
        "capture_end_monotonic_ns": capture_end,
        "normalized_upids": upids,
        "raw_evidence": raw_evidence,
        "stat": stat or {"device": 1, "inode": sequence, "size": len(raw_evidence)},
        "sha256": hashlib.sha256(raw_evidence.encode("utf-8")).hexdigest(),
        "readable": True,
        "complete": True,
    }
    records["surface_observations"].append(record)
    return record


def _use_watch_as_only_discovery(
    records: dict[str, list[dict[str, Any]]], watch: dict[str, Any]
) -> None:
    records["watch_events"] = [watch]
    for scan in records["scan_rounds"]:
        scan["exact_normalized_upids"] = []
        scan["watch_drained_through_sequence"] = 1
    exact = records["exact_upid"][0]
    exact["discovery_source"] = "watch"
    exact["discovery_reference"] = 1


def _use_surface_as_only_discovery(
    records: dict[str, list[dict[str, Any]]], source: str
) -> dict[str, Any]:
    records["watch_events"] = []
    for scan in records["scan_rounds"]:
        scan["exact_normalized_upids"] = []
        scan["watch_drained_through_sequence"] = 0
    surface = next(
        record
        for record in reversed(records["surface_observations"])
        if record["source"] == source
    )
    exact = records["exact_upid"][0]
    exact["discovery_source"] = "active" if source == "active" else "archive"
    exact["discovery_reference"] = surface["observation_sequence"]
    return surface


def _use_scan_as_only_discovery(
    records: dict[str, list[dict[str, Any]]], scan_sequence: int
) -> None:
    records["watch_events"] = []
    for scan in records["scan_rounds"]:
        scan["watch_drained_through_sequence"] = 0
    exact = records["exact_upid"][0]
    exact["discovery_source"] = "scan"
    exact["discovery_reference"] = scan_sequence


def _set_pagination_pages(
    records: dict[str, list[dict[str, Any]]],
    evidence_id: str,
    intervals: list[tuple[int, int]],
) -> None:
    assert len(intervals) >= 2
    first = records["api_pages"][1]
    for sequence, (request_start, response_end) in enumerate(intervals, 1):
        page = first if sequence == 1 else dict(first)
        page.update(
            {
                "source": "archive",
                "start_offset": (sequence - 1) * 10,
                "request_identity": f"pagination-page-{sequence}",
                "request_start_monotonic_ns": request_start,
                "response_end_monotonic_ns": response_end,
                "phenomenon_id": evidence_id,
                "page_sequence": sequence,
            }
        )
        if sequence > 1:
            records["api_pages"].append(page)


def _ground_truth_operation(sequence: int, upid: str) -> list[dict[str, Any]]:
    request_id = f"request-{sequence}"
    return [
        {
            "event": "request_start",
            "generator_sequence": sequence,
            "request_id": request_id,
            "operation": "stopall_reserved_absent_vmid",
            "monotonic_ns": 100 + sequence * 10,
            "wall_timestamp": f"2026-08-25T00:00:{sequence:02d}Z",
            "generator_process_identity": "synthetic-generator:1",
        },
        {
            "event": "request_end",
            "generator_sequence": sequence,
            "request_id": request_id,
            "operation": "stopall_reserved_absent_vmid",
            "returned_upid": upid,
            "expected_task_type": "stopall",
            "expected_task_id": "",
            "outcome": "success",
            "within_generator_window": True,
            "generator_window_relation": "inside_generator_window",
            "b_s1_body_start_membership": "unknown",
            "body_start_evidence": None,
            "monotonic_ns": 120 + sequence * 10,
            "wall_timestamp": f"2026-08-25T00:00:{sequence:02d}Z",
            "generator_process_identity": "synthetic-generator:1",
        },
    ]


def _default_capture() -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    manifest = {
        "schema_revision": SCHEMA_REVISION,
        "experiment_id": "family-b-13",
        "run_uuid": "00000000-0000-4000-8000-000000000013",
        "protocol_revision": PROTOCOL_REVISION,
        "expected_b_s1_revision": EXPECTED_B_S1_REVISION,
        "fixture_id": "synthetic-fixture-13",
        "fixture_kind": "synthetic",
        "subrun_id": "13A",
        "clock_contract": {
            "contract_revision": CLOCK_CONTRACT_REVISION,
            "clock_kind": "CLOCK_MONOTONIC",
            "clock_domain_id": "synthetic-clock-domain-1",
            "boot_id": "synthetic-boot-id",
            "fixture_id": "synthetic-fixture-13",
            "node_identity": {"kind": "synthetic", "value": "node-a"},
            "time_namespace_id": "synthetic-time-namespace-1",
            "correlation_state": "synthetic_single_shared_domain",
            "participant_clock_domain_ids": {
                "manifest_boundaries": "synthetic-clock-domain-1",
                "reader": "synthetic-clock-domain-1",
                "generator": "synthetic-clock-domain-1",
                "pre_t0": "synthetic-clock-domain-1",
                "watch": "synthetic-clock-domain-1",
                "scan": "synthetic-clock-domain-1",
                "surface": "synthetic-clock-domain-1",
                "api": "synthetic-clock-domain-1",
                "exact": "synthetic-clock-domain-1",
                "harness": "synthetic-clock-domain-1",
            },
        },
        "t0_monotonic_ns": 100,
        "experiment_generator_window": {
            "start_monotonic_ns": 100,
            "end_monotonic_ns": 300,
        },
        "baseline_upids": [],
        "baseline_observation": {
            "capture_start_monotonic_ns": 60,
            "capture_end_monotonic_ns": 80,
            "committed_at_monotonic_ns": 90,
            "normalized_upids": [],
            "raw_evidence": "[]",
            "sha256": hashlib.sha256(b"[]").hexdigest(),
            "complete": True,
        },
        "t0_quiescence": {
            "state": "QUIESCENT",
            "committed_at_monotonic_ns": 100,
            "pending_upids": [],
            "surface_observation_sequences": {
                "active": 1,
                "index": 2,
                "index.1": 3,
            },
            "pre_t0_establishment": {
                "root_watch_establishment_sequence": 1,
                "baseline_scan_sequences": [1, 2],
                "watch_drained_through_sequence": 2,
            },
            "baseline_classifications": [],
        },
        "node_identity": {"kind": "synthetic", "value": "node-a"},
        "boot_id": "synthetic-boot-id",
        "version_ledger": [
            {
                "component": component,
                "installed_version": version,
                "source_commit": commit,
            }
            for component, (version, commit) in EXPECTED_SOURCE_LEDGER.items()
        ],
        "loaded_code_status": "exact_context_matched",
        "kernel_context": {"release": "synthetic", "source_commit": None},
        "filesystem_context": {"type": "synthetic", "mount_id": "fixture"},
        "started_at": "2026-08-25T00:00:00Z",
        "ended_at": "2026-08-25T00:01:00Z",
        "reader_context": {
            "process_identity": "synthetic-reader:1",
            "heartbeat_timeout_ns": 100,
        },
        "generator_context": {
            "process_identity": "synthetic-generator:1",
            "ground_truth_source": "operation_initiator_return_value",
        },
        "generator_contract": {
            "contract_revision": GENERATOR_CONTRACT_REVISION,
            "approval_state": "synthetic",
            "fixture_id": "synthetic-fixture-13",
            "subrun_id": "13A",
            "approved_operation": "stopall_reserved_absent_vmid",
            "expected_task_type": "stopall",
            "expected_task_id_policy": {"kind": "exact", "value": ""},
            "expected_node": "fixture",
            "expected_owner": "generator@pve",
            "maximum_operation_count": 1000,
            "maximum_duration_seconds": 600,
        },
        "subrun_contract": {
            "contract_revision": SUBRUN_CONTRACT_REVISION,
            "subrun_id": "13A",
            "required_phenomena": ["low_volume_enumeration"],
            "evidence_ids": {"low_volume_enumeration": "enumeration-1"},
        },
        "safety_limits": {
            "minimum_free_disk_bytes": 1,
            "minimum_free_log_bytes": 1,
            "maximum_task_rate_per_minute": 1000,
        },
        "capture_completeness": {
            "ground_truth_finalized": True,
            "pre_t0_establishment_complete": True,
            "watch_capture_complete": True,
            "scan_capture_complete": True,
            "surface_capture_complete": True,
            "api_capture_complete": True,
            "exact_upid_capture_complete": True,
            "harness_capture_complete": True,
        },
        "capture_files": dict(CAPTURE_FILES),
        "candidate_close": {
            "state": "CLOSED_COMPLETE",
            "event_id": "close-1",
            "monotonic_ns": 300,
            "known_upids": [UPID_A],
        },
    }
    records = {
        "ground_truth": [
            *_ground_truth_operation(1, UPID_A),
            {
                "event": "generator_finalized",
                "last_sequence": 1,
                "total_operations": 1,
                "durable_flush_complete": True,
                "generator_process_identity": "synthetic-generator:1",
                "monotonic_ns": 150,
                "wall_timestamp": "2026-08-25T00:00:02Z",
            },
        ],
        "pre_t0_establishment": [
            {
                "establishment_sequence": 1,
                "event": "watch_installed",
                "watcher_sequence": 1,
                "watch_scope": "task_root",
                "watched_path": "tasks",
                "monotonic_ns": 50,
                "complete": True,
            },
            {
                "establishment_sequence": 2,
                "event": "watch_installed",
                "watcher_sequence": 2,
                "watch_scope": "bucket",
                "watched_path": "tasks/0",
                "bucket": "0",
                "bucket_origin": "existing_at_root_install",
                "monotonic_ns": 51,
                "complete": True,
            },
            {
                "establishment_sequence": 3,
                "event": "baseline_scan",
                "phase": "PRE_T0_BASELINE",
                "baseline_scan_sequence": 1,
                "scan_start_monotonic_ns": 60,
                "scan_end_monotonic_ns": 65,
                "exact_normalized_upids": [],
                "bucket_set": ["0"],
                "watch_drained_through_sequence": 2,
                "unreadable_entries": [],
                "malformed_entries": [],
                "complete": True,
            },
            {
                "establishment_sequence": 4,
                "event": "baseline_scan",
                "phase": "PRE_T0_BASELINE",
                "baseline_scan_sequence": 2,
                "scan_start_monotonic_ns": 70,
                "scan_end_monotonic_ns": 75,
                "exact_normalized_upids": [],
                "bucket_set": ["0"],
                "watch_drained_through_sequence": 2,
                "unreadable_entries": [],
                "malformed_entries": [],
                "complete": True,
            },
        ],
        "watch_events": [
            {
                "watcher_sequence": 1,
                "event_type": "exact_log_event",
                "mask": ["IN_CREATE"],
                "watched_path": "tasks/0",
                "filename": UPID_A,
                "normalized_upid": UPID_A,
                "queue_overflow": False,
                "watch_descriptor": 1,
                "cookie": 0,
                "raw_mask": 256,
                "raw_order": 1,
                "monotonic_ns": 140,
                "wall_timestamp": "2026-08-25T00:00:02Z",
            }
        ],
        "scan_rounds": [
            {
                "scan_sequence": 1,
                "round_id": "scan-1",
                "scan_start_monotonic_ns": 180,
                "scan_end_monotonic_ns": 190,
                "exact_normalized_upids": [UPID_A],
                "bucket_set": ["0"],
                "stat_metadata": {},
                "unreadable_entries": [],
                "malformed_entries": [],
                "complete": True,
                "watch_drained_through_sequence": 1,
                "consistency_marker": "fixed_point",
            },
            {
                "scan_sequence": 2,
                "round_id": "scan-2",
                "scan_start_monotonic_ns": 200,
                "scan_end_monotonic_ns": 210,
                "exact_normalized_upids": [UPID_A],
                "bucket_set": ["0"],
                "stat_metadata": {},
                "unreadable_entries": [],
                "malformed_entries": [],
                "complete": True,
                "watch_drained_through_sequence": 1,
                "consistency_marker": "fixed_point",
            },
        ],
        "surface_observations": [
            {
                "observation_sequence": sequence,
                "source": source,
                "capture_start_monotonic_ns": 81 if sequence <= 3 else 220,
                "capture_end_monotonic_ns": 82 if sequence <= 3 else 221,
                "normalized_upids": [],
                "raw_evidence": "",
                "stat": {"device": 1, "inode": sequence, "size": 0},
                "sha256": hashlib.sha256(b"").hexdigest(),
                "readable": True,
                "complete": True,
            }
            for sequence, source in enumerate(
                ("active", "index", "index.1", "active", "index", "index.1"),
                1,
            )
        ],
        "api_pages": [
            {
                "source": source,
                "start_offset": 0,
                "limit": 10,
                "normalized_upids": [],
                "request_identity": f"api-{source}-1",
                "request_start_monotonic_ns": 225,
                "response_end_monotonic_ns": 226,
                "restart_reason": None,
                "complete_response": True,
            }
            for source in ("active", "archive", "all")
        ],
        "exact_upid": [
            {
                "observation_sequence": 1,
                "known_upid": UPID_A,
                "status_result": {
                    "available": True,
                    "raw_evidence": "stopped",
                    "sha256": hashlib.sha256(b"stopped").hexdigest(),
                    "task_state": "stopped",
                },
                "log_result": {
                    "available": True,
                    "raw_evidence": "TASK OK",
                    "sha256": hashlib.sha256(b"TASK OK").hexdigest(),
                    "terminal_status": "TASK OK",
                },
                "presence": True,
                "readable": True,
                "previously_known": True,
                "discovery_source": "watch",
                "discovery_reference": 1,
                "final_status_interpretation": "ok",
                "capture_start_monotonic_ns": 230,
                "capture_end_monotonic_ns": 231,
            }
        ],
        "harness_events": [
            {
                "event": "process_start",
                "process_identity": "synthetic-reader:1",
                "monotonic_ns": 50,
            },
            {
                "event": "analyzer_version",
                "analyzer_revision": ANALYZER_REVISION,
                "process_identity": "synthetic-reader:1",
                "monotonic_ns": 51,
            },
            {
                "event": "heartbeat",
                "heartbeat_sequence": 1,
                "healthy": True,
                "process_identity": "synthetic-reader:1",
                "monotonic_ns": 250,
            },
            {
                "event": "capture_finalized",
                "complete": True,
                "process_identity": "synthetic-reader:1",
                "monotonic_ns": 310,
            },
            {
                "event": "process_stop",
                "process_identity": "synthetic-reader:1",
                "monotonic_ns": 320,
            },
        ],
    }
    return manifest, records


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.write_text(
        "".join(json.dumps(record, sort_keys=True) + "\n" for record in records),
        encoding="utf-8",
    )


def _seal(capture_dir: Path, run_uuid: str) -> None:
    entries = []
    for name in sorted({"manifest.json", *CAPTURE_FILES.values()}):
        content = (capture_dir / name).read_bytes()
        entries.append(
            {"name": name, "sha256": hashlib.sha256(content).hexdigest(), "size": len(content)}
        )
    payload = {
        "schema_revision": SCHEMA_REVISION,
        "run_uuid": run_uuid,
        "analyzer_revision": ANALYZER_REVISION,
        "analyzer_commit": "synthetic-test-commit",
        "analyzer_source_sha256": hashlib.sha256(
            Path("scripts/research/blocker_b_family_b_13_analyzer.py").read_bytes()
        ).hexdigest(),
        "files": entries,
    }
    payload["overall_manifest_hash"] = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    _write_json(capture_dir / "seal.json", payload)


def _materialize(
    tmp_path: Path,
    manifest: dict[str, Any],
    records: dict[str, list[dict[str, Any]]],
) -> Path:
    capture_dir = tmp_path / "capture"
    capture_dir.mkdir()
    for sequence, event in enumerate(records["harness_events"], 1):
        event["harness_sequence"] = sequence
        event.setdefault("process_identity", "synthetic-reader:1")
    for sequence, event in enumerate(
        (event for event in records["harness_events"] if event["event"] == "heartbeat"),
        1,
    ):
        event["heartbeat_sequence"] = sequence
    _write_json(capture_dir / "manifest.json", manifest)
    for key, name in CAPTURE_FILES.items():
        _write_jsonl(capture_dir / name, records[key])
    _seal(capture_dir, manifest["run_uuid"])
    return capture_dir


def _add_ground_truth_operation(
    manifest: dict[str, Any], records: dict[str, list[dict[str, Any]]], upid: str
) -> None:
    records["ground_truth"].pop()
    second = _ground_truth_operation(2, upid)
    # The generator appends this stream in capture order, so the second
    # operation must start no earlier than the first one ended.
    second[0]["monotonic_ns"] = 140
    second[1]["monotonic_ns"] = 150
    records["ground_truth"].extend(second)
    records["ground_truth"].append(
        {
            "event": "generator_finalized",
            "last_sequence": 2,
            "total_operations": 2,
            "durable_flush_complete": True,
            "generator_process_identity": "synthetic-generator:1",
            "monotonic_ns": 160,
            "wall_timestamp": "2026-08-25T00:00:03Z",
        }
    )
    manifest["candidate_close"]["known_upids"] = [UPID_A]


def _add_finalized_historical_baseline(
    manifest: dict[str, Any], records: dict[str, list[dict[str, Any]]], upid: str
) -> None:
    baseline_raw = json.dumps([upid], separators=(",", ":"))
    manifest["baseline_upids"] = [upid]
    manifest["baseline_observation"]["normalized_upids"] = [upid]
    manifest["baseline_observation"]["raw_evidence"] = baseline_raw
    manifest["baseline_observation"]["sha256"] = hashlib.sha256(
        baseline_raw.encode("utf-8")
    ).hexdigest()
    for scan in records["pre_t0_establishment"][-2:]:
        scan["exact_normalized_upids"] = [upid]
    for scan in records["scan_rounds"]:
        scan["exact_normalized_upids"] = [UPID_A, upid]
    archive = records["surface_observations"][1]
    _set_surface_raw(archive, _archive_raw(upid), [upid])
    generated_exact = records["exact_upid"][0]
    generated_exact["observation_sequence"] = 2
    baseline_exact = {
        **generated_exact,
        "observation_sequence": 1,
        "known_upid": upid,
        "discovery_source": "baseline",
        "discovery_reference": "manifest.baseline_upids",
        "capture_start_monotonic_ns": 83,
        "capture_end_monotonic_ns": 84,
    }
    records["exact_upid"].insert(0, baseline_exact)
    manifest["t0_quiescence"]["baseline_classifications"] = [
        {
            "upid": upid,
            "lifecycle_state": "finalized",
            "operation_classification": "supported_in_scope",
            "exact_observation_sequence": 1,
        }
    ]


def _move_generator_operation_after_t1(
    records: dict[str, list[dict[str, Any]]],
) -> None:
    start, end = records["ground_truth"][:2]
    start["monotonic_ns"] = 310
    end["monotonic_ns"] = 320
    end["generator_window_relation"] = "after_generator_window"
    end["within_generator_window"] = False
    records["ground_truth"][-1]["monotonic_ns"] = 330


def _resequence_pre_t0(records: dict[str, list[dict[str, Any]]]) -> None:
    for sequence, record in enumerate(records["pre_t0_establishment"], 1):
        record["establishment_sequence"] = sequence


def _set_scan_history(
    records: dict[str, list[dict[str, Any]]],
    upid_sets: list[list[str]],
    watermarks: list[int],
) -> None:
    assert len(upid_sets) == len(watermarks)
    records["scan_rounds"] = [
        {
            "scan_sequence": sequence,
            "round_id": f"scan-{sequence}",
            "scan_start_monotonic_ns": 180 + (sequence - 1) * 20,
            "scan_end_monotonic_ns": 190 + (sequence - 1) * 20,
            "exact_normalized_upids": upids,
            "bucket_set": ["0"],
            "stat_metadata": {},
            "unreadable_entries": [],
            "malformed_entries": [],
            "complete": True,
            "watch_drained_through_sequence": watermark,
            "consistency_marker": "fixed_point",
        }
        for sequence, (upids, watermark) in enumerate(
            zip(upid_sets, watermarks, strict=True), 1
        )
    ]


def test_perfect_enumeration_is_only_tested_interleaving(tmp_path: Path) -> None:
    manifest, records = _default_capture()
    result = analyze_capture(_materialize(tmp_path, manifest, records))
    assert result.outcome is AnalyzerOutcome.PASS
    assert result.as_dict()["architecture_effect"] == "NONE"


def test_t0_index_surface_equal_to_request_start_cannot_manufacture_discovery(
    tmp_path: Path,
) -> None:
    manifest, records = _default_capture()
    records["ground_truth"][0]["monotonic_ns"] = 100
    records["watch_events"] = []
    for scan in records["scan_rounds"]:
        scan["exact_normalized_upids"] = []
        scan["watch_drained_through_sequence"] = 0
    surface = records["surface_observations"][1]
    surface["capture_start_monotonic_ns"] = 81
    surface["capture_end_monotonic_ns"] = 100
    _set_surface_raw(surface, _archive_raw(UPID_A), [UPID_A])
    exact = records["exact_upid"][0]
    exact["discovery_source"] = "archive"
    exact["discovery_reference"] = surface["observation_sequence"]

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.INCOMPLETE
    assert result.reasons == (
        "surface_generated_upid:index:2_not_strictly_after_request_start",
    )


def test_ordinary_surface_equal_to_request_start_is_incomplete(
    tmp_path: Path,
) -> None:
    manifest, records = _default_capture()
    records["ground_truth"][0]["monotonic_ns"] = 100
    surface = _use_surface_as_only_discovery(records, "active")
    surface["capture_start_monotonic_ns"] = 95
    surface["capture_end_monotonic_ns"] = 100
    _set_surface_raw(surface, _active_raw(UPID_A), [UPID_A])

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.INCOMPLETE
    assert result.reasons == (
        "surface_generated_upid:active:4_not_strictly_after_request_start",
    )


def test_ordinary_surface_after_request_start_is_temporally_eligible(
    tmp_path: Path,
) -> None:
    manifest, records = _default_capture()
    records["ground_truth"][0]["monotonic_ns"] = 100
    surface = _use_surface_as_only_discovery(records, "active")
    surface["capture_start_monotonic_ns"] = 95
    surface["capture_end_monotonic_ns"] = 101
    _set_surface_raw(surface, _active_raw(UPID_A), [UPID_A])

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.PASS


def test_pre_t0_watch_cannot_manufacture_post_t0_discovery(
    tmp_path: Path,
) -> None:
    manifest, records = _default_capture()
    _use_watch_as_only_discovery(
        records,
        _watch_event(
            1,
            masks=["IN_CREATE"],
            monotonic_ns=90,
            upid=UPID_A,
        ),
    )

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.INCOMPLETE
    assert result.reasons == ("post_t0_watch_event_not_after_t0:1",)


def test_watch_exactly_at_t0_belongs_to_pre_t0_establishment(
    tmp_path: Path,
) -> None:
    manifest, records = _default_capture()
    _use_watch_as_only_discovery(
        records,
        _watch_event(
            1,
            masks=["IN_CREATE"],
            monotonic_ns=100,
            upid=UPID_A,
        ),
    )

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.INCOMPLETE
    assert result.reasons == ("post_t0_watch_event_not_after_t0:1",)


def test_post_t0_watch_before_returning_request_start_is_incomplete(
    tmp_path: Path,
) -> None:
    manifest, records = _default_capture()
    _use_watch_as_only_discovery(
        records,
        _watch_event(
            1,
            masks=["IN_CREATE"],
            monotonic_ns=101,
            upid=UPID_A,
        ),
    )

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.INCOMPLETE
    assert result.reasons == (
        "watch_generated_upid:1_not_strictly_after_request_start",
    )


def test_watch_equal_to_request_start_is_incomplete(
    tmp_path: Path,
) -> None:
    manifest, records = _default_capture()
    _use_watch_as_only_discovery(
        records,
        _watch_event(
            1,
            masks=["IN_CREATE"],
            monotonic_ns=110,
            upid=UPID_A,
        ),
    )

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.INCOMPLETE
    assert result.reasons == (
        "watch_generated_upid:1_not_strictly_after_request_start",
    )


def test_watch_after_request_start_is_temporally_eligible(
    tmp_path: Path,
) -> None:
    manifest, records = _default_capture()
    _use_watch_as_only_discovery(
        records,
        _watch_event(
            1,
            masks=["IN_CREATE"],
            monotonic_ns=111,
            upid=UPID_A,
        ),
    )

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.PASS


def test_post_close_watch_cannot_satisfy_candidate_discovery(
    tmp_path: Path,
) -> None:
    manifest, records = _default_capture()
    watch = _watch_event(
        1,
        masks=["IN_CREATE"],
        monotonic_ns=301,
        upid=UPID_A,
    )
    _use_watch_as_only_discovery(records, watch)
    for scan in records["scan_rounds"]:
        scan["watch_drained_through_sequence"] = 0

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.INCOMPLETE
    assert result.reasons == ("exact_upid_without_valid_discovery_provenance",)


def test_scan_ending_before_returning_request_start_is_incomplete(
    tmp_path: Path,
) -> None:
    manifest, records = _default_capture()
    _use_scan_as_only_discovery(records, 1)
    records["scan_rounds"][0]["scan_start_monotonic_ns"] = 101
    records["scan_rounds"][0]["scan_end_monotonic_ns"] = 109

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.INCOMPLETE
    assert result.reasons == (
        "scan_generated_upid:1_not_strictly_after_request_start",
    )


def test_scan_ending_at_returning_request_start_is_incomplete(
    tmp_path: Path,
) -> None:
    manifest, records = _default_capture()
    _use_scan_as_only_discovery(records, 1)
    records["scan_rounds"][0]["scan_start_monotonic_ns"] = 109
    records["scan_rounds"][0]["scan_end_monotonic_ns"] = 110

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.INCOMPLETE
    assert result.reasons == (
        "scan_generated_upid:1_not_strictly_after_request_start",
    )


def test_scan_spanning_returning_request_start_is_temporally_eligible(
    tmp_path: Path,
) -> None:
    manifest, records = _default_capture()
    _use_scan_as_only_discovery(records, 1)
    first, second = records["scan_rounds"]
    first["scan_start_monotonic_ns"] = 109
    first["scan_end_monotonic_ns"] = 111
    second["scan_start_monotonic_ns"] = 112
    second["scan_end_monotonic_ns"] = 113

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.PASS


@pytest.mark.parametrize("source", ["active", "index", "index.1"])
def test_surface_ending_before_returning_request_start_is_incomplete(
    tmp_path: Path, source: str
) -> None:
    manifest, records = _default_capture()
    surface = _use_surface_as_only_discovery(records, source)
    surface["capture_start_monotonic_ns"] = 99
    surface["capture_end_monotonic_ns"] = 100
    raw_evidence = (
        _active_raw(UPID_A) if source == "active" else _archive_raw(UPID_A)
    )
    _set_surface_raw(surface, raw_evidence, [UPID_A])

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.INCOMPLETE
    assert result.reasons == (
        f"surface_generated_upid:{source}:"
        f"{surface['observation_sequence']}_not_strictly_after_request_start",
    )


@pytest.mark.parametrize("source", ["active", "index", "index.1"])
def test_surface_ending_at_returning_request_start_is_incomplete(
    tmp_path: Path, source: str
) -> None:
    manifest, records = _default_capture()
    surface = _use_surface_as_only_discovery(records, source)
    surface["capture_start_monotonic_ns"] = 109
    surface["capture_end_monotonic_ns"] = 110
    raw_evidence = (
        _active_raw(UPID_A) if source == "active" else _archive_raw(UPID_A)
    )
    _set_surface_raw(surface, raw_evidence, [UPID_A])

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.INCOMPLETE
    assert result.reasons == (
        f"surface_generated_upid:{source}:"
        f"{surface['observation_sequence']}_not_strictly_after_request_start",
    )


@pytest.mark.parametrize("source", ["active", "index", "index.1"])
def test_surface_spanning_returning_request_start_is_temporally_eligible(
    tmp_path: Path, source: str
) -> None:
    manifest, records = _default_capture()
    surface = _use_surface_as_only_discovery(records, source)
    surface["capture_start_monotonic_ns"] = 109
    surface["capture_end_monotonic_ns"] = 111
    raw_evidence = (
        _active_raw(UPID_A) if source == "active" else _archive_raw(UPID_A)
    )
    _set_surface_raw(surface, raw_evidence, [UPID_A])

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.PASS


def test_explicit_shared_synthetic_clock_domain_may_continue(tmp_path: Path) -> None:
    manifest, records = _default_capture()

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.PASS


def test_generator_clock_domain_mismatch_is_ineligible(tmp_path: Path) -> None:
    manifest, records = _default_capture()
    manifest["clock_contract"]["participant_clock_domain_ids"]["generator"] = (
        "unrelated-monotonic-domain"
    )

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.INELIGIBLE
    assert "clock_domain_mismatch:generator" in result.reasons[0]


def test_missing_clock_contract_on_disposable_fixture_is_ineligible(
    tmp_path: Path,
) -> None:
    manifest, records = _default_capture()
    manifest["fixture_kind"] = "disposable_pve"
    manifest["fixture_id"] = "disposable-fixture-13"
    manifest["generator_contract"].update(
        {
            "approval_state": "approved",
            "fixture_id": "disposable-fixture-13",
        }
    )
    del manifest["clock_contract"]

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.INELIGIBLE
    assert result.reasons[0].startswith("clock_contract_ineligible:")


def test_clock_contract_boot_id_mismatch_is_ineligible(tmp_path: Path) -> None:
    manifest, records = _default_capture()
    manifest["clock_contract"]["boot_id"] = "different-boot-id"

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.INELIGIBLE
    assert "clock_contract_boot_id_mismatch" in result.reasons[0]


def test_request_window_omission_is_not_precise_b_s1_body_start_no_go(
    tmp_path: Path,
) -> None:
    manifest, records = _default_capture()
    _add_ground_truth_operation(manifest, records, UPID_B)
    result = analyze_capture(_materialize(tmp_path, manifest, records))
    assert result.outcome is AnalyzerOutcome.ENUMERATION_WITNESS
    assert result.witness is not None
    assert result.witness["ground_truth_upid"] == UPID_B
    assert result.witness["b_s1_body_start_membership"] == "UNKNOWN"
    assert result.witness["classification"] == (
        "ENUMERATION OMISSION FOR TESTED GENERATOR WINDOW"
    )


def test_request_timing_cannot_self_assert_b_s1_body_start_membership(
    tmp_path: Path,
) -> None:
    manifest, records = _default_capture()
    records["ground_truth"][1]["b_s1_body_start_membership"] = "in_interval"

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.INCOMPLETE
    assert "b_s1_body_start_membership_not_established" in result.reasons


@pytest.mark.parametrize(
    ("event_type", "mask", "overflow"),
    [
        ("queue_overflow", ["IN_Q_OVERFLOW"], True),
        ("watch_invalidation", ["IN_IGNORED"], False),
    ],
)
def test_watch_loss_signals_gap(
    tmp_path: Path, event_type: str, mask: list[str], overflow: bool
) -> None:
    manifest, records = _default_capture()
    evidence_ids = _set_subrun(
        manifest, "13E", ["watch_overflow_or_invalidation"]
    )
    records["watch_events"].append(
        {
            "watcher_sequence": 2,
            "event_type": event_type,
            "mask": mask,
            "watched_path": "tasks",
            "filename": "",
            "queue_overflow": overflow,
            "watch_descriptor": -1 if overflow else 1,
            "cookie": 0,
            "raw_mask": 16384 if overflow else 32768,
            "raw_order": 2,
            "monotonic_ns": 160,
            "wall_timestamp": "2026-08-25T00:00:03Z",
            "phenomenon_id": evidence_ids["watch_overflow_or_invalidation"],
        }
    )
    records["scan_rounds"][0]["watch_drained_through_sequence"] = 1
    records["scan_rounds"][1]["watch_drained_through_sequence"] = 2
    result = analyze_capture(_materialize(tmp_path, manifest, records))
    assert result.outcome is AnalyzerOutcome.GAP


def test_subrun_13e_signal_does_not_require_unrelated_generated_work(
    tmp_path: Path,
) -> None:
    manifest, records = _default_capture()
    evidence_ids = _set_subrun(
        manifest, "13E", ["watch_overflow_or_invalidation"]
    )
    records["ground_truth"] = [
        {
            "event": "generator_finalized",
            "last_sequence": 0,
            "total_operations": 0,
            "durable_flush_complete": True,
            "generator_process_identity": "synthetic-generator:1",
            "monotonic_ns": 150,
            "wall_timestamp": "2026-08-25T00:00:02Z",
        }
    ]
    records["watch_events"] = [
        _watch_event(
            1,
            masks=["IN_Q_OVERFLOW"],
            monotonic_ns=160,
            event_type="queue_overflow",
            phenomenon_id=evidence_ids["watch_overflow_or_invalidation"],
        )
    ]
    for scan in records["scan_rounds"]:
        scan["exact_normalized_upids"] = []
        scan["watch_drained_through_sequence"] = 1
    records["exact_upid"] = []
    manifest["candidate_close"]["known_upids"] = []

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.GAP
    assert "watch_queue_overflow" in result.reasons


def test_13e_post_close_signal_with_zero_operations_cannot_pass(
    tmp_path: Path,
) -> None:
    manifest, records = _default_capture()
    evidence_ids = _set_subrun(
        manifest, "13E", ["watch_overflow_or_invalidation"]
    )
    records["ground_truth"] = [
        {
            "event": "generator_finalized",
            "last_sequence": 0,
            "total_operations": 0,
            "durable_flush_complete": True,
            "generator_process_identity": "synthetic-generator:1",
            "monotonic_ns": 150,
            "wall_timestamp": "2026-08-25T00:00:02Z",
        }
    ]
    records["watch_events"] = [
        _watch_event(
            1,
            masks=["IN_Q_OVERFLOW"],
            monotonic_ns=305,
            event_type="queue_overflow",
            phenomenon_id=evidence_ids["watch_overflow_or_invalidation"],
        )
    ]
    for scan in records["scan_rounds"]:
        scan["exact_normalized_upids"] = []
        scan["watch_drained_through_sequence"] = 0
    records["exact_upid"] = []
    manifest["candidate_close"]["known_upids"] = []

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.INCOMPLETE
    assert "subrun_watch_loss_signal_not_evidenced_before_close" in result.reasons


def test_13e_post_close_signal_with_normal_generated_work_cannot_pass(
    tmp_path: Path,
) -> None:
    manifest, records = _default_capture()
    evidence_ids = _set_subrun(
        manifest, "13E", ["watch_overflow_or_invalidation"]
    )
    records["watch_events"].append(
        _watch_event(
            2,
            masks=["IN_Q_OVERFLOW"],
            monotonic_ns=305,
            event_type="queue_overflow",
            phenomenon_id=evidence_ids["watch_overflow_or_invalidation"],
        )
    )

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.INCOMPLETE
    assert "subrun_watch_loss_signal_not_evidenced_before_close" in result.reasons


def test_13e_matching_signal_at_close_or_earlier_is_gap(tmp_path: Path) -> None:
    manifest, records = _default_capture()
    evidence_ids = _set_subrun(
        manifest, "13E", ["watch_overflow_or_invalidation"]
    )
    records["watch_events"].append(
        _watch_event(
            2,
            masks=["IN_IGNORED"],
            monotonic_ns=300,
            event_type="watch_invalidation",
            phenomenon_id=evidence_ids["watch_overflow_or_invalidation"],
        )
    )
    records["scan_rounds"][0]["watch_drained_through_sequence"] = 1
    records["scan_rounds"][1]["watch_drained_through_sequence"] = 2
    records["scan_rounds"][-1]["scan_end_monotonic_ns"] = 300

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.GAP
    assert "watch_invalidation_or_loss" in result.reasons


def test_13e_post_close_match_plus_benign_in_window_watch_cannot_pass(
    tmp_path: Path,
) -> None:
    manifest, records = _default_capture()
    evidence_ids = _set_subrun(
        manifest, "13E", ["watch_overflow_or_invalidation"]
    )
    records["ground_truth"] = [
        {
            "event": "generator_finalized",
            "last_sequence": 0,
            "total_operations": 0,
            "durable_flush_complete": True,
            "generator_process_identity": "synthetic-generator:1",
            "monotonic_ns": 150,
            "wall_timestamp": "2026-08-25T00:00:02Z",
        }
    ]
    records["watch_events"] = [
        _watch_event(1, masks=["IN_ATTRIB"], monotonic_ns=160, filename="index"),
        _watch_event(
            2,
            masks=["IN_Q_OVERFLOW"],
            monotonic_ns=305,
            event_type="queue_overflow",
            phenomenon_id=evidence_ids["watch_overflow_or_invalidation"],
        ),
    ]
    for scan in records["scan_rounds"]:
        scan["exact_normalized_upids"] = []
        scan["watch_drained_through_sequence"] = 1
    records["exact_upid"] = []
    manifest["candidate_close"]["known_upids"] = []

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.INCOMPLETE
    assert "subrun_watch_loss_signal_not_evidenced_before_close" in result.reasons


def test_duplicate_api_pages_tolerated_only_with_primary_reconciliation(tmp_path: Path) -> None:
    manifest, records = _default_capture()
    evidence_ids = _set_subrun(manifest, "13B", ["pagination_movement"])
    archive_page = records["api_pages"][1]
    archive_page["phenomenon_id"] = evidence_ids["pagination_movement"]
    archive_page["page_sequence"] = 1
    archive_page["request_start_monotonic_ns"] = 115
    archive_page["response_end_monotonic_ns"] = 125
    duplicate = dict(records["api_pages"][1])
    duplicate["request_identity"] = "api-archive-duplicate"
    duplicate["start_offset"] = 10
    duplicate["page_sequence"] = 2
    records["api_pages"].append(duplicate)
    result = analyze_capture(_materialize(tmp_path, manifest, records))
    assert result.outcome is AnalyzerOutcome.PASS


@pytest.mark.parametrize(
    ("intervals", "expected_outcome"),
    [
        ([(100, 110), (101, 110)], AnalyzerOutcome.INCOMPLETE),
        ([(100, 111), (101, 111)], AnalyzerOutcome.PASS),
        ([(130, 140), (130, 141)], AnalyzerOutcome.INCOMPLETE),
        ([(129, 140), (129, 141)], AnalyzerOutcome.PASS),
    ],
    ids=[
        "page-end-equals-request-start",
        "page-end-strictly-after-request-start",
        "page-start-equals-request-end",
        "page-start-strictly-before-request-end",
    ],
)
def test_13b_requires_strict_generator_request_overlap(
    tmp_path: Path,
    intervals: list[tuple[int, int]],
    expected_outcome: AnalyzerOutcome,
) -> None:
    manifest, records = _default_capture()
    evidence_id = _set_subrun(
        manifest, "13B", ["pagination_movement"]
    )["pagination_movement"]
    _set_pagination_pages(records, evidence_id, intervals)

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is expected_outcome
    if expected_outcome is AnalyzerOutcome.INCOMPLETE:
        assert result.reasons == (
            "subrun_pagination_movement_not_evidenced",
        )


def test_13b_multiple_equality_only_pages_do_not_establish_overlap(
    tmp_path: Path,
) -> None:
    manifest, records = _default_capture()
    evidence_id = _set_subrun(
        manifest, "13B", ["pagination_movement"]
    )["pagination_movement"]
    _set_pagination_pages(records, evidence_id, [(100, 110), (130, 140)])

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.INCOMPLETE
    assert result.reasons == ("subrun_pagination_movement_not_evidenced",)


def test_13b_one_genuine_overlap_preserves_positive_control(
    tmp_path: Path,
) -> None:
    manifest, records = _default_capture()
    evidence_id = _set_subrun(
        manifest, "13B", ["pagination_movement"]
    )["pagination_movement"]
    _set_pagination_pages(records, evidence_id, [(100, 110), (129, 140)])

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.PASS


def test_offset_repetition_cannot_heal_unknown_omission(tmp_path: Path) -> None:
    manifest, records = _default_capture()
    _add_ground_truth_operation(manifest, records, UPID_B)
    for offset in (0, 10, 0, 10):
        records["api_pages"].append(
            {
                "source": "archive",
                "start_offset": offset,
                "limit": 10,
                "normalized_upids": [UPID_A],
                "request_identity": f"repeat-{offset}-{len(records['api_pages'])}",
                "request_start_monotonic_ns": 227,
                "response_end_monotonic_ns": 228,
                "restart_reason": "prefix_changed" if offset == 0 else None,
                "complete_response": True,
            }
        )
    result = analyze_capture(_materialize(tmp_path, manifest, records))
    assert result.outcome is AnalyzerOutcome.ENUMERATION_WITNESS


def test_exact_confirmation_with_scan_discovery_survives_surface_handoff(
    tmp_path: Path,
) -> None:
    manifest, records = _default_capture()
    records["watch_events"] = []
    for scan in records["scan_rounds"]:
        scan["watch_drained_through_sequence"] = 0
    records["exact_upid"][0]["discovery_source"] = "scan"
    records["exact_upid"][0]["discovery_reference"] = 2
    result = analyze_capture(_materialize(tmp_path, manifest, records))
    assert result.outcome is AnalyzerOutcome.PASS


def test_cleanup_deleting_known_exact_log_is_gap(tmp_path: Path) -> None:
    manifest, records = _default_capture()
    records["watch_events"].append(
        {
            "watcher_sequence": 2,
            "event_type": "exact_log_event",
            "mask": ["IN_DELETE"],
            "watched_path": "tasks/0",
            "filename": UPID_A,
            "normalized_upid": UPID_A,
            "queue_overflow": False,
            "watch_descriptor": 1,
            "cookie": 0,
            "raw_mask": 512,
            "raw_order": 2,
            "monotonic_ns": 240,
            "wall_timestamp": "2026-08-25T00:00:04Z",
        }
    )
    result = analyze_capture(_materialize(tmp_path, manifest, records))
    assert result.outcome is AnalyzerOutcome.GAP


def test_unknown_pre_enumeration_cleanup_preserves_generator_window_witness(
    tmp_path: Path,
) -> None:
    manifest, records = _default_capture()
    _add_ground_truth_operation(manifest, records, UPID_B)
    records["harness_events"].insert(
        -2,
        {
            "event": "synthetic_unknown_pre_enumeration_log_deleted",
            "upid": UPID_B,
            "monotonic_ns": 170,
        },
    )
    result = analyze_capture(_materialize(tmp_path, manifest, records))
    assert result.outcome is AnalyzerOutcome.ENUMERATION_WITNESS


def test_surviving_anchors_do_not_hide_unknown_intermediate_loss(tmp_path: Path) -> None:
    manifest, records = _default_capture()
    _add_ground_truth_operation(manifest, records, UPID_B)
    records["ground_truth"].pop()
    third = _ground_truth_operation(3, UPID_C)
    third[0]["monotonic_ns"] = 160
    third[1]["monotonic_ns"] = 170
    records["ground_truth"].extend(third)
    records["ground_truth"].append(
        {
            "event": "generator_finalized",
            "last_sequence": 3,
            "total_operations": 3,
            "durable_flush_complete": True,
            "generator_process_identity": "synthetic-generator:1",
            "monotonic_ns": 180,
            "wall_timestamp": "2026-08-25T00:00:04Z",
        }
    )
    for scan in records["scan_rounds"]:
        scan["exact_normalized_upids"] = [UPID_A, UPID_C]
    records["exact_upid"].append(
        {
            **records["exact_upid"][0],
            "observation_sequence": 2,
            "known_upid": UPID_C,
            "discovery_source": "scan",
            "discovery_reference": 2,
        }
    )
    manifest["candidate_close"]["known_upids"] = [UPID_A, UPID_C]
    result = analyze_capture(_materialize(tmp_path, manifest, records))
    assert result.outcome is AnalyzerOutcome.ENUMERATION_WITNESS
    assert result.witness is not None
    assert result.witness["ground_truth_upid"] == UPID_B


def test_crash_or_missing_heartbeat_before_close_is_incomplete(tmp_path: Path) -> None:
    manifest, records = _default_capture()
    records["harness_events"] = [
        event for event in records["harness_events"] if event["event"] != "heartbeat"
    ]
    records["harness_events"].insert(
        -1, {"event": "process_crash", "monotonic_ns": 240}
    )
    result = analyze_capture(_materialize(tmp_path, manifest, records))
    assert result.outcome is AnalyzerOutcome.INCOMPLETE


def test_corrupt_capture_file_fails_closed(tmp_path: Path) -> None:
    manifest, records = _default_capture()
    capture_dir = _materialize(tmp_path, manifest, records)
    (capture_dir / CAPTURE_FILES["scan_rounds"]).write_text("{broken\n", encoding="utf-8")
    result = analyze_capture(capture_dir)
    assert result.outcome is AnalyzerOutcome.INCOMPLETE


@pytest.mark.parametrize("defect", ["sequence", "finalization"])
def test_ground_truth_gap_or_missing_finalization_is_harness_unknown(
    tmp_path: Path, defect: str
) -> None:
    manifest, records = _default_capture()
    if defect == "sequence":
        records["ground_truth"][0]["generator_sequence"] = 2
    else:
        records["ground_truth"].pop()
    result = analyze_capture(_materialize(tmp_path, manifest, records))
    assert result.outcome is AnalyzerOutcome.INCOMPLETE


def test_source_version_context_mismatch_is_ineligible(tmp_path: Path) -> None:
    manifest, records = _default_capture()
    manifest["version_ledger"][0]["source_commit"] = "0" * 40
    result = analyze_capture(_materialize(tmp_path, manifest, records))
    assert result.outcome is AnalyzerOutcome.INELIGIBLE


def test_late_task_around_t1_requires_gap_not_positive_close(tmp_path: Path) -> None:
    manifest, records = _default_capture()
    records["ground_truth"][1]["generator_window_relation"] = "ambiguous"
    records["harness_events"].insert(
        -2,
        {"event": "gap_signal", "reason": "t1_log_body_order_ambiguous", "monotonic_ns": 299},
    )
    manifest["candidate_close"]["state"] = "GAP_LATCHED"
    result = analyze_capture(_materialize(tmp_path, manifest, records))
    assert result.outcome is AnalyzerOutcome.GAP


def test_exact_observation_without_enumeration_provenance_cannot_pass(
    tmp_path: Path,
) -> None:
    manifest, records = _default_capture()
    records["watch_events"] = []
    for scan in records["scan_rounds"]:
        scan["exact_normalized_upids"] = []
        scan["watch_drained_through_sequence"] = 0
    manifest["candidate_close"]["known_upids"] = []

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.INCOMPLETE
    assert "exact_upid_without_valid_discovery_provenance" in result.reasons


def test_capture_supplied_fixed_point_labels_cannot_hide_different_scan_sets(
    tmp_path: Path,
) -> None:
    manifest, records = _default_capture()
    records["scan_rounds"][0]["exact_normalized_upids"] = []
    records["scan_rounds"][1]["exact_normalized_upids"] = [UPID_A]

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.GAP
    assert "terminal_scan_fixed_point_not_reached" in result.reasons


def test_equal_terminal_scans_with_undrained_watch_event_cannot_pass(
    tmp_path: Path,
) -> None:
    manifest, records = _default_capture()
    records["watch_events"].append(
        _watch_event(2, masks=["IN_ATTRIB"], monotonic_ns=215, filename="index")
    )

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.GAP
    assert "watch_events_undrained_at_terminal_scan" in result.reasons


def test_computed_equal_terminal_scans_with_drained_queue_may_pass(
    tmp_path: Path,
) -> None:
    manifest, records = _default_capture()

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.PASS


def test_ordered_scan_history_detects_exact_log_disappearance(
    tmp_path: Path,
) -> None:
    manifest, records = _default_capture()
    _set_scan_history(records, [[UPID_A], [], []], [1, 1, 1])

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.GAP
    assert "exact_log_disappeared_between_scans" in result.reasons


def test_shuffled_scan_jsonl_cannot_hide_exact_log_disappearance(
    tmp_path: Path,
) -> None:
    manifest, records = _default_capture()
    _set_scan_history(records, [[UPID_A], [], []], [1, 1, 1])
    scans = records["scan_rounds"]
    records["scan_rounds"] = [scans[1], scans[2], scans[0]]

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.INCOMPLETE
    assert "scan_sequence_not_jsonl_ordered" in result.reasons


def test_shuffled_scan_jsonl_cannot_bypass_watermark_ordering(
    tmp_path: Path,
) -> None:
    manifest, records = _default_capture()
    records["watch_events"].extend(
        [
            _watch_event(2, masks=["IN_ATTRIB"], monotonic_ns=195, filename="index"),
            _watch_event(3, masks=["IN_ATTRIB"], monotonic_ns=215, filename="index"),
        ]
    )
    _set_scan_history(
        records,
        [[UPID_A], [UPID_A], [UPID_A]],
        [1, 2, 3],
    )
    scans = records["scan_rounds"]
    records["scan_rounds"] = [scans[1], scans[2], scans[0]]

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.INCOMPLETE
    assert "scan_sequence_not_jsonl_ordered" in result.reasons


def test_valid_ordered_three_scan_history_may_pass(tmp_path: Path) -> None:
    manifest, records = _default_capture()
    _set_scan_history(
        records,
        [[UPID_A], [UPID_A], [UPID_A]],
        [1, 1, 1],
    )

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.PASS


def test_missing_watch_referenced_by_scan_watermark_is_incomplete(
    tmp_path: Path,
) -> None:
    manifest, records = _default_capture()
    records["watch_events"] = []
    for scan in records["scan_rounds"]:
        scan["watch_drained_through_sequence"] = 7

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.INCOMPLETE
    assert "scan_watch_drain_watermark_reference_missing" in result.reasons


def test_raw_watch_order_must_match_capture_order(tmp_path: Path) -> None:
    manifest, records = _default_capture()
    records["watch_events"][0]["raw_order"] = 7

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.INCOMPLETE
    assert "watch_raw_order_not_contiguous_or_capture_ordered" in result.reasons

def test_unexpected_post_t0_task_cannot_be_silently_omitted_at_close(
    tmp_path: Path,
) -> None:
    manifest, records = _default_capture()
    records["watch_events"].append(
        _watch_event(2, masks=["IN_CREATE"], monotonic_ns=150, upid=UPID_X)
    )
    for scan in records["scan_rounds"]:
        scan["exact_normalized_upids"] = [UPID_A, UPID_X]
        scan["watch_drained_through_sequence"] = 2

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.GAP
    assert "unexpected_post_t0_upid" in result.reasons
    assert "candidate_close_omits_observed_post_t0_upid" in result.reasons
    assert "unexpected_post_t0_task_type" in result.reasons


def test_baseline_cannot_be_committed_after_t0(tmp_path: Path) -> None:
    manifest, records = _default_capture()
    manifest["baseline_observation"]["committed_at_monotonic_ns"] = 101

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.INCOMPLETE
    assert "baseline_not_captured_and_committed_before_t0" in result.reasons


def test_baseline_worker_active_across_t0_cannot_pass(tmp_path: Path) -> None:
    manifest, records = _default_capture()
    _add_finalized_historical_baseline(manifest, records, UPID_B)
    active = records["surface_observations"][0]
    _set_surface_raw(active, _active_raw(UPID_B), [UPID_B])

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is not AnalyzerOutcome.PASS
    assert "t0_quiescence_active_worker_present" in result.reasons


def test_finalized_historical_baseline_allows_quiescent_t0(
    tmp_path: Path,
) -> None:
    manifest, records = _default_capture()
    _add_finalized_historical_baseline(manifest, records, UPID_B)

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.PASS


def _raw_result(available: bool, raw_evidence: str, parsed_field: str, parsed: str) -> dict[str, Any]:
    return {
        "available": available,
        "raw_evidence": raw_evidence,
        "sha256": hashlib.sha256(raw_evidence.encode("utf-8")).hexdigest(),
        parsed_field: parsed,
    }


def _set_referenced_baseline_exact(
    records: dict[str, list[dict[str, Any]]],
    *,
    task_state: str = "stopped",
    terminal_status: str = "TASK OK",
    interpretation: str = "ok",
    available: bool = True,
    **overrides: Any,
) -> dict[str, Any]:
    """Rewrite the exact observation that `t0_quiescence` actually references."""

    exact = records["exact_upid"][0]
    assert exact["observation_sequence"] == 1
    exact["status_result"] = _raw_result(
        available, task_state if available else "", "task_state", task_state
    )
    exact["log_result"] = _raw_result(
        available, terminal_status if available else "", "terminal_status", terminal_status
    )
    exact["final_status_interpretation"] = interpretation
    exact.update(overrides)
    return exact


def _append_baseline_exact(
    records: dict[str, list[dict[str, Any]]],
    upid: str,
    *,
    task_state: str = "stopped",
    terminal_status: str = "TASK OK",
    interpretation: str = "ok",
    capture_start: int,
    capture_end: int,
) -> dict[str, Any]:
    """Append one more exact observation of the same baseline UPID."""

    record = {
        "observation_sequence": len(records["exact_upid"]) + 1,
        "known_upid": upid,
        "status_result": _raw_result(True, task_state, "task_state", task_state),
        "log_result": _raw_result(True, terminal_status, "terminal_status", terminal_status),
        "presence": True,
        "readable": True,
        "previously_known": True,
        "discovery_source": "baseline",
        "discovery_reference": "manifest.baseline_upids",
        "final_status_interpretation": interpretation,
        "capture_start_monotonic_ns": capture_start,
        "capture_end_monotonic_ns": capture_end,
    }
    records["exact_upid"].append(record)
    return record


def test_referenced_non_final_baseline_exact_without_borrow_is_incomplete(
    tmp_path: Path,
) -> None:
    """A: the referenced pre-T0 observation alone must carry the finality."""

    manifest, records = _default_capture()
    _add_finalized_historical_baseline(manifest, records, UPID_B)
    _set_referenced_baseline_exact(
        records,
        task_state="running",
        terminal_status="starting",
        interpretation="not_final",
    )

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.INCOMPLETE
    assert "t0_quiescence_final_exact_reference_invalid" in result.reasons


@pytest.mark.parametrize("interpretation", ["not_final", "unknown"])
def test_post_t0_exact_cannot_finalize_referenced_pre_t0_observation(
    tmp_path: Path, interpretation: str
) -> None:
    """B/C: post-T0 evidence must not discharge a pre-T0 quiescence obligation.

    The referenced pre-T0 bytes are identical to the control above; only a
    later observation of the same UPID is added.  Its existence must not change
    the T0 verdict.
    """

    manifest, records = _default_capture()
    _add_finalized_historical_baseline(manifest, records, UPID_B)
    referenced = _set_referenced_baseline_exact(
        records,
        task_state="running",
        terminal_status="starting",
        interpretation=interpretation,
    )
    assert referenced["capture_end_monotonic_ns"] < manifest["t0_monotonic_ns"]
    _append_baseline_exact(
        records, UPID_B, capture_start=240, capture_end=241
    )

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.INCOMPLETE
    assert "t0_quiescence_final_exact_reference_invalid" in result.reasons


def test_referenced_pre_t0_observation_proving_finality_may_pass(
    tmp_path: Path,
) -> None:
    """D: positive control -- the reference itself proves finality before T0."""

    manifest, records = _default_capture()
    _add_finalized_historical_baseline(manifest, records, UPID_B)
    referenced = _set_referenced_baseline_exact(
        records, task_state="stopped", terminal_status="TASK OK", interpretation="ok"
    )
    assert referenced["capture_end_monotonic_ns"] <= manifest["t0_monotonic_ns"]

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.PASS


def test_second_later_final_observation_does_not_disturb_valid_reference(
    tmp_path: Path,
) -> None:
    """E: an extra final observation of the same UPID is simply redundant."""

    manifest, records = _default_capture()
    _add_finalized_historical_baseline(manifest, records, UPID_B)
    _append_baseline_exact(records, UPID_B, capture_start=240, capture_end=241)

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.PASS


@pytest.mark.parametrize(
    "overrides,interpretation",
    [
        ({"presence": False}, "absent"),
        ({"readable": False}, "unreadable"),
    ],
)
def test_referenced_absent_or_unreadable_exact_cannot_be_rehabilitated(
    tmp_path: Path, overrides: dict[str, Any], interpretation: str
) -> None:
    """F: a lost referenced observation stays lost despite a later good read."""

    manifest, records = _default_capture()
    _add_finalized_historical_baseline(manifest, records, UPID_B)
    _set_referenced_baseline_exact(
        records, available=False, interpretation=interpretation, **overrides
    )
    _append_baseline_exact(records, UPID_B, capture_start=240, capture_end=241)

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.INCOMPLETE
    assert "t0_quiescence_final_exact_reference_invalid" in result.reasons


def test_reference_to_late_exact_is_invalid_even_with_pre_t0_final_sibling(
    tmp_path: Path,
) -> None:
    """G: authority follows the reference, not the convenient observation."""

    manifest, records = _default_capture()
    _add_finalized_historical_baseline(manifest, records, UPID_B)
    referenced = _set_referenced_baseline_exact(
        records, capture_start_monotonic_ns=240, capture_end_monotonic_ns=241
    )
    assert referenced["capture_end_monotonic_ns"] > manifest["t0_monotonic_ns"]
    _append_baseline_exact(records, UPID_B, capture_start=83, capture_end=84)

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.INCOMPLETE
    assert "t0_quiescence_final_exact_not_before_t0" in result.reasons


def _generated_exact(
    sequence: int,
    *,
    capture_start: int,
    capture_end: int,
    task_state: str = "stopped",
    terminal_status: str = "TASK OK",
    interpretation: str = "ok",
    available: bool = True,
    **overrides: Any,
) -> dict[str, Any]:
    """One exact observation of the generated UPID, discovered by watch 1."""

    record = {
        "observation_sequence": sequence,
        "known_upid": UPID_A,
        "status_result": _raw_result(
            available, task_state if available else "", "task_state", task_state
        ),
        "log_result": _raw_result(
            available,
            terminal_status if available else "",
            "terminal_status",
            terminal_status,
        ),
        "presence": True,
        "readable": True,
        "previously_known": True,
        "discovery_source": "watch",
        "discovery_reference": 1,
        "final_status_interpretation": interpretation,
        "capture_start_monotonic_ns": capture_start,
        "capture_end_monotonic_ns": capture_end,
    }
    record.update(overrides)
    return record


def _running_exact(sequence: int, *, capture_start: int, capture_end: int,
                   interpretation: str = "not_final") -> dict[str, Any]:
    return _generated_exact(
        sequence,
        capture_start=capture_start,
        capture_end=capture_end,
        task_state="running",
        terminal_status="starting",
        interpretation=interpretation,
    )


def test_non_final_then_final_exact_progression_may_pass(tmp_path: Path) -> None:
    """1: `non-final -> final` is the ordinary lifecycle and must still pass."""

    manifest, records = _default_capture()
    records["exact_upid"] = [
        _running_exact(1, capture_start=205, capture_end=210),
        _running_exact(2, capture_start=215, capture_end=220),
        _generated_exact(3, capture_start=225, capture_end=230),
    ]

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.PASS


def test_repeated_final_exact_observations_may_pass(tmp_path: Path) -> None:
    """2: redundant confirmation of an already final task is not a conflict."""

    manifest, records = _default_capture()
    records["exact_upid"] = [
        _generated_exact(1, capture_start=225, capture_end=230),
        _generated_exact(2, capture_start=245, capture_end=250),
        _generated_exact(3, capture_start=265, capture_end=270),
    ]

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.PASS


@pytest.mark.parametrize("interpretation", ["not_final", "unknown"])
def test_final_then_non_final_exact_observation_cannot_pass(
    tmp_path: Path, interpretation: str
) -> None:
    """3/4: a task that reached a final status cannot be seen running again.

    `exact_final` is UPID-level, so without this the later contradicting read
    left the aggregate -- and therefore the positive close -- untouched.
    """

    manifest, records = _default_capture()
    records["exact_upid"] = [
        _generated_exact(1, capture_start=225, capture_end=230),
        _running_exact(
            2, capture_start=245, capture_end=250, interpretation=interpretation
        ),
    ]

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.INCOMPLETE
    assert result.reasons == ("exact_upid_final_status_regressed",)


def test_final_status_regression_is_decided_by_capture_window_not_ordinal(
    tmp_path: Path,
) -> None:
    """3: `observation_sequence` is an identifier, not a chronology.

    The same two capture windows must reach the same verdict when the declared
    ordinals and the physical JSONL order are both reversed.
    """

    manifest, records = _default_capture()
    records["exact_upid"] = [
        _running_exact(1, capture_start=245, capture_end=250),
        _generated_exact(2, capture_start=225, capture_end=230),
    ]

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.INCOMPLETE
    assert result.reasons == ("exact_upid_final_status_regressed",)


@pytest.mark.parametrize(
    "overrides,interpretation,reason",
    [
        ({"presence": False}, "absent", "known_exact_upid_lost"),
        ({"readable": False}, "unreadable", "known_exact_upid_lost"),
        ({"available": False}, "unknown", "exact_upid_raw_result_unavailable"),
    ],
)
def test_final_then_lost_exact_observation_keeps_existing_gap_semantics(
    tmp_path: Path, overrides: dict[str, Any], interpretation: str, reason: str
) -> None:
    """5/6: evidence loss after completion stays a gap, not a contradiction.

    A finished task's log may legitimately be removed by cleanup, so an absent,
    unreadable, or unavailable later read is loss -- already latched above --
    and must not be reclassified by the lifecycle rule.
    """

    manifest, records = _default_capture()
    records["exact_upid"] = [
        _generated_exact(1, capture_start=225, capture_end=230),
        _generated_exact(
            2,
            capture_start=245,
            capture_end=250,
            interpretation=interpretation,
            **overrides,
        ),
    ]

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.GAP
    assert reason in result.reasons


def test_later_final_sibling_cannot_launder_intermediate_regression(
    tmp_path: Path,
) -> None:
    """7: comparison is against the *earliest* final read of that UPID.

    A third, independently valid final observation after the contradiction must
    not restore a positive close.
    """

    manifest, records = _default_capture()
    records["exact_upid"] = [
        _generated_exact(1, capture_start=225, capture_end=230),
        _running_exact(2, capture_start=245, capture_end=250),
        _generated_exact(3, capture_start=265, capture_end=270),
    ]

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.INCOMPLETE
    assert result.reasons == ("exact_upid_final_status_regressed",)


@pytest.mark.parametrize(
    "non_final_window",
    [(229, 250), (215, 225)],
    ids=["overlapping", "touching"],
)
def test_unorderable_exact_windows_fail_closed(
    tmp_path: Path, non_final_window: tuple[int, int]
) -> None:
    """D: overlapping or merely touching windows do not order two reads.

    Neither pair proves a `non-final -> final` progression, so neither may be
    read as one.  A genuine progression needs strict separation.
    """

    manifest, records = _default_capture()
    capture_start, capture_end = non_final_window
    records["exact_upid"] = [
        _running_exact(1, capture_start=capture_start, capture_end=capture_end),
        _generated_exact(2, capture_start=225, capture_end=230),
    ]

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.INCOMPLETE
    assert result.reasons == ("exact_upid_final_status_regressed",)

    strictly_earlier = tmp_path / "strictly-earlier"
    strictly_earlier.mkdir()
    records["exact_upid"][0] = _running_exact(1, capture_start=215, capture_end=224)
    progression = analyze_capture(
        _materialize(strictly_earlier, manifest, records)
    )
    assert progression.outcome is AnalyzerOutcome.PASS


def test_contradictory_exact_observation_cannot_become_omission_witness(
    tmp_path: Path,
) -> None:
    """8: an inconsistent capture must never be reported as a B-S1 omission."""

    manifest, records = _default_capture()
    _add_ground_truth_operation(manifest, records, UPID_B)
    witness = analyze_capture(_materialize(tmp_path, manifest, records))
    assert witness.outcome is AnalyzerOutcome.ENUMERATION_WITNESS

    contradictory = tmp_path / "contradictory"
    contradictory.mkdir()
    records["exact_upid"] = [
        _generated_exact(1, capture_start=225, capture_end=230),
        _running_exact(2, capture_start=245, capture_end=250),
    ]
    result = analyze_capture(_materialize(contradictory, manifest, records))

    assert result.outcome is AnalyzerOutcome.INCOMPLETE
    assert result.reasons == ("exact_upid_final_status_regressed",)


def _final_exact(
    sequence: int,
    *,
    capture_start: int,
    capture_end: int,
    terminal_status: str,
    interpretation: str,
) -> dict[str, Any]:
    """One self-sufficient final observation with an explicit terminal result."""

    return _generated_exact(
        sequence,
        capture_start=capture_start,
        capture_end=capture_end,
        terminal_status=terminal_status,
        interpretation=interpretation,
    )


OK_RESULT = ("TASK OK", "ok")
WARNING_RESULT = ("WARNINGS: 2", "warning")
ERROR_RESULT = ("TASK ERROR: boom", "error")


@pytest.mark.parametrize(
    "terminal_status,interpretation",
    [OK_RESULT, WARNING_RESULT, ERROR_RESULT],
    ids=["ok", "warning", "error"],
)
def test_agreeing_final_exact_observations_may_pass(
    tmp_path: Path, terminal_status: str, interpretation: str
) -> None:
    """1/2: repeated reads that agree on the terminal result are redundant."""

    manifest, records = _default_capture()
    records["exact_upid"] = [
        _final_exact(
            1,
            capture_start=225,
            capture_end=230,
            terminal_status=terminal_status,
            interpretation=interpretation,
        ),
        _final_exact(
            2,
            capture_start=245,
            capture_end=250,
            terminal_status=terminal_status,
            interpretation=interpretation,
        ),
    ]

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.PASS


@pytest.mark.parametrize(
    "first,second",
    [
        (OK_RESULT, ERROR_RESULT),
        (ERROR_RESULT, OK_RESULT),
        (WARNING_RESULT, OK_RESULT),
        (OK_RESULT, WARNING_RESULT),
        (WARNING_RESULT, ERROR_RESULT),
    ],
    ids=["ok-error", "error-ok", "warning-ok", "ok-warning", "warning-error"],
)
def test_conflicting_final_exact_terminal_results_cannot_pass(
    tmp_path: Path, first: tuple[str, str], second: tuple[str, str]
) -> None:
    """3/4/5/6: one immutable task cannot have two terminal outcomes.

    `ok`, `warning`, and `error` all mean final, so the UPID-level `exact_final`
    boolean is blind to which one the task actually reached.
    """

    manifest, records = _default_capture()
    records["exact_upid"] = [
        _final_exact(
            1, capture_start=225, capture_end=230,
            terminal_status=first[0], interpretation=first[1],
        ),
        _final_exact(
            2, capture_start=245, capture_end=250,
            terminal_status=second[0], interpretation=second[1],
        ),
    ]

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.INCOMPLETE
    assert result.reasons == ("exact_upid_terminal_status_conflict",)


def test_same_interpretation_with_different_raw_terminal_line_cannot_pass(
    tmp_path: Path,
) -> None:
    """The comparison is on sealed raw evidence, not the interpretation label.

    Two `error` reads of one immutable log cannot disagree about its last line.
    """

    manifest, records = _default_capture()
    records["exact_upid"] = [
        _final_exact(
            1, capture_start=225, capture_end=230,
            terminal_status="TASK ERROR: boom", interpretation="error",
        ),
        _final_exact(
            2, capture_start=245, capture_end=250,
            terminal_status="TASK ERROR: other", interpretation="error",
        ),
    ]

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.INCOMPLETE
    assert result.reasons == ("exact_upid_terminal_status_conflict",)


def test_third_agreeing_final_read_cannot_launder_terminal_conflict(
    tmp_path: Path,
) -> None:
    """7: agreement elsewhere does not repair a contradicted terminal result."""

    manifest, records = _default_capture()
    records["exact_upid"] = [
        _final_exact(1, capture_start=225, capture_end=230,
                     terminal_status="TASK OK", interpretation="ok"),
        _final_exact(2, capture_start=245, capture_end=250,
                     terminal_status="TASK ERROR: boom", interpretation="error"),
        _final_exact(3, capture_start=265, capture_end=270,
                     terminal_status="TASK OK", interpretation="ok"),
    ]

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.INCOMPLETE
    assert result.reasons == ("exact_upid_terminal_status_conflict",)


@pytest.mark.parametrize(
    "windows",
    [((245, 250), (225, 230)), ((225, 230), (225, 230))],
    ids=["reversed-order", "identical-windows"],
)
def test_terminal_status_conflict_needs_no_chronology(
    tmp_path: Path, windows: tuple[tuple[int, int], tuple[int, int]]
) -> None:
    """8: terminal outcome is immutable, so ordering is irrelevant here.

    The declared ordinals, the physical JSONL order, and the capture windows are
    all varied; a contradicted terminal result must convict the capture anyway.
    """

    manifest, records = _default_capture()
    (first_start, first_end), (second_start, second_end) = windows
    records["exact_upid"] = [
        _final_exact(1, capture_start=first_start, capture_end=first_end,
                     terminal_status="TASK ERROR: boom", interpretation="error"),
        _final_exact(2, capture_start=second_start, capture_end=second_end,
                     terminal_status="TASK OK", interpretation="ok"),
    ]

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.INCOMPLETE
    assert result.reasons == ("exact_upid_terminal_status_conflict",)


def test_terminal_status_conflict_cannot_become_omission_witness(
    tmp_path: Path,
) -> None:
    """9: an incoherent capture must never be reported against B-S1.

    Without this the contradiction was simply absent from the gap set, so a
    separately omitted generated UPID still produced an omission witness --
    contradictory evidence promoted into a research finding.
    """

    manifest, records = _default_capture()
    _add_ground_truth_operation(manifest, records, UPID_B)
    witness = analyze_capture(_materialize(tmp_path, manifest, records))
    assert witness.outcome is AnalyzerOutcome.ENUMERATION_WITNESS

    conflicted = tmp_path / "terminal-conflict"
    conflicted.mkdir()
    records["exact_upid"] = [
        _final_exact(1, capture_start=225, capture_end=230,
                     terminal_status="TASK OK", interpretation="ok"),
        _final_exact(2, capture_start=245, capture_end=250,
                     terminal_status="TASK ERROR: boom", interpretation="error"),
    ]
    result = analyze_capture(_materialize(conflicted, manifest, records))

    assert result.outcome is AnalyzerOutcome.INCOMPLETE
    assert result.reasons == ("exact_upid_terminal_status_conflict",)


def test_terminal_conflict_does_not_reclassify_lost_exact_evidence(
    tmp_path: Path,
) -> None:
    """E: a later lost read is evidence loss, never a terminal conflict."""

    manifest, records = _default_capture()
    records["exact_upid"] = [
        _final_exact(1, capture_start=225, capture_end=230,
                     terminal_status="TASK OK", interpretation="ok"),
        _generated_exact(2, capture_start=245, capture_end=250,
                         terminal_status="TASK ERROR: boom",
                         interpretation="unreadable", readable=False),
    ]

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.GAP
    assert "known_exact_upid_lost" in result.reasons


def test_baseline_upid_final_status_regression_also_fails_closed(
    tmp_path: Path,
) -> None:
    """E: the lifecycle rule is a physical fact, not a scope-dependent one."""

    manifest, records = _default_capture()
    _add_finalized_historical_baseline(manifest, records, UPID_B)
    _append_baseline_exact(
        records,
        UPID_B,
        task_state="running",
        terminal_status="starting",
        interpretation="not_final",
        capture_start=245,
        capture_end=250,
    )

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.INCOMPLETE
    assert result.reasons == ("exact_upid_final_status_regressed",)


def test_post_t0_watch_and_fixed_point_cannot_establish_quiescent_t0(
    tmp_path: Path,
) -> None:
    manifest, records = _default_capture()
    root, bucket, first_scan, second_scan = records["pre_t0_establishment"]
    root["monotonic_ns"] = 110
    bucket["monotonic_ns"] = 111
    first_scan["scan_start_monotonic_ns"] = 120
    first_scan["scan_end_monotonic_ns"] = 125
    second_scan["scan_start_monotonic_ns"] = 130
    second_scan["scan_end_monotonic_ns"] = 135

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.INCOMPLETE
    assert "pre_t0_watch_event_after_t0" in result.reasons


def test_quiescence_commit_before_t0_is_invalid_even_with_later_watch_event(
    tmp_path: Path,
) -> None:
    manifest, records = _default_capture()
    manifest["t0_quiescence"]["committed_at_monotonic_ns"] = 90
    records["pre_t0_establishment"].append(
        {
            "establishment_sequence": 5,
            "event": "watch_event",
            "watcher_sequence": 3,
            "watch_scope": "task_root",
            "mask": ["IN_ATTRIB"],
            "monotonic_ns": 95,
            "complete": True,
        }
    )

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.INCOMPLETE
    assert "t0_quiescence_commit_must_equal_t0" in result.reasons


def test_watch_event_after_terminal_fixed_point_before_t0_cannot_pass(
    tmp_path: Path,
) -> None:
    manifest, records = _default_capture()
    records["pre_t0_establishment"].append(
        {
            "establishment_sequence": 5,
            "event": "watch_event",
            "watcher_sequence": 3,
            "watch_scope": "task_root",
            "mask": ["IN_ATTRIB"],
            "monotonic_ns": 95,
            "complete": True,
        }
    )

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.INCOMPLETE
    assert "pre_t0_watch_event_undrained" in result.reasons


def test_quiescence_commit_at_t0_with_drained_fixed_point_may_pass(
    tmp_path: Path,
) -> None:
    manifest, records = _default_capture()
    _add_finalized_historical_baseline(manifest, records, UPID_B)

    assert manifest["t0_quiescence"]["committed_at_monotonic_ns"] == manifest[
        "t0_monotonic_ns"
    ]
    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.PASS


def _pre_t0_root(establishment: int, watcher: int) -> dict[str, Any]:
    return {
        "establishment_sequence": establishment,
        "event": "watch_installed",
        "watcher_sequence": watcher,
        "watch_scope": "task_root",
        "watched_path": "tasks",
        "monotonic_ns": 50,
        "complete": True,
    }


def _pre_t0_bucket(establishment: int, watcher: int) -> dict[str, Any]:
    return {
        "establishment_sequence": establishment,
        "event": "watch_installed",
        "watcher_sequence": watcher,
        "watch_scope": "bucket",
        "watched_path": "tasks/0",
        "bucket": "0",
        "bucket_origin": "existing_at_root_install",
        "monotonic_ns": 51,
        "complete": True,
    }


def _pre_t0_scan(
    establishment: int, scan: int, start: int, end: int, watermark: int
) -> dict[str, Any]:
    return {
        "establishment_sequence": establishment,
        "event": "baseline_scan",
        "phase": "PRE_T0_BASELINE",
        "baseline_scan_sequence": scan,
        "scan_start_monotonic_ns": start,
        "scan_end_monotonic_ns": end,
        "exact_normalized_upids": [],
        "bucket_set": ["0"],
        "watch_drained_through_sequence": watermark,
        "unreadable_entries": [],
        "malformed_entries": [],
        "complete": True,
    }


def _pre_t0_watch(establishment: int, watcher: int, monotonic_ns: int) -> dict[str, Any]:
    return {
        "establishment_sequence": establishment,
        "event": "watch_event",
        "watcher_sequence": watcher,
        "watched_path": "tasks/0",
        "monotonic_ns": monotonic_ns,
        "complete": True,
    }


def _with_pre_t0_stream(
    manifest: dict[str, Any],
    records: dict[str, list[dict[str, Any]]],
    stream: list[dict[str, Any]],
    *,
    root_reference: int,
    watermark: int,
) -> None:
    records["pre_t0_establishment"] = stream
    manifest["t0_quiescence"]["pre_t0_establishment"] = {
        "root_watch_establishment_sequence": root_reference,
        "baseline_scan_sequences": [1, 2],
        "watch_drained_through_sequence": watermark,
    }


def test_permuted_pre_t0_ordinals_cannot_hide_late_watcher_event(
    tmp_path: Path,
) -> None:
    """The terminal fixed point is capture order, not a self-declared ordinal.

    A watcher event physically recorded after the terminal baseline scan is
    relabelled with a smaller ``establishment_sequence`` and a ``watcher_sequence``
    that hides it beneath the declared drain watermark.
    """

    manifest, records = _default_capture()
    _with_pre_t0_stream(
        manifest,
        records,
        [
            _pre_t0_root(1, 3),
            _pre_t0_bucket(2, 2),
            _pre_t0_scan(4, 1, 60, 65, 3),
            _pre_t0_scan(5, 2, 70, 75, 3),
            _pre_t0_watch(3, 1, 76),
        ],
        root_reference=1,
        watermark=3,
    )

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.INCOMPLETE
    assert result.reasons == ("pre_t0_establishment_sequence_not_jsonl_ordered",)


def test_permuted_pre_t0_establishment_sequence_alone_is_incomplete(
    tmp_path: Path,
) -> None:
    manifest, records = _default_capture()
    _with_pre_t0_stream(
        manifest,
        records,
        [
            _pre_t0_root(2, 1),
            _pre_t0_bucket(1, 2),
            _pre_t0_scan(3, 1, 60, 65, 2),
            _pre_t0_scan(4, 2, 70, 75, 2),
        ],
        root_reference=2,
        watermark=2,
    )

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.INCOMPLETE
    assert result.reasons == ("pre_t0_establishment_sequence_not_jsonl_ordered",)


def test_permuted_pre_t0_watcher_sequence_alone_is_incomplete(
    tmp_path: Path,
) -> None:
    manifest, records = _default_capture()
    _with_pre_t0_stream(
        manifest,
        records,
        [
            _pre_t0_root(1, 2),
            _pre_t0_bucket(2, 1),
            _pre_t0_scan(3, 1, 60, 65, 2),
            _pre_t0_scan(4, 2, 70, 75, 2),
        ],
        root_reference=1,
        watermark=2,
    )

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.INCOMPLETE
    assert result.reasons == ("pre_t0_watcher_sequence_not_jsonl_ordered",)


def test_permuted_pre_t0_baseline_scan_sequence_alone_is_incomplete(
    tmp_path: Path,
) -> None:
    manifest, records = _default_capture()
    _with_pre_t0_stream(
        manifest,
        records,
        [
            _pre_t0_root(1, 1),
            _pre_t0_bucket(2, 2),
            _pre_t0_scan(3, 2, 60, 65, 2),
            _pre_t0_scan(4, 1, 70, 75, 2),
        ],
        root_reference=1,
        watermark=2,
    )

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.INCOMPLETE
    assert result.reasons == ("pre_t0_baseline_scan_sequence_not_jsonl_ordered",)


def test_late_watcher_event_invalidates_fixed_point_with_honest_ordinals(
    tmp_path: Path,
) -> None:
    """Ordinals fully consistent with capture order still cannot rescue it."""

    manifest, records = _default_capture()
    _with_pre_t0_stream(
        manifest,
        records,
        [
            _pre_t0_root(1, 1),
            _pre_t0_bucket(2, 2),
            _pre_t0_scan(3, 1, 60, 65, 3),
            _pre_t0_scan(4, 2, 70, 75, 3),
            _pre_t0_watch(5, 3, 76),
        ],
        root_reference=1,
        watermark=3,
    )

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.INCOMPLETE
    assert result.reasons == ("pre_t0_watch_drain_watermark_after_scan",)


def test_watcher_below_watermark_cannot_postdate_terminal_scan(
    tmp_path: Path,
) -> None:
    """Ordinal binding plus ascending capture time forces the watermark prefix.

    Once ``watcher_sequence`` equals physical capture position, a record below
    the watermark cannot carry a later timestamp than the record at the mark, so
    an attempt to hide a late event beneath the watermark fails closed on
    capture-time ordering instead.
    """

    manifest, records = _default_capture()
    _with_pre_t0_stream(
        manifest,
        records,
        [
            _pre_t0_root(1, 1),
            _pre_t0_bucket(2, 2),
            _pre_t0_watch(3, 3, 52),
            _pre_t0_scan(4, 1, 60, 65, 3),
            _pre_t0_scan(5, 2, 70, 75, 3),
        ],
        root_reference=1,
        watermark=3,
    )
    records["pre_t0_establishment"][1]["monotonic_ns"] = 76

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.INCOMPLETE
    assert "pre_t0_establishment_time_reversed" in result.reasons


def test_watcher_event_inside_terminal_scan_window_may_pass(
    tmp_path: Path,
) -> None:
    manifest, records = _default_capture()
    _with_pre_t0_stream(
        manifest,
        records,
        [
            _pre_t0_root(1, 1),
            _pre_t0_bucket(2, 2),
            _pre_t0_scan(3, 1, 60, 65, 2),
            _pre_t0_watch(4, 3, 72),
            _pre_t0_scan(5, 2, 70, 75, 3),
        ],
        root_reference=1,
        watermark=3,
    )

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.PASS


def test_watcher_event_exactly_at_terminal_scan_end_may_pass(
    tmp_path: Path,
) -> None:
    manifest, records = _default_capture()
    _with_pre_t0_stream(
        manifest,
        records,
        [
            _pre_t0_root(1, 1),
            _pre_t0_bucket(2, 2),
            _pre_t0_scan(3, 1, 60, 65, 2),
            _pre_t0_watch(4, 3, 75),
            _pre_t0_scan(5, 2, 70, 75, 3),
        ],
        root_reference=1,
        watermark=3,
    )

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.PASS


def test_ordered_pre_t0_stream_positive_control_may_pass(tmp_path: Path) -> None:
    manifest, records = _default_capture()
    _with_pre_t0_stream(
        manifest,
        records,
        [
            _pre_t0_root(1, 1),
            _pre_t0_bucket(2, 2),
            _pre_t0_scan(3, 1, 60, 65, 2),
            _pre_t0_scan(4, 2, 70, 75, 2),
        ],
        root_reference=1,
        watermark=2,
    )

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.PASS


def test_ground_truth_request_end_physically_before_start_is_incomplete(
    tmp_path: Path,
) -> None:
    manifest, records = _default_capture()
    start, end, finalized = records["ground_truth"]
    records["ground_truth"] = [end, start, finalized]

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.INCOMPLETE
    assert result.reasons == (
        "ground_truth_request_end_precedes_start_in_capture:1",
    )


def _gt_pair(sequence: int, upid: str, start: int, end: int) -> list[dict[str, Any]]:
    records = _ground_truth_operation(sequence, upid)
    records[0]["monotonic_ns"] = start
    records[1]["monotonic_ns"] = end
    return records


def _two_operation_ground_truth(
    manifest: dict[str, Any],
    records: dict[str, list[dict[str, Any]]],
    stream: list[dict[str, Any]],
    *,
    evidence: tuple[int, int],
) -> None:
    records["ground_truth"] = stream + [
        {
            "event": "generator_finalized",
            "last_sequence": 2,
            "total_operations": 2,
            "durable_flush_complete": True,
            "generator_process_identity": "synthetic-generator:1",
            "monotonic_ns": 150,
            "wall_timestamp": "2026-08-25T00:00:09Z",
        }
    ]
    manifest["candidate_close"]["known_upids"] = [UPID_A, UPID_B]
    records["watch_events"] = [
        _watch_event(1, masks=["IN_CREATE"], monotonic_ns=evidence[0], upid=UPID_A),
        _watch_event(2, masks=["IN_CREATE"], monotonic_ns=evidence[1], upid=UPID_B),
    ]
    for scan in records["scan_rounds"]:
        scan["exact_normalized_upids"] = [UPID_A, UPID_B]
        scan["watch_drained_through_sequence"] = 2
    first = records["exact_upid"][0]
    first["discovery_reference"] = 1
    records["exact_upid"].append(
        {
            **first,
            "observation_sequence": 2,
            "known_upid": UPID_B,
            "discovery_source": "watch",
            "discovery_reference": 2,
        }
    )


def test_backdated_request_start_cannot_admit_earlier_evidence(
    tmp_path: Path,
) -> None:
    """A physically late ``request_start`` cannot backdate its own causal bound.

    ``B``'s start record is appended after a ground-truth record at 130 but
    declares 100, which would otherwise let evidence at 105 satisfy the strict
    ``> request_start`` bound for a request that had not yet been initiated.
    """

    manifest, records = _default_capture()
    _two_operation_ground_truth(
        manifest,
        records,
        _gt_pair(1, UPID_A, 110, 130) + _gt_pair(2, UPID_B, 100, 140),
        evidence=(140, 105),
    )

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.INCOMPLETE
    assert result.reasons == ("ground_truth_monotonic_time_reversed_in_capture",)


def test_backdated_request_end_is_incomplete(tmp_path: Path) -> None:
    """The end still follows its own start, but reverses the capture stream."""

    manifest, records = _default_capture()
    _two_operation_ground_truth(
        manifest,
        records,
        _gt_pair(1, UPID_A, 110, 140) + _gt_pair(2, UPID_B, 120, 130),
        evidence=(145, 146),
    )

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.INCOMPLETE
    assert result.reasons == ("ground_truth_monotonic_time_reversed_in_capture",)


def test_concurrent_generator_operations_may_pass(tmp_path: Path) -> None:
    """Overlapping requests are legal while the capture stream never reverses."""

    manifest, records = _default_capture()
    start_a, end_a = _gt_pair(1, UPID_A, 110, 130)
    start_b, end_b = _gt_pair(2, UPID_B, 120, 140)
    _two_operation_ground_truth(
        manifest,
        records,
        [start_a, start_b, end_a, end_b],
        evidence=(145, 146),
    )

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.PASS


def test_equal_adjacent_ground_truth_timestamps_may_pass(tmp_path: Path) -> None:
    """CLOCK_MONOTONIC may repeat at timer resolution, so equality is legal."""

    manifest, records = _default_capture()
    _two_operation_ground_truth(
        manifest,
        records,
        _gt_pair(1, UPID_A, 110, 130) + _gt_pair(2, UPID_B, 130, 130),
        evidence=(140, 141),
    )

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.PASS


def test_ground_truth_finalizer_must_be_physically_last(tmp_path: Path) -> None:
    manifest, records = _default_capture()
    start, end, finalized = records["ground_truth"]
    records["ground_truth"] = [finalized, start, end]

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.INCOMPLETE
    assert result.reasons == ("ground_truth_finalizer_not_physically_last",)


def test_permuted_harness_sequence_is_incomplete(tmp_path: Path) -> None:
    manifest, records = _default_capture()
    result_dir = _materialize(tmp_path, manifest, records)
    stream = [
        json.loads(line)
        for line in (result_dir / CAPTURE_FILES["harness_events"])
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    stream[0], stream[1] = stream[1], stream[0]
    _write_jsonl(result_dir / CAPTURE_FILES["harness_events"], stream)
    _seal(result_dir, manifest["run_uuid"])

    result = analyze_capture(result_dir)

    assert result.outcome is AnalyzerOutcome.INCOMPLETE
    assert "harness_sequence_not_contiguous_or_ordered" in result.reasons


def test_missing_root_watch_before_baseline_cannot_pass(tmp_path: Path) -> None:
    manifest, records = _default_capture()
    records["pre_t0_establishment"][0]["watch_scope"] = "bucket"
    records["pre_t0_establishment"][0]["bucket"] = "root-is-not-a-bucket"

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.INCOMPLETE
    assert "pre_t0_root_watch_reference_invalid" in result.reasons


def test_pre_t0_baseline_scan_sets_must_match(tmp_path: Path) -> None:
    manifest, records = _default_capture()
    records["pre_t0_establishment"][-1]["exact_normalized_upids"] = [UPID_A]

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.INCOMPLETE
    assert "pre_t0_baseline_fixed_point_not_reached" in result.reasons


def test_undrained_pre_t0_watch_event_cannot_pass(tmp_path: Path) -> None:
    manifest, records = _default_capture()
    records["pre_t0_establishment"].append(
        {
            "establishment_sequence": 5,
            "event": "watch_event",
            "watcher_sequence": 3,
            "watch_scope": "task_root",
            "mask": ["IN_ATTRIB"],
            "monotonic_ns": 76,
            "complete": True,
        }
    )

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.INCOMPLETE
    assert "pre_t0_watch_event_undrained" in result.reasons


def test_lazy_bucket_without_child_watch_and_rescan_cannot_pass(
    tmp_path: Path,
) -> None:
    manifest, records = _default_capture()
    records["pre_t0_establishment"].insert(
        2,
        {
            "event": "bucket_created",
            "watcher_sequence": 3,
            "watch_scope": "task_root",
            "bucket": "f",
            "mask": ["IN_CREATE", "IN_ISDIR"],
            "raw_mask": _raw_mask(["IN_CREATE", "IN_ISDIR"]),
            "monotonic_ns": 52,
            "complete": True,
        },
    )
    _resequence_pre_t0(records)
    for scan in records["pre_t0_establishment"][-2:]:
        scan["bucket_set"] = ["0", "f"]
        scan["watch_drained_through_sequence"] = 3
    manifest["t0_quiescence"]["pre_t0_establishment"][
        "watch_drained_through_sequence"
    ] = 3

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.INCOMPLETE
    assert "pre_t0_bucket_watch_missing:f" in result.reasons


@pytest.mark.parametrize("creation_mask", ["IN_CREATE", "IN_MOVED_TO"])
def test_watch_first_pre_t0_fixed_point_with_lazy_bucket_may_continue(
    tmp_path: Path, creation_mask: str
) -> None:
    manifest, records = _default_capture()
    records["pre_t0_establishment"][2:2] = [
        {
            "event": "bucket_created",
            "watcher_sequence": 3,
            "watch_scope": "task_root",
            "bucket": "f",
            "mask": [creation_mask, "IN_ISDIR"],
            "raw_mask": _raw_mask([creation_mask, "IN_ISDIR"]),
            "monotonic_ns": 52,
            "complete": True,
        },
        {
            "event": "watch_installed",
            "watcher_sequence": 4,
            "watch_scope": "bucket",
            "watched_path": "tasks/f",
            "bucket": "f",
            "bucket_origin": "root_event",
            "trigger_watcher_sequence": 3,
            "monotonic_ns": 53,
            "complete": True,
        },
        {
            "event": "bucket_rescan",
            "phase": "PRE_T0_BUCKET_RESCAN",
            "bucket": "f",
            "trigger_watcher_sequence": 3,
            "scan_start_monotonic_ns": 54,
            "scan_end_monotonic_ns": 55,
            "exact_normalized_upids": [],
            "unreadable_entries": [],
            "malformed_entries": [],
            "complete": True,
        },
    ]
    _resequence_pre_t0(records)
    for scan in records["pre_t0_establishment"][-2:]:
        scan["bucket_set"] = ["0", "f"]
        scan["watch_drained_through_sequence"] = 4
    manifest["t0_quiescence"]["pre_t0_establishment"][
        "watch_drained_through_sequence"
    ] = 4

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.PASS


def test_t0_baseline_owner_classification_is_not_inferred_from_type_and_id(
    tmp_path: Path,
) -> None:
    manifest, records = _default_capture()
    _add_finalized_historical_baseline(manifest, records, UPID_OWNER_X)

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.INCOMPLETE
    assert "t0_quiescence_operation_unclassified" in result.reasons


def test_13a_baseline_only_with_generator_after_t1_cannot_pass(
    tmp_path: Path,
) -> None:
    manifest, records = _default_capture()
    _add_finalized_historical_baseline(manifest, records, UPID_B)
    _move_generator_operation_after_t1(records)
    records["watch_events"] = []
    for scan in records["scan_rounds"]:
        scan["exact_normalized_upids"] = [UPID_B]
        scan["watch_drained_through_sequence"] = 0
    records["exact_upid"] = [records["exact_upid"][0]]
    manifest["candidate_close"]["known_upids"] = []

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.INCOMPLETE
    assert "subrun_generated_window_work_missing" in result.reasons


def test_13b_static_baseline_pages_without_generated_window_work_cannot_pass(
    tmp_path: Path,
) -> None:
    manifest, records = _default_capture()
    _add_finalized_historical_baseline(manifest, records, UPID_B)
    _move_generator_operation_after_t1(records)
    records["watch_events"] = []
    for scan in records["scan_rounds"]:
        scan["exact_normalized_upids"] = [UPID_B]
        scan["watch_drained_through_sequence"] = 0
    records["exact_upid"] = [records["exact_upid"][0]]
    manifest["candidate_close"]["known_upids"] = []
    evidence_ids = _set_subrun(manifest, "13B", ["pagination_movement"])
    archive_page = records["api_pages"][1]
    archive_page.update(
        {
            "normalized_upids": [UPID_B],
            "phenomenon_id": evidence_ids["pagination_movement"],
            "page_sequence": 1,
        }
    )
    second_page = dict(archive_page)
    second_page.update(
        {
            "request_identity": "baseline-page-2",
            "start_offset": 10,
            "page_sequence": 2,
        }
    )
    records["api_pages"].append(second_page)

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.INCOMPLETE
    assert "subrun_generated_window_work_missing" in result.reasons


@pytest.mark.parametrize(
    ("subrun_id", "phenomena"),
    [
        ("13A", ["low_volume_enumeration"]),
        ("13B", ["pagination_movement"]),
        ("13C", ["active_archive_handoff"]),
        ("13D", ["index_rotation"]),
        ("13E", ["watch_overflow_or_invalidation"]),
        ("13F", ["watch_scan_creation_race"]),
        ("13G", ["pagination_movement", "active_archive_handoff"]),
    ],
)
def test_subrun_label_without_required_phenomenon_cannot_pass(
    tmp_path: Path, subrun_id: str, phenomena: list[str]
) -> None:
    manifest, records = _default_capture()
    _set_subrun(manifest, subrun_id, phenomena)
    if subrun_id == "13A":
        records["watch_events"] = []
        records["exact_upid"] = []
        for scan in records["scan_rounds"]:
            scan["exact_normalized_upids"] = []
            scan["watch_drained_through_sequence"] = 0
        manifest["candidate_close"]["known_upids"] = []

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is not AnalyzerOutcome.PASS


def test_subrun_13c_requires_raw_referenced_handoff_evidence(tmp_path: Path) -> None:
    manifest, records = _default_capture()
    evidence_ids = _set_subrun(manifest, "13C", ["active_archive_handoff"])
    active = _append_surface(
        records,
        "active",
        capture_start=150,
        capture_end=160,
        raw_evidence=_active_raw(UPID_A),
        upids=[UPID_A],
    )
    archive = _append_surface(
        records,
        "index",
        capture_start=170,
        capture_end=175,
        raw_evidence=_archive_raw(UPID_A),
        upids=[UPID_A],
    )
    records["harness_events"].insert(
        -2,
        {
            "event": "active_archive_handoff",
            "phenomenon_id": evidence_ids["active_archive_handoff"],
            "target_upid": UPID_A,
            "active_observation_sequence": active["observation_sequence"],
            "archive_observation_sequence": archive["observation_sequence"],
            "monotonic_ns": 176,
        },
    )

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.PASS

    active["normalized_upids"] = []
    mismatch_tmp = tmp_path / "13c-raw-projection-mismatch"
    mismatch_tmp.mkdir()
    mismatch = analyze_capture(_materialize(mismatch_tmp, manifest, records))
    assert mismatch.outcome is AnalyzerOutcome.INCOMPLETE
    assert mismatch.reasons == (
        f"surface_normalized_upids_mismatch_raw_evidence:active:"
        f"{active['observation_sequence']}",
    )


def test_subrun_13c_baseline_only_handoff_target_cannot_pass(tmp_path: Path) -> None:
    manifest, records = _default_capture()
    _add_finalized_historical_baseline(manifest, records, UPID_B)
    evidence_ids = _set_subrun(manifest, "13C", ["active_archive_handoff"])
    active = _append_surface(
        records,
        "active",
        capture_start=150,
        capture_end=160,
        raw_evidence=_active_raw(UPID_B),
        upids=[UPID_B],
    )
    archive = _append_surface(
        records,
        "index",
        capture_start=170,
        capture_end=175,
        raw_evidence=_archive_raw(UPID_B),
        upids=[UPID_B],
    )
    records["harness_events"].insert(
        -2,
        {
            "event": "active_archive_handoff",
            "phenomenon_id": evidence_ids["active_archive_handoff"],
            "target_upid": UPID_B,
            "active_observation_sequence": active["observation_sequence"],
            "archive_observation_sequence": archive["observation_sequence"],
            "monotonic_ns": 176,
        },
    )

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.INCOMPLETE
    assert "subrun_handoff_not_evidenced" in result.reasons


def test_subrun_13d_requires_raw_rotation_identity_and_watch_evidence(
    tmp_path: Path,
) -> None:
    manifest, records = _default_capture()
    evidence_ids = _set_subrun(manifest, "13D", ["index_rotation"])
    before = _append_surface(
        records,
        "index",
        capture_start=150,
        capture_end=160,
        raw_evidence=_archive_raw(UPID_A),
        upids=[UPID_A],
        stat={"device": 1, "inode": 20, "size": len(_archive_raw(UPID_A))},
    )
    rotated = _append_surface(
        records,
        "index.1",
        capture_start=180,
        capture_end=185,
        raw_evidence=_archive_raw(UPID_A),
        upids=[UPID_A],
        stat={"device": 1, "inode": 20, "size": len(_archive_raw(UPID_A))},
    )
    after = _append_surface(
        records,
        "index",
        capture_start=180,
        capture_end=185,
        raw_evidence="",
        upids=[],
        stat={"device": 1, "inode": 21, "size": 0},
    )
    records["ground_truth"][-1]["monotonic_ns"] = 190
    records["watch_events"].append(
        _watch_event(
            2,
            masks=["IN_MOVED_TO"],
            monotonic_ns=170,
            filename="index.1",
            phenomenon_id=evidence_ids["index_rotation"],
        )
    )
    for scan in records["scan_rounds"]:
        scan["watch_drained_through_sequence"] = 2
    records["harness_events"].insert(
        -2,
        {
            "event": "index_rotation",
            "phenomenon_id": evidence_ids["index_rotation"],
            "before_index_sequence": before["observation_sequence"],
            "after_index_sequence": after["observation_sequence"],
            "after_index1_sequence": rotated["observation_sequence"],
            "watcher_sequence": 2,
            "generator_sequences": [1],
            "monotonic_ns": 186,
        },
    )

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.PASS

    before["normalized_upids"] = []
    mismatch_tmp = tmp_path / "13d-raw-projection-mismatch"
    mismatch_tmp.mkdir()
    mismatch = analyze_capture(_materialize(mismatch_tmp, manifest, records))
    assert mismatch.outcome is AnalyzerOutcome.INCOMPLETE
    assert mismatch.reasons == (
        f"surface_normalized_upids_mismatch_raw_evidence:index:"
        f"{before['observation_sequence']}",
    )
    before["normalized_upids"] = [UPID_A]

    _set_surface_raw(before, "", [])
    _set_surface_raw(rotated, "", [])
    ambient_tmp = tmp_path / "ambient-only"
    ambient_tmp.mkdir()
    ambient_result = analyze_capture(_materialize(ambient_tmp, manifest, records))
    assert ambient_result.outcome is AnalyzerOutcome.INCOMPLETE
    assert "subrun_index_rotation_not_evidenced" in ambient_result.reasons


def _f13_marker(
    manifest: dict[str, Any], records: dict[str, list[dict[str, Any]]]
) -> dict[str, Any]:
    evidence_ids = _set_subrun(manifest, "13F", ["watch_scan_creation_race"])
    records["watch_events"][0]["monotonic_ns"] = 185
    return {
        "event": "scheduled_interleaving",
        "kind": "watch_scan_creation_race",
        "phenomenon_id": evidence_ids["watch_scan_creation_race"],
        "target_upid": UPID_A,
        "scan_sequence": 1,
        "watcher_sequence": 1,
        "monotonic_ns": 170,
    }


def _c13_marker(
    manifest: dict[str, Any], records: dict[str, list[dict[str, Any]]]
) -> dict[str, Any]:
    evidence_ids = _set_subrun(manifest, "13C", ["active_archive_handoff"])
    active = _append_surface(
        records, "active", capture_start=150, capture_end=160,
        raw_evidence=_active_raw(UPID_A), upids=[UPID_A],
    )
    archive = _append_surface(
        records, "index", capture_start=170, capture_end=175,
        raw_evidence=_archive_raw(UPID_A), upids=[UPID_A],
    )
    return {
        "event": "active_archive_handoff",
        "phenomenon_id": evidence_ids["active_archive_handoff"],
        "target_upid": UPID_A,
        "active_observation_sequence": active["observation_sequence"],
        "archive_observation_sequence": archive["observation_sequence"],
        "monotonic_ns": 176,
    }


def _d13_marker(
    manifest: dict[str, Any], records: dict[str, list[dict[str, Any]]]
) -> dict[str, Any]:
    evidence_ids = _set_subrun(manifest, "13D", ["index_rotation"])
    raw = _archive_raw(UPID_A)
    before = _append_surface(
        records, "index", capture_start=150, capture_end=160, raw_evidence=raw,
        upids=[UPID_A], stat={"device": 1, "inode": 20, "size": len(raw)},
    )
    rotated = _append_surface(
        records, "index.1", capture_start=180, capture_end=185, raw_evidence=raw,
        upids=[UPID_A], stat={"device": 1, "inode": 20, "size": len(raw)},
    )
    after = _append_surface(
        records, "index", capture_start=180, capture_end=185, raw_evidence="",
        upids=[], stat={"device": 1, "inode": 21, "size": 0},
    )
    records["ground_truth"][-1]["monotonic_ns"] = 190
    records["watch_events"].append(
        _watch_event(
            2, masks=["IN_MOVED_TO"], monotonic_ns=170, filename="index.1",
            phenomenon_id=evidence_ids["index_rotation"],
        )
    )
    for scan in records["scan_rounds"]:
        scan["watch_drained_through_sequence"] = 2
    return {
        "event": "index_rotation",
        "phenomenon_id": evidence_ids["index_rotation"],
        "before_index_sequence": before["observation_sequence"],
        "after_index_sequence": after["observation_sequence"],
        "after_index1_sequence": rotated["observation_sequence"],
        "watcher_sequence": 2,
        "generator_sequences": [1],
        "monotonic_ns": 186,
    }


@pytest.mark.parametrize(
    ("builder", "outcome"),
    [
        (_f13_marker, AnalyzerOutcome.PASS),
        (_c13_marker, AnalyzerOutcome.PASS),
        (_d13_marker, AnalyzerOutcome.PASS),
    ],
)
def test_subrun_marker_before_capture_finalization_may_pass(
    tmp_path: Path, builder: Any, outcome: AnalyzerOutcome
) -> None:
    manifest, records = _default_capture()
    records["harness_events"].insert(-2, builder(manifest, records))

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is outcome


@pytest.mark.parametrize("builder", [_f13_marker, _c13_marker, _d13_marker])
def test_subrun_marker_after_capture_finalization_is_incomplete(
    tmp_path: Path, builder: Any
) -> None:
    """`capture_finalized` closes semantic capture.

    The marker keeps a fully valid backdated ``monotonic_ns`` and valid
    referenced evidence; only its physical position moved past finalization.
    """

    manifest, records = _default_capture()
    records["harness_events"].insert(-1, builder(manifest, records))

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.INCOMPLETE
    assert result.reasons == ("harness_record_after_capture_finalized",)


def test_gap_signal_after_capture_finalization_is_incomplete_not_gap(
    tmp_path: Path,
) -> None:
    """A post-finalization gap signal cannot retroactively latch a GAP."""

    manifest, records = _default_capture()
    signal = {"event": "gap_signal", "reason": "injected_loss", "monotonic_ns": 260}

    before_dir = tmp_path / "before"
    before_dir.mkdir()
    before_manifest, before_records = _default_capture()
    before_records["harness_events"].insert(-2, dict(signal))
    before = analyze_capture(_materialize(before_dir, before_manifest, before_records))
    assert before.outcome is AnalyzerOutcome.GAP
    assert before.reasons == ("injected_loss",)

    records["harness_events"].insert(-1, signal)
    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.INCOMPLETE
    assert result.reasons == ("harness_record_after_capture_finalized",)


def test_heartbeat_after_capture_finalization_is_incomplete(tmp_path: Path) -> None:
    manifest, records = _default_capture()
    records["harness_events"].insert(
        -1, {"event": "heartbeat", "healthy": True, "monotonic_ns": 260}
    )

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.INCOMPLETE
    assert result.reasons == ("harness_record_after_capture_finalized",)


def test_process_stop_must_be_physically_last(tmp_path: Path) -> None:
    manifest, records = _default_capture()
    records["harness_events"].append(
        {"event": "heartbeat", "healthy": True, "monotonic_ns": 330}
    )

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.INCOMPLETE
    assert result.reasons == ("harness_record_after_capture_finalized",)


def test_default_harness_lifecycle_may_pass(tmp_path: Path) -> None:
    manifest, records = _default_capture()
    assert [record["event"] for record in records["harness_events"]][-2:] == [
        "capture_finalized",
        "process_stop",
    ]

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.PASS


def test_subrun_13f_requires_referenced_watch_during_scan(tmp_path: Path) -> None:
    manifest, records = _default_capture()
    evidence_ids = _set_subrun(manifest, "13F", ["watch_scan_creation_race"])
    records["watch_events"][0]["monotonic_ns"] = 185
    records["harness_events"].insert(
        -2,
        {
            "event": "scheduled_interleaving",
            "kind": "watch_scan_creation_race",
            "phenomenon_id": evidence_ids["watch_scan_creation_race"],
            "target_upid": UPID_A,
            "scan_sequence": 1,
            "watcher_sequence": 1,
            "monotonic_ns": 170,
        },
    )

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.PASS


def test_subrun_13f_baseline_only_race_target_cannot_pass(tmp_path: Path) -> None:
    manifest, records = _default_capture()
    _add_finalized_historical_baseline(manifest, records, UPID_B)
    evidence_ids = _set_subrun(manifest, "13F", ["watch_scan_creation_race"])
    records["watch_events"].append(
        _watch_event(2, masks=["IN_CREATE"], monotonic_ns=185, upid=UPID_B)
    )
    records["watch_events"][1]["phenomenon_id"] = evidence_ids[
        "watch_scan_creation_race"
    ]
    for scan in records["scan_rounds"]:
        scan["watch_drained_through_sequence"] = 2
    records["harness_events"].insert(
        -2,
        {
            "event": "scheduled_interleaving",
            "kind": "watch_scan_creation_race",
            "phenomenon_id": evidence_ids["watch_scan_creation_race"],
            "target_upid": UPID_B,
            "scan_sequence": 1,
            "watcher_sequence": 2,
            "monotonic_ns": 170,
        },
    )

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.INCOMPLETE
    assert "subrun_watch_scan_race_not_evidenced" in result.reasons


def test_subrun_13g_requires_every_declared_combined_phenomenon(
    tmp_path: Path,
) -> None:
    manifest, records = _default_capture()
    evidence_ids = _set_subrun(
        manifest,
        "13G",
        ["pagination_movement", "active_archive_handoff"],
    )
    archive_page = records["api_pages"][1]
    archive_page["phenomenon_id"] = evidence_ids["pagination_movement"]
    archive_page["page_sequence"] = 1
    archive_page["request_start_monotonic_ns"] = 115
    archive_page["response_end_monotonic_ns"] = 125
    second_page = dict(archive_page)
    second_page["request_identity"] = "combined-page-2"
    second_page["start_offset"] = 10
    second_page["page_sequence"] = 2
    records["api_pages"].append(second_page)
    active = _append_surface(
        records,
        "active",
        capture_start=150,
        capture_end=160,
        raw_evidence=_active_raw(UPID_A),
        upids=[UPID_A],
    )
    archive = _append_surface(
        records,
        "index",
        capture_start=170,
        capture_end=175,
        raw_evidence=_archive_raw(UPID_A),
        upids=[UPID_A],
    )
    records["harness_events"].insert(
        -2,
        {
            "event": "active_archive_handoff",
            "phenomenon_id": evidence_ids["active_archive_handoff"],
            "target_upid": UPID_A,
            "active_observation_sequence": active["observation_sequence"],
            "archive_observation_sequence": archive["observation_sequence"],
            "monotonic_ns": 176,
        },
    )

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.PASS

    for page in (archive_page, second_page):
        page["request_start_monotonic_ns"] = 100
        page["response_end_monotonic_ns"] = 110
    equality_tmp = tmp_path / "13g-pagination-equality-only"
    equality_tmp.mkdir()

    equality_result = analyze_capture(
        _materialize(equality_tmp, manifest, records)
    )

    assert equality_result.outcome is AnalyzerOutcome.INCOMPLETE
    assert equality_result.reasons == (
        "subrun_pagination_movement_not_evidenced",
    )


def test_healthy_then_unhealthy_heartbeat_before_close_cannot_pass(
    tmp_path: Path,
) -> None:
    manifest, records = _default_capture()
    records["harness_events"].insert(
        -2,
        {
            "event": "heartbeat",
            "healthy": False,
            "monotonic_ns": 290,
        },
    )

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.GAP
    assert "observer_unhealthy_heartbeat" in result.reasons


def test_structurally_valid_stale_heartbeat_is_gap(tmp_path: Path) -> None:
    manifest, records = _default_capture()
    manifest["reader_context"]["heartbeat_timeout_ns"] = 40

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.GAP
    assert "observer_unhealthy_or_stale_at_close" in result.reasons


def test_missing_heartbeat_stream_is_incomplete(tmp_path: Path) -> None:
    manifest, records = _default_capture()
    records["harness_events"] = [
        event
        for event in records["harness_events"]
        if event["event"] != "heartbeat"
    ]

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.INCOMPLETE
    assert "harness_heartbeat_missing" in result.reasons


def test_reader_process_stop_before_close_cannot_pass(tmp_path: Path) -> None:
    manifest, records = _default_capture()
    next(
        event for event in records["harness_events"] if event["event"] == "process_stop"
    )["monotonic_ns"] = 290

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.INCOMPLETE


def test_wrong_reader_process_identity_cannot_pass(tmp_path: Path) -> None:
    manifest, records = _default_capture()
    next(
        event for event in records["harness_events"] if event["event"] == "heartbeat"
    )["process_identity"] = "wrong-reader:9"

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.INCOMPLETE
    assert "harness_reader_process_identity_mismatch" in result.reasons


def test_unreadable_absent_exact_evidence_cannot_claim_final_ok(tmp_path: Path) -> None:
    manifest, records = _default_capture()
    exact = records["exact_upid"][0]
    exact["presence"] = False
    exact["readable"] = False
    exact["previously_known"] = False
    exact["final_status_interpretation"] = "ok"

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is not AnalyzerOutcome.PASS


def test_exact_parsed_final_status_must_match_raw_evidence(tmp_path: Path) -> None:
    manifest, records = _default_capture()
    exact = records["exact_upid"][0]
    exact["log_result"]["raw_evidence"] = "TASK ERROR"
    exact["log_result"]["sha256"] = hashlib.sha256(b"TASK ERROR").hexdigest()

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.INCOMPLETE
    assert "exact_upid_parsed_result_mismatches_raw_evidence" in result.reasons


def test_exact_final_status_captured_after_close_cannot_finalize_t1(tmp_path: Path) -> None:
    manifest, records = _default_capture()
    records["exact_upid"][0]["capture_end_monotonic_ns"] = 301

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.INCOMPLETE
    assert "exact_upid_capture_time_invalid_for_close" in result.reasons


def test_analyzer_source_hash_mismatch_is_ineligible(tmp_path: Path) -> None:
    manifest, records = _default_capture()
    capture_dir = _materialize(tmp_path, manifest, records)
    seal_path = capture_dir / "seal.json"
    seal = json.loads(seal_path.read_text(encoding="utf-8"))
    seal["analyzer_source_sha256"] = "0" * 64
    _write_json(seal_path, seal)

    result = analyze_capture(capture_dir)

    assert result.outcome is AnalyzerOutcome.INELIGIBLE
    assert "analyzer_source_hash_mismatch" in result.reasons


def test_surface_hash_must_match_raw_evidence(tmp_path: Path) -> None:
    manifest, records = _default_capture()
    records["surface_observations"][0]["raw_evidence"] = "changed"

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.INCOMPLETE
    assert result.reasons == ("surface_hash_mismatch:active:1",)


@pytest.mark.parametrize("source", ["active", "index", "index.1"])
def test_surface_declared_upid_absent_from_raw_cannot_manufacture_discovery(
    tmp_path: Path, source: str
) -> None:
    manifest, records = _default_capture()
    surface = _use_surface_as_only_discovery(records, source)
    _set_surface_raw(surface, "", [UPID_A])

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.INCOMPLETE
    assert result.reasons == (
        "surface_normalized_upids_mismatch_raw_evidence:"
        f"{source}:{surface['observation_sequence']}",
    )


@pytest.mark.parametrize(
    ("declared", "expected_reason"),
    [
        ([], "surface_normalized_upids_mismatch_raw_evidence"),
        ([UPID_B], "surface_normalized_upids_mismatch_raw_evidence"),
        ([UPID_A, UPID_A], "surface_declared_normalized_upids_duplicate"),
    ],
)
def test_surface_raw_upid_requires_exact_duplicate_free_declared_projection(
    tmp_path: Path, declared: list[str], expected_reason: str
) -> None:
    manifest, records = _default_capture()
    surface = _use_surface_as_only_discovery(records, "active")
    _set_surface_raw(surface, _active_raw(UPID_A), declared)

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.INCOMPLETE
    assert result.reasons == (
        f"{expected_reason}:active:{surface['observation_sequence']}",
    )


def test_surface_raw_and_declared_upids_exactly_agree_may_continue(
    tmp_path: Path,
) -> None:
    manifest, records = _default_capture()
    surface = _use_surface_as_only_discovery(records, "active")
    _set_surface_raw(surface, _active_raw(UPID_A), [UPID_A])

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.PASS


def test_malformed_surface_raw_evidence_is_incomplete(tmp_path: Path) -> None:
    manifest, records = _default_capture()
    surface = _use_surface_as_only_discovery(records, "index")
    _set_surface_raw(surface, f"not-an-archive-line {UPID_A}\n", [UPID_A])

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.INCOMPLETE
    assert result.reasons == ("surface_raw_evidence_malformed:index",)


def test_duplicate_upid_in_surface_raw_evidence_is_incomplete(tmp_path: Path) -> None:
    manifest, records = _default_capture()
    surface = _use_surface_as_only_discovery(records, "index.1")
    _set_surface_raw(surface, _archive_raw(UPID_A, UPID_A), [UPID_A])

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.INCOMPLETE
    assert result.reasons == ("surface_raw_evidence_duplicate_upid:index.1",)


@pytest.mark.parametrize("source", ["active", "index", "index.1"])
def test_changing_only_surface_projection_cannot_change_raw_enumeration_to_pass(
    tmp_path: Path, source: str
) -> None:
    manifest, records = _default_capture()
    surface = _use_surface_as_only_discovery(records, source)
    records["exact_upid"] = []
    manifest["candidate_close"]["known_upids"] = []
    _set_surface_raw(surface, "", [])
    missing = analyze_capture(_materialize(tmp_path, manifest, records))
    assert missing.outcome is AnalyzerOutcome.ENUMERATION_WITNESS

    surface["normalized_upids"] = [UPID_A]
    changed_dir = tmp_path / "changed-projection"
    changed_dir.mkdir()
    changed = analyze_capture(_materialize(changed_dir, manifest, records))

    assert changed.outcome is AnalyzerOutcome.INCOMPLETE
    assert changed.outcome is not AnalyzerOutcome.PASS


@pytest.mark.parametrize(
    ("raw_mask", "declared_mask"),
    [
        (INOTIFY_MASKS["IN_ACCESS"], ["IN_CREATE"]),
        (INOTIFY_MASKS["IN_CREATE"], ["IN_ACCESS"]),
    ],
)
def test_watch_raw_mask_text_disagreement_cannot_manufacture_discovery(
    tmp_path: Path, raw_mask: int, declared_mask: list[str]
) -> None:
    manifest, records = _default_capture()
    watch = _watch_event(1, masks=declared_mask, monotonic_ns=140, upid=UPID_A)
    watch["raw_mask"] = raw_mask
    _use_watch_as_only_discovery(records, watch)

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.INCOMPLETE
    assert result.reasons == ("inotify_mask_mismatch_raw_mask:watch:1",)


@pytest.mark.parametrize(
    ("raw_mask", "declared_mask", "queue_overflow"),
    [
        (INOTIFY_MASKS["IN_Q_OVERFLOW"], [], False),
        (INOTIFY_MASKS["IN_ATTRIB"], ["IN_Q_OVERFLOW"], True),
    ],
)
def test_watch_overflow_projection_must_match_raw_mask(
    tmp_path: Path,
    raw_mask: int,
    declared_mask: list[str],
    queue_overflow: bool,
) -> None:
    manifest, records = _default_capture()
    watch = _watch_event(
        1,
        masks=declared_mask,
        monotonic_ns=160,
        event_type="queue_overflow",
    )
    watch["raw_mask"] = raw_mask
    watch["queue_overflow"] = queue_overflow
    records["watch_events"] = [watch]

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.INCOMPLETE
    assert result.reasons == ("inotify_mask_mismatch_raw_mask:watch:1",)


def test_queue_overflow_boolean_must_match_agreed_raw_mask(tmp_path: Path) -> None:
    manifest, records = _default_capture()
    watch = _watch_event(
        1,
        masks=["IN_Q_OVERFLOW"],
        monotonic_ns=160,
        event_type="queue_overflow",
    )
    watch["queue_overflow"] = False
    records["watch_events"] = [watch]

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.INCOMPLETE
    assert result.reasons == ("watch_queue_overflow_mismatch_raw_mask:1",)


def test_13e_textual_fake_overflow_cannot_create_gap_or_pass(tmp_path: Path) -> None:
    manifest, records = _default_capture()
    evidence_ids = _set_subrun(
        manifest, "13E", ["watch_overflow_or_invalidation"]
    )
    watch = _watch_event(
        1,
        masks=["IN_Q_OVERFLOW"],
        monotonic_ns=160,
        event_type="queue_overflow",
        phenomenon_id=evidence_ids["watch_overflow_or_invalidation"],
    )
    watch["raw_mask"] = INOTIFY_MASKS["IN_ATTRIB"]
    records["watch_events"] = [watch]

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.INCOMPLETE
    assert result.outcome not in {AnalyzerOutcome.GAP, AnalyzerOutcome.PASS}


@pytest.mark.parametrize(
    "mask",
    ["IN_IGNORED", "IN_UNMOUNT", "IN_DELETE_SELF", "IN_MOVE_SELF"],
)
def test_raw_agreed_observer_loss_variants_latch_gap(
    tmp_path: Path, mask: str
) -> None:
    manifest, records = _default_capture()
    evidence_ids = _set_subrun(
        manifest, "13E", ["watch_overflow_or_invalidation"]
    )
    watch = _watch_event(
        1,
        masks=[mask],
        monotonic_ns=160,
        event_type="watch_invalidation",
        phenomenon_id=evidence_ids["watch_overflow_or_invalidation"],
    )
    records["watch_events"] = [watch]
    records["exact_upid"][0]["discovery_source"] = "scan"
    records["exact_upid"][0]["discovery_reference"] = 2
    for scan in records["scan_rounds"]:
        scan["watch_drained_through_sequence"] = 1

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.GAP
    assert "watch_invalidation_or_loss" in result.reasons


@pytest.mark.parametrize(
    "mask",
    ["IN_IGNORED", "IN_UNMOUNT", "IN_DELETE_SELF", "IN_MOVE_SELF"],
)
def test_observer_loss_text_disagreement_is_incomplete(
    tmp_path: Path, mask: str
) -> None:
    manifest, records = _default_capture()
    watch = _watch_event(
        1, masks=[mask], monotonic_ns=160, event_type="watch_invalidation"
    )
    watch["raw_mask"] = INOTIFY_MASKS["IN_ATTRIB"]
    records["watch_events"] = [watch]

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.INCOMPLETE


@pytest.mark.parametrize("mask", ["IN_CREATE", "IN_MOVED_TO", "IN_CLOSE_WRITE"])
def test_raw_agreed_discovery_variants_may_continue(
    tmp_path: Path, mask: str
) -> None:
    manifest, records = _default_capture()
    _use_watch_as_only_discovery(
        records,
        _watch_event(1, masks=[mask], monotonic_ns=140, upid=UPID_A),
    )

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.PASS


@pytest.mark.parametrize("mask", ["IN_DELETE", "IN_MOVED_FROM"])
def test_raw_agreed_deletion_variants_preserve_gap_semantics(
    tmp_path: Path, mask: str
) -> None:
    manifest, records = _default_capture()
    records["watch_events"].append(
        _watch_event(2, masks=[mask], monotonic_ns=240, upid=UPID_A)
    )

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.GAP
    assert "ground_truth_exact_log_deleted" in result.reasons


def test_unknown_inotify_raw_bit_is_incomplete(tmp_path: Path) -> None:
    manifest, records = _default_capture()
    records["watch_events"][0]["raw_mask"] |= 0x00001000

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.INCOMPLETE
    assert result.reasons == (
        "inotify_raw_mask_unknown_bits:watch:1:0x00001000",
    )


def test_watch_normalized_upid_must_be_derived_from_filename(tmp_path: Path) -> None:
    manifest, records = _default_capture()
    records["watch_events"][0]["filename"] = UPID_B

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.INCOMPLETE
    assert result.reasons == ("watch_normalized_upid_mismatch_filename:1",)


def test_after_t1_operation_cannot_self_declare_in_generator_window(
    tmp_path: Path,
) -> None:
    manifest, records = _default_capture()
    start, end = records["ground_truth"][:2]
    start["monotonic_ns"] = 310
    end["monotonic_ns"] = 320
    end["generator_window_relation"] = "after_generator_window"
    end["within_generator_window"] = True
    records["ground_truth"][-1]["monotonic_ns"] = 330

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.INCOMPLETE
    assert "ground_truth_generator_window_declaration_mismatch" in result.reasons


def test_disposable_fixture_without_generator_contract_is_ineligible(
    tmp_path: Path,
) -> None:
    manifest, records = _default_capture()
    manifest["fixture_kind"] = "disposable_pve"
    manifest["fixture_id"] = "disposable-fixture-13"
    manifest["clock_contract"]["fixture_id"] = "disposable-fixture-13"
    manifest["clock_contract"]["correlation_state"] = (
        "verified_single_shared_domain"
    )
    del manifest["generator_contract"]

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.INELIGIBLE
    assert result.reasons[0].startswith("generator_contract_ineligible:")


def test_generated_upid_must_match_approved_generator_owner(tmp_path: Path) -> None:
    manifest, records = _default_capture()
    records["ground_truth"][1]["returned_upid"] = (
        "UPID:fixture:00000001:00000001:00000001:stopall::other@pve:"
    )

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.INELIGIBLE
    assert "generated_upid_outside_approved_contract" in result.reasons


def test_prohibited_host_log_path_is_rejected_before_any_file_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened: list[Path] = []
    original_open = Path.open

    def recording_open(self: Path, *args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
        opened.append(self)
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", recording_open)
    prohibited = Path("/") / "var" / "log" / "pve"
    result = analyze_capture(prohibited)
    assert result.outcome is AnalyzerOutcome.INCOMPLETE
    assert opened == []


def test_analyzer_source_has_no_network_subprocess_or_collection_capability() -> None:
    source = Path("scripts/research/blocker_b_family_b_13_analyzer.py").read_text(
        encoding="utf-8"
    )
    for forbidden_import in (
        "import socket",
        "import subprocess",
        "import urllib",
        "import httpx",
        "import requests",
    ):
        assert forbidden_import not in source
    for forbidden_token in ("pct ", "qm ", "pvesh", "8006", "collect-current-host"):
        assert forbidden_token not in source


def test_analysis_attempts_no_network_or_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest, records = _default_capture()
    capture_dir = _materialize(tmp_path, manifest, records)

    def prohibited(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("offline analyzer attempted prohibited external access")

    monkeypatch.setattr(socket, "socket", prohibited)
    monkeypatch.setattr(subprocess, "Popen", prohibited)
    monkeypatch.setattr(subprocess, "run", prohibited)

    result = analyze_capture(capture_dir)
    assert result.outcome is AnalyzerOutcome.PASS
