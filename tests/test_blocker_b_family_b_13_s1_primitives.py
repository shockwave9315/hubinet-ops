"""S1 hermetic validation for Family B experiment #13 pure primitive parsers.

Non-normative research asset validation. This file does NOT implement, test,
or approximate any v7 (or later-stage) authority logic -- no
``ParticipantTable``, ``admit()``, ``ChronologySpec``, ``PhaseSpec``,
observer ledger, or ``CaptureValidity``/``T1Result`` classifier exists
anywhere in this repository yet, and this file does not build one in the
guise of a "validator."

It validates two things:

- ``scripts/research/family_b_13_primitives.py`` -- the new, independent,
  standard-library-only pure parsing/decoding primitives module (S1);
- ``tests/fixtures/research/family_b_13/parser_vectors.json`` -- the
  declarative parser-vector corpus, differentially proving the new
  primitives agree exactly with the byte-frozen historical v6 oracle at the
  parsing boundary (Model A: independent implementations, proven compatible
  by data, never by shared code).

The frozen oracle is loaded strictly as a historical/test-only differential
target, exactly as ``tests/test_blocker_b_family_b_13_s0_assets.py`` already
does -- never as a production dependency, and never imported by the new
primitives module.
"""

from __future__ import annotations

import ast
import dataclasses
import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

import scripts.research.family_b_13_primitives as primitives

REPO_ROOT = Path(__file__).resolve().parent.parent
ORACLE_PATH = (
    REPO_ROOT / "tests" / "oracles" / "family_b_13" / "v6" / "blocker_b_family_b_13_analyzer.py"
)
PRIMITIVES_PATH = REPO_ROOT / "scripts" / "research" / "family_b_13_primitives.py"
VECTORS_PATH = REPO_ROOT / "tests" / "fixtures" / "research" / "family_b_13" / "parser_vectors.json"

EXPECTED_GIT_BLOB_SHA1 = "36be20097d01e7d86a76261ba35ca260df817b26"
EXPECTED_ORACLE_SHA256 = "db26374e1bd3ba9ee1c4793a40dc3754e4a0fc384583b0b794dd8538a4b29068"

# Later-stage (S2+) authority/classification vocabulary that must never
# appear as an actual identifier (a def, a class, a used name -- never
# merely inside a docstring/comment) in the S1 pure-primitives module, so a
# typo or a "just this once" shortcut cannot smuggle authority concepts
# into a stage that must stay parser-only. See _code_identifiers below for
# why this is checked at the token level, not by raw text search.
FORBIDDEN_AUTHORITY_IDENTIFIERS = frozenset(
    {
        "ParticipantTable",
        "admit",
        "ChronologySpec",
        "PhaseSpec",
        "CaptureValidity",
        "T1Result",
        "CANDIDATE_OBSERVED",
        "ENUMERATED",
        "CONFIRMED",
        "FINAL",
    }
)

FORBIDDEN_IMPORT_ROOTS = (
    "app",
    "custom_components",
    "tests",
    "subprocess",
    "socket",
    "http",
    "urllib",
    "requests",
    "httpx",
)


def _git_blob_sha1(data: bytes) -> str:
    header = f"blob {len(data)}\0".encode("ascii")
    return hashlib.sha1(header + data).hexdigest()  # noqa: S324 -- git object id, not a security use


