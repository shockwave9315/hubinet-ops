"""Offline-only analyzer for Family B experiment #13 captures.

The analyzer accepts one explicit capture directory.  It has no collection,
network, subprocess, PVE API, or host-discovery capability.  Its output is
research-local and never grants architecture trust.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


SCHEMA_REVISION = "family-b-13-capture-v1"
PROTOCOL_REVISION = "family-b-13-preexecution-v1"
EXPECTED_B_S1_REVISION = "B-S1-2B-f2fd1ddb442fb1e0202a7a0800a05c330b6ac9cc"
ANALYZER_REVISION = "family-b-13-analyzer-v1"
MAX_JSONL_LINE_BYTES = 1_048_576

UPID_PATTERN = re.compile(
    r"^UPID:([A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?):"
    r"([0-9A-Fa-f]{8}):([0-9A-Fa-f]{8,9}):([0-9A-Fa-f]{8}):"
    r"([^:\s/]+):([^:\s/]*):([^:\s/]+):$"
)

CAPTURE_FILES = {
    "ground_truth": "ground-truth.jsonl",
    "watch_events": "watch-events.jsonl",
    "scan_rounds": "scan-rounds.jsonl",
    "surface_observations": "surface-observations.jsonl",
    "api_pages": "api-pages.jsonl",
    "exact_upid": "exact-upid.jsonl",
    "harness_events": "harness-events.jsonl",
}

EXPECTED_SOURCE_LEDGER = {
    "pve-manager": ("9.2.11", "f6997e698c7933ea8e62319e2bf1bf7262daa56a"),
    "pve-cluster": ("9.1.6", "7091d92e594952dba65c1e57568b3d7cc244e960"),
    "pve-common": ("9.2.1", "f665029eac78022e81810ab2e44eace57ade13fb"),
    "pve-access-control": ("9.1.1", "5ccd07d9302562b73374d331b63d25b04b86766c"),
    "pve-ha-manager": ("5.2.5", "c73364c19d5317e6df5bb1c1b727d080a5e897ef"),
    "pve-storage": ("9.1.8", "cd5c90ccd9ffd14a9578f58bbf528e78120f8bf2"),
    "qemu-server": ("9.2.6", "e6352be67f70042a7433a3a3c712b36d02f9f7cb"),
    "pve-container": ("6.1.13", "c8132559faedb76a56498d411bf3e024c1ff07e7"),
    "pve-guest-common": ("6.0.5", "191c23e385e5dbed1938b2d1d322196831ef9331"),
}


class AnalyzerOutcome(str, Enum):
    PASS = "ANALYZER_PASS_TESTED_INTERLEAVING"
    GAP = "B_S1_GAP_DETECTED"
    FALSE_CLOSED = "FALSE_CLOSED_COMPLETE_WITNESS"
    INCOMPLETE = "HARNESS_INCOMPLETE"
    INELIGIBLE = "ENVIRONMENT_INELIGIBLE"


@dataclass(frozen=True)
class AnalysisResult:
    outcome: AnalyzerOutcome
    reasons: tuple[str, ...]
    witness: Mapping[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "outcome": self.outcome.value,
            "reasons": list(self.reasons),
            "architecture_effect": "NONE",
        }
        if self.witness is not None:
            result["witness"] = dict(self.witness)
        return result


class CaptureError(ValueError):
    """The capture cannot be interpreted completely."""


class EnvironmentError(ValueError):
    """The capture context is not eligible for this exact protocol."""


def _is_within(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def _capture_root(raw_path: str | os.PathLike[str]) -> Path:
    # Lexically reject the prohibited host tree before stat(), resolve(), or read.
    lexical = Path(os.path.abspath(os.fspath(raw_path)))
    prohibited = Path(os.sep, "var", "log", "pve")
    if _is_within(lexical, prohibited):
        raise CaptureError("prohibited_host_pve_log_path")
    if not lexical.exists() or not lexical.is_dir() or lexical.is_symlink():
        raise CaptureError("capture_directory_missing_or_unsafe")
    root = lexical.resolve(strict=True)
    if _is_within(root, prohibited):
        raise CaptureError("capture_resolves_to_prohibited_host_pve_log_path")
    return root


def _safe_file(root: Path, name: str) -> Path:
    if Path(name).name != name or name in {"", ".", ".."}:
        raise CaptureError(f"unsafe_capture_filename:{name}")
    path = root / name
    if path.is_symlink() or not path.is_file():
        raise CaptureError(f"missing_or_unsafe_capture_file:{name}")
    if path.resolve(strict=True).parent != root:
        raise CaptureError(f"capture_file_escapes_root:{name}")
    return path


def _load_json(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CaptureError(f"invalid_json:{path.name}:{type(exc).__name__}") from exc


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        with path.open("rb") as handle:
            for line_number, raw_line in enumerate(handle, 1):
                if len(raw_line) > MAX_JSONL_LINE_BYTES:
                    raise CaptureError(f"jsonl_line_too_large:{path.name}:{line_number}")
                if not raw_line.strip():
                    raise CaptureError(f"blank_jsonl_line:{path.name}:{line_number}")
                try:
                    value = json.loads(raw_line.decode("utf-8"))
                except (UnicodeError, json.JSONDecodeError) as exc:
                    raise CaptureError(
                        f"invalid_jsonl:{path.name}:{line_number}:{type(exc).__name__}"
                    ) from exc
                if not isinstance(value, dict):
                    raise CaptureError(f"jsonl_record_not_object:{path.name}:{line_number}")
                records.append(value)
    except OSError as exc:
        raise CaptureError(f"unreadable_jsonl:{path.name}:{type(exc).__name__}") from exc
    return records


def _require_mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise CaptureError(f"field_not_object:{field}")
    return value


def _require_sequence(value: Any, field: str) -> Sequence[Any]:
    if not isinstance(value, list):
        raise CaptureError(f"field_not_array:{field}")
    return value


def _require_text(record: Mapping[str, Any], field: str) -> str:
    value = record.get(field)
    if not isinstance(value, str) or not value:
        raise CaptureError(f"field_not_nonempty_string:{field}")
    return value


def _require_string(record: Mapping[str, Any], field: str) -> str:
    value = record.get(field)
    if not isinstance(value, str):
        raise CaptureError(f"field_not_string:{field}")
    return value


def _require_int(record: Mapping[str, Any], field: str) -> int:
    value = record.get(field)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise CaptureError(f"field_not_nonnegative_integer:{field}")
    return value


def _require_signed_int(record: Mapping[str, Any], field: str) -> int:
    value = record.get(field)
    if not isinstance(value, int) or isinstance(value, bool):
        raise CaptureError(f"field_not_integer:{field}")
    return value


def _require_bool(record: Mapping[str, Any], field: str) -> bool:
    value = record.get(field)
    if not isinstance(value, bool):
        raise CaptureError(f"field_not_boolean:{field}")
    return value


def _require_upid(value: Any, field: str) -> str:
    if not isinstance(value, str) or UPID_PATTERN.fullmatch(value) is None:
        raise CaptureError(f"field_not_normalized_upid:{field}")
    return value


def _validate_manifest(manifest: Mapping[str, Any]) -> None:
    exact_values = {
        "schema_revision": SCHEMA_REVISION,
        "experiment_id": "family-b-13",
        "protocol_revision": PROTOCOL_REVISION,
        "expected_b_s1_revision": EXPECTED_B_S1_REVISION,
    }
    for field, expected in exact_values.items():
        if manifest.get(field) != expected:
            raise EnvironmentError(f"context_mismatch:{field}")

    for field in (
        "run_uuid",
        "fixture_id",
        "boot_id",
        "subrun_id",
        "started_at",
        "ended_at",
    ):
        _require_text(manifest, field)

    fixture_kind = _require_text(manifest, "fixture_kind")
    fixture_id = str(manifest["fixture_id"])
    if fixture_id.lower() == "ct112":
        raise EnvironmentError("ct112_is_not_a_family_b_fixture")
    if fixture_kind not in {"synthetic", "disposable_pve"}:
        raise EnvironmentError("unsupported_fixture_kind")
    if fixture_kind == "disposable_pve" and "placeholder" in fixture_id.lower():
        raise EnvironmentError("fixture_identity_is_placeholder")

    for field in (
        "node_identity",
        "kernel_context",
        "filesystem_context",
        "reader_context",
        "generator_context",
        "safety_limits",
        "capture_completeness",
        "candidate_close",
    ):
        _require_mapping(manifest.get(field), field)

    files = _require_mapping(manifest.get("capture_files"), "capture_files")
    if dict(files) != CAPTURE_FILES:
        raise CaptureError("capture_file_map_mismatch")

    completeness = _require_mapping(
        manifest["capture_completeness"], "capture_completeness"
    )
    required_markers = {
        "ground_truth_finalized",
        "watch_capture_complete",
        "scan_capture_complete",
        "surface_capture_complete",
        "api_capture_complete",
        "exact_upid_capture_complete",
        "harness_capture_complete",
    }
    if any(completeness.get(marker) is not True for marker in required_markers):
        raise CaptureError("capture_completeness_marker_false_or_missing")

    generator = _require_mapping(manifest["generator_context"], "generator_context")
    if _require_int(generator, "maximum_operation_count") == 0:
        raise CaptureError("generator_operation_limit_zero")
    if _require_int(generator, "maximum_duration_seconds") == 0:
        raise CaptureError("generator_duration_limit_zero")
    _require_text(generator, "process_identity")
    if generator.get("ground_truth_source") != "operation_initiator_return_value":
        raise CaptureError("ground_truth_not_independent")
    reader = _require_mapping(manifest["reader_context"], "reader_context")
    _require_text(reader, "process_identity")
    if _require_int(reader, "heartbeat_timeout_ns") == 0:
        raise CaptureError("reader_heartbeat_timeout_zero")
    safety_limits = _require_mapping(manifest["safety_limits"], "safety_limits")
    for field in (
        "minimum_free_disk_bytes",
        "minimum_free_log_bytes",
        "maximum_task_rate_per_minute",
    ):
        if _require_int(safety_limits, field) == 0:
            raise CaptureError(f"safety_limit_zero:{field}")
    if manifest["subrun_id"] not in {f"13{letter}" for letter in "ABCDEFG"}:
        raise EnvironmentError("unsupported_subrun_id")

    ledger = _require_sequence(manifest.get("version_ledger"), "version_ledger")
    observed: dict[str, tuple[str, str]] = {}
    for index, entry_value in enumerate(ledger):
        entry = _require_mapping(entry_value, f"version_ledger[{index}]")
        component = _require_text(entry, "component")
        if component in observed:
            raise CaptureError(f"source_ledger_duplicate_component:{component}")
        observed[component] = (
            _require_text(entry, "installed_version"),
            _require_text(entry, "source_commit"),
        )
    for component, expected in EXPECTED_SOURCE_LEDGER.items():
        if observed.get(component) != expected:
            raise EnvironmentError(f"source_version_mismatch:{component}")
    if manifest.get("loaded_code_status") != "exact_context_matched":
        raise EnvironmentError("loaded_code_context_not_matched")


def _validate_seal(root: Path, seal: Mapping[str, Any]) -> None:
    if seal.get("schema_revision") != SCHEMA_REVISION:
        raise CaptureError("seal_schema_mismatch")
    if seal.get("analyzer_revision") != ANALYZER_REVISION:
        raise EnvironmentError("analyzer_revision_mismatch")
    _require_text(seal, "analyzer_commit")
    entries = _require_sequence(seal.get("files"), "seal.files")
    expected_names = {"manifest.json", *CAPTURE_FILES.values()}
    seen: set[str] = set()
    canonical_entries: list[dict[str, Any]] = []
    for index, value in enumerate(entries):
        entry = _require_mapping(value, f"seal.files[{index}]")
        name = _require_text(entry, "name")
        if name in seen or name not in expected_names:
            raise CaptureError(f"seal_file_set_invalid:{name}")
        seen.add(name)
        expected_hash = _require_text(entry, "sha256")
        if re.fullmatch(r"[0-9a-f]{64}", expected_hash) is None:
            raise CaptureError(f"seal_hash_not_sha256:{name}")
        expected_size = _require_int(entry, "size")
        path = _safe_file(root, name)
        content = path.read_bytes()
        if len(content) != expected_size:
            raise CaptureError(f"seal_size_mismatch:{name}")
        if hashlib.sha256(content).hexdigest() != expected_hash:
            raise CaptureError(f"seal_hash_mismatch:{name}")
        canonical_entries.append(
            {"name": name, "sha256": expected_hash, "size": expected_size}
        )
    if seen != expected_names:
        raise CaptureError("seal_file_set_incomplete")
    payload = {
        "schema_revision": SCHEMA_REVISION,
        "run_uuid": _require_text(seal, "run_uuid"),
        "analyzer_revision": ANALYZER_REVISION,
        "analyzer_commit": str(seal["analyzer_commit"]),
        "files": sorted(canonical_entries, key=lambda item: item["name"]),
    }
    actual = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    overall_manifest_hash = seal.get("overall_manifest_hash")
    if (
        not isinstance(overall_manifest_hash, str)
        or re.fullmatch(r"[0-9a-f]{64}", overall_manifest_hash) is None
        or overall_manifest_hash != actual
    ):
        raise CaptureError("overall_manifest_hash_mismatch")


def _ground_truth(
    records: Sequence[Mapping[str, Any]], manifest: Mapping[str, Any]
) -> list[dict[str, Any]]:
    starts: dict[int, Mapping[str, Any]] = {}
    ends: dict[int, Mapping[str, Any]] = {}
    finalizers: list[Mapping[str, Any]] = []
    process_identity = str(
        _require_mapping(manifest["generator_context"], "generator_context")[
            "process_identity"
        ]
    )
    for record in records:
        event = _require_text(record, "event")
        if event == "generator_finalized":
            finalizers.append(record)
            continue
        if event not in {"request_start", "request_end"}:
            raise CaptureError(f"unknown_ground_truth_event:{event}")
        sequence = _require_int(record, "generator_sequence")
        if sequence == 0:
            raise CaptureError("ground_truth_sequence_zero")
        _require_text(record, "request_id")
        _require_text(record, "operation")
        _require_int(record, "monotonic_ns")
        _require_text(record, "wall_timestamp")
        if record.get("generator_process_identity") != process_identity:
            raise CaptureError("ground_truth_process_identity_mismatch")
        target = starts if event == "request_start" else ends
        if sequence in target:
            raise CaptureError(f"duplicate_ground_truth_event:{event}:{sequence}")
        target[sequence] = record

    if len(finalizers) != 1:
        raise CaptureError("ground_truth_finalization_marker_missing_or_duplicate")
    finalized = finalizers[0]
    if finalized.get("generator_process_identity") != process_identity:
        raise CaptureError("ground_truth_finalizer_process_identity_mismatch")
    finalized_monotonic_ns = _require_int(finalized, "monotonic_ns")
    _require_text(finalized, "wall_timestamp")
    if finalized.get("durable_flush_complete") is not True:
        raise CaptureError("ground_truth_finalization_not_durable")
    last_sequence = _require_int(finalized, "last_sequence")
    total_operations = _require_int(finalized, "total_operations")
    if total_operations == 0:
        raise CaptureError("ground_truth_contains_no_operations")
    generator_context = _require_mapping(
        manifest["generator_context"], "generator_context"
    )
    if total_operations > _require_int(generator_context, "maximum_operation_count"):
        raise CaptureError("ground_truth_operation_limit_exceeded")
    expected_sequences = set(range(1, last_sequence + 1))
    if set(starts) != expected_sequences or set(ends) != expected_sequences:
        raise CaptureError("ground_truth_sequence_gap")
    if total_operations != last_sequence:
        raise CaptureError("ground_truth_finalization_count_mismatch")

    operations: list[dict[str, Any]] = []
    seen_upids: set[str] = set()
    for sequence in sorted(expected_sequences):
        start = starts[sequence]
        end = ends[sequence]
        for field in ("request_id", "operation"):
            if start[field] != end.get(field):
                raise CaptureError(f"ground_truth_request_pair_mismatch:{sequence}:{field}")
        if end.get("outcome") != "success":
            raise CaptureError(f"ground_truth_request_not_successful:{sequence}")
        upid = _require_upid(end.get("returned_upid"), "returned_upid")
        if upid in seen_upids:
            raise CaptureError("ground_truth_duplicate_returned_upid")
        seen_upids.add(upid)
        expected_task_type = _require_text(end, "expected_task_type")
        expected_task_id = end.get("expected_task_id")
        if not isinstance(expected_task_id, str):
            raise CaptureError("ground_truth_expected_task_id_not_string")
        match = UPID_PATTERN.fullmatch(upid)
        assert match is not None
        if match.group(5) != expected_task_type or match.group(6) != expected_task_id:
            raise CaptureError("ground_truth_expected_task_does_not_match_upid")
        relation = end.get("boundary_relation")
        if relation not in {"before_t1", "after_t1", "ambiguous"}:
            raise CaptureError("ground_truth_boundary_relation_invalid")
        within_scope = _require_bool(end, "within_scope")
        request_end_monotonic_ns = _require_int(end, "monotonic_ns")
        if request_end_monotonic_ns < start["monotonic_ns"]:
            raise CaptureError("ground_truth_request_time_reversed")
        operations.append(
            {
                "sequence": sequence,
                "request_id": end["request_id"],
                "operation": end["operation"],
                "upid": upid,
                "expected_task_type": end["expected_task_type"],
                "expected_task_id": end["expected_task_id"],
                "boundary_relation": relation,
                "within_scope": within_scope,
                "request_start_monotonic_ns": start["monotonic_ns"],
                "request_end_monotonic_ns": request_end_monotonic_ns,
            }
        )
    first_request_monotonic_ns = min(
        operation["request_start_monotonic_ns"] for operation in operations
    )
    last_request_monotonic_ns = max(
        operation["request_end_monotonic_ns"] for operation in operations
    )
    if finalized_monotonic_ns < last_request_monotonic_ns:
        raise CaptureError("ground_truth_finalizer_precedes_request_end")
    operation_span_ns = finalized_monotonic_ns - first_request_monotonic_ns
    maximum_duration_ns = (
        _require_int(generator_context, "maximum_duration_seconds") * 1_000_000_000
    )
    if operation_span_ns > maximum_duration_ns:
        raise CaptureError("ground_truth_duration_limit_exceeded")
    return operations


def _event_upids(records: Iterable[Mapping[str, Any]], field: str) -> set[str]:
    result: set[str] = set()
    for record in records:
        for value in _require_sequence(record.get(field), field):
            result.add(_require_upid(value, field))
    return result


def _analyze_loaded(
    manifest: Mapping[str, Any], records: Mapping[str, Sequence[Mapping[str, Any]]]
) -> AnalysisResult:
    operations = _ground_truth(records["ground_truth"], manifest)
    close = _require_mapping(manifest["candidate_close"], "candidate_close")
    close_state = _require_text(close, "state")
    if close_state not in {"CLOSED_COMPLETE", "GAP_LATCHED"}:
        raise CaptureError("candidate_close_not_terminal")
    close_monotonic = _require_int(close, "monotonic_ns")
    close_event_id = _require_text(close, "event_id")
    declared_known = {
        _require_upid(value, "candidate_close.known_upids")
        for value in _require_sequence(close.get("known_upids"), "candidate_close.known_upids")
    }

    harness = records["harness_events"]
    kinds: list[str] = []
    for record in harness:
        event = _require_text(record, "event")
        kinds.append(event)
        _require_int(record, "monotonic_ns")
        if event in {"process_start", "process_stop"}:
            _require_text(record, "process_identity")
        elif event == "heartbeat":
            _require_bool(record, "healthy")
        elif event == "capture_finalized":
            _require_bool(record, "complete")
    if kinds.count("process_start") != 1 or kinds.count("process_stop") != 1:
        raise CaptureError("harness_process_boundary_missing_or_duplicate")
    if "process_crash" in kinds:
        raise CaptureError("harness_process_crash")
    if kinds.count("capture_finalized") != 1:
        raise CaptureError("harness_capture_finalization_missing_or_duplicate")
    if not any(
        record.get("event") == "analyzer_version"
        and record.get("analyzer_revision") == ANALYZER_REVISION
        for record in harness
    ):
        raise CaptureError("harness_analyzer_version_missing_or_mismatch")
    heartbeats = [
        record
        for record in harness
        if record.get("event") == "heartbeat" and record.get("healthy") is True
    ]
    if not heartbeats:
        raise CaptureError("harness_heartbeat_missing")
    latest_heartbeat = max(_require_int(record, "monotonic_ns") for record in heartbeats)
    timeout = _require_int(
        _require_mapping(manifest["reader_context"], "reader_context"),
        "heartbeat_timeout_ns",
    )
    if latest_heartbeat > close_monotonic or close_monotonic - latest_heartbeat > timeout:
        raise CaptureError("harness_heartbeat_stale_at_close")
    finalizer = next(record for record in harness if record.get("event") == "capture_finalized")
    if finalizer.get("complete") is not True:
        raise CaptureError("harness_capture_finalization_false")

    gap_reasons: set[str] = set()
    watch_known: set[str] = set()
    watch_deleted: set[str] = set()
    watcher_sequences: set[int] = set()
    for record in records["watch_events"]:
        watcher_sequence = _require_int(record, "watcher_sequence")
        if watcher_sequence == 0 or watcher_sequence in watcher_sequences:
            raise CaptureError("watcher_sequence_zero_or_duplicate")
        watcher_sequences.add(watcher_sequence)
        _require_signed_int(record, "watch_descriptor")
        _require_int(record, "cookie")
        _require_int(record, "raw_mask")
        _require_int(record, "raw_order")
        _require_string(record, "watched_path")
        _require_string(record, "filename")
        _require_bool(record, "queue_overflow")
        _require_int(record, "monotonic_ns")
        _require_text(record, "wall_timestamp")
        event_type = _require_text(record, "event_type")
        masks = {
            str(value)
            for value in _require_sequence(record.get("mask"), "watch.mask")
        }
        if record.get("queue_overflow") is True or "IN_Q_OVERFLOW" in masks:
            gap_reasons.add("watch_queue_overflow")
        if event_type in {"watch_invalidation", "watch_loss"} or masks.intersection(
            {"IN_IGNORED", "IN_UNMOUNT", "IN_DELETE_SELF", "IN_MOVE_SELF"}
        ):
            gap_reasons.add("watch_invalidation_or_loss")
        upid_value = record.get("normalized_upid")
        if upid_value is not None:
            upid = _require_upid(upid_value, "watch.normalized_upid")
            if masks.intersection({"IN_CREATE", "IN_MOVED_TO", "IN_CLOSE_WRITE"}):
                watch_known.add(upid)
            if masks.intersection({"IN_DELETE", "IN_MOVED_FROM"}):
                watch_deleted.add(upid)
    if watcher_sequences and watcher_sequences != set(
        range(1, max(watcher_sequences) + 1)
    ):
        raise CaptureError("watcher_sequence_gap")

    scan_known: set[str] = set()
    prior_scan: set[str] | None = None
    fixed_point_rounds = 0
    scan_round_ids: set[str] = set()
    for record in records["scan_rounds"]:
        round_id = _require_text(record, "round_id")
        if round_id in scan_round_ids:
            raise CaptureError("duplicate_scan_round_id")
        scan_round_ids.add(round_id)
        scan_start = _require_int(record, "scan_start_monotonic_ns")
        scan_end = _require_int(record, "scan_end_monotonic_ns")
        if scan_end < scan_start:
            raise CaptureError("scan_time_reversed")
        current = {
            _require_upid(value, "scan.exact_normalized_upids")
            for value in _require_sequence(
                record.get("exact_normalized_upids"), "scan.exact_normalized_upids"
            )
        }
        for value in _require_sequence(record.get("bucket_set"), "scan.bucket_set"):
            if not isinstance(value, str):
                raise CaptureError("scan_bucket_not_string")
        _require_mapping(record.get("stat_metadata"), "scan.stat_metadata")
        unreadable = _require_sequence(record.get("unreadable_entries"), "scan.unreadable_entries")
        malformed = _require_sequence(record.get("malformed_entries"), "scan.malformed_entries")
        if unreadable or malformed or record.get("consistency_marker") == "inconsistent":
            gap_reasons.add("scan_unreadable_malformed_or_inconsistent")
        if record.get("consistency_marker") == "fixed_point":
            fixed_point_rounds += 1
        if prior_scan is not None and prior_scan - current:
            gap_reasons.add("exact_log_disappeared_between_scans")
        prior_scan = current
        scan_known.update(current)
    if fixed_point_rounds < 2:
        raise CaptureError("scan_fixed_point_evidence_incomplete")

    required_surfaces = {"active", "index", "index.1"}
    seen_surfaces: set[str] = set()
    for record in records["surface_observations"]:
        source = _require_text(record, "source")
        if source not in required_surfaces:
            raise CaptureError(f"unknown_surface_source:{source}")
        seen_surfaces.add(source)
        capture_start = _require_int(record, "capture_start_monotonic_ns")
        capture_end = _require_int(record, "capture_end_monotonic_ns")
        if capture_end < capture_start:
            raise CaptureError("surface_capture_time_reversed")
        for value in _require_sequence(
            record.get("normalized_upids"), "surface.normalized_upids"
        ):
            _require_upid(value, "surface.normalized_upids")
        _require_string(record, "raw_evidence")
        _require_mapping(record.get("stat"), "surface.stat")
        surface_hash = _require_text(record, "sha256")
        if re.fullmatch(r"[0-9a-f]{64}", surface_hash) is None:
            raise CaptureError("surface_hash_not_sha256")
        readable = _require_bool(record, "readable")
        complete = _require_bool(record, "complete")
        if not readable or not complete:
            gap_reasons.add(f"surface_incomplete:{source}")
    if seen_surfaces != required_surfaces:
        raise CaptureError("required_surface_observation_missing")

    api_seen: set[str] = set()
    api_sources: set[str] = set()
    api_request_ids: set[str] = set()
    for record in records["api_pages"]:
        source = _require_text(record, "source")
        if source not in {"active", "archive", "all"}:
            raise CaptureError(f"unknown_api_source:{source}")
        api_sources.add(source)
        _require_int(record, "start_offset")
        if _require_int(record, "limit") == 0:
            raise CaptureError("api_limit_zero")
        request_identity = _require_text(record, "request_identity")
        if request_identity in api_request_ids:
            raise CaptureError("duplicate_api_request_identity")
        api_request_ids.add(request_identity)
        request_start = _require_int(record, "request_start_monotonic_ns")
        response_end = _require_int(record, "response_end_monotonic_ns")
        if response_end < request_start:
            raise CaptureError("api_response_time_reversed")
        restart_reason = record.get("restart_reason")
        if restart_reason is not None and not isinstance(restart_reason, str):
            raise CaptureError("api_restart_reason_not_string_or_null")
        api_seen.update(
            _require_upid(value, "api.normalized_upids")
            for value in _require_sequence(record.get("normalized_upids"), "api.normalized_upids")
        )
        if not _require_bool(record, "complete_response"):
            gap_reasons.add("api_response_incomplete")
    if api_sources != {"active", "archive", "all"}:
        raise CaptureError("required_api_profile_missing")

    exact_known: set[str] = set()
    exact_final: set[str] = set()
    for record in records["exact_upid"]:
        upid = _require_upid(record.get("known_upid"), "exact.known_upid")
        capture_start = _require_int(record, "capture_start_monotonic_ns")
        capture_end = _require_int(record, "capture_end_monotonic_ns")
        if capture_end < capture_start:
            raise CaptureError("exact_upid_capture_time_reversed")
        if "status_result" not in record or "log_result" not in record:
            raise CaptureError("exact_upid_result_field_missing")
        presence = _require_bool(record, "presence")
        readable = _require_bool(record, "readable")
        previously_known = _require_bool(record, "previously_known")
        final_interpretation = _require_text(record, "final_status_interpretation")
        if final_interpretation not in {
            "ok",
            "warning",
            "error",
            "not_final",
            "unknown",
            "unreadable",
            "absent",
        }:
            raise CaptureError("exact_upid_final_interpretation_invalid")
        if presence and readable:
            exact_known.add(upid)
        if final_interpretation in {"ok", "warning", "error"}:
            exact_final.add(upid)
        if previously_known and (not presence or not readable):
            gap_reasons.add("known_exact_upid_lost")

    primary_known = watch_known | scan_known | exact_known
    if declared_known - primary_known:
        raise CaptureError("candidate_declares_unpreserved_known_upid")
    in_scope = [operation for operation in operations if operation["within_scope"]]
    expected_upids = {str(operation["upid"]) for operation in in_scope}
    ambiguous = {
        str(operation["upid"])
        for operation in operations
        if operation["boundary_relation"] == "ambiguous"
    }
    if ambiguous:
        gap_reasons.add("t1_boundary_ordering_ambiguous")
    if expected_upids & watch_deleted:
        gap_reasons.add("ground_truth_exact_log_deleted")
    if (expected_upids & primary_known) - exact_final:
        gap_reasons.add("ground_truth_final_status_unreconciled")

    for record in harness:
        if record.get("event") == "gap_signal":
            gap_reasons.add(_require_text(record, "reason"))

    missing = expected_upids - primary_known
    if gap_reasons or close_state == "GAP_LATCHED":
        return AnalysisResult(AnalyzerOutcome.GAP, tuple(sorted(gap_reasons or {"candidate_gap"})))

    if close_state == "CLOSED_COMPLETE" and missing:
        operation_by_upid = {str(operation["upid"]): operation for operation in in_scope}
        missing_upid = sorted(missing)[0]
        operation = operation_by_upid[missing_upid]
        witness = {
            "classification": "B-S1 NO-GO FOR THE TESTED EXACT SCOPE",
            "ground_truth_upid": missing_upid,
            "ground_truth_generator_sequence": operation["sequence"],
            "operation": operation["operation"],
            "within_declared_scope": True,
            "omitted_from_primary_b_s1_observation_set": True,
            "candidate_close_event_id": close_event_id,
            "candidate_close_state": close_state,
            "required_gap_signal_recorded": False,
            "preserved_evidence_files": [
                "manifest.json",
                *sorted(CAPTURE_FILES.values()),
                "seal.json",
            ],
        }
        return AnalysisResult(
            AnalyzerOutcome.FALSE_CLOSED,
            ("ground_truth_upid_missing_without_gap",),
            witness,
        )

    if close_state != "CLOSED_COMPLETE":
        raise CaptureError("candidate_did_not_close")
    if expected_upids - declared_known:
        raise CaptureError("candidate_close_known_set_incomplete")
    if expected_upids - exact_final:
        raise CaptureError("exact_final_status_set_incomplete")

    # api_seen is intentionally not a completeness requirement.  Duplicate or
    # omitted mutable pages are tolerable only because the exact-log/watch plane
    # independently preserved every ground-truth UPID above.
    _ = api_seen
    return AnalysisResult(
        AnalyzerOutcome.PASS,
        ("ground_truth_equals_reconciled_primary_observation_set_for_tested_interleaving",),
    )


def analyze_capture(capture_dir: str | os.PathLike[str]) -> AnalysisResult:
    """Analyze one explicit sealed capture directory, failing closed."""

    try:
        root = _capture_root(capture_dir)
        manifest_value = _load_json(_safe_file(root, "manifest.json"))
        manifest = _require_mapping(manifest_value, "manifest")
        _validate_manifest(manifest)
        seal_value = _load_json(_safe_file(root, "seal.json"))
        seal = _require_mapping(seal_value, "seal")
        _validate_seal(root, seal)
        if seal.get("run_uuid") != manifest.get("run_uuid"):
            raise CaptureError("seal_run_uuid_mismatch")
        records = {
            key: _load_jsonl(_safe_file(root, name)) for key, name in CAPTURE_FILES.items()
        }
        return _analyze_loaded(manifest, records)
    except EnvironmentError as exc:
        return AnalysisResult(AnalyzerOutcome.INELIGIBLE, (str(exc),))
    except (CaptureError, OSError) as exc:
        return AnalysisResult(AnalyzerOutcome.INCOMPLETE, (str(exc),))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    analyze = subparsers.add_parser("analyze", help="analyze an explicit offline capture")
    analyze.add_argument("--capture-dir", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    result = analyze_capture(args.capture_dir)
    print(json.dumps(result.as_dict(), sort_keys=True))
    return 0 if result.outcome in {AnalyzerOutcome.PASS, AnalyzerOutcome.GAP} else 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ANALYZER_REVISION",
    "AnalyzerOutcome",
    "AnalysisResult",
    "CAPTURE_FILES",
    "EXPECTED_B_S1_REVISION",
    "EXPECTED_SOURCE_LEDGER",
    "PROTOCOL_REVISION",
    "SCHEMA_REVISION",
    "analyze_capture",
]
