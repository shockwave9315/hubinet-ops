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
        "raw_mask": 1,
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
            "within_scope": True,
            "boundary_relation": "before_t1",
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
        "t0_monotonic_ns": 100,
        "baseline_upids": [],
        "baseline_observation": {
            "capture_start_monotonic_ns": 70,
            "capture_end_monotonic_ns": 80,
            "committed_at_monotonic_ns": 90,
            "normalized_upids": [],
            "raw_evidence": "[]",
            "sha256": hashlib.sha256(b"[]").hexdigest(),
            "complete": True,
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
                "capture_start_monotonic_ns": 220,
                "capture_end_monotonic_ns": 221,
                "normalized_upids": [],
                "raw_evidence": "",
                "stat": {"device": 1, "inode": sequence, "size": 0},
                "sha256": hashlib.sha256(b"").hexdigest(),
                "readable": True,
                "complete": True,
            }
            for sequence, source in enumerate(("active", "index", "index.1"), 1)
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
                "monotonic_ns": 90,
            },
            {
                "event": "analyzer_version",
                "analyzer_revision": ANALYZER_REVISION,
                "process_identity": "synthetic-reader:1",
                "monotonic_ns": 91,
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
    records["ground_truth"].extend(_ground_truth_operation(2, upid))
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


def test_perfect_enumeration_is_only_tested_interleaving(tmp_path: Path) -> None:
    manifest, records = _default_capture()
    result = analyze_capture(_materialize(tmp_path, manifest, records))
    assert result.outcome is AnalyzerOutcome.PASS
    assert result.as_dict()["architecture_effect"] == "NONE"


def test_unknown_generated_upid_missing_everywhere_is_false_clean(tmp_path: Path) -> None:
    manifest, records = _default_capture()
    _add_ground_truth_operation(manifest, records, UPID_B)
    result = analyze_capture(_materialize(tmp_path, manifest, records))
    assert result.outcome is AnalyzerOutcome.FALSE_CLOSED
    assert result.witness is not None
    assert result.witness["ground_truth_upid"] == UPID_B


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
    for scan in records["scan_rounds"]:
        scan["watch_drained_through_sequence"] = 2
    result = analyze_capture(_materialize(tmp_path, manifest, records))
    assert result.outcome is AnalyzerOutcome.GAP


def test_duplicate_api_pages_tolerated_only_with_primary_reconciliation(tmp_path: Path) -> None:
    manifest, records = _default_capture()
    evidence_ids = _set_subrun(manifest, "13B", ["pagination_movement"])
    archive_page = records["api_pages"][1]
    archive_page["phenomenon_id"] = evidence_ids["pagination_movement"]
    archive_page["page_sequence"] = 1
    duplicate = dict(records["api_pages"][1])
    duplicate["request_identity"] = "api-archive-duplicate"
    duplicate["start_offset"] = 10
    duplicate["page_sequence"] = 2
    records["api_pages"].append(duplicate)
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
    assert result.outcome is AnalyzerOutcome.FALSE_CLOSED


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


def test_unknown_pre_enumeration_cleanup_can_witness_false_close(tmp_path: Path) -> None:
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
    assert result.outcome is AnalyzerOutcome.FALSE_CLOSED


def test_surviving_anchors_do_not_hide_unknown_intermediate_loss(tmp_path: Path) -> None:
    manifest, records = _default_capture()
    _add_ground_truth_operation(manifest, records, UPID_B)
    records["ground_truth"].pop()
    records["ground_truth"].extend(_ground_truth_operation(3, UPID_C))
    records["ground_truth"].append(
        {
            "event": "generator_finalized",
            "last_sequence": 3,
            "total_operations": 3,
            "durable_flush_complete": True,
            "generator_process_identity": "synthetic-generator:1",
            "monotonic_ns": 170,
            "wall_timestamp": "2026-08-25T00:00:04Z",
        }
    )
    for scan in records["scan_rounds"]:
        scan["exact_normalized_upids"] = [UPID_A, UPID_C]
    records["exact_upid"].append(
        {
            **records["exact_upid"][0],
            "known_upid": UPID_C,
            "discovery_source": "scan",
            "discovery_reference": 2,
        }
    )
    manifest["candidate_close"]["known_upids"] = [UPID_A, UPID_C]
    result = analyze_capture(_materialize(tmp_path, manifest, records))
    assert result.outcome is AnalyzerOutcome.FALSE_CLOSED
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
    records["ground_truth"][1]["boundary_relation"] = "ambiguous"
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
    active, archive = records["surface_observations"][:2]
    active["capture_start_monotonic_ns"] = 150
    active["capture_end_monotonic_ns"] = 160
    archive["capture_start_monotonic_ns"] = 170
    archive["capture_end_monotonic_ns"] = 175
    _set_surface_raw(active, "active A", [UPID_A])
    _set_surface_raw(archive, "archive A", [UPID_A])
    records["harness_events"].insert(
        -2,
        {
            "event": "active_archive_handoff",
            "phenomenon_id": evidence_ids["active_archive_handoff"],
            "target_upid": UPID_A,
            "active_observation_sequence": 1,
            "archive_observation_sequence": 2,
            "monotonic_ns": 176,
        },
    )

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.PASS


def test_subrun_13d_requires_raw_rotation_identity_and_watch_evidence(
    tmp_path: Path,
) -> None:
    manifest, records = _default_capture()
    evidence_ids = _set_subrun(manifest, "13D", ["index_rotation"])
    before = records["surface_observations"][1]
    rotated = records["surface_observations"][2]
    before["capture_start_monotonic_ns"] = 150
    before["capture_end_monotonic_ns"] = 160
    before["stat"] = {"device": 1, "inode": 20, "size": 9}
    _set_surface_raw(before, "old-index", [])
    rotated["capture_start_monotonic_ns"] = 180
    rotated["capture_end_monotonic_ns"] = 185
    rotated["stat"] = {"device": 1, "inode": 20, "size": 9}
    _set_surface_raw(rotated, "old-index", [])
    after = {
        **before,
        "observation_sequence": 4,
        "capture_start_monotonic_ns": 180,
        "capture_end_monotonic_ns": 185,
        "stat": {"device": 1, "inode": 21, "size": 9},
    }
    _set_surface_raw(after, "new-index", [])
    records["surface_observations"].append(after)
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
            "before_index_sequence": 2,
            "after_index_sequence": 4,
            "after_index1_sequence": 3,
            "watcher_sequence": 2,
            "monotonic_ns": 186,
        },
    )

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
    second_page = dict(archive_page)
    second_page["request_identity"] = "combined-page-2"
    second_page["start_offset"] = 10
    second_page["page_sequence"] = 2
    records["api_pages"].append(second_page)
    active, archive = records["surface_observations"][:2]
    active["capture_start_monotonic_ns"] = 150
    active["capture_end_monotonic_ns"] = 160
    archive["capture_start_monotonic_ns"] = 170
    archive["capture_end_monotonic_ns"] = 175
    _set_surface_raw(active, "combined active A", [UPID_A])
    _set_surface_raw(archive, "combined archive A", [UPID_A])
    records["harness_events"].insert(
        -2,
        {
            "event": "active_archive_handoff",
            "phenomenon_id": evidence_ids["active_archive_handoff"],
            "target_upid": UPID_A,
            "active_observation_sequence": 1,
            "archive_observation_sequence": 2,
            "monotonic_ns": 176,
        },
    )

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.PASS


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


def test_after_t1_operation_cannot_self_declare_within_scope(tmp_path: Path) -> None:
    manifest, records = _default_capture()
    start, end = records["ground_truth"][:2]
    start["monotonic_ns"] = 310
    end["monotonic_ns"] = 320
    end["boundary_relation"] = "after_t1"
    end["within_scope"] = True
    records["ground_truth"][-1]["monotonic_ns"] = 330

    result = analyze_capture(_materialize(tmp_path, manifest, records))

    assert result.outcome is AnalyzerOutcome.INCOMPLETE
    assert "ground_truth_scope_declaration_mismatch" in result.reasons


def test_disposable_fixture_without_generator_contract_is_ineligible(
    tmp_path: Path,
) -> None:
    manifest, records = _default_capture()
    manifest["fixture_kind"] = "disposable_pve"
    manifest["fixture_id"] = "disposable-fixture-13"
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
