"""Hermetic S0.1 asset validation for the Family B experiment #13 redesign.

Non-normative research asset validation. This file does NOT implement, test,
or approximate any v7 authority logic (`ParticipantTable`, `admit()`,
`ChronologySpec`, `PhaseSpec`, an observer ledger, or `CaptureValidity`/
`T1Result` classifiers). The only behavioral analyzer executed here is the
byte-frozen historical v6 oracle, loaded strictly as historical/test-only
data -- never as a production dependency.

It validates three checked-in S0.1 assets:

- ``tests/oracles/family_b_13/v6/`` -- the byte-frozen v6 oracle and its
  provenance manifest (G10);
- ``tests/fixtures/research/family_b_13/`` -- the declarative sealed-capture
  witness corpus and its manifest. Each row's ``expected_v6_result`` is the
  full, exact ``AnalysisResult.as_dict()`` (oracle replay must match it
  exactly), but each row's ``intended_v7_outcome`` is outcome-only, non-
  authoritative migration metadata -- v7's future reason strings/witness
  bodies are undesigned and are deliberately not frozen here;
- ``tests/fixtures/research/family_b_13/migration_expectations.json`` -- the
  v6 -> v7 differential migration ledger (G7), validated against a
  cause/cell-specific outcome-pair -> required-``reason_class`` matrix, not
  merely against the set of allowed reason classes.

See ``docs/architecture/research/blocker-b-family-b-13-authority-core-redesign.md``
for the non-normative design record these assets support.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
ORACLE_DIR = REPO_ROOT / "tests" / "oracles" / "family_b_13" / "v6"
ORACLE_PATH = ORACLE_DIR / "blocker_b_family_b_13_analyzer.py"
ORACLE_MANIFEST_PATH = ORACLE_DIR / "oracle_manifest.json"
CORPUS_ROOT = REPO_ROOT / "tests" / "fixtures" / "research" / "family_b_13"
CAPTURES_ROOT = CORPUS_ROOT / "captures"
CORPUS_MANIFEST_PATH = CORPUS_ROOT / "corpus_manifest.json"
MIGRATION_LEDGER_PATH = CORPUS_ROOT / "migration_expectations.json"

EXPECTED_SOURCE_COMMIT = "56723770b5edb3a574a16c9b73d2ad5f668d903c"
EXPECTED_SOURCE_PATH = "scripts/research/blocker_b_family_b_13_analyzer.py"
EXPECTED_GIT_BLOB_SHA1 = "36be20097d01e7d86a76261ba35ca260df817b26"
EXPECTED_SCHEMA_REVISION = "family-b-13-capture-v6"
EXPECTED_PROTOCOL_REVISION = "family-b-13-preexecution-v6"
EXPECTED_ANALYZER_REVISION = "family-b-13-analyzer-v6"

ALL_OUTCOMES = frozenset(
    {
        "ANALYZER_PASS_TESTED_INTERLEAVING",
        "B_S1_GAP_DETECTED",
        "GENERATOR_WINDOW_ENUMERATION_OMISSION_WITNESS",
        "HARNESS_INCOMPLETE",
        "ENVIRONMENT_INELIGIBLE",
    }
)

# G7: absolute / structural restrictions, current for this redesign, never
# overridable by any ledger reason including CONTRACT_AMENDMENT. Written as
# a direct, explicit set of concrete pairs -- not as an over-broad base rule
# with exceptions subtracted out -- so nothing here can silently widen if a
# new outcome is ever added to ALL_OUTCOMES.
ABSOLUTE_FORBIDDEN_PAIRS = frozenset(
    {
        # v6 HARNESS_INCOMPLETE -> v7 PASS: forbidden for this redesign
        # (G12: v7 integrity coverage must not convert an incoherent
        # capture into positive evidence). GAP -> PASS and WITNESS ->
        # INCOMPLETE-or-other transitions are NOT in this set -- only this
        # exact incomplete-sourced cell is.
        ("HARNESS_INCOMPLETE", "ANALYZER_PASS_TESTED_INTERLEAVING"),
        # v6 ENVIRONMENT_INELIGIBLE -> any non-INELIGIBLE v7 outcome:
        # absolutely forbidden, applicability requirements are not loosened.
        ("ENVIRONMENT_INELIGIBLE", "ANALYZER_PASS_TESTED_INTERLEAVING"),
        ("ENVIRONMENT_INELIGIBLE", "B_S1_GAP_DETECTED"),
        ("ENVIRONMENT_INELIGIBLE", "GENERATOR_WINDOW_ENUMERATION_OMISSION_WITNESS"),
        ("ENVIRONMENT_INELIGIBLE", "HARNESS_INCOMPLETE"),
    }
)

# G7: contract-amendment-only cells. Legal only with reason_class ==
# CONTRACT_AMENDMENT and a non-empty contract_amendment reference.
CONTRACT_ONLY_PAIRS = frozenset(
    {
        ("HARNESS_INCOMPLETE", "B_S1_GAP_DETECTED"),
        ("HARNESS_INCOMPLETE", "GENERATOR_WINDOW_ENUMERATION_OMISSION_WITNESS"),
    }
)

# Not authorized by the G7 correction, and not to be inferred from the
# separately-legal GAP -> PASS cell. Must never appear regardless of
# reason_class -- including V6_INTERVAL_POLLUTION, which is exactly the
# class that makes GAP -> PASS legal but does not extend to WITNESS -> PASS.
NOT_AUTHORIZED_PAIRS = frozenset(
    {
        ("GENERATOR_WINDOW_ENUMERATION_OMISSION_WITNESS", "ANALYZER_PASS_TESTED_INTERLEAVING"),
    }
)

ALLOWED_REASON_CLASSES = frozenset(
    {"V6_UNSOUND", "V6_INTERVAL_POLLUTION", "V6_CRASH", "CONTRACT_AMENDMENT"}
)

# G7 cause/cell-specific matrix (redesign doc §6.E): the exact reason_class
# required for each outcome-pair that is legal with a plain (non-contract)
# ledger row. A pair not present here is either the same outcome twice (no
# ledger row needed at all), one of ABSOLUTE_FORBIDDEN_PAIRS, one of
# CONTRACT_ONLY_PAIRS, or one of NOT_AUTHORIZED_PAIRS -- never a fifth,
# unclassified case (see test_every_differing_outcome_pair_is_classified).
REQUIRED_REASON_CLASS_FOR_TRANSITION: dict[tuple[str, str], str] = {
    ("ANALYZER_PASS_TESTED_INTERLEAVING", "HARNESS_INCOMPLETE"): "V6_UNSOUND",
    ("ANALYZER_PASS_TESTED_INTERLEAVING", "B_S1_GAP_DETECTED"): "V6_UNSOUND",
    (
        "ANALYZER_PASS_TESTED_INTERLEAVING",
        "GENERATOR_WINDOW_ENUMERATION_OMISSION_WITNESS",
    ): "V6_UNSOUND",
    ("ANALYZER_PASS_TESTED_INTERLEAVING", "ENVIRONMENT_INELIGIBLE"): "V6_UNSOUND",
    ("B_S1_GAP_DETECTED", "ANALYZER_PASS_TESTED_INTERLEAVING"): "V6_INTERVAL_POLLUTION",
    (
        "B_S1_GAP_DETECTED",
        "GENERATOR_WINDOW_ENUMERATION_OMISSION_WITNESS",
    ): "V6_INTERVAL_POLLUTION",
    ("B_S1_GAP_DETECTED", "HARNESS_INCOMPLETE"): "V6_UNSOUND",
    ("B_S1_GAP_DETECTED", "ENVIRONMENT_INELIGIBLE"): "V6_UNSOUND",
    (
        "GENERATOR_WINDOW_ENUMERATION_OMISSION_WITNESS",
        "B_S1_GAP_DETECTED",
    ): "V6_UNSOUND",
    (
        "GENERATOR_WINDOW_ENUMERATION_OMISSION_WITNESS",
        "HARNESS_INCOMPLETE",
    ): "V6_UNSOUND",
    (
        "GENERATOR_WINDOW_ENUMERATION_OMISSION_WITNESS",
        "ENVIRONMENT_INELIGIBLE",
    ): "V6_UNSOUND",
    ("HARNESS_INCOMPLETE", "ENVIRONMENT_INELIGIBLE"): "V6_UNSOUND",
}

# The subset of REQUIRED_REASON_CLASS_FOR_TRANSITION that targets
# ENVIRONMENT_INELIGIBLE ("non-INELIGIBLE -> INELIGIBLE"): these additionally
# require an exact nonempty source_ref describing the reviewed applicability
# witness (redesign doc §6.E).
NON_INELIGIBLE_TO_INELIGIBLE_PAIRS = frozenset(
    {
        pair
        for pair in REQUIRED_REASON_CLASS_FOR_TRANSITION
        if pair[1] == "ENVIRONMENT_INELIGIBLE"
    }
)


def _check_reason_class_matches_matrix(row: dict[str, Any]) -> None:
    """Raise AssertionError unless ``row``'s reason_class is exactly the one
    required for its (from_v6_outcome, to_v7_outcome) pair. Shared by the
    real-ledger integrity test and the synthetic negative-probe tests so
    both exercise the identical validation path."""

    pair = (row["from_v6_outcome"], row["to_v7_outcome"])
    category = _classify_pair(*pair)
    assert category == "ALLOWED_WITH_LEDGER", (row.get("fixture_id"), category)
    required = REQUIRED_REASON_CLASS_FOR_TRANSITION[pair]
    assert row["reason_class"] == required, (
        row.get("fixture_id"),
        pair,
        "got",
        row["reason_class"],
        "required",
        required,
    )


def _classify_pair(from_outcome: str, to_outcome: str) -> str:
    """Classify one (from_v6_outcome, to_v7_outcome) pair into exactly one
    G7 category. Every one of the 20 differing pairs over ALL_OUTCOMES must
    land in exactly one category -- see
    test_every_differing_outcome_pair_is_classified."""

    if from_outcome == to_outcome:
        return "SAME"
    if (from_outcome, to_outcome) in ABSOLUTE_FORBIDDEN_PAIRS:
        return "ABSOLUTE_FORBIDDEN"
    if (from_outcome, to_outcome) in CONTRACT_ONLY_PAIRS:
        return "CONTRACT_ONLY"
    if (from_outcome, to_outcome) in NOT_AUTHORIZED_PAIRS:
        return "NOT_AUTHORIZED"
    if (from_outcome, to_outcome) in REQUIRED_REASON_CLASS_FOR_TRANSITION:
        return "ALLOWED_WITH_LEDGER"
    return "UNCLASSIFIED"

# The six witnesses the S0.1 task requires to be materialized: the latest
# stop-triggering four plus the two model-derived witnesses E1/E3. A typo or
# removal of any of these fixture ids must fail this suite.
REQUIRED_WITNESS_FIXTURE_IDS = frozenset(
    {
        "stop_reader_pre_t0_before_process_start",  # reader/pre-T0 causality witness
        "stop_generator_sequence_relabel",  # generator sequence/physical-order witness
        "stop_duplicate_subrun_evidence_ids",  # duplicate evidence-ID witness
        "stop_post_t1_gap_signal_rewrites_t1",  # post-T1 gap witness
        "model_e1_pre_t0_surface_influences_candidate",  # E1
        "model_e3_13c_handoff_after_t1",  # E3
    }
)


def _sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git_blob_sha1(data: bytes) -> str:
    header = f"blob {len(data)}\0".encode("ascii")
    return hashlib.sha1(header + data).hexdigest()  # noqa: S324 -- git object id, not a security use


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def oracle_manifest() -> dict[str, Any]:
    return _load_json(ORACLE_MANIFEST_PATH)


@pytest.fixture(scope="module")
def oracle_bytes() -> bytes:
    return ORACLE_PATH.read_bytes()


@pytest.fixture(scope="module")
def oracle_module() -> ModuleType:
    """Load the frozen v6 oracle as an isolated, test-only module.

    Uses importlib file-location loading rather than a package import so
    that (a) no ``__init__.py`` needs to be added under ``tests/oracles/``
    and (b) it is unambiguous that nothing outside this test file imports
    the frozen oracle.
    """

    module_name = "_frozen_family_b_13_v6_oracle_test_only"
    spec = importlib.util.spec_from_file_location(module_name, ORACLE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclasses' string-annotation resolution looks the defining module up
    # in sys.modules, so it must be registered before exec_module runs.
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        del sys.modules[module_name]
        raise
    return module


@pytest.fixture(scope="module")
def corpus_rows() -> list[dict[str, Any]]:
    return _load_json(CORPUS_MANIFEST_PATH)


@pytest.fixture(scope="module")
def corpus_by_id(corpus_rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {row["fixture_id"]: row for row in corpus_rows}


@pytest.fixture(scope="module")
def migration_rows() -> list[dict[str, Any]]:
    return _load_json(MIGRATION_LEDGER_PATH)


# ---------------------------------------------------------------------------
# A. G10 -- frozen oracle byte identity
# ---------------------------------------------------------------------------


def test_oracle_git_blob_identity(oracle_bytes: bytes) -> None:
    assert _git_blob_sha1(oracle_bytes) == EXPECTED_GIT_BLOB_SHA1


def test_oracle_sha256_matches_manifest(
    oracle_bytes: bytes, oracle_manifest: dict[str, Any]
) -> None:
    assert hashlib.sha256(oracle_bytes).hexdigest() == oracle_manifest["sha256"]
    # Independently recompute -- the manifest field must not be trusted blind.
    assert oracle_manifest["sha256"] == (
        "db26374e1bd3ba9ee1c4793a40dc3754e4a0fc384583b0b794dd8538a4b29068"
    )


def test_oracle_manifest_provenance_exact(oracle_manifest: dict[str, Any]) -> None:
    assert oracle_manifest["source_commit"] == EXPECTED_SOURCE_COMMIT
    assert oracle_manifest["source_path"] == EXPECTED_SOURCE_PATH
    assert oracle_manifest["source_git_blob_sha1"] == EXPECTED_GIT_BLOB_SHA1
    assert oracle_manifest["schema_revision"] == EXPECTED_SCHEMA_REVISION
    assert oracle_manifest["protocol_revision"] == EXPECTED_PROTOCOL_REVISION
    assert oracle_manifest["analyzer_revision"] == EXPECTED_ANALYZER_REVISION
    assert oracle_manifest["role"] == "historical_differential_oracle_only"


def test_oracle_module_revision_constants_exact(oracle_module: ModuleType) -> None:
    assert oracle_module.SCHEMA_REVISION == EXPECTED_SCHEMA_REVISION
    assert oracle_module.PROTOCOL_REVISION == EXPECTED_PROTOCOL_REVISION
    assert oracle_module.ANALYZER_REVISION == EXPECTED_ANALYZER_REVISION


def test_oracle_bytes_and_pinned_blob_assertion_are_independent() -> None:
    """Changing the oracle bytes without changing the pinned blob assertion
    must NOT be able to make the identity test pass -- i.e. the blob check
    is computed from the file, never from a value that could be silently
    regenerated to match a tampered file."""

    tampered = ORACLE_PATH.read_bytes() + b"\n# tampered\n"
    assert _git_blob_sha1(tampered) != EXPECTED_GIT_BLOB_SHA1
    assert hashlib.sha256(tampered).hexdigest() != (
        "db26374e1bd3ba9ee1c4793a40dc3754e4a0fc384583b0b794dd8538a4b29068"
    )


# ---------------------------------------------------------------------------
# B. corpus structure
# ---------------------------------------------------------------------------


def test_corpus_manifest_fixture_ids_unique(corpus_rows: list[dict[str, Any]]) -> None:
    ids = [row["fixture_id"] for row in corpus_rows]
    assert len(ids) == len(set(ids))
    assert len(ids) >= 29


def test_corpus_manifest_paths_relative_and_bounded(
    corpus_rows: list[dict[str, Any]],
) -> None:
    for row in corpus_rows:
        relative = row["relative_capture_path"]
        assert not relative.startswith("/")
        assert ".." not in Path(relative).parts
        resolved = (CORPUS_ROOT / relative).resolve()
        assert resolved.is_relative_to(CORPUS_ROOT.resolve())
        assert resolved.is_dir(), f"missing capture directory: {relative}"


def test_corpus_manifest_source_commit_exact(corpus_rows: list[dict[str, Any]]) -> None:
    for row in corpus_rows:
        assert row["source_commit"] == EXPECTED_SOURCE_COMMIT


def test_corpus_manifest_file_hashes_match_checked_in_bytes(
    corpus_rows: list[dict[str, Any]],
) -> None:
    for row in corpus_rows:
        capture_dir = CORPUS_ROOT / row["relative_capture_path"]
        declared = row["file_hashes"]
        actual_names = {p.name for p in capture_dir.iterdir()}
        assert set(declared) == actual_names, row["fixture_id"]
        for name, expected_hash in declared.items():
            assert _sha256_of(capture_dir / name) == expected_hash, (
                row["fixture_id"],
                name,
            )


def test_corpus_captures_on_disk_have_no_extra_untracked_fixture_dirs(
    corpus_rows: list[dict[str, Any]],
) -> None:
    declared = {row["relative_capture_path"].split("/", 1)[1] for row in corpus_rows}
    on_disk = {p.name for p in CAPTURES_ROOT.iterdir() if p.is_dir()}
    assert on_disk == declared


def test_corpus_manifest_kinds_are_valid(corpus_rows: list[dict[str, Any]]) -> None:
    valid_kinds = {"positive_control", "historical_witness", "model_derived_witness"}
    for row in corpus_rows:
        assert row["kind"] in valid_kinds, row["fixture_id"]


# ---------------------------------------------------------------------------
# C. frozen oracle replay
# ---------------------------------------------------------------------------


def test_frozen_oracle_replay_matches_expected_v6_result(
    oracle_module: ModuleType, corpus_rows: list[dict[str, Any]]
) -> None:
    for row in corpus_rows:
        capture_dir = CORPUS_ROOT / row["relative_capture_path"]
        result = oracle_module.analyze_capture(capture_dir)
        actual = result.as_dict()
        expected = row["expected_v6_result"]
        assert actual == expected, (
            f"{row['fixture_id']}: frozen oracle replay {actual} != "
            f"checked-in expected_v6_result {expected}"
        )


def test_frozen_oracle_replay_outcomes_are_known(
    corpus_rows: list[dict[str, Any]],
) -> None:
    for row in corpus_rows:
        assert row["expected_v6_result"]["outcome"] in ALL_OUTCOMES, row["fixture_id"]


# ---------------------------------------------------------------------------
# D. migration ledger integrity (G7)
# ---------------------------------------------------------------------------


def test_migration_ledger_fixture_ids_unique(migration_rows: list[dict[str, Any]]) -> None:
    ids = [row["fixture_id"] for row in migration_rows]
    assert len(ids) == len(set(ids))


def test_migration_ledger_rows_reference_corpus_fixtures(
    migration_rows: list[dict[str, Any]], corpus_by_id: dict[str, dict[str, Any]]
) -> None:
    for row in migration_rows:
        assert row["fixture_id"] in corpus_by_id, row["fixture_id"]


def test_migration_ledger_from_outcome_matches_frozen_oracle(
    migration_rows: list[dict[str, Any]], corpus_by_id: dict[str, dict[str, Any]]
) -> None:
    for row in migration_rows:
        fixture = corpus_by_id[row["fixture_id"]]
        assert row["from_v6_outcome"] == fixture["expected_v6_result"]["outcome"]


def test_migration_ledger_to_outcome_matches_intended_v7_outcome(
    migration_rows: list[dict[str, Any]], corpus_by_id: dict[str, dict[str, Any]]
) -> None:
    for row in migration_rows:
        fixture = corpus_by_id[row["fixture_id"]]
        assert row["to_v7_outcome"] == fixture["intended_v7_outcome"]


def test_migration_required_agrees_with_actual_difference(
    corpus_rows: list[dict[str, Any]],
) -> None:
    """G7 migration semantics are outcome-to-outcome only: a fixture whose
    ``intended_v7_outcome`` equals its frozen v6 outcome needs no migration,
    regardless of any (undesigned, non-authoritative) reason-string detail
    inside ``expected_v6_result``."""

    for row in corpus_rows:
        differs = row["expected_v6_result"]["outcome"] != row["intended_v7_outcome"]
        assert row["migration_required"] == differs, row["fixture_id"]


def test_every_differing_fixture_has_exactly_one_ledger_row(
    corpus_rows: list[dict[str, Any]], migration_rows: list[dict[str, Any]]
) -> None:
    differing_ids = {
        row["fixture_id"]
        for row in corpus_rows
        if row["expected_v6_result"]["outcome"] != row["intended_v7_outcome"]
    }
    ledgered_ids = {row["fixture_id"] for row in migration_rows}
    assert differing_ids == ledgered_ids


def test_no_ledger_row_for_identical_outcome(
    corpus_rows: list[dict[str, Any]], migration_rows: list[dict[str, Any]]
) -> None:
    ledgered_ids = {row["fixture_id"] for row in migration_rows}
    for row in corpus_rows:
        if row["expected_v6_result"]["outcome"] == row["intended_v7_outcome"]:
            assert row["fixture_id"] not in ledgered_ids


def test_migration_ledger_reason_classes_are_allowed(
    migration_rows: list[dict[str, Any]],
) -> None:
    for row in migration_rows:
        assert row["reason_class"] in ALLOWED_REASON_CLASSES, row["fixture_id"]


def test_migration_ledger_reason_class_matches_the_cell_specific_matrix(
    migration_rows: list[dict[str, Any]],
) -> None:
    """Membership in ALLOWED_REASON_CLASSES is not sufficient: each pair has
    exactly one required reason_class (redesign doc §6.E), e.g. GAP -> PASS
    must be V6_INTERVAL_POLLUTION, never V6_UNSOUND."""

    for row in migration_rows:
        _check_reason_class_matches_matrix(row)


def test_non_ineligible_to_ineligible_rows_carry_a_source_ref(
    migration_rows: list[dict[str, Any]],
) -> None:
    for row in migration_rows:
        pair = (row["from_v6_outcome"], row["to_v7_outcome"])
        if pair in NON_INELIGIBLE_TO_INELIGIBLE_PAIRS:
            assert row["reason_class"] == "V6_UNSOUND"
            assert isinstance(row["source_ref"], str) and row["source_ref"].strip()


@pytest.mark.parametrize(
    ("from_outcome", "to_outcome", "wrong_reason_class"),
    [
        ("B_S1_GAP_DETECTED", "ANALYZER_PASS_TESTED_INTERLEAVING", "V6_UNSOUND"),
        ("ANALYZER_PASS_TESTED_INTERLEAVING", "HARNESS_INCOMPLETE", "V6_INTERVAL_POLLUTION"),
        (
            "GENERATOR_WINDOW_ENUMERATION_OMISSION_WITNESS",
            "B_S1_GAP_DETECTED",
            "V6_INTERVAL_POLLUTION",
        ),
    ],
)
def test_correct_pair_with_wrong_reason_class_is_rejected(
    from_outcome: str, to_outcome: str, wrong_reason_class: str
) -> None:
    """A ledger row with the *correct* outcome pair but the *wrong*
    reason_class must be rejected by the cell-specific matrix, even though
    both outcomes and the reason_class are each individually valid values.
    This exercises the exact same validation function the real ledger rows
    are checked with, so weakening that matrix would fail this test."""

    pair = (from_outcome, to_outcome)
    assert _classify_pair(*pair) == "ALLOWED_WITH_LEDGER"
    assert wrong_reason_class in ALLOWED_REASON_CLASSES
    assert wrong_reason_class != REQUIRED_REASON_CLASS_FOR_TRANSITION[pair]
    synthetic_row = {
        "fixture_id": "synthetic-wrong-reason-class-probe",
        "from_v6_outcome": from_outcome,
        "to_v7_outcome": to_outcome,
        "reason_class": wrong_reason_class,
    }
    with pytest.raises(AssertionError):
        _check_reason_class_matches_matrix(synthetic_row)


def test_every_differing_outcome_pair_is_classified(
) -> None:
    """Every (from, to) pair over ALL_OUTCOMES with from != to must land in
    exactly one G7 category -- SAME is excluded by construction, so this
    covers ABSOLUTE_FORBIDDEN, CONTRACT_ONLY, NOT_AUTHORIZED, and
    ALLOWED_WITH_LEDGER. None may be UNCLASSIFIED, and none may belong to
    more than one category."""

    seen_pairs: dict[tuple[str, str], str] = {}
    for from_outcome in ALL_OUTCOMES:
        for to_outcome in ALL_OUTCOMES:
            if from_outcome == to_outcome:
                continue
            pair = (from_outcome, to_outcome)
            category = _classify_pair(*pair)
            assert category != "UNCLASSIFIED", pair
            seen_pairs[pair] = category
    categories = [
        ABSOLUTE_FORBIDDEN_PAIRS,
        CONTRACT_ONLY_PAIRS,
        NOT_AUTHORIZED_PAIRS,
        frozenset(REQUIRED_REASON_CLASS_FOR_TRANSITION),
    ]
    for pair in seen_pairs:
        membership_count = sum(1 for group in categories if pair in group)
        assert membership_count == 1, pair


def test_migration_ledger_rows_are_exact_single_fixture_bindings(
    migration_rows: list[dict[str, Any]],
) -> None:
    """No wildcard fixture IDs and no wildcard result pairs: every row must
    bind exactly one concrete fixture_id and one concrete outcome pair."""

    for row in migration_rows:
        assert isinstance(row["fixture_id"], str) and row["fixture_id"]
        assert row["from_v6_outcome"] in ALL_OUTCOMES
        assert row["to_v7_outcome"] in ALL_OUTCOMES
        assert "*" not in row["fixture_id"]


# ---------------------------------------------------------------------------
# E. G7 absolute forbidden pairs
# ---------------------------------------------------------------------------


def test_no_ledger_row_represents_an_absolute_forbidden_pair(
    migration_rows: list[dict[str, Any]],
) -> None:
    for row in migration_rows:
        pair = (row["from_v6_outcome"], row["to_v7_outcome"])
        assert pair not in ABSOLUTE_FORBIDDEN_PAIRS, row["fixture_id"]


def test_no_ledger_row_represents_a_not_authorized_pair(
    migration_rows: list[dict[str, Any]],
) -> None:
    for row in migration_rows:
        pair = (row["from_v6_outcome"], row["to_v7_outcome"])
        assert pair not in NOT_AUTHORIZED_PAIRS, row["fixture_id"]


@pytest.mark.parametrize(
    ("from_outcome", "to_outcome"),
    sorted(ABSOLUTE_FORBIDDEN_PAIRS),
)
def test_absolute_forbidden_pairs_are_rejected_even_with_contract_amendment(
    from_outcome: str, to_outcome: str
) -> None:
    """No ledger reason, including CONTRACT_AMENDMENT, can legalize an
    absolute-forbidden cell. This is a property of the fixed pair set, not
    of any specific checked-in row."""

    candidate_row = {
        "fixture_id": "synthetic-forbidden-probe",
        "from_v6_outcome": from_outcome,
        "to_v7_outcome": to_outcome,
        "reason_class": "CONTRACT_AMENDMENT",
        "source_ref": "synthetic",
        "explanation": "synthetic probe row -- must never be accepted",
        "contract_amendment": "synthetic-accepted-amendment-reference",
    }
    pair = (candidate_row["from_v6_outcome"], candidate_row["to_v7_outcome"])
    assert pair in ABSOLUTE_FORBIDDEN_PAIRS


# ---------------------------------------------------------------------------
# F. contract-only cells
# ---------------------------------------------------------------------------


def test_current_ledger_has_zero_contract_amendment_rows(
    migration_rows: list[dict[str, Any]],
) -> None:
    """The current redesign does not loosen capture-v6 (S0.1 task §14): no
    checked-in row may use CONTRACT_AMENDMENT unless a separately accepted
    contract change actually exists, and none does yet."""

    contract_amendment_rows = [
        row for row in migration_rows if row["reason_class"] == "CONTRACT_AMENDMENT"
    ]
    assert contract_amendment_rows == []


def test_contract_only_pairs_require_nonempty_contract_amendment_reference(
    migration_rows: list[dict[str, Any]],
) -> None:
    for row in migration_rows:
        pair = (row["from_v6_outcome"], row["to_v7_outcome"])
        if pair in CONTRACT_ONLY_PAIRS:
            assert row["reason_class"] == "CONTRACT_AMENDMENT"
            assert row["contract_amendment"]
    # And the converse: any row currently claiming CONTRACT_AMENDMENT must
    # actually be a contract-only pair (no borrowing the label elsewhere).
    for row in migration_rows:
        if row["reason_class"] == "CONTRACT_AMENDMENT":
            pair = (row["from_v6_outcome"], row["to_v7_outcome"])
            assert pair in CONTRACT_ONLY_PAIRS


def test_non_contract_only_rows_have_no_contract_amendment_reference(
    migration_rows: list[dict[str, Any]],
) -> None:
    for row in migration_rows:
        pair = (row["from_v6_outcome"], row["to_v7_outcome"])
        if pair not in CONTRACT_ONLY_PAIRS:
            assert row["contract_amendment"] is None


# ---------------------------------------------------------------------------
# G. required witness presence
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture_id", sorted(REQUIRED_WITNESS_FIXTURE_IDS))
def test_required_witness_is_materialized(
    fixture_id: str, corpus_by_id: dict[str, dict[str, Any]]
) -> None:
    assert fixture_id in corpus_by_id


def test_required_witnesses_are_exactly_the_stop_four_plus_e1_e3(
    corpus_by_id: dict[str, dict[str, Any]],
) -> None:
    assert REQUIRED_WITNESS_FIXTURE_IDS.issubset(corpus_by_id)
    assert len(REQUIRED_WITNESS_FIXTURE_IDS) == 6


def test_required_witnesses_all_carry_a_migration_row(
    corpus_by_id: dict[str, dict[str, Any]], migration_rows: list[dict[str, Any]]
) -> None:
    """Every one of the six mandatory witnesses is, by construction, a case
    where frozen v6 is either wrong (the stop-four) or a model-derived leak
    (E1/E3) -- each must therefore carry exactly one migration ledger row."""

    ledgered_ids = {row["fixture_id"] for row in migration_rows}
    assert REQUIRED_WITNESS_FIXTURE_IDS.issubset(ledgered_ids)
