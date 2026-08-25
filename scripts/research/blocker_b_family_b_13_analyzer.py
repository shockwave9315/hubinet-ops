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
from typing import Any, Mapping, Sequence


SCHEMA_REVISION = "family-b-13-capture-v2"
PROTOCOL_REVISION = "family-b-13-preexecution-v2"
EXPECTED_B_S1_REVISION = "B-S1-2B-f2fd1ddb442fb1e0202a7a0800a05c330b6ac9cc"
ANALYZER_REVISION = "family-b-13-analyzer-v2"
GENERATOR_CONTRACT_REVISION = "family-b-13-generator-contract-v1"
SUBRUN_CONTRACT_REVISION = "family-b-13-subrun-contract-v1"
MAX_JSONL_LINE_BYTES = 1_048_576

SUBRUN_PHENOMENA = {
    "13A": frozenset({"low_volume_enumeration"}),
    "13B": frozenset({"pagination_movement"}),
    "13C": frozenset({"active_archive_handoff"}),
    "13D": frozenset({"index_rotation"}),
    "13E": frozenset({"watch_overflow_or_invalidation"}),
    "13F": frozenset({"watch_scan_creation_race"}),
}
COMBINED_PHENOMENA = frozenset(
    {
        "pagination_movement",
        "active_archive_handoff",
        "index_rotation",
        "watch_scan_creation_race",
    }
)

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


def _decode_upid(upid: str) -> dict[str, str]:
    match = UPID_PATTERN.fullmatch(upid)
    if match is None:
        raise CaptureError("cannot_decode_normalized_upid")
    return {
        "node": match.group(1),
        "task_type": match.group(5),
        "task_id": match.group(6),
        "owner": match.group(7),
    }


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _analyzer_source_sha256() -> str:
    try:
        return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    except OSError as exc:
        raise EnvironmentError("analyzer_source_unreadable") from exc


def _validate_generator_contract(
    manifest: Mapping[str, Any], fixture_kind: str
) -> Mapping[str, Any]:
    try:
        contract = _require_mapping(
            manifest.get("generator_contract"), "generator_contract"
        )
        if contract.get("contract_revision") != GENERATOR_CONTRACT_REVISION:
            raise CaptureError("generator_contract_revision_mismatch")
        if contract.get("fixture_id") != manifest.get("fixture_id"):
            raise CaptureError("generator_contract_fixture_mismatch")
        if contract.get("subrun_id") != manifest.get("subrun_id"):
            raise CaptureError("generator_contract_subrun_mismatch")
        approval_state = _require_text(contract, "approval_state")
        expected_approval = "synthetic" if fixture_kind == "synthetic" else "approved"
        if approval_state != expected_approval:
            raise CaptureError("generator_contract_not_approved_for_fixture_kind")
        for field in (
            "approved_operation",
            "expected_task_type",
            "expected_node",
            "expected_owner",
        ):
            _require_text(contract, field)
        task_id_policy = _require_mapping(
            contract.get("expected_task_id_policy"),
            "generator_contract.expected_task_id_policy",
        )
        if task_id_policy.get("kind") != "exact" or not isinstance(
            task_id_policy.get("value"), str
        ):
            raise CaptureError("generator_task_id_policy_not_exact")
        if _require_int(contract, "maximum_operation_count") == 0:
            raise CaptureError("generator_operation_limit_zero")
        if _require_int(contract, "maximum_duration_seconds") == 0:
            raise CaptureError("generator_duration_limit_zero")
        return contract
    except CaptureError as exc:
        if fixture_kind == "disposable_pve":
            raise EnvironmentError(f"generator_contract_ineligible:{exc}") from exc
        raise