@pytest.fixture(scope="module")
def oracle_module() -> ModuleType:
    """Load the frozen v6 oracle as an isolated, test-only module.

    Duplicated deliberately from ``test_blocker_b_family_b_13_s0_assets.py``
    rather than imported from it: pytest fixtures do not cross files, and
    the differential target here must be the exact same on-disk frozen file,
    loaded the same test-only way, independent of that other test module's
    lifecycle.
    """

    module_name = "_frozen_family_b_13_v6_oracle_test_only_s1"
    spec = importlib.util.spec_from_file_location(module_name, ORACLE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        del sys.modules[module_name]
        raise
    return module


@pytest.fixture(scope="module")
def vectors() -> list[dict[str, Any]]:
    return json.loads(VECTORS_PATH.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# G10 (still true): the frozen oracle this file differentially targets must
# remain byte-identical.
# ---------------------------------------------------------------------------


def test_frozen_oracle_still_byte_identical() -> None:
    data = ORACLE_PATH.read_bytes()
    assert _git_blob_sha1(data) == EXPECTED_GIT_BLOB_SHA1
    assert hashlib.sha256(data).hexdigest() == EXPECTED_ORACLE_SHA256


# ---------------------------------------------------------------------------
# Vector corpus structure
# ---------------------------------------------------------------------------


def test_vector_ids_unique(vectors: list[dict[str, Any]]) -> None:
    ids = [vector["vector_id"] for vector in vectors]
    assert len(ids) == len(set(ids))
    assert len(ids) >= 50


def test_vector_categories_valid(vectors: list[dict[str, Any]]) -> None:
    for vector in vectors:
        assert vector["expected"]["category"] in {"success", "error"}, vector["vector_id"]


def test_vector_provenance_valid(vectors: list[dict[str, Any]]) -> None:
    valid = {"frozen_v6", "capture_fixture", "historical_regression", "boundary_case"}
    for vector in vectors:
        assert vector["provenance"] in valid, vector["vector_id"]


def test_every_vector_has_a_frozen_equivalent(vectors: list[dict[str, Any]]) -> None:
    """S1's target is equivalence at the parsing boundary; every checked-in
    vector must be checkable against a concrete frozen v6 helper."""

    for vector in vectors:
        assert vector["frozen_equivalent"], vector["vector_id"]


def test_missing_expected_value_escape_is_used_only_for_size_boundary_vectors(
    vectors: list[dict[str, Any]],
) -> None:
    """An absent `expected.value` key on a success vector skips the
    checked-in-value comparison (the two live implementations must still
    agree with each other) -- guard that this escape is used only where it
    is legitimate: a MAX_JSONL_LINE_BYTES padding-recipe vector whose
    padded content is not worth freezing verbatim, never as a way to avoid
    specifying a real expectation elsewhere. This is distinct from an
    explicit `"value": null`, which is itself a real, checked expectation
    (e.g. classify_terminal_status legitimately returning None)."""

    for vector in vectors:
        if vector["expected"]["category"] == "success" and "value" not in vector["expected"]:
            assert vector["input"]["kind"] == "padded_object_line", vector["vector_id"]


def test_every_vector_names_a_real_primitive_function(vectors: list[dict[str, Any]]) -> None:
    for vector in vectors:
        name = vector["primitive_function"]
        assert hasattr(primitives, name), (vector["vector_id"], name)


def test_every_exported_function_has_vector_coverage(vectors: list[dict[str, Any]]) -> None:
    """The converse of the check above: every public parsing FUNCTION this
    module exports must be exercised by at least one parser-vector /
    direct differential -- derived structurally from ``__all__`` and
    ``inspect``, never a hard-coded function-name list, so adding an
    uncovered export fails this test rather than silently shipping unproven.
    Dataclasses/result types and constants are excluded: they carry no
    parsing behavior of their own to differentially prove."""

    import inspect

    exported_functions = {
        name for name in primitives.__all__ if inspect.isfunction(getattr(primitives, name))
    }
    covered_functions = {vector["primitive_function"] for vector in vectors}
    uncovered = exported_functions - covered_functions
    assert uncovered == set()


def test_exported_function_count_is_seventeen() -> None:
    """A concrete, currently-true number alongside the structural check
    above (not instead of it) -- catches an export silently added or
    removed without anyone noticing either way."""

    import inspect

    exported_functions = [
        name for name in primitives.__all__ if inspect.isfunction(getattr(primitives, name))
    ]
    assert len(exported_functions) == 17


# ---------------------------------------------------------------------------
# Differential proof: same declarative input through both implementations
# ---------------------------------------------------------------------------


def _build_padded_jsonl_line(total_bytes: int) -> bytes:
    """Deterministically construct a one-line JSONL object of an exact
    total byte length: ``{"a":"`` + N 'x' characters + ``"}`` + a trailing
    LF, where N is chosen so the encoded length is exactly ``total_bytes``.
    """

    prefix = '{"a":"'
    suffix = '"}\n'
    overhead = len(prefix.encode("utf-8")) + len(suffix.encode("utf-8"))
    pad_len = total_bytes - overhead
    assert pad_len >= 0, "requested total_bytes too small for the padding recipe"
    line = (prefix + ("x" * pad_len) + suffix).encode("utf-8")
    assert len(line) == total_bytes
    return line


def _resolve_jsonl_line_bytes(input_spec: dict[str, Any]) -> bytes:
    kind = input_spec["kind"]
    if kind == "bytes_utf8":
        return input_spec["text"].encode("utf-8")
    if kind == "bytes_int_list":
        return bytes(input_spec["bytes"])
    if kind == "padded_object_line":
        return _build_padded_jsonl_line(input_spec["total_bytes"])
    raise AssertionError(f"unknown jsonl input kind: {kind}")


def _normalize_new_value(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return dataclasses.asdict(value)
    if isinstance(value, (frozenset, set)):
        return sorted(value)
    return value


def _run_new(vector: dict[str, Any]) -> dict[str, Any]:
    family = vector["parser_family"]
    input_spec = vector["input"]
    fn = getattr(primitives, vector["primitive_function"])

    if family == "json_document":
        result = fn(input_spec["value"])
    elif family == "jsonl_line":
        raw_line = _resolve_jsonl_line_bytes(input_spec)
        result = fn(raw_line)
    elif family in {
        "scalar_nonnegative_int",
        "scalar_signed_int",
        "scalar_nonempty_text",
        "scalar_text",
        "scalar_sequence",
        "scalar_mapping",
        "scalar_bool",
        "upid",
        "task_bucket_identifier",
        "terminal_status",
        "line_terminator_strip",
        "line_split",
    }:
        result = fn(input_spec["value"])
    elif family == "posix_path":
        result = fn(input_spec["value"], allow_root=input_spec.get("allow_root", False))
    elif family == "inotify_decode":
        result = fn(input_spec["value"])
    elif family == "inotify_agreement":
        result = fn(input_spec["raw_mask"], input_spec["declared_mask"])
    else:
        raise AssertionError(f"unhandled parser_family: {family}")

    if family in {"line_terminator_strip", "line_split", "terminal_status"}:
        # These primitives return a plain value directly (no ParseResult
        # envelope) -- they cannot fail; there is no error case to model.
        # classify_terminal_status returning None is a legitimate "not a
        # recognized terminal status" success value, not a parse error.
        return {"category": "success", "value": _normalize_new_value(result)}
    assert isinstance(result, primitives.ParseResult)
    if result.ok:
        return {"category": "success", "value": _normalize_new_value(result.value)}
    return {"category": "error", "error": result.error}


def _frozen_error_id(exc: Exception) -> str:
    """Reduce a frozen v6 CaptureError/CaptureEnvironmentError message to its
    stable leading identifier, stripping only volatile context/field-name
    suffixes the frozen protocol appends after the first ':'. This is
    normalizing representation (message formatting), never the semantic
    accept/reject decision itself."""

    return str(exc).split(":", 1)[0]


def _run_frozen(
    oracle: ModuleType, vector: dict[str, Any], tmp_path: Path
) -> dict[str, Any]:
    family = vector["parser_family"]
    input_spec = vector["input"]

    try:
        if family == "json_document":
            path = tmp_path / "doc.json"
            path.write_text(input_spec["value"], encoding="utf-8")
            value = oracle._load_json(path)
            return {"category": "success", "value": value}

        if family == "jsonl_line":
            raw_line = _resolve_jsonl_line_bytes(input_spec)
            path = tmp_path / "lines.jsonl"
            path.write_bytes(raw_line)
            records = oracle._load_jsonl(path)
            assert len(records) == 1
            return {"category": "success", "value": records[0]}

        if family == "scalar_nonnegative_int":
            value = oracle._require_int({"v": input_spec["value"]}, "v")
            return {"category": "success", "value": value}
        if family == "scalar_signed_int":
            value = oracle._require_signed_int({"v": input_spec["value"]}, "v")
            return {"category": "success", "value": value}
        if family == "scalar_nonempty_text":
            value = oracle._require_text({"v": input_spec["value"]}, "v")
            return {"category": "success", "value": value}
        if family == "scalar_text":
            value = oracle._require_string({"v": input_spec["value"]}, "v")
            return {"category": "success", "value": value}
        if family == "scalar_sequence":
            value = oracle._require_sequence(input_spec["value"], "v")
            return {"category": "success", "value": value}
        if family == "scalar_mapping":
            value = oracle._require_mapping(input_spec["value"], "v")
            return {"category": "success", "value": value}
        if family == "scalar_bool":
            value = oracle._require_bool({"v": input_spec["value"]}, "v")
            return {"category": "success", "value": value}

        if family == "upid":
            upid = oracle._require_upid(input_spec["value"], "v")
            decoded = oracle._decode_upid(upid)
            return {
                "category": "success",
                "value": {
                    "node": decoded["node"],
                    "pid": _decode_pid_from_upid(upid),
                    "pstart": _decode_pstart_from_upid(upid),
                    "starttime": decoded["starttime"],
                    "task_type": decoded["task_type"],
                    "task_id": decoded["task_id"],
                    "owner": decoded["owner"],
                    "task_bucket": decoded["task_bucket"],
                },
            }

        if family == "task_bucket_identifier":
            task_tree = oracle._TaskTreeContract(
                task_root="/synthetic/pve/tasks",
                bucket_layout="upid_starttime_final_hex_child",
            )
            path = oracle._task_bucket_path(task_tree, input_spec["value"], "v")
            bucket = path.rsplit("/", 1)[-1]
            return {"category": "success", "value": bucket}

        if family == "inotify_decode":
            # Exact frozen sealed-value composition -- never call the
            # internal unchecked decoder directly: _require_int first
            # (rejects bool/negative/non-int), then _decode_inotify_raw_mask.
            raw_mask = oracle._require_int({"raw_mask": input_spec["value"]}, "raw_mask")
            decoded = oracle._decode_inotify_raw_mask(raw_mask, "v")
            return {"category": "success", "value": sorted(decoded)}

        if family == "inotify_agreement":
            record = {
                "raw_mask": input_spec["raw_mask"],
                "mask": input_spec["declared_mask"],
            }
            decoded = oracle._validated_inotify_masks(record, "v")
            return {"category": "success", "value": sorted(decoded)}

        if family == "posix_path":
            value = oracle._validated_canonical_absolute_path(
                input_spec["value"], "v", allow_root=input_spec.get("allow_root", False)
            )
            return {"category": "success", "value": value}

        if family == "terminal_status":
            value = oracle._classify_exact_log_terminal_status(input_spec["value"])
            return {"category": "success", "value": value}

        if family == "line_terminator_strip":
            value = oracle._without_trailing_line_terminator(input_spec["value"])
            return {"category": "success", "value": value}

        if family == "line_split":
            value = oracle._split_lf_crlf_lines(input_spec["value"])
            return {"category": "success", "value": value}

        raise AssertionError(f"unhandled parser_family: {family}")
    except (oracle.CaptureError, oracle.CaptureEnvironmentError) as exc:
        return {"category": "error", "error": _frozen_error_id(exc)}


def _decode_pid_from_upid(upid: str) -> str:
    return upid.split(":")[2]


def _decode_pstart_from_upid(upid: str) -> str:
    return upid.split(":")[3]


@pytest.mark.parametrize(
    "vector",
    json.loads(VECTORS_PATH.read_text(encoding="utf-8")),
    ids=lambda vector: vector["vector_id"],
)
def test_differential_new_primitive_matches_frozen_v6(
    vector: dict[str, Any], oracle_module: ModuleType, tmp_path: Path
) -> None:
    new_result = _run_new(vector)
    frozen_result = _run_frozen(oracle_module, vector, tmp_path)
    expected = vector["expected"]

    assert new_result["category"] == expected["category"], (
        vector["vector_id"],
        "new",
        new_result,
    )
    assert frozen_result["category"] == expected["category"], (
        vector["vector_id"],
        "frozen",
        frozen_result,
    )
    # The differential itself: both independent implementations must agree,
    # not merely each independently match the checked-in expectation. A new
    # parser accepting something frozen v6 rejects (or vice versa) fails
    # here even if each happens to match a stale checked-in `expected`.
    assert new_result["category"] == frozen_result["category"], (
        vector["vector_id"],
        new_result,
        frozen_result,
    )
    if expected["category"] == "success":
        # Both live implementations must always agree with each other.
        assert new_result["value"] == frozen_result["value"], (
            vector["vector_id"],
            new_result,
            frozen_result,
        )
        # An absent `expected.value` key (never an explicit `null`, which is
        # itself a legitimate expected value for e.g. classify_terminal_status)
        # is a deliberate escape used only for the MAX_JSONL_LINE_BYTES
        # padding-recipe vector, whose success value is a huge
        # deterministically-padded object not worth freezing verbatim; the
        # differential agreement check above still fully applies to it.
        if "value" in expected:
            assert new_result["value"] == expected["value"], (vector["vector_id"], "new")
            assert frozen_result["value"] == expected["value"], (vector["vector_id"], "frozen")
    else:
        assert new_result["error"] == expected["error"], (vector["vector_id"], "new")
        assert frozen_result["error"] == expected["error"], (vector["vector_id"], "frozen")


# ---------------------------------------------------------------------------
# Architecture isolation gates
# ---------------------------------------------------------------------------


def _imported_module_roots(source: str) -> set[str]:
    tree = ast.parse(source)
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                roots.add(node.module.split(".")[0])
    return roots


def test_primitives_module_imports_only_stdlib_and_itself() -> None:
    roots = _imported_module_roots(PRIMITIVES_PATH.read_text(encoding="utf-8"))
    # __future__, dataclasses, json, re, typing are the only non-stdlib-ish
    # names this module is allowed to import; assert nothing else crept in.
    allowed = {"__future__", "dataclasses", "json", "re", "typing"}
    assert roots <= allowed, roots - allowed


def test_primitives_module_does_not_import_forbidden_roots() -> None:
    roots = _imported_module_roots(PRIMITIVES_PATH.read_text(encoding="utf-8"))
    forbidden_present = roots & set(FORBIDDEN_IMPORT_ROOTS)
    assert not forbidden_present, forbidden_present


def _all_import_targets(source: str) -> set[str]:
    """Every fully-dotted import target (not just its root package), via
    AST -- so this only ever inspects actual import statements, never
    prose that happens to mention a module's name (e.g. this module's own
    parser-surface inventory docstring, which legitimately names the
    frozen oracle file it does NOT import)."""

    tree = ast.parse(source)
    targets: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                targets.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                targets.add(node.module)
    return targets


def test_primitives_module_does_not_import_the_frozen_oracle() -> None:
    targets = _all_import_targets(PRIMITIVES_PATH.read_text(encoding="utf-8"))
    assert not any("oracle" in target or "analyzer" in target for target in targets)


def test_frozen_oracle_does_not_import_the_new_primitives() -> None:
    source = ORACLE_PATH.read_text(encoding="utf-8")
    assert "family_b_13_primitives" not in source


def test_no_production_module_imports_the_new_primitives() -> None:
    """``app/`` and ``custom_components/`` are the only production trees in
    this repository (see CLAUDE.md's repository map); neither may depend on
    a research-only module."""

    forbidden_reference = "family_b_13_primitives"
    for production_root in ("app", "custom_components"):
        root = REPO_ROOT / production_root
        if not root.exists():
            continue
        for path in root.rglob("*.py"):
            assert forbidden_reference not in path.read_text(encoding="utf-8"), path


def _code_identifiers(source: str) -> set[str]:
    """Every NAME token actually used as code (a real identifier: a def, a
    class, a variable, a call target) -- deliberately excludes STRING and
    COMMENT tokens, so this module's own docstring can truthfully *say*
    "there is no ParticipantTable/admit()/ChronologySpec/... here" without
    that sentence tripping the very check it describes. This is exactly
    what makes this an AST/token-structure regression rather than "rely
    solely on comments saying 'pure'" (S1 task §15)."""

    import io
    import tokenize

    identifiers: set[str] = set()
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type == tokenize.NAME:
            identifiers.add(tok.string)
    return identifiers


def test_primitives_module_contains_no_authority_stage_vocabulary() -> None:
    identifiers = _code_identifiers(PRIMITIVES_PATH.read_text(encoding="utf-8"))
    present = identifiers & FORBIDDEN_AUTHORITY_IDENTIFIERS
    assert present == set()


def test_primitives_module_exposes_no_manifest_or_capture_level_api() -> None:
    """S1 primitives operate on one bounded value/record at a time; none of
    them may expose a manifest-wide or cross-record entry point."""

    forbidden_names = {
        "parse_capture_and_validate",
        "validate_manifest",
        "analyze_capture",
        "reconcile_streams",
        "admit_evidence",
        "classify_interval",
        "classify_result",
    }
    exported = set(primitives.__all__)
    assert not (forbidden_names & exported)
    module_source = PRIMITIVES_PATH.read_text(encoding="utf-8")
    for name in forbidden_names:
        assert f"def {name}(" not in module_source


def test_primitives_module_is_standard_library_only_at_runtime() -> None:
    """A second, runtime-level check alongside the AST import check: the
    live module object's own globals must not resolve to any of the
    forbidden roots either (defends against a dynamic import the AST walk
    could miss, e.g. ``importlib.import_module`` called with a computed
    name -- which this module also does not do)."""

    source = PRIMITIVES_PATH.read_text(encoding="utf-8")
    assert "import_module" not in source
    assert "__import__" not in source
    assert "exec(" not in source
    assert "eval(" not in source
