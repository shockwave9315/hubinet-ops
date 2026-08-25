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


SCHEMA_REVISION = "family-b-13-capture-v4"
PROTOCOL_REVISION = "family-b-13-preexecution-v4"
EXPECTED_B_S1_REVISION = "B-S1-2B-f2fd1ddb442fb1e0202a7a0800a05c330b6ac9cc"
ANALYZER_REVISION = "family-b-13-analyzer-v4"
GENERATOR_CONTRACT_REVISION = "family-b-13-generator-contract-v1"
SUBRUN_CONTRACT_REVISION = "family-b-13-subrun-contract-v1"
CLOCK_CONTRACT_REVISION = "family-b-13-clock-contract-v1"
MAX_JSONL_LINE_BYTES = 1_048_576

# Linux inotify event-mask values from the UAPI contract in
# include/uapi/linux/inotify.h.  Only event-output bits used by this bounded
# research protocol are accepted; watch-configuration flags are not event
# evidence and unknown bits fail closed.
INOTIFY_EVENT_MASKS = {
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
INOTIFY_KNOWN_RAW_MASK = sum(INOTIFY_EVENT_MASKS.values())

CLOCK_PARTICIPANTS = frozenset(
    {
        "manifest_boundaries",
        "reader",
        "generator",
        "pre_t0",
        "watch",
        "scan",
        "surface",
        "api",
        "exact",
        "harness",
    }
)

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
    "pre_t0_establishment": "pre-t0-establishment.jsonl",
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
    ENUMERATION_WITNESS = "GENERATOR_WINDOW_ENUMERATION_OMISSION_WITNESS"
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


class CaptureEnvironmentError(ValueError):
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


def _decode_inotify_raw_mask(raw_mask: int, context: str) -> frozenset[str]:
    unknown_bits = raw_mask & ~INOTIFY_KNOWN_RAW_MASK
    if unknown_bits:
        raise CaptureError(
            f"inotify_raw_mask_unknown_bits:{context}:0x{unknown_bits:08x}"
        )
    return frozenset(
        name for name, value in INOTIFY_EVENT_MASKS.items() if raw_mask & value
    )


def _validated_inotify_masks(
    record: Mapping[str, Any], context: str
) -> frozenset[str]:
    raw_mask = _require_int(record, "raw_mask")
    decoded = _decode_inotify_raw_mask(raw_mask, context)
    declared_values = _require_sequence(record.get("mask"), f"{context}.mask")
    declared = {
        _require_text({"value": value}, "value") for value in declared_values
    }
    if len(declared) != len(declared_values):
        raise CaptureError(f"inotify_declared_mask_duplicate:{context}")
    if declared != decoded:
        raise CaptureError(f"inotify_mask_mismatch_raw_mask:{context}")
    return decoded


def _parse_surface_raw_evidence(source: str, raw_evidence: str) -> frozenset[str]:
    """Parse the exact v4 UTF-8 serialization of one local PVE task surface."""

    if source == "active":
        line_pattern = re.compile(
            r"^(?P<upid>\S+) [01](?: [0-9A-F]{8}"
            r"(?: [^ \t\r\n][^\r\n]*)?)?\n$"
        )
    elif source in {"index", "index.1"}:
        line_pattern = re.compile(
            r"^(?P<upid>\S+) [0-9A-F]{8} [^ \t\r\n][^\r\n]*\n$"
        )
    else:
        raise CaptureError(f"surface_raw_evidence_unknown_source:{source}")

    parsed: set[str] = set()
    for line in raw_evidence.splitlines(keepends=True):
        match = line_pattern.fullmatch(line)
        if match is None:
            raise CaptureError(f"surface_raw_evidence_malformed:{source}")
        upid = _require_upid(match.group("upid"), f"surface.raw_evidence:{source}")
        if upid in parsed:
            raise CaptureError(f"surface_raw_evidence_duplicate_upid:{source}")
        parsed.add(upid)
    if raw_evidence and not raw_evidence.endswith("\n"):
        raise CaptureError(f"surface_raw_evidence_malformed:{source}")
    return frozenset(parsed)


def _analyzer_source_sha256() -> str:
    try:
        return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    except OSError as exc:
        raise CaptureEnvironmentError("analyzer_source_unreadable") from exc


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
            raise CaptureEnvironmentError(
                f"generator_contract_ineligible:{exc}"
            ) from exc
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
        raise CaptureEnvironmentError("unsupported_subrun_id")
    evidence_ids = _require_mapping(
        contract.get("evidence_ids"), "subrun_contract.evidence_ids"
    )
    if set(evidence_ids) != required:
        raise CaptureError("subrun_contract_evidence_id_set_mismatch")
    for phenomenon, evidence_id in evidence_ids.items():
        if not isinstance(evidence_id, str) or not evidence_id:
            raise CaptureError(f"subrun_evidence_id_invalid:{phenomenon}")
    return contract


def _validate_clock_contract(
    manifest: Mapping[str, Any], fixture_kind: str
) -> Mapping[str, Any]:
    """Require one explicitly bound CLOCK_MONOTONIC domain for every plane."""

    try:
        contract = _require_mapping(manifest.get("clock_contract"), "clock_contract")
        if contract.get("contract_revision") != CLOCK_CONTRACT_REVISION:
            raise CaptureError("clock_contract_revision_mismatch")
        if contract.get("clock_kind") != "CLOCK_MONOTONIC":
            raise CaptureError("clock_contract_kind_mismatch")
        domain_id = _require_text(contract, "clock_domain_id")
        if contract.get("boot_id") != manifest.get("boot_id"):
            raise CaptureError("clock_contract_boot_id_mismatch")
        if contract.get("fixture_id") != manifest.get("fixture_id"):
            raise CaptureError("clock_contract_fixture_id_mismatch")
        if contract.get("node_identity") != manifest.get("node_identity"):
            raise CaptureError("clock_contract_node_identity_mismatch")
        _require_text(contract, "time_namespace_id")
        expected_correlation = (
            "synthetic_single_shared_domain"
            if fixture_kind == "synthetic"
            else "verified_single_shared_domain"
        )
        if contract.get("correlation_state") != expected_correlation:
            raise CaptureError("clock_contract_correlation_state_mismatch")
        participants = _require_mapping(
            contract.get("participant_clock_domain_ids"),
            "clock_contract.participant_clock_domain_ids",
        )
        if set(participants) != CLOCK_PARTICIPANTS:
            raise CaptureError("clock_contract_participant_set_mismatch")
        for participant, participant_domain in participants.items():
            if participant_domain != domain_id:
                raise CaptureError(f"clock_domain_mismatch:{participant}")
        return contract
    except CaptureError as exc:
        # Cross-process monotonic comparisons are load-bearing throughout v4.
        # A missing or mismatched contract is therefore an environment
        # eligibility failure, including for synthetic captures.
        raise CaptureEnvironmentError(f"clock_contract_ineligible:{exc}") from exc


def _validate_manifest(manifest: Mapping[str, Any]) -> None:
    exact_values = {
        "schema_revision": SCHEMA_REVISION,
        "experiment_id": "family-b-13",
        "protocol_revision": PROTOCOL_REVISION,
        "expected_b_s1_revision": EXPECTED_B_S1_REVISION,
    }
    for field, expected in exact_values.items():
        if manifest.get(field) != expected:
            raise CaptureEnvironmentError(f"context_mismatch:{field}")

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
        raise CaptureEnvironmentError("ct112_is_not_a_family_b_fixture")
    if fixture_kind not in {"synthetic", "disposable_pve"}:
        raise CaptureEnvironmentError("unsupported_fixture_kind")
    if fixture_kind == "disposable_pve" and "placeholder" in fixture_id.lower():
        raise CaptureEnvironmentError("fixture_identity_is_placeholder")

    # Establish one shared monotonic domain before validating any timestamp
    # relation emitted by different capture participants.
    _validate_clock_contract(manifest, fixture_kind)

    for field in (
        "node_identity",
        "kernel_context",
        "filesystem_context",
        "reader_context",
        "generator_context",
        "experiment_generator_window",
        "baseline_observation",
        "t0_quiescence",
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
        "pre_t0_establishment_complete",
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
            raise CaptureEnvironmentError(f"source_version_mismatch:{component}")
    if manifest.get("loaded_code_status") != "exact_context_matched":
        raise CaptureEnvironmentError("loaded_code_context_not_matched")


def _validate_seal(root: Path, seal: Mapping[str, Any]) -> None:
    if seal.get("schema_revision") != SCHEMA_REVISION:
        raise CaptureError("seal_schema_mismatch")
    if seal.get("analyzer_revision") != ANALYZER_REVISION:
        raise CaptureEnvironmentError("analyzer_revision_mismatch")
    _require_text(seal, "analyzer_commit")
    analyzer_source_sha256 = _require_text(seal, "analyzer_source_sha256")
    if re.fullmatch(r"[0-9a-f]{64}", analyzer_source_sha256) is None:
        raise CaptureError("analyzer_source_hash_not_sha256")
    if analyzer_source_sha256 != _analyzer_source_sha256():
        raise CaptureEnvironmentError("analyzer_source_hash_mismatch")
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
    generator_window = _require_mapping(
        manifest["experiment_generator_window"], "experiment_generator_window"
    )
    generator_window_start = _require_int(generator_window, "start_monotonic_ns")
    generator_window_end = _require_int(generator_window, "end_monotonic_ns")
    if t1_monotonic_ns <= t0_monotonic_ns:
        raise CaptureError("candidate_close_not_after_t0")
    if not (
        t0_monotonic_ns
        <= generator_window_start
        < generator_window_end
        <= t1_monotonic_ns
    ):
        raise CaptureError("experiment_generator_window_outside_candidate_interval")
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
    subrun_id = _require_text(manifest, "subrun_id")
    if total_operations == 0 and subrun_id != "13E":
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
            raise CaptureEnvironmentError(
                "generated_operation_outside_approved_contract"
            )
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
            raise CaptureEnvironmentError("generated_upid_outside_approved_contract")
        relation = end.get("generator_window_relation")
        if relation not in {
            "inside_generator_window",
            "after_generator_window",
            "ambiguous",
        }:
            raise CaptureError("ground_truth_generator_window_relation_invalid")
        declared_generator_membership = _require_bool(
            end, "within_generator_window"
        )
        body_start_membership = _require_text(
            end, "b_s1_body_start_membership"
        )
        if (
            body_start_membership != "unknown"
            or end.get("body_start_evidence") is not None
        ):
            raise CaptureError("b_s1_body_start_membership_not_established")
        request_end_monotonic_ns = _require_int(end, "monotonic_ns")
        if request_end_monotonic_ns < start["monotonic_ns"]:
            raise CaptureError("ground_truth_request_time_reversed")
        if relation == "inside_generator_window":
            derived_generator_membership = (
                generator_window_start <= start["monotonic_ns"]
                and request_end_monotonic_ns <= generator_window_end
            )
            if not derived_generator_membership:
                raise CaptureError("generator_window_timing_inconsistent")
        elif relation == "after_generator_window":
            derived_generator_membership = False
            if start["monotonic_ns"] < generator_window_end:
                raise CaptureError("after_generator_window_timing_inconsistent")
        else:
            derived_generator_membership = False
        if (
            relation != "ambiguous"
            and declared_generator_membership != derived_generator_membership
        ):
            raise CaptureError("ground_truth_generator_window_declaration_mismatch")
        operations.append(
            {
                "sequence": sequence,
                "request_id": end["request_id"],
                "operation": end["operation"],
                "upid": upid,
                "expected_task_type": end["expected_task_type"],
                "expected_task_id": end["expected_task_id"],
                "generator_window_relation": relation,
                "within_generator_window": derived_generator_membership,
                "b_s1_body_start_membership": "unknown",
                "request_start_monotonic_ns": start["monotonic_ns"],
                "request_end_monotonic_ns": request_end_monotonic_ns,
                "generator_finalized_monotonic_ns": finalized_monotonic_ns,
            }
        )
    if operations:
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
            _require_int(generator_contract, "maximum_duration_seconds")
            * 1_000_000_000
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


def _watch_gap_reasons(record: Mapping[str, Any]) -> frozenset[str]:
    """Return the exact observer-gap reasons structurally carried by a watch."""

    masks = set(record.get("_masks", ()))
    reasons: set[str] = set()
    if "IN_Q_OVERFLOW" in masks:
        reasons.add("watch_queue_overflow")
    if masks.intersection(
        {"IN_IGNORED", "IN_UNMOUNT", "IN_DELETE_SELF", "IN_MOVE_SELF"}
    ):
        reasons.add("watch_invalidation_or_loss")
    return frozenset(reasons)


def _validate_pre_t0_establishment(
    manifest: Mapping[str, Any], records: Sequence[Mapping[str, Any]]
) -> None:
    """Prove that the candidate's watch-first baseline protocol was executed."""

    t0 = _require_int(manifest, "t0_monotonic_ns")
    baseline = _require_mapping(
        manifest["baseline_observation"], "baseline_observation"
    )
    baseline_start = _require_int(baseline, "capture_start_monotonic_ns")
    baseline_end = _require_int(baseline, "capture_end_monotonic_ns")
    baseline_upids = {
        _require_upid(value, "baseline_upids")
        for value in _require_sequence(manifest.get("baseline_upids"), "baseline_upids")
    }
    quiescence = _require_mapping(manifest["t0_quiescence"], "t0_quiescence")
    committed = _require_int(quiescence, "committed_at_monotonic_ns")
    if committed != t0:
        raise CaptureError("t0_quiescence_commit_must_equal_t0")
    establishment_reference = _require_mapping(
        quiescence.get("pre_t0_establishment"),
        "t0_quiescence.pre_t0_establishment",
    )
    root_reference = _require_int(
        establishment_reference, "root_watch_establishment_sequence"
    )
    scan_references = [
        _require_int({"value": value}, "value")
        for value in _require_sequence(
            establishment_reference.get("baseline_scan_sequences"),
            "t0_quiescence.pre_t0_establishment.baseline_scan_sequences",
        )
    ]
    declared_watermark = _require_int(
        establishment_reference, "watch_drained_through_sequence"
    )
    if len(scan_references) != 2 or scan_references[1] != scan_references[0] + 1:
        raise CaptureError("pre_t0_baseline_scan_reference_invalid")

    by_establishment_sequence: dict[int, Mapping[str, Any]] = {}
    watcher_records: dict[int, Mapping[str, Any]] = {}
    baseline_scans: dict[int, Mapping[str, Any]] = {}
    bucket_rescans: list[Mapping[str, Any]] = []
    prior_time = -1
    for record in records:
        sequence = _require_int(record, "establishment_sequence")
        if sequence == 0 or sequence in by_establishment_sequence:
            raise CaptureError("pre_t0_establishment_sequence_zero_or_duplicate")
        by_establishment_sequence[sequence] = record
        event = _require_text(record, "event")
        if event in {"watch_installed", "bucket_created", "watch_event"}:
            watcher_sequence = _require_int(record, "watcher_sequence")
            if watcher_sequence == 0 or watcher_sequence in watcher_records:
                raise CaptureError("pre_t0_watcher_sequence_zero_or_duplicate")
            event_time = _require_int(record, "monotonic_ns")
            if event_time > t0:
                raise CaptureError("pre_t0_watch_event_after_t0")
            if event_time < prior_time:
                raise CaptureError("pre_t0_establishment_time_reversed")
            prior_time = event_time
            if not _require_bool(record, "complete"):
                raise CaptureError("pre_t0_watch_record_incomplete")
            watcher_records[watcher_sequence] = record
        elif event == "baseline_scan":
            if record.get("phase") != "PRE_T0_BASELINE":
                raise CaptureError("pre_t0_baseline_scan_phase_invalid")
            scan_start = _require_int(record, "scan_start_monotonic_ns")
            scan_end = _require_int(record, "scan_end_monotonic_ns")
            if scan_end < scan_start or scan_end > t0:
                raise CaptureError("pre_t0_baseline_scan_time_invalid")
            scan_sequence = _require_int(record, "baseline_scan_sequence")
            if scan_sequence == 0 or scan_sequence in baseline_scans:
                raise CaptureError("pre_t0_baseline_scan_sequence_zero_or_duplicate")
            baseline_scans[scan_sequence] = record
        elif event == "bucket_rescan":
            if record.get("phase") != "PRE_T0_BUCKET_RESCAN":
                raise CaptureError("pre_t0_bucket_rescan_phase_invalid")
            rescan_start = _require_int(record, "scan_start_monotonic_ns")
            rescan_end = _require_int(record, "scan_end_monotonic_ns")
            if rescan_end < rescan_start or rescan_end > t0:
                raise CaptureError("pre_t0_bucket_rescan_time_invalid")
            bucket_rescans.append(record)
        else:
            raise CaptureError(f"unknown_pre_t0_establishment_event:{event}")

    if set(by_establishment_sequence) != set(range(1, len(records) + 1)):
        raise CaptureError("pre_t0_establishment_sequence_gap")
    if not watcher_records or set(watcher_records) != set(
        range(1, max(watcher_records) + 1)
    ):
        raise CaptureError("pre_t0_watcher_sequence_gap")
    if not baseline_scans or set(baseline_scans) != set(
        range(1, max(baseline_scans) + 1)
    ):
        raise CaptureError("pre_t0_baseline_scan_sequence_gap")
    if scan_references != [len(baseline_scans) - 1, len(baseline_scans)]:
        raise CaptureError("pre_t0_baseline_scan_reference_not_terminal")

    root_record = by_establishment_sequence.get(root_reference)
    if (
        root_record is None
        or root_record.get("event") != "watch_installed"
        or root_record.get("watch_scope") != "task_root"
    ):
        raise CaptureError("pre_t0_root_watch_reference_invalid")
    root_time = _require_int(root_record, "monotonic_ns")
    if root_time >= baseline_start:
        raise CaptureError("pre_t0_root_watch_not_installed_before_baseline")

    selected_scans = [baseline_scans[sequence] for sequence in scan_references]
    scan_sets: list[set[str]] = []
    bucket_sets: list[set[str]] = []
    prior_watermark = 0
    for scan in selected_scans:
        scan_start = _require_int(scan, "scan_start_monotonic_ns")
        scan_end = _require_int(scan, "scan_end_monotonic_ns")
        if not (
            root_time < baseline_start <= scan_start <= scan_end <= baseline_end
            <= committed == t0
        ):
            raise CaptureError("pre_t0_baseline_scan_time_invalid")
        if (
            not _require_bool(scan, "complete")
            or _require_sequence(scan.get("unreadable_entries"), "pre_t0.unreadable_entries")
            or _require_sequence(scan.get("malformed_entries"), "pre_t0.malformed_entries")
        ):
            raise CaptureError("pre_t0_baseline_scan_incomplete")
        normalized = {
            _require_upid(value, "pre_t0.exact_normalized_upids")
            for value in _require_sequence(
                scan.get("exact_normalized_upids"),
                "pre_t0.exact_normalized_upids",
            )
        }
        buckets = {
            _require_text({"value": value}, "value")
            for value in _require_sequence(scan.get("bucket_set"), "pre_t0.bucket_set")
        }
        watermark = _require_int(scan, "watch_drained_through_sequence")
        if (
            watermark == 0
            or watermark < prior_watermark
            or watermark > max(watcher_records)
        ):
            raise CaptureError("pre_t0_watch_drain_watermark_invalid")
        if _require_int(watcher_records[watermark], "monotonic_ns") > scan_end:
            raise CaptureError("pre_t0_watch_drain_watermark_after_scan")
        prior_watermark = watermark
        scan_sets.append(normalized)
        bucket_sets.append(buckets)

    if _require_int(selected_scans[0], "scan_end_monotonic_ns") > _require_int(
        selected_scans[1], "scan_start_monotonic_ns"
    ):
        raise CaptureError("pre_t0_baseline_scan_order_invalid")
    if scan_sets[0] != scan_sets[1] or scan_sets[1] != baseline_upids:
        raise CaptureError("pre_t0_baseline_fixed_point_not_reached")
    if bucket_sets[0] != bucket_sets[1]:
        raise CaptureError("pre_t0_baseline_bucket_set_changed")
    if prior_watermark != declared_watermark:
        raise CaptureError("pre_t0_declared_watch_watermark_mismatch")
    relevant_pre_t0_watchers = {
        sequence
        for sequence, record in watcher_records.items()
        if _require_int(record, "monotonic_ns") <= t0
    }
    if relevant_pre_t0_watchers and declared_watermark < max(relevant_pre_t0_watchers):
        raise CaptureError("pre_t0_watch_event_undrained")
    terminal_scan_establishment_sequence = _require_int(
        selected_scans[1], "establishment_sequence"
    )
    if any(
        _require_int(record, "establishment_sequence")
        > terminal_scan_establishment_sequence
        for record in watcher_records.values()
        if _require_int(record, "monotonic_ns") <= t0
    ):
        raise CaptureError("pre_t0_watch_event_after_terminal_fixed_point")

    first_scan_start = _require_int(selected_scans[0], "scan_start_monotonic_ns")
    installed_buckets: dict[str, list[Mapping[str, Any]]] = {}
    for record in watcher_records.values():
        if record.get("event") == "watch_installed" and record.get("watch_scope") == "bucket":
            bucket = _require_text(record, "bucket")
            origin = _require_text(record, "bucket_origin")
            if origin == "existing_at_root_install":
                if record.get("trigger_watcher_sequence") is not None:
                    raise CaptureError("pre_t0_existing_bucket_has_event_trigger")
            elif origin == "root_event":
                _require_int(record, "trigger_watcher_sequence")
            else:
                raise CaptureError("pre_t0_bucket_watch_origin_invalid")
            installed_buckets.setdefault(bucket, []).append(record)
    for bucket in bucket_sets[0]:
        installs = installed_buckets.get(bucket, [])
        if not any(
            root_time <= _require_int(record, "monotonic_ns") <= first_scan_start
            for record in installs
        ):
            raise CaptureError(f"pre_t0_bucket_watch_missing:{bucket}")

    lazy_events = [
        record for record in watcher_records.values() if record.get("event") == "bucket_created"
    ]
    lazy_buckets: set[str] = set()
    for event in lazy_events:
        bucket = _require_text(event, "bucket")
        if bucket in lazy_buckets:
            raise CaptureError("pre_t0_duplicate_lazy_bucket_event")
        lazy_buckets.add(bucket)
        masks = _validated_inotify_masks(
            event, f"pre_t0.bucket_created:{_require_int(event, 'watcher_sequence')}"
        )
        event_time = _require_int(event, "monotonic_ns")
        if (
            event.get("watch_scope") != "task_root"
            or "IN_ISDIR" not in masks
            or not masks.intersection({"IN_CREATE", "IN_MOVED_TO"})
            or not root_time <= event_time <= t0
        ):
            raise CaptureError("pre_t0_lazy_bucket_event_invalid")
        trigger_sequence = _require_int(event, "watcher_sequence")
        installs = [
            record
            for record in installed_buckets.get(bucket, [])
            if event_time <= _require_int(record, "monotonic_ns") <= first_scan_start
            and record.get("bucket_origin") == "root_event"
            and record.get("trigger_watcher_sequence") == trigger_sequence
        ]
        rescans = [
            record
            for record in bucket_rescans
            if record.get("bucket") == bucket
            and record.get("trigger_watcher_sequence") == trigger_sequence
        ]
        if len(installs) != 1 or len(rescans) != 1:
            raise CaptureError("pre_t0_lazy_bucket_watch_or_rescan_missing")
        rescan = rescans[0]
        rescan_start = _require_int(rescan, "scan_start_monotonic_ns")
        rescan_end = _require_int(rescan, "scan_end_monotonic_ns")
        {
            _require_upid(value, "pre_t0.rescan.exact_normalized_upids")
            for value in _require_sequence(
                rescan.get("exact_normalized_upids"),
                "pre_t0.rescan.exact_normalized_upids",
            )
        }
        if (
            not _require_bool(rescan, "complete")
            or _require_sequence(rescan.get("unreadable_entries"), "pre_t0.rescan.unreadable_entries")
            or _require_sequence(rescan.get("malformed_entries"), "pre_t0.rescan.malformed_entries")
            or not _require_int(installs[0], "monotonic_ns")
            <= rescan_start
            <= rescan_end
            <= first_scan_start
        ):
            raise CaptureError("pre_t0_lazy_bucket_rescan_invalid")
    if len(bucket_rescans) != len(lazy_events):
        raise CaptureError("pre_t0_bucket_rescan_without_unique_lazy_event")

    # A bucket installation's origin is explicit: a post-root discovery event
    # cannot be relabeled as an initially existing bucket merely because its
    # watch happened to be installed before enumeration began.
    for bucket, installs in installed_buckets.items():
        for install in installs:
            if install.get("bucket_origin") == "root_event" and bucket not in lazy_buckets:
                raise CaptureError("pre_t0_lazy_bucket_watch_without_root_event")
            if (
                install.get("bucket_origin") == "existing_at_root_install"
                and _require_int(install, "monotonic_ns") >= baseline_start
            ):
                raise CaptureError("pre_t0_existing_bucket_watch_installed_too_late")


def _validate_t0_quiescence(
    manifest: Mapping[str, Any],
    surfaces: Mapping[int, Mapping[str, Any]],
    exact_records: Mapping[int, Mapping[str, Any]],
    exact_final: set[str],
) -> None:
    t0 = _require_int(manifest, "t0_monotonic_ns")
    baseline = _require_mapping(
        manifest["baseline_observation"], "baseline_observation"
    )
    baseline_end = _require_int(baseline, "capture_end_monotonic_ns")
    baseline_upids = {
        _require_upid(value, "baseline_upids")
        for value in _require_sequence(manifest.get("baseline_upids"), "baseline_upids")
    }
    generator_contract = _validate_generator_contract(
        manifest, _require_text(manifest, "fixture_kind")
    )
    task_id_policy = _require_mapping(
        generator_contract["expected_task_id_policy"],
        "generator_contract.expected_task_id_policy",
    )
    quiescence = _require_mapping(manifest["t0_quiescence"], "t0_quiescence")
    if quiescence.get("state") != "QUIESCENT":
        raise CaptureError("t0_not_declared_quiescent")
    committed = _require_int(quiescence, "committed_at_monotonic_ns")
    if not baseline_end <= committed == t0:
        raise CaptureError("t0_quiescence_commit_time_invalid")
    pending = {
        _require_upid(value, "t0_quiescence.pending_upids")
        for value in _require_sequence(
            quiescence.get("pending_upids"), "t0_quiescence.pending_upids"
        )
    }
    if pending:
        raise CaptureError("t0_quiescence_has_pending_upids")

    surface_references = _require_mapping(
        quiescence.get("surface_observation_sequences"),
        "t0_quiescence.surface_observation_sequences",
    )
    if set(surface_references) != {"active", "index", "index.1"}:
        raise CaptureError("t0_quiescence_surface_reference_set_invalid")
    referenced_surfaces: dict[str, Mapping[str, Any]] = {}
    for source in ("active", "index", "index.1"):
        sequence = _require_int(surface_references, source)
        surface = surfaces.get(sequence)
        if surface is None or surface.get("source") != source:
            raise CaptureError(f"t0_quiescence_surface_reference_invalid:{source}")
        if not (
            baseline_end
            <= _require_int(surface, "capture_start_monotonic_ns")
            <= _require_int(surface, "capture_end_monotonic_ns")
            <= committed
            == t0
        ):
            raise CaptureError(f"t0_quiescence_surface_time_invalid:{source}")
        if not surface.get("readable") or not surface.get("complete"):
            raise CaptureError(f"t0_quiescence_surface_incomplete:{source}")
        referenced_surfaces[source] = surface
    if referenced_surfaces["active"]["_normalized_upids"]:
        raise CaptureError("t0_quiescence_active_worker_present")

    classifications = _require_sequence(
        quiescence.get("baseline_classifications"),
        "t0_quiescence.baseline_classifications",
    )
    classified_upids: set[str] = set()
    latest_exact_end = baseline_end
    for index, value in enumerate(classifications):
        record = _require_mapping(
            value, f"t0_quiescence.baseline_classifications[{index}]"
        )
        upid = _require_upid(record.get("upid"), "baseline_classification.upid")
        if upid in classified_upids:
            raise CaptureError("t0_quiescence_duplicate_baseline_classification")
        classified_upids.add(upid)
        if record.get("lifecycle_state") != "finalized":
            raise CaptureError("t0_quiescence_baseline_not_finalized")
        decoded = _decode_upid(upid)
        matches_generator_scope = (
            decoded["node"] == generator_contract["expected_node"]
            and decoded["task_type"] == generator_contract["expected_task_type"]
            and decoded["task_id"] == task_id_policy["value"]
            and decoded["owner"] == generator_contract["expected_owner"]
        )
        expected_classification = (
            "supported_in_scope"
            if matches_generator_scope
            else "classified_out_of_scope"
        )
        if record.get("operation_classification") != expected_classification:
            raise CaptureError("t0_quiescence_operation_unclassified")
        exact_sequence = _require_int(record, "exact_observation_sequence")
        exact = exact_records.get(exact_sequence)
        if exact is None or exact.get("known_upid") != upid or upid not in exact_final:
            raise CaptureError("t0_quiescence_final_exact_reference_invalid")
        exact_end = _require_int(exact, "capture_end_monotonic_ns")
        if exact_end > committed or exact_end > t0:
            raise CaptureError("t0_quiescence_final_exact_not_before_t0")
        latest_exact_end = max(latest_exact_end, exact_end)
    if classified_upids != baseline_upids:
        raise CaptureError("t0_quiescence_baseline_classification_set_mismatch")
    if latest_exact_end > committed:
        raise CaptureError("t0_quiescence_precedes_final_exact_evidence")


def _validate_subrun_obligations(
    manifest: Mapping[str, Any],
    expected_upids: set[str],
    generator_window_operations: Sequence[Mapping[str, Any]],
    positive_close_candidate: bool,
    close_monotonic: int,
    watches: Mapping[int, Mapping[str, Any]],
    scans: Mapping[int, Mapping[str, Any]],
    surfaces: Mapping[int, Mapping[str, Any]],
    api_pages: Sequence[Mapping[str, Any]],
    harness: Sequence[Mapping[str, Any]],
) -> None:
    contract = _validate_subrun_contract(manifest)
    required = set(contract["required_phenomena"])
    evidence_ids = _require_mapping(contract["evidence_ids"], "evidence_ids")
    subrun_id = _require_text(manifest, "subrun_id")
    operations_by_sequence = {
        int(operation["sequence"]): operation
        for operation in generator_window_operations
    }

    if positive_close_candidate and subrun_id != "13E" and not expected_upids:
        raise CaptureError("subrun_generated_window_work_missing")

    if (
        positive_close_candidate
        and "low_volume_enumeration" in required
        and not expected_upids
    ):
        raise CaptureError("subrun_13a_enumeration_not_exercised")

    if "pagination_movement" in required:
        evidence_id = str(evidence_ids["pagination_movement"])
        pages = [page for page in api_pages if page.get("phenomenon_id") == evidence_id]
        page_sequences = [_require_int(page, "page_sequence") for page in pages]
        page_starts = [
            _require_int(page, "request_start_monotonic_ns") for page in pages
        ]
        overlaps_generator = any(
            _require_int(page, "request_start_monotonic_ns")
            <= int(operation["request_end_monotonic_ns"])
            and _require_int(page, "response_end_monotonic_ns")
            >= int(operation["request_start_monotonic_ns"])
            for page in pages
            for operation in generator_window_operations
        )
        if (
            len(pages) < 2
            or page_sequences != list(range(1, len(pages) + 1))
            or page_starts != sorted(page_starts)
            or len({_require_int(page, "start_offset") for page in pages}) < 2
            or len({_require_text(page, "source") for page in pages}) != 1
            or not overlaps_generator
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
            target not in expected_upids
            or target not in scan_set
            or watch.get("_normalized_upid") != target
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
            target not in expected_upids
            or active.get("source") != "active"
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
        generator_sequences = {
            _require_int({"value": value}, "value")
            for value in _require_sequence(
                marker.get("generator_sequences"),
                "rotation.generator_sequences",
            )
        }
        referenced_operations = [
            operations_by_sequence[sequence]
            for sequence in sorted(generator_sequences)
            if sequence in operations_by_sequence
        ]
        rotation_within_generated_run = bool(referenced_operations) and (
            min(
                int(operation["request_start_monotonic_ns"])
                for operation in referenced_operations
            )
            <= watch_time
            <= max(
                int(operation["generator_finalized_monotonic_ns"])
                for operation in referenced_operations
            )
        )
        rotation_generated_upids = (
            set(before["_normalized_upids"])
            | set(rotated["_normalized_upids"])
            | set(after["_normalized_upids"])
        ) & expected_upids
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
            or generator_sequences != set(operations_by_sequence)
            or not rotation_within_generated_run
            or not rotation_generated_upids
        ):
            raise CaptureError("subrun_index_rotation_not_evidenced")

    if "watch_overflow_or_invalidation" in required:
        evidence_id = str(evidence_ids["watch_overflow_or_invalidation"])
        relevant_matching_signals = [
            watch
            for watch in watches.values()
            if _require_int(watch, "monotonic_ns") <= close_monotonic
            and watch.get("phenomenon_id") == evidence_id
            and _watch_gap_reasons(watch)
        ]
        if not relevant_matching_signals:
            raise CaptureError("subrun_watch_loss_signal_not_evidenced_before_close")


def _analyze_loaded(
    manifest: Mapping[str, Any], records: Mapping[str, Sequence[Mapping[str, Any]]]
) -> AnalysisResult:
    _validate_pre_t0_establishment(
        manifest, records["pre_t0_establishment"]
    )
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
    physical_watcher_sequences: list[int] = []
    raw_orders: list[int] = []
    for source_record in records["watch_events"]:
        record = dict(source_record)
        watcher_sequence = _require_int(record, "watcher_sequence")
        if watcher_sequence == 0 or watcher_sequence in watches:
            raise CaptureError("watcher_sequence_zero_or_duplicate")
        physical_watcher_sequences.append(watcher_sequence)
        _require_signed_int(record, "watch_descriptor")
        _require_int(record, "cookie")
        raw_order = _require_int(record, "raw_order")
        if raw_order == 0:
            raise CaptureError("watch_raw_order_zero")
        raw_orders.append(raw_order)
        _require_string(record, "watched_path")
        _require_string(record, "filename")
        queue_overflow = _require_bool(record, "queue_overflow")
        event_time = _require_int(record, "monotonic_ns")
        _require_text(record, "wall_timestamp")
        _require_text(record, "event_type")
        masks = _validated_inotify_masks(record, f"watch:{watcher_sequence}")
        if queue_overflow != ("IN_Q_OVERFLOW" in masks):
            raise CaptureError(
                f"watch_queue_overflow_mismatch_raw_mask:{watcher_sequence}"
            )
        record["_masks"] = masks
        watches[watcher_sequence] = record
        relevant = event_time <= close_monotonic
        if relevant:
            gap_reasons.update(_watch_gap_reasons(record))
        filename = _require_string(record, "filename")
        parsed_upid = filename if UPID_PATTERN.fullmatch(filename) else None
        declared_upid_value = record.get("normalized_upid")
        declared_upid = (
            _require_upid(declared_upid_value, "watch.normalized_upid")
            if declared_upid_value is not None
            else None
        )
        if parsed_upid != declared_upid:
            raise CaptureError(
                f"watch_normalized_upid_mismatch_filename:{watcher_sequence}"
            )
        record["_normalized_upid"] = parsed_upid
        if parsed_upid is not None:
            upid = parsed_upid
            if relevant and masks.intersection(
                {"IN_CREATE", "IN_MOVED_TO", "IN_CLOSE_WRITE"}
            ):
                watch_known.add(upid)
            if relevant and masks.intersection({"IN_DELETE", "IN_MOVED_FROM"}):
                watch_deleted.add(upid)
    expected_watch_order = list(range(1, len(watches) + 1))
    if physical_watcher_sequences != expected_watch_order:
        raise CaptureError("watcher_sequence_not_contiguous_or_jsonl_ordered")
    if raw_orders != expected_watch_order or raw_orders != physical_watcher_sequences:
        raise CaptureError("watch_raw_order_not_contiguous_or_capture_ordered")
    watcher_times = [
        _require_int(watches[sequence], "monotonic_ns") for sequence in sorted(watches)
    ]
    if watcher_times != sorted(watcher_times):
        raise CaptureError("watcher_sequence_time_reversed")

    scan_known: set[str] = set()
    scans: dict[int, dict[str, Any]] = {}
    physical_scan_sequences: list[int] = []

    # Phase 1: decode each record without applying sequence-sensitive meaning.
    for source_record in records["scan_rounds"]:
        record = dict(source_record)
        sequence = _require_int(record, "scan_sequence")
        if sequence == 0 or sequence in scans:
            raise CaptureError("scan_sequence_zero_or_duplicate")
        physical_scan_sequences.append(sequence)
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
        _require_bool(record, "complete")
        _require_int(record, "watch_drained_through_sequence")
        record["_unreadable_entries"] = unreadable
        record["_malformed_entries"] = malformed
        scans[sequence] = record

    expected_scan_sequences = list(range(1, len(scans) + 1))
    if len(scans) < 2 or set(scans) != set(expected_scan_sequences):
        raise CaptureError("scan_sequence_not_contiguous_or_too_short")
    if physical_scan_sequences != expected_scan_sequences:
        raise CaptureError("scan_sequence_not_jsonl_ordered")

    # Phase 2: every adjacency, disappearance, watermark, and timing decision
    # follows declared scan_sequence, never JSONL iteration order.
    prior_scan: set[str] | None = None
    prior_watermark = 0
    prior_scan_end: int | None = None
    for sequence in sorted(scans):
        record = scans[sequence]
        scan_start = _require_int(record, "scan_start_monotonic_ns")
        scan_end = _require_int(record, "scan_end_monotonic_ns")
        current = set(record["_normalized_upids"])
        watermark = _require_int(record, "watch_drained_through_sequence")
        if watermark < prior_watermark:
            raise CaptureError("scan_watch_drain_watermark_reversed")
        if watermark:
            referenced_watch = watches.get(watermark)
            if referenced_watch is None:
                raise CaptureError("scan_watch_drain_watermark_reference_missing")
            if _require_int(referenced_watch, "monotonic_ns") > scan_end:
                raise CaptureError("scan_watch_drain_watermark_after_scan_end")
        if prior_scan_end is not None and scan_start < prior_scan_end:
            raise CaptureError("scan_sequence_time_overlap_or_reversed")
        if (
            record["_unreadable_entries"]
            or record["_malformed_entries"]
            or not record["complete"]
            or record.get("consistency_marker") == "inconsistent"
        ):
            gap_reasons.add("scan_unreadable_malformed_or_inconsistent")
        if prior_scan is not None and prior_scan - current:
            gap_reasons.add("exact_log_disappeared_between_scans")
        prior_scan = current
        prior_watermark = watermark
        prior_scan_end = scan_end
        scan_known.update(current)
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
        declared_values = _require_sequence(
            record.get("normalized_upids"), "surface.normalized_upids"
        )
        declared_upids = {
            _require_upid(value, "surface.normalized_upids")
            for value in declared_values
        }
        if len(declared_upids) != len(declared_values):
            raise CaptureError(
                f"surface_declared_normalized_upids_duplicate:{source}:{sequence}"
            )
        raw_evidence = _require_string(record, "raw_evidence")
        _require_mapping(record.get("stat"), "surface.stat")
        surface_hash = _require_text(record, "sha256")
        if surface_hash != _sha256_text(raw_evidence):
            raise CaptureError(f"surface_hash_mismatch:{source}:{sequence}")
        parsed_upids = _parse_surface_raw_evidence(source, raw_evidence)
        if parsed_upids != declared_upids:
            raise CaptureError(
                f"surface_normalized_upids_mismatch_raw_evidence:{source}:{sequence}"
            )
        record["_normalized_upids"] = parsed_upids
        readable = _require_bool(record, "readable")
        complete = _require_bool(record, "complete")
        if not readable or not complete:
            gap_reasons.add(f"surface_incomplete:{source}")
        surface_known.update(parsed_upids)
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
    exact_records: dict[int, Mapping[str, Any]] = {}
    for record in records["exact_upid"]:
        observation_sequence = _require_int(record, "observation_sequence")
        if observation_sequence == 0 or observation_sequence in exact_records:
            raise CaptureError("exact_observation_sequence_zero_or_duplicate")
        exact_records[observation_sequence] = record
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
                and watch.get("_normalized_upid") == upid
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

    if set(exact_records) != set(range(1, len(exact_records) + 1)):
        raise CaptureError("exact_observation_sequence_gap")
    _validate_t0_quiescence(
        manifest,
        surfaces,
        exact_records,
        exact_final,
    )

    generator_window_operations = [
        operation for operation in operations if operation["within_generator_window"]
    ]
    expected_upids = {
        str(operation["upid"]) for operation in generator_window_operations
    }
    ambiguous = {
        str(operation["upid"])
        for operation in operations
        if operation["generator_window_relation"] == "ambiguous"
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
        generator_window_operations,
        close_state == "CLOSED_COMPLETE" and not gap_reasons,
        close_monotonic,
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
            str(operation["upid"]): operation
            for operation in generator_window_operations
        }
        missing_upid = sorted(missing)[0]
        operation = operation_by_upid[missing_upid]
        witness = {
            "classification": "ENUMERATION OMISSION FOR TESTED GENERATOR WINDOW",
            "ground_truth_upid": missing_upid,
            "ground_truth_generator_sequence": operation["sequence"],
            "operation": operation["operation"],
            "within_experiment_generator_window": True,
            "b_s1_body_start_membership": "UNKNOWN",
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
            AnalyzerOutcome.ENUMERATION_WITNESS,
            ("generator_window_upid_missing_without_gap",),
            witness,
        )

    if close_state != "CLOSED_COMPLETE":
        raise CaptureError("candidate_did_not_close")
    if expected_upids != declared_known:
        raise CaptureError("candidate_close_known_set_not_exact_generator_window")
    if expected_upids - exact_final:
        raise CaptureError("exact_final_status_set_incomplete")

    # API pages remain corroborative.  They can expose a gap, but never add an
    # otherwise unknown UPID to the completeness-bearing enumeration set.
    return AnalysisResult(
        AnalyzerOutcome.PASS,
        (
            "generator_window_ground_truth_equals_reconciled_enumeration_set_"
            "for_tested_interleaving",
        ),
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
    except CaptureEnvironmentError as exc:
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
    "CLOCK_CONTRACT_REVISION",
    "EXPECTED_B_S1_REVISION",
    "EXPECTED_SOURCE_LEDGER",
    "GENERATOR_CONTRACT_REVISION",
    "PROTOCOL_REVISION",
    "SCHEMA_REVISION",
    "SUBRUN_CONTRACT_REVISION",
    "analyze_capture",
]