def _validate_subrun_contract(manifest: Mapping[str, Any]) -> Mapping[str, Any]:
    contract = _require_mapping(manifest.get("subrun_contract"), "subrun_contract")
    if contract.get("contract_revision") != SUBRUN_CONTRACT_REVISION:
        raise CaptureError("subrun_contract_revision_mismatch")
    subrun_id = _require_text(manifest, "subrun_id")
    if contract.get("subrun_id") != subrun_id:
        raise CaptureError("subrun_contract_id_mismatch")
    required = {
        _require_text({"value": value}, "value")
        for value in _require_sequence(
            contract.get("required_phenomena"), "subrun_contract.required_phenomena"
        )
    }
    if len(required) != len(contract["required_phenomena"]):
        raise CaptureError("subrun_contract_duplicate_phenomenon")
    if subrun_id in SUBRUN_PHENOMENA:
        if required != SUBRUN_PHENOMENA[subrun_id]:
            raise CaptureError("subrun_contract_required_phenomena_mismatch")
    elif subrun_id == "13G":
        if len(required) < 2 or not required.issubset(COMBINED_PHENOMENA):
            raise CaptureError("combined_subrun_contract_invalid")
    else:
        raise EnvironmentError("unsupported_subrun_id")
    evidence_ids = _require_mapping(
        contract.get("evidence_ids"), "subrun_contract.evidence_ids"
    )
    if set(evidence_ids) != required:
        raise CaptureError("subrun_contract_evidence_id_set_mismatch")
    for phenomenon, evidence_id in evidence_ids.items():
        if not isinstance(evidence_id, str) or not evidence_id:
            raise CaptureError(f"subrun_evidence_id_invalid:{phenomenon}")
    return contract


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
        "baseline_observation",
        "subrun_contract",
        "safety_limits",
        "capture_completeness",
        "candidate_close",
    ):
        _require_mapping(manifest.get(field), field)

    t0_monotonic_ns = _require_int(manifest, "t0_monotonic_ns")
    baseline_upids = {
        _require_upid(value, "baseline_upids")
        for value in _require_sequence(manifest.get("baseline_upids"), "baseline_upids")
    }
    if len(baseline_upids) != len(manifest["baseline_upids"]):
        raise CaptureError("duplicate_baseline_upid")
    baseline = _require_mapping(
        manifest["baseline_observation"], "baseline_observation"
    )
    baseline_start = _require_int(baseline, "capture_start_monotonic_ns")
    baseline_end = _require_int(baseline, "capture_end_monotonic_ns")
    baseline_commit = _require_int(baseline, "committed_at_monotonic_ns")
    if not (baseline_start <= baseline_end <= baseline_commit < t0_monotonic_ns):
        raise CaptureError("baseline_not_captured_and_committed_before_t0")
    if not _require_bool(baseline, "complete"):
        raise CaptureError("baseline_observation_incomplete")
    baseline_raw = _require_string(baseline, "raw_evidence")
    if _require_text(baseline, "sha256") != _sha256_text(baseline_raw):
        raise CaptureError("baseline_observation_hash_mismatch")
    try:
        baseline_raw_value = json.loads(baseline_raw)
    except json.JSONDecodeError as exc:
        raise CaptureError("baseline_raw_evidence_invalid_json") from exc
    baseline_raw_upids = {
        _require_upid(value, "baseline_observation.raw_evidence")
        for value in _require_sequence(
            baseline_raw_value, "baseline_observation.raw_evidence"
        )
    }
    baseline_declared_upids = {
        _require_upid(value, "baseline_observation.normalized_upids")
        for value in _require_sequence(
            baseline.get("normalized_upids"), "baseline_observation.normalized_upids"
        )
    }
    if (
        len(baseline_raw_upids) != len(baseline_raw_value)
        or len(baseline_declared_upids) != len(baseline["normalized_upids"])
        or baseline_raw_upids != baseline_declared_upids
        or baseline_declared_upids != baseline_upids
    ):
        raise CaptureError("baseline_observation_set_mismatch")

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
    _require_text(generator, "process_identity")
    if generator.get("ground_truth_source") != "operation_initiator_return_value":
        raise CaptureError("ground_truth_not_independent")
    _validate_generator_contract(manifest, fixture_kind)
    _validate_subrun_contract(manifest)
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
    analyzer_source_sha256 = _require_text(seal, "analyzer_source_sha256")
    if re.fullmatch(r"[0-9a-f]{64}", analyzer_source_sha256) is None:
        raise CaptureError("analyzer_source_hash_not_sha256")
    if analyzer_source_sha256 != _analyzer_source_sha256():
        raise EnvironmentError("analyzer_source_hash_mismatch")
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
        "analyzer_source_sha256": analyzer_source_sha256,
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
    fixture_kind = _require_text(manifest, "fixture_kind")
    generator_contract = _validate_generator_contract(manifest, fixture_kind)
    close = _require_mapping(manifest["candidate_close"], "candidate_close")
    t0_monotonic_ns = _require_int(manifest, "t0_monotonic_ns")
    t1_monotonic_ns = _require_int(close, "monotonic_ns")
    if t1_monotonic_ns <= t0_monotonic_ns:
        raise CaptureError("candidate_close_not_after_t0")
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
    if total_operations > _require_int(
        generator_contract, "maximum_operation_count"
    ):
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
        if end["operation"] != generator_contract["approved_operation"]:
            raise EnvironmentError("generated_operation_outside_approved_contract")
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
        decoded = _decode_upid(upid)
        if (
            decoded["task_type"] != expected_task_type
            or decoded["task_id"] != expected_task_id
        ):
            raise CaptureError("ground_truth_expected_task_does_not_match_upid")
        task_id_policy = _require_mapping(
            generator_contract["expected_task_id_policy"],
            "generator_contract.expected_task_id_policy",
        )
        if (
            expected_task_type != generator_contract["expected_task_type"]
            or expected_task_id != task_id_policy["value"]
            or decoded["node"] != generator_contract["expected_node"]
            or decoded["owner"] != generator_contract["expected_owner"]
        ):
            raise EnvironmentError("generated_upid_outside_approved_contract")
        relation = end.get("boundary_relation")
        if relation not in {"before_t1", "after_t1", "ambiguous"}:
            raise CaptureError("ground_truth_boundary_relation_invalid")
        declared_within_scope = _require_bool(end, "within_scope")
        request_end_monotonic_ns = _require_int(end, "monotonic_ns")
        if request_end_monotonic_ns < start["monotonic_ns"]:
            raise CaptureError("ground_truth_request_time_reversed")
        if relation == "before_t1":
            derived_within_scope = (
                t0_monotonic_ns <= start["monotonic_ns"]
                and request_end_monotonic_ns <= t1_monotonic_ns
            )
            if not derived_within_scope:
                raise CaptureError("before_t1_timing_inconsistent")
        elif relation == "after_t1":
            derived_within_scope = False
            if start["monotonic_ns"] < t1_monotonic_ns:
                raise CaptureError("after_t1_timing_inconsistent")
        else:
            derived_within_scope = False
        if relation != "ambiguous" and declared_within_scope != derived_within_scope:
            raise CaptureError("ground_truth_scope_declaration_mismatch")
        operations.append(
            {
                "sequence": sequence,
                "request_id": end["request_id"],
                "operation": end["operation"],
                "upid": upid,
                "expected_task_type": end["expected_task_type"],
                "expected_task_id": end["expected_task_id"],
                "boundary_relation": relation,
                "within_scope": derived_within_scope,
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
        _require_int(generator_contract, "maximum_duration_seconds") * 1_000_000_000
    )
    if operation_span_ns > maximum_duration_ns:
        raise CaptureError("ground_truth_duration_limit_exceeded")
    return operations


def _validate_raw_result(
    record: Mapping[str, Any], field: str
) -> Mapping[str, Any]:
    result = _require_mapping(record.get(field), f"exact.{field}")
    available = _require_bool(result, "available")
    raw_evidence = _require_string(result, "raw_evidence")
    evidence_hash = _require_text(result, "sha256")
    if evidence_hash != _sha256_text(raw_evidence):
        raise CaptureError(f"exact_{field}_hash_mismatch")
    if available != bool(raw_evidence):
        raise CaptureError(f"exact_{field}_availability_inconsistent")
    return result


def _stat_identity(record: Mapping[str, Any], field: str) -> tuple[int, int]:
    stat = _require_mapping(record.get("stat"), field)
    return (_require_int(stat, "device"), _require_int(stat, "inode"))


def _validate_subrun_obligations(
    manifest: Mapping[str, Any],
    expected_upids: set[str],
    enumerated_known: set[str],
    watches: Mapping[int, Mapping[str, Any]],
    scans: Mapping[int, Mapping[str, Any]],
    surfaces: Mapping[int, Mapping[str, Any]],
    api_pages: Sequence[Mapping[str, Any]],
    harness: Sequence[Mapping[str, Any]],
) -> None:
    contract = _validate_subrun_contract(manifest)
    required = set(contract["required_phenomena"])
    evidence_ids = _require_mapping(contract["evidence_ids"], "evidence_ids")

    if "low_volume_enumeration" in required and not (expected_upids or enumerated_known):
        raise CaptureError("subrun_13a_enumeration_not_exercised")

    if "pagination_movement" in required:
        evidence_id = str(evidence_ids["pagination_movement"])
        pages = [page for page in api_pages if page.get("phenomenon_id") == evidence_id]
        page_sequences = [_require_int(page, "page_sequence") for page in pages]
        page_starts = [
            _require_int(page, "request_start_monotonic_ns") for page in pages
        ]
        if (
            len(pages) < 2
            or page_sequences != list(range(1, len(pages) + 1))
            or page_starts != sorted(page_starts)
            or len({_require_int(page, "start_offset") for page in pages}) < 2
            or len({_require_text(page, "source") for page in pages}) != 1
        ):
            raise CaptureError("subrun_pagination_movement_not_evidenced")

    if "watch_scan_creation_race" in required:
        evidence_id = str(evidence_ids["watch_scan_creation_race"])
        markers = [
            record
            for record in harness
            if record.get("event") == "scheduled_interleaving"
            and record.get("phenomenon_id") == evidence_id
            and record.get("kind") == "watch_scan_creation_race"
        ]
        if len(markers) != 1:
            raise CaptureError("subrun_watch_scan_race_marker_missing")
        marker = markers[0]
        target = _require_upid(marker.get("target_upid"), "race.target_upid")
        scan = scans.get(_require_int(marker, "scan_sequence"))
        watch = watches.get(_require_int(marker, "watcher_sequence"))
        if scan is None or watch is None:
            raise CaptureError("subrun_watch_scan_race_reference_missing")
        scan_set = set(scan["_normalized_upids"])
        watch_time = _require_int(watch, "monotonic_ns")
        marker_time = _require_int(marker, "monotonic_ns")
        if (
            target not in scan_set
            or watch.get("normalized_upid") != target
            or marker_time > _require_int(scan, "scan_start_monotonic_ns")
            or not (
                _require_int(scan, "scan_start_monotonic_ns")
                <= watch_time
                <= _require_int(scan, "scan_end_monotonic_ns")
            )
        ):
            raise CaptureError("subrun_watch_scan_race_not_evidenced")

    if "active_archive_handoff" in required:
        evidence_id = str(evidence_ids["active_archive_handoff"])
        markers = [
            record
            for record in harness
            if record.get("event") == "active_archive_handoff"
            and record.get("phenomenon_id") == evidence_id
        ]
        if len(markers) != 1:
            raise CaptureError("subrun_handoff_marker_missing")
        marker = markers[0]
        target = _require_upid(marker.get("target_upid"), "handoff.target_upid")
        active = surfaces.get(_require_int(marker, "active_observation_sequence"))
        archive = surfaces.get(_require_int(marker, "archive_observation_sequence"))
        if active is None or archive is None:
            raise CaptureError("subrun_handoff_reference_missing")
        if (
            active.get("source") != "active"
            or archive.get("source") not in {"index", "index.1"}
            or target not in set(active["_normalized_upids"])
            or target not in set(archive["_normalized_upids"])
            or _require_int(active, "capture_end_monotonic_ns")
            > _require_int(archive, "capture_start_monotonic_ns")
        ):
            raise CaptureError("subrun_handoff_not_evidenced")

    if "index_rotation" in required:
        evidence_id = str(evidence_ids["index_rotation"])
        markers = [
            record
            for record in harness
            if record.get("event") == "index_rotation"
            and record.get("phenomenon_id") == evidence_id
        ]
        if len(markers) != 1:
            raise CaptureError("subrun_rotation_marker_missing")
        marker = markers[0]
        before = surfaces.get(_require_int(marker, "before_index_sequence"))
        after = surfaces.get(_require_int(marker, "after_index_sequence"))
        rotated = surfaces.get(_require_int(marker, "after_index1_sequence"))
        watch = watches.get(_require_int(marker, "watcher_sequence"))
        if before is None or after is None or rotated is None or watch is None:
            raise CaptureError("subrun_rotation_reference_missing")
        watch_masks = set(watch["_masks"])
        before_end = _require_int(before, "capture_end_monotonic_ns")
        watch_time = _require_int(watch, "monotonic_ns")
        after_start = _require_int(after, "capture_start_monotonic_ns")
        rotated_start = _require_int(rotated, "capture_start_monotonic_ns")
        marker_time = _require_int(marker, "monotonic_ns")
        if (
            before.get("source") != "index"
            or after.get("source") != "index"
            or rotated.get("source") != "index.1"
            or before.get("sha256") != rotated.get("sha256")
            or _stat_identity(before, "rotation.before.stat")
            != _stat_identity(rotated, "rotation.index1.stat")
            or _stat_identity(before, "rotation.before.stat")
            == _stat_identity(after, "rotation.after.stat")
            or before.get("sha256") == after.get("sha256")
            or not watch_masks.intersection({"IN_MOVED_FROM", "IN_MOVED_TO"})
            or watch.get("filename") not in {"index", "index.1"}
            or watch.get("phenomenon_id") != evidence_id
            or not (before_end <= watch_time <= min(after_start, rotated_start))
            or marker_time
            < max(
                _require_int(after, "capture_end_monotonic_ns"),
                _require_int(rotated, "capture_end_monotonic_ns"),
            )
        ):
            raise CaptureError("subrun_index_rotation_not_evidenced")

    if "watch_overflow_or_invalidation" in required:
        evidence_id = str(evidence_ids["watch_overflow_or_invalidation"])
        signal_present = any(
            watch.get("phenomenon_id") == evidence_id
            and (
                watch.get("queue_overflow") is True
                or set(watch["_masks"]).intersection(
                    {
                        "IN_Q_OVERFLOW",
                        "IN_IGNORED",
                        "IN_UNMOUNT",
                        "IN_DELETE_SELF",
                        "IN_MOVE_SELF",
                    }
                )
                or watch.get("event_type") in {"watch_invalidation", "watch_loss"}
            )
            for watch in watches.values()
        )
        if not signal_present:
            raise CaptureError("subrun_watch_loss_signal_not_evidenced")


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
    t0_monotonic = _require_int(manifest, "t0_monotonic_ns")
    declared_known = {
        _require_upid(value, "candidate_close.known_upids")
        for value in _require_sequence(
            close.get("known_upids"), "candidate_close.known_upids"
        )
    }
    baseline_upids = {
        _require_upid(value, "baseline_upids")
        for value in _require_sequence(manifest.get("baseline_upids"), "baseline_upids")
    }
    generator_contract = _validate_generator_contract(
        manifest, _require_text(manifest, "fixture_kind")
    )
    gap_reasons: set[str] = set()

    harness = list(records["harness_events"])
    reader_context = _require_mapping(manifest["reader_context"], "reader_context")
    reader_identity = _require_text(reader_context, "process_identity")
    harness_sequences: list[int] = []
    kinds: list[str] = []
    for record in harness:
        harness_sequences.append(_require_int(record, "harness_sequence"))
        event = _require_text(record, "event")
        kinds.append(event)
        _require_int(record, "monotonic_ns")
        if record.get("process_identity") != reader_identity:
            raise CaptureError("harness_reader_process_identity_mismatch")
    if harness_sequences != list(range(1, len(harness) + 1)):
        raise CaptureError("harness_sequence_not_contiguous_or_ordered")
    if kinds.count("process_start") != 1 or kinds.count("process_stop") != 1:
        raise CaptureError("harness_process_boundary_missing_or_duplicate")
    if kinds.count("capture_finalized") != 1:
        raise CaptureError("harness_capture_finalization_missing_or_duplicate")
    starts = [record for record in harness if record["event"] == "process_start"]
    stops = [record for record in harness if record["event"] == "process_stop"]
    finalizers = [record for record in harness if record["event"] == "capture_finalized"]
    process_start = _require_int(starts[0], "monotonic_ns")
    process_stop = _require_int(stops[0], "monotonic_ns")
    capture_finalized = _require_int(finalizers[0], "monotonic_ns")
    if not (
        _require_int(starts[0], "harness_sequence")
        < _require_int(finalizers[0], "harness_sequence")
        < _require_int(stops[0], "harness_sequence")
        and process_start
        <= t0_monotonic
        < close_monotonic
        < capture_finalized
        <= process_stop
    ):
        raise CaptureError("harness_process_close_finalization_order_invalid")
    if not _require_bool(finalizers[0], "complete"):
        raise CaptureError("harness_capture_finalization_false")
    if any(record["event"] == "process_crash" for record in harness):
        raise CaptureError("harness_process_crash")
    version_events = [record for record in harness if record["event"] == "analyzer_version"]
    if (
        len(version_events) != 1
        or version_events[0].get("analyzer_revision") != ANALYZER_REVISION
        or not (
            process_start
            <= _require_int(version_events[0], "monotonic_ns")
            <= close_monotonic
        )
    ):
        raise CaptureError("harness_analyzer_version_missing_or_mismatch")
    heartbeats = [record for record in harness if record["event"] == "heartbeat"]
    heartbeat_sequences = [_require_int(record, "heartbeat_sequence") for record in heartbeats]
    if heartbeat_sequences != list(range(1, len(heartbeats) + 1)):
        raise CaptureError("heartbeat_sequence_not_contiguous_or_ordered")
    heartbeat_times = [_require_int(record, "monotonic_ns") for record in heartbeats]
    if heartbeat_times != sorted(heartbeat_times) or len(set(heartbeat_times)) != len(
        heartbeat_times
    ):
        raise CaptureError("heartbeat_time_not_strictly_ordered")
    relevant_heartbeats = [
        record
        for record in heartbeats
        if process_start <= _require_int(record, "monotonic_ns") <= close_monotonic
    ]
    if not relevant_heartbeats:
        raise CaptureError("harness_heartbeat_missing")
    for heartbeat in relevant_heartbeats:
        if not _require_bool(heartbeat, "healthy"):
            gap_reasons.add("observer_unhealthy_heartbeat")
    last_heartbeat = relevant_heartbeats[-1]
    timeout = _require_int(reader_context, "heartbeat_timeout_ns")
    if (
        not last_heartbeat["healthy"]
        or close_monotonic - _require_int(last_heartbeat, "monotonic_ns") > timeout
    ):
        gap_reasons.add("observer_unhealthy_or_stale_at_close")

    watch_known: set[str] = set()
    watch_deleted: set[str] = set()
    watches: dict[int, dict[str, Any]] = {}
    for source_record in records["watch_events"]:
        record = dict(source_record)
        watcher_sequence = _require_int(record, "watcher_sequence")
        if watcher_sequence == 0 or watcher_sequence in watches:
            raise CaptureError("watcher_sequence_zero_or_duplicate")
        _require_signed_int(record, "watch_descriptor")
        _require_int(record, "cookie")
        _require_int(record, "raw_mask")
        _require_int(record, "raw_order")
        _require_string(record, "watched_path")
        _require_string(record, "filename")
        overflow = _require_bool(record, "queue_overflow")
        event_time = _require_int(record, "monotonic_ns")
        _require_text(record, "wall_timestamp")
        event_type = _require_text(record, "event_type")
        masks = {
            _require_text({"value": value}, "value")
            for value in _require_sequence(record.get("mask"), "watch.mask")
        }
        record["_masks"] = masks
        watches[watcher_sequence] = record
        relevant = event_time <= close_monotonic
        if relevant and (overflow or "IN_Q_OVERFLOW" in masks):
            gap_reasons.add("watch_queue_overflow")
        if relevant and (
            event_type in {"watch_invalidation", "watch_loss"}
            or masks.intersection(
                {"IN_IGNORED", "IN_UNMOUNT", "IN_DELETE_SELF", "IN_MOVE_SELF"}
            )
        ):
            gap_reasons.add("watch_invalidation_or_loss")
        upid_value = record.get("normalized_upid")
        if upid_value is not None:
            upid = _require_upid(upid_value, "watch.normalized_upid")
            if relevant and masks.intersection(
                {"IN_CREATE", "IN_MOVED_TO", "IN_CLOSE_WRITE"}
            ):
                watch_known.add(upid)
            if relevant and masks.intersection({"IN_DELETE", "IN_MOVED_FROM"}):
                watch_deleted.add(upid)
    if watches and set(watches) != set(range(1, max(watches) + 1)):
        raise CaptureError("watcher_sequence_gap")
    watcher_times = [
        _require_int(watches[sequence], "monotonic_ns") for sequence in sorted(watches)
    ]
    if watcher_times != sorted(watcher_times):
        raise CaptureError("watcher_sequence_time_reversed")

    scan_known: set[str] = set()
    prior_scan: set[str] | None = None
    scans: dict[int, dict[str, Any]] = {}
    prior_watermark = 0
    for source_record in records["scan_rounds"]:
        record = dict(source_record)
        sequence = _require_int(record, "scan_sequence")
        if sequence == 0 or sequence in scans:
            raise CaptureError("scan_sequence_zero_or_duplicate")
        _require_text(record, "round_id")
        scan_start = _require_int(record, "scan_start_monotonic_ns")
        scan_end = _require_int(record, "scan_end_monotonic_ns")
        if scan_end < scan_start or scan_end > close_monotonic:
            raise CaptureError("scan_time_invalid_for_close")
        current = {
            _require_upid(value, "scan.exact_normalized_upids")
            for value in _require_sequence(
                record.get("exact_normalized_upids"), "scan.exact_normalized_upids"
            )
        }
        record["_normalized_upids"] = current
        for value in _require_sequence(record.get("bucket_set"), "scan.bucket_set"):
            if not isinstance(value, str):
                raise CaptureError("scan_bucket_not_string")
        _require_mapping(record.get("stat_metadata"), "scan.stat_metadata")
        unreadable = _require_sequence(
            record.get("unreadable_entries"), "scan.unreadable_entries"
        )
        malformed = _require_sequence(
            record.get("malformed_entries"), "scan.malformed_entries"
        )
        complete = _require_bool(record, "complete")
        watermark = _require_int(record, "watch_drained_through_sequence")
        if watermark < prior_watermark or (watches and watermark > max(watches)):
            raise CaptureError("scan_watch_drain_watermark_invalid")
        if watermark and _require_int(watches[watermark], "monotonic_ns") > scan_end:
            raise CaptureError("scan_watch_drain_watermark_after_scan_end")
        prior_watermark = watermark
        if unreadable or malformed or not complete or record.get("consistency_marker") == "inconsistent":
            gap_reasons.add("scan_unreadable_malformed_or_inconsistent")
        if prior_scan is not None and prior_scan - current:
            gap_reasons.add("exact_log_disappeared_between_scans")
        prior_scan = current
        scan_known.update(current)
        scans[sequence] = record
    if set(scans) != set(range(1, len(scans) + 1)) or len(scans) < 2:
        raise CaptureError("scan_sequence_not_contiguous_or_too_short")
    terminal_scans = [scans[len(scans) - 1], scans[len(scans)]]
    if not all(scan["complete"] for scan in terminal_scans):
        gap_reasons.add("terminal_scan_incomplete")
    if terminal_scans[0]["_normalized_upids"] != terminal_scans[1]["_normalized_upids"]:
        gap_reasons.add("terminal_scan_fixed_point_not_reached")
    relevant_watch_sequences = {
        sequence
        for sequence, watch in watches.items()
        if _require_int(watch, "monotonic_ns") <= close_monotonic
    }
    terminal_watermark = _require_int(
        terminal_scans[1], "watch_drained_through_sequence"
    )
    if relevant_watch_sequences and terminal_watermark < max(relevant_watch_sequences):
        gap_reasons.add("watch_events_undrained_at_terminal_scan")
    if baseline_upids - set(terminal_scans[1]["_normalized_upids"]):
        gap_reasons.add("baseline_exact_log_missing_at_close")

    required_surfaces = {"active", "index", "index.1"}
    seen_surfaces: set[str] = set()
    surface_known: set[str] = set()
    surfaces: dict[int, dict[str, Any]] = {}
    for source_record in records["surface_observations"]:
        record = dict(source_record)
        sequence = _require_int(record, "observation_sequence")
        if sequence == 0 or sequence in surfaces:
            raise CaptureError("surface_observation_sequence_zero_or_duplicate")
        source = _require_text(record, "source")
        if source not in required_surfaces:
            raise CaptureError(f"unknown_surface_source:{source}")
        seen_surfaces.add(source)
        capture_start = _require_int(record, "capture_start_monotonic_ns")
        capture_end = _require_int(record, "capture_end_monotonic_ns")
        if capture_end < capture_start or capture_end > close_monotonic:
            raise CaptureError("surface_capture_time_invalid_for_close")
        normalized = {
            _require_upid(value, "surface.normalized_upids")
            for value in _require_sequence(
                record.get("normalized_upids"), "surface.normalized_upids"
            )
        }
        record["_normalized_upids"] = normalized
        raw_evidence = _require_string(record, "raw_evidence")
        _require_mapping(record.get("stat"), "surface.stat")
        surface_hash = _require_text(record, "sha256")
        if surface_hash != _sha256_text(raw_evidence):
            raise CaptureError(f"surface_hash_mismatch:{source}:{sequence}")
        readable = _require_bool(record, "readable")
        complete = _require_bool(record, "complete")
        if not readable or not complete:
            gap_reasons.add(f"surface_incomplete:{source}")
        surface_known.update(normalized)
        surfaces[sequence] = record
    if set(surfaces) != set(range(1, len(surfaces) + 1)):
        raise CaptureError("surface_observation_sequence_gap")
    if seen_surfaces != required_surfaces:
        raise CaptureError("required_surface_observation_missing")

    api_seen: set[str] = set()
    api_sources: set[str] = set()
    api_request_ids: set[str] = set()
    api_records: list[Mapping[str, Any]] = []
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
        if response_end < request_start or response_end > close_monotonic:
            raise CaptureError("api_response_time_invalid_for_close")
        restart_reason = record.get("restart_reason")
        if restart_reason is not None and not isinstance(restart_reason, str):
            raise CaptureError("api_restart_reason_not_string_or_null")
        api_seen.update(
            _require_upid(value, "api.normalized_upids")
            for value in _require_sequence(
                record.get("normalized_upids"), "api.normalized_upids"
            )
        )
        if not _require_bool(record, "complete_response"):
            gap_reasons.add("api_response_incomplete")
        api_records.append(record)
    if api_sources != {"active", "archive", "all"}:
        raise CaptureError("required_api_profile_missing")

    enumerated_known = watch_known | scan_known | surface_known
    exact_confirmed: set[str] = set()
    exact_final: set[str] = set()
    for record in records["exact_upid"]:
        upid = _require_upid(record.get("known_upid"), "exact.known_upid")
        capture_start = _require_int(record, "capture_start_monotonic_ns")
        capture_end = _require_int(record, "capture_end_monotonic_ns")
        if capture_end < capture_start or capture_end > close_monotonic:
            raise CaptureError("exact_upid_capture_time_invalid_for_close")
        discovery_source = _require_text(record, "discovery_source")
        discovery_reference = record.get("discovery_reference")
        provenance_valid = False
        if discovery_source == "baseline":
            provenance_valid = (
                discovery_reference == "manifest.baseline_upids" and upid in baseline_upids
            )
        elif discovery_source == "watch" and isinstance(discovery_reference, int):
            watch = watches.get(discovery_reference)
            provenance_valid = bool(
                watch
                and watch.get("normalized_upid") == upid
                and set(watch["_masks"]).intersection(
                    {"IN_CREATE", "IN_MOVED_TO", "IN_CLOSE_WRITE"}
                )
                and _require_int(watch, "monotonic_ns") <= capture_start
            )
        elif discovery_source == "scan" and isinstance(discovery_reference, int):
            scan = scans.get(discovery_reference)
            provenance_valid = bool(
                scan
                and upid in set(scan["_normalized_upids"])
                and _require_int(scan, "scan_end_monotonic_ns") <= capture_start
            )
        elif discovery_source in {"active", "archive"} and isinstance(
            discovery_reference, int
        ):
            surface = surfaces.get(discovery_reference)
            expected_sources = {"active"} if discovery_source == "active" else {"index", "index.1"}
            provenance_valid = bool(
                surface
                and surface.get("source") in expected_sources
                and upid in set(surface["_normalized_upids"])
                and _require_int(surface, "capture_end_monotonic_ns") <= capture_start
            )
        if not provenance_valid or upid not in (enumerated_known | baseline_upids):
            raise CaptureError("exact_upid_without_valid_discovery_provenance")
        if not _require_bool(record, "previously_known"):
            raise CaptureError("exact_upid_not_marked_previously_known")
        presence = _require_bool(record, "presence")
        readable = _require_bool(record, "readable")
        status_result = _validate_raw_result(record, "status_result")
        log_result = _validate_raw_result(record, "log_result")
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
        if not presence or not readable:
            gap_reasons.add("known_exact_upid_lost")
            continue
        if not status_result["available"] or not log_result["available"]:
            gap_reasons.add("exact_upid_raw_result_unavailable")
            continue
        exact_confirmed.add(upid)
        task_state = _require_text(status_result, "task_state")
        terminal_status = _require_text(log_result, "terminal_status")
        status_raw = _require_string(status_result, "raw_evidence").strip()
        log_lines = [
            line.strip()
            for line in _require_string(log_result, "raw_evidence").splitlines()
            if line.strip()
        ]
        if status_raw != task_state or not log_lines or log_lines[-1] != terminal_status:
            raise CaptureError("exact_upid_parsed_result_mismatches_raw_evidence")
        interpretation_consistent = (
            final_interpretation == "ok"
            and task_state == "stopped"
            and terminal_status == "TASK OK"
        ) or (
            final_interpretation == "warning"
            and task_state == "stopped"
            and terminal_status.startswith("WARNINGS:")
        ) or (
            final_interpretation == "error"
            and task_state == "stopped"
            and terminal_status not in {"", "TASK OK"}
            and not terminal_status.startswith("WARNINGS:")
        ) or final_interpretation in {"not_final", "unknown"}
        if not interpretation_consistent:
            raise CaptureError("exact_upid_final_interpretation_inconsistent")
        if final_interpretation in {"ok", "warning", "error"}:
            exact_final.add(upid)

    in_scope = [operation for operation in operations if operation["within_scope"]]
    expected_upids = {str(operation["upid"]) for operation in in_scope}
    ambiguous = {
        str(operation["upid"])
        for operation in operations
        if operation["boundary_relation"] == "ambiguous"
    }
    if ambiguous:
        gap_reasons.add("t1_boundary_ordering_ambiguous")
    post_t0_observed = enumerated_known - baseline_upids
    unexpected = post_t0_observed - expected_upids
    if unexpected:
        gap_reasons.add("unexpected_post_t0_upid")
    for upid in post_t0_observed:
        decoded = _decode_upid(upid)
        task_id_policy = _require_mapping(
            generator_contract["expected_task_id_policy"],
            "generator_contract.expected_task_id_policy",
        )
        if decoded["node"] != generator_contract["expected_node"]:
            gap_reasons.add("unexpected_post_t0_node")
        if decoded["owner"] != generator_contract["expected_owner"]:
            gap_reasons.add("unexpected_post_t0_owner")
        if decoded["task_type"] != generator_contract["expected_task_type"]:
            gap_reasons.add("unexpected_post_t0_task_type")
        if decoded["task_id"] != task_id_policy["value"]:
            gap_reasons.add("unexpected_post_t0_task_id")
    if (api_seen - baseline_upids) - expected_upids:
        gap_reasons.add("unexpected_api_corroboration_upid")
    if declared_known - post_t0_observed:
        raise CaptureError("candidate_declares_unenumerated_post_t0_upid")
    if post_t0_observed - declared_known:
        gap_reasons.add("candidate_close_omits_observed_post_t0_upid")
    if expected_upids & watch_deleted:
        gap_reasons.add("ground_truth_exact_log_deleted")
    if (expected_upids & enumerated_known) - exact_confirmed:
        gap_reasons.add("ground_truth_exact_confirmation_missing")
    if (expected_upids & enumerated_known) - exact_final:
        gap_reasons.add("ground_truth_final_status_unreconciled")

    for record in harness:
        if record.get("event") == "gap_signal":
            gap_reasons.add(_require_text(record, "reason"))

    _validate_subrun_obligations(
        manifest,
        expected_upids,
        enumerated_known,
        watches,
        scans,
        surfaces,
        api_records,
        harness,
    )

    missing = expected_upids - enumerated_known
    if gap_reasons or close_state == "GAP_LATCHED":
        return AnalysisResult(
            AnalyzerOutcome.GAP,
            tuple(sorted(gap_reasons or {"candidate_gap"})),
        )

    if close_state == "CLOSED_COMPLETE" and missing:
        operation_by_upid = {
            str(operation["upid"]): operation for operation in in_scope
        }
        missing_upid = sorted(missing)[0]
        operation = operation_by_upid[missing_upid]
        witness = {
            "classification": "B-S1 NO-GO FOR THE TESTED EXACT SCOPE",
            "ground_truth_upid": missing_upid,
            "ground_truth_generator_sequence": operation["sequence"],
            "operation": operation["operation"],
            "within_declared_scope": True,
            "omitted_from_b_s1_enumeration_set": True,
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
    if expected_upids != declared_known:
        raise CaptureError("candidate_close_known_set_not_exact_ground_truth_scope")
    if expected_upids - exact_final:
        raise CaptureError("exact_final_status_set_incomplete")

    # API pages remain corroborative.  They can expose a gap, but never add an
    # otherwise unknown UPID to the completeness-bearing enumeration set.
    return AnalysisResult(
        AnalyzerOutcome.PASS,
        ("ground_truth_equals_reconciled_enumeration_set_for_tested_interleaving",),
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
    "GENERATOR_CONTRACT_REVISION",
    "PROTOCOL_REVISION",
    "SCHEMA_REVISION",
    "SUBRUN_CONTRACT_REVISION",
    "analyze_capture",
]
