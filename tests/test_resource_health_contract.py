"""Operator-declared per-resource health contracts: schema, authority, races.

The product rule these tests exist to defend is narrow and absolute: a health
contract says what "healthy" would mean for ONE exact resource incarnation,
and the absence of one is *unconfigured*, never a pass. Everything below is
either that rule, the bounds that keep the durable row deterministic, the
atomicity that keeps a half-written contract out of any committed state, or
the read-time verification that catches an inconsistent row set a direct-SQL
repair could still have reconstructed.

A current package-managed LXC is nevertheless never *left* unconfigured: the
backend provisions the built-in `guest_operational` default for it during
reconciliation, so revision 1 of any LXC here is that default and an
explicit operator contract starts at revision 2. The primitives above still
have to distinguish "no contract" from "a contract", so the tests that are
about that distinction use `_unconfigured` to get back there deliberately.

No health EXECUTION exists in this stage, so nothing here runs, schedules, or
interprets a probe.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3

import pytest

from app.inventory import (
    AuthorityConflict,
    DEFAULT_HEALTH_PROBES,
    AuthorityInvariantError,
    AuthorityNotFound,
    HealthContractError,
    HealthContractRevisionConflict,
    HealthProbeKind,
    InventoryAuthority,
    InventoryAuthorityStore,
    InventoryPublication,
    MAX_HEALTH_PROBES,
    MAX_HEALTH_PROBE_TARGET_LENGTH,
    ResourceHealthProbe,
    health_contract_fingerprint,
)
from app.inventory.discovery import (
    BaselineCompleteness,
    BaselineMode,
    DetailReadStatus,
    DiscoveredNode,
    DiscoveredResource,
    NormalizedDiscoverySnapshot,
    ProviderGuestLocatorSet,
    ProviderNodeScope,
    SourceAvailability,
)
from tests.test_package_scan_authority import START, _reconcile, _system


def _rediscover(
    authority: InventoryAuthority,
    source_id: str,
    *,
    resource_type: str = "lxc",
    name: str = "guest",
    node_name: str = "pve-a",
    node_names: tuple[str, ...] = ("pve-a",),
    observed_at: str = START.isoformat(),
) -> None:
    """One more complete discovery run at the SAME VMID.

    A resource-type change is this architecture's VMID-reuse replacement
    signal; anything else is the same durable resource observed again, with
    whatever name or node it now has.
    """

    run = authority.issue_discovery_run(source_id, 1)
    authority.finalize_successful_discovery_run(
        source_id,
        run.run_id,
        NormalizedDiscoverySnapshot(
            run_id=run.run_id,
            discovery_run_sequence=run.discovery_run_sequence,
            inventory_source_id=source_id,
            expected_source_config_revision=run.expected_source_config_revision,
            endpoint_id=run.expected_endpoint_id,
            canonical_transport_locator=run.expected_canonical_transport_locator,
            canonicalization_contract_version=run.expected_canonicalization_contract_version,
            expected_transport_trust_revision=run.expected_transport_trust_revision,
            provider_contract_version=1,
            observed_at=observed_at,
            source_facts={},
            source_availability=SourceAvailability.AVAILABLE,
            baseline_completeness=BaselineCompleteness.COMPLETE,
            baseline_mode=BaselineMode.CLUSTER,
            acl_topology_hash_before="acl",
            acl_topology_hash_after="acl",
            permission_snapshot_hash_before="permissions",
            permission_snapshot_hash_after="permissions",
            permission_coverage_complete=True,
            boundary_consistent=True,
            covered_nodes=node_names,
            failed_baseline_scopes=(),
            detail_summary={
                "ok_count": 1,
                "temporarily_unavailable_count": 0,
                "error_count": 0,
            },
            failed_detail_scopes=(),
            nodes=tuple(
                DiscoveredNode(item, "online", True, observed_at, {})
                for item in node_names
            ),
            resources=(
                DiscoveredResource(
                    source_id,
                    101,
                    resource_type,
                    name,
                    "running",
                    node_name,
                    observed_at,
                    DetailReadStatus.OK,
                    {},
                ),
            ),
            provider_node_scope=ProviderNodeScope._from_provider(
                BaselineMode.CLUSTER, node_names
            ),
            provider_guest_locators=ProviderGuestLocatorSet._from_provider(
                ({"vmid": 101, "type": resource_type, "node": node_name},)
            ),
        ),
    )


SYSTEMD = HealthProbeKind.SYSTEMD_UNIT_ACTIVE
# v0.5 dropped Docker-specific health probes entirely (see
# tests/test_health_scope_reduction.py for the explicit-refusal coverage).
# `systemd_unit_active` is now the only targeted advanced kind, and these two
# names stay distinct aliases for it -- purely so the many generic
# revision/versioning/race tests below that only need "two probes with
# different (kind, target) identities" read the way they always did, never
# because the two names still mean two different KINDS.
RUNNING = HealthProbeKind.SYSTEMD_UNIT_ACTIVE
HEALTHY = HealthProbeKind.SYSTEMD_UNIT_ACTIVE


def _probes(*pairs: tuple[HealthProbeKind, str]) -> tuple[ResourceHealthProbe, ...]:
    return tuple(ResourceHealthProbe(kind=kind, target=target) for kind, target in pairs)


DEFAULT_PROBES = _probes(
    (SYSTEMD, "nginx.service"),
    (HEALTHY, "immich_server"),
)


def _health_contract_view(store, authority, resource_id):
    view = InventoryPublication(store, authority).read()
    resource = next(
        item for item in view.resources if item["resource_id"] == resource_id
    )
    return resource["health_contract"]


def _raw_rows(store) -> tuple[list, list]:
    with sqlite3.connect(store.path) as connection:
        connection.row_factory = sqlite3.Row
        contracts = connection.execute(
            "SELECT * FROM resource_health_contracts"
        ).fetchall()
        probes = connection.execute(
            "SELECT * FROM resource_health_contract_probes ORDER BY resource_id, probe_index"
        ).fetchall()
    return contracts, probes


def _last_allocated_revision(store, resource_id: str) -> int | None:
    with sqlite3.connect(store.path) as connection:
        row = connection.execute(
            "SELECT last_revision FROM resource_health_contract_revision_state "
            "WHERE resource_id=?",
            (resource_id,),
        ).fetchone()
    return None if row is None else int(row[0])


GUEST_OPERATIONAL = HealthProbeKind.GUEST_OPERATIONAL


def _unconfigured(authority, resource_id: str) -> None:
    """Take a current managed LXC back to genuinely *unconfigured*.

    The low-level clear is the only route there, because reconciliation
    provisions the built-in default for every current managed LXC. It does
    not rewind the revision allocator, so the first explicit contract after
    it is revision 2 -- exactly the anti-ABA property `clear` has always had.
    """

    authority.clear_resource_health_contract(resource_id)


# ===========================================================================
# guest_operational: the v0.5 built-in DEFAULT, never a faked target.
# ===========================================================================


def test_guest_operational_can_be_declared_with_no_target(tmp_path: Path) -> None:
    _, store, authority, resource = _system(tmp_path)
    rid = resource.resource_id

    contract = authority.replace_resource_health_contract(
        rid, (ResourceHealthProbe(kind=GUEST_OPERATIONAL, target=None),)
    )
    assert contract.probes == (
        ResourceHealthProbe(kind=GUEST_OPERATIONAL, target=None),
    )
    assert authority.resource_health_contract(rid).probes[0].target is None

    # Durable, and correctly nullable in the real schema, not merely in the
    # Python model.
    _, probe_rows = _raw_rows(store)
    (row,) = [row for row in probe_rows if row["resource_id"] == rid]
    assert row["target"] is None
    assert row["kind"] == "guest_operational"


def test_guest_operational_rejects_a_supplied_target(tmp_path: Path) -> None:
    """No faked target -- `"guest"`, a VMID string, anything -- is accepted
    for the one kind that names no container or unit."""

    _, _, authority, resource = _system(tmp_path)
    with pytest.raises(HealthContractError, match="must not carry a target"):
        authority.replace_resource_health_contract(
            resource.resource_id,
            (ResourceHealthProbe(kind=GUEST_OPERATIONAL, target="guest"),),
        )


def test_every_other_kind_still_requires_a_target(tmp_path: Path) -> None:
    _, _, authority, resource = _system(tmp_path)
    with pytest.raises(HealthContractError):
        authority.replace_resource_health_contract(
            resource.resource_id,
            (ResourceHealthProbe(kind=RUNNING, target=None),),
        )


def test_at_most_one_guest_operational_probe_is_ever_accepted(
    tmp_path: Path,
) -> None:
    """Two `guest_operational` probes have the IDENTICAL (kind, None)
    identity, so the existing duplicate-probe rule is exactly the "at most
    one" rule for this kind -- no separate mechanism was invented."""

    _, _, authority, resource = _system(tmp_path)
    with pytest.raises(HealthContractError, match="duplicate"):
        authority.replace_resource_health_contract(
            resource.resource_id,
            (
                ResourceHealthProbe(kind=GUEST_OPERATIONAL, target=None),
                ResourceHealthProbe(kind=GUEST_OPERATIONAL, target=None),
            ),
        )


def test_guest_operational_and_systemd_mixed_contract_refused(
    tmp_path: Path,
) -> None:
    """v0.5 health scope reduction: the baseline and an advanced contract
    never mix. `guest_operational` is the backend-owned baseline; an explicit
    `systemd_unit_active` contract REPLACES it entirely, it does not extend
    it -- mixing them would silently reintroduce the settling coupling the
    baseline is deliberately free of (PRODUCT.md, "What healthy means")."""

    _, _, authority, resource = _system(tmp_path)
    with pytest.raises(HealthContractError, match="guest_operational"):
        authority.replace_resource_health_contract(
            resource.resource_id,
            DEFAULT_PROBES + (ResourceHealthProbe(kind=GUEST_OPERATIONAL, target=None),),
        )


def test_sql_directly_refuses_inserting_a_non_null_target_for_guest_operational(
    tmp_path: Path,
) -> None:
    """Defense in depth in the REAL deployed schema, not just the standalone
    mirror this test file's SQL-level section already exercises for the
    other kinds. Probe rows are insert-only (never updated in place), so
    this proves the CHECK constraint at the one place a bad row could ever
    be introduced."""

    _, store, authority, resource = _system(tmp_path)
    with sqlite3.connect(store.path) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO resource_health_contract_probes "
                "VALUES (?, 0, 'guest_operational', 'fake')",
                (resource.resource_id,),
            )


def test_sql_directly_refuses_a_guest_operational_row_beside_another_probe(
    tmp_path: Path,
) -> None:
    """v21 defense in depth: `resource_health_contract_no_mixed_baseline`
    refuses a `guest_operational` row the moment its parent contract's own
    declared `probe_count` is not exactly 1 -- independently of the Python
    `canonical_health_probes` check every ordinary write already goes
    through, and independently of `_ONE_GUEST_OPERATIONAL_PROBE_SQL`'s
    partial unique index (which only stops a SECOND `guest_operational`
    row, not a DIFFERENT kind alongside the first)."""

    _, store, authority, resource = _system(tmp_path)
    existing = authority.resource_health_contract(resource.resource_id)
    assert existing.probes == (ResourceHealthProbe(kind=GUEST_OPERATIONAL, target=None),)

    with sqlite3.connect(store.path) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        # Replace the real singleton contract row with one that CLAIMS two
        # probes but has none yet -- the exact pre-state a legitimate
        # two-probe contract has for one instant during its own atomic
        # replacement (contract row inserted, probe rows not yet filled).
        connection.execute(
            "DELETE FROM resource_health_contracts WHERE resource_id=?",
            (resource.resource_id,),
        )
        connection.execute(
            "DELETE FROM resource_health_contract_probes WHERE resource_id=?",
            (resource.resource_id,),
        )
        connection.execute(
            "INSERT INTO resource_health_contracts("
            "resource_id, revision, fingerprint, probe_count, created_at, "
            "updated_at) VALUES (?, ?, ?, 2, ?, ?)",
            (
                resource.resource_id,
                existing.revision,
                existing.fingerprint,
                existing.created_at,
                existing.updated_at,
            ),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO resource_health_contract_probes"
                "(resource_id, probe_index, kind, target) "
                "VALUES (?, 0, 'guest_operational', NULL)",
                (resource.resource_id,),
            )


# ===========================================================================
# A. SCHEMA
# ===========================================================================


def test_fresh_database_is_schema_v15_with_the_health_contract_tables(
    tmp_path: Path,
) -> None:
    from app.inventory.store import AUTHORITY_SCHEMA_MARKER, AUTHORITY_SCHEMA_VERSION

    assert AUTHORITY_SCHEMA_VERSION == 21
    InventoryAuthorityStore(tmp_path / "authority.db")
    with sqlite3.connect(tmp_path / "authority.db") as connection:
        marker, version = connection.execute(
            "SELECT marker, schema_version FROM authority_schema"
        ).fetchone()
        user_version = connection.execute("PRAGMA user_version").fetchone()[0]
        names = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','trigger','index')"
            ).fetchall()
        }
    assert (marker, version, user_version) == (
        AUTHORITY_SCHEMA_MARKER,
        AUTHORITY_SCHEMA_VERSION,
        AUTHORITY_SCHEMA_VERSION,
    )
    assert {
        "resource_health_contracts",
        "resource_health_contract_probes",
        "resource_health_contract_revision_state",
        "resource_health_contract_update_immutable",
        "resource_health_contract_revision_never_regresses",
        "resource_health_contract_revision_state_no_delete",
        "resource_health_contract_revision_is_the_allocated_one",
        "resource_health_contract_probe_belongs_to_declared_contract",
        "resource_health_contract_probe_update_immutable",
        "resource_health_contract_probe_delete_needs_no_live_contract",
    } <= names


def test_sql_permits_exactly_one_contract_per_resource(tmp_path: Path) -> None:
    _, store, authority, resource = _system(tmp_path)
    authority.replace_resource_health_contract(resource.resource_id, DEFAULT_PROBES)
    with sqlite3.connect(store.path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO resource_health_contracts("
                "resource_id, revision, fingerprint, probe_count, created_at, updated_at) "
                "VALUES(?, 9, ?, 1, 'x', 'x')",
                (resource.resource_id, "b" * 64),
            )


def test_sql_refuses_an_empty_contract_and_an_over_bound_probe_count(
    tmp_path: Path,
) -> None:
    """`probe_count = 0` is the "absence is not health" rule in SQL."""

    _, store, _authority, resource = _system(tmp_path)
    with sqlite3.connect(store.path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        for probe_count in (0, -1, MAX_HEALTH_PROBES + 1):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO resource_health_contracts("
                    "resource_id, revision, fingerprint, probe_count, created_at, "
                    "updated_at) VALUES(?, 1, ?, ?, 'x', 'x')",
                    (resource.resource_id, "a" * 64, probe_count),
                )


def test_sql_refuses_an_unsupported_probe_kind_and_a_malformed_target(
    tmp_path: Path,
) -> None:
    _, store, authority, resource = _system(tmp_path)
    authority.replace_resource_health_contract(
        resource.resource_id, _probes((SYSTEMD, "nginx.service"))
    )
    with sqlite3.connect(store.path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        # A kind outside the three the product can truthfully execute.
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO resource_health_contract_probes("
                "resource_id, probe_index, kind, target) VALUES(?, 1, 'http_get', 'x')",
                (resource.resource_id,),
            )
        # A target that would stop being one bounded opaque argument.
        for target in ("", "a b", "a\tb", "a\nb", "x" * (MAX_HEALTH_PROBE_TARGET_LENGTH + 1)):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO resource_health_contract_probes("
                    "resource_id, probe_index, kind, target) "
                    "VALUES(?, 1, 'systemd_unit_active', ?)",
                    (resource.resource_id, target),
                )


def test_sql_refuses_a_duplicate_probe_and_a_probe_beyond_the_declared_set(
    tmp_path: Path,
) -> None:
    _, store, authority, resource = _system(tmp_path)
    authority.replace_resource_health_contract(
        resource.resource_id, _probes((SYSTEMD, "nginx.service"))
    )
    with sqlite3.connect(store.path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        # Index 1 is beyond probe_count=1: the contract declared one probe.
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO resource_health_contract_probes("
                "resource_id, probe_index, kind, target) "
                "VALUES(?, 1, 'systemd_unit_active', 'redis.service')",
                (resource.resource_id,),
            )
        # And a duplicate identity is refused even at a legal-looking index.
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO resource_health_contract_probes("
                "resource_id, probe_index, kind, target) "
                "VALUES(?, 0, 'systemd_unit_active', 'nginx.service')",
                (resource.resource_id,),
            )


def test_sql_refuses_editing_a_contract_or_shrinking_a_live_probe_set(
    tmp_path: Path,
) -> None:
    """A contract is replaced whole or cleared -- never patched underneath."""

    _, store, authority, resource = _system(tmp_path)
    authority.replace_resource_health_contract(resource.resource_id, DEFAULT_PROBES)
    with sqlite3.connect(store.path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE resource_health_contracts SET revision=99 WHERE resource_id=?",
                (resource.resource_id,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE resource_health_contract_probes SET target='other' "
                "WHERE resource_id=?",
                (resource.resource_id,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "DELETE FROM resource_health_contract_probes "
                "WHERE resource_id=? AND probe_index=1",
                (resource.resource_id,),
            )
    # The complete original contract is untouched by all three refusals.
    # Revision 2: revision 1 is the built-in default this replaced.
    stored = store.resource_health_contract(resource.resource_id)
    assert stored is not None
    assert stored.revision == 2
    assert stored.probes == tuple(
        sorted(DEFAULT_PROBES, key=lambda probe: (probe.kind.value, probe.target))
    )


def test_a_probe_row_cannot_outlive_its_contract(tmp_path: Path) -> None:
    """The deferred FK still forbids an orphan at COMMIT, not merely inside."""

    _, store, authority, resource = _system(tmp_path)
    authority.replace_resource_health_contract(resource.resource_id, DEFAULT_PROBES)
    connection = sqlite3.connect(store.path, isolation_level=None)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "DELETE FROM resource_health_contracts WHERE resource_id=?",
            (resource.resource_id,),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.commit()
    finally:
        connection.close()


def test_a_short_probe_set_is_refused_at_read_rather_than_reported_complete(
    tmp_path: Path,
) -> None:
    """A contract that no longer describes its own probes is not readable.

    The triggers reject every ordinary way to reach this, but hand-written SQL
    can still assemble it by rebuilding the header around a shortened probe
    set -- and in a trusted-admin product it is not worth pretending
    otherwise. What matters is that the read refuses it: returning a short
    contract would understate what the operator required.
    """

    _, store, authority, resource = _system(tmp_path)
    authority.replace_resource_health_contract(resource.resource_id, DEFAULT_PROBES)
    connection = sqlite3.connect(store.path, isolation_level=None)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        header = connection.execute(
            "SELECT * FROM resource_health_contracts WHERE resource_id=?",
            (resource.resource_id,),
        ).fetchone()
        connection.execute(
            "DELETE FROM resource_health_contracts WHERE resource_id=?",
            (resource.resource_id,),
        )
        connection.execute(
            "DELETE FROM resource_health_contract_probes "
            "WHERE resource_id=? AND probe_index=1",
            (resource.resource_id,),
        )
        connection.execute(
            "INSERT INTO resource_health_contracts("
            "resource_id, revision, fingerprint, probe_count, created_at, updated_at) "
            "VALUES(?, ?, ?, ?, ?, ?)",
            tuple(header),
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(AuthorityInvariantError, match="declared count"):
        store.resource_health_contract(resource.resource_id)


# ===========================================================================
# B. CONTRACT MATERIAL: BOUNDS, CANONICAL ORDER, FINGERPRINT
# ===========================================================================


def test_the_fingerprint_is_independent_of_declaration_order(tmp_path: Path) -> None:
    _, _store, authority, resource = _system(tmp_path)
    forward = _probes(
        (SYSTEMD, "nginx.service"), (HEALTHY, "immich_server"), (RUNNING, "redis")
    )
    first = authority.replace_resource_health_contract(resource.resource_id, forward)
    assert first.fingerprint == health_contract_fingerprint(tuple(reversed(forward)))

    # Re-declaring the same set in a different order is not a change: it must
    # not consume a revision, because a revision has to mean the contract
    # actually became different.
    repeated = authority.replace_resource_health_contract(
        resource.resource_id, tuple(reversed(forward))
    )
    assert repeated == first
    assert [probe.target for probe in first.probes] == [
        "immich_server",
        "nginx.service",
        "redis",
    ]


def test_the_fingerprint_separates_target_within_one_kind(tmp_path: Path) -> None:
    """Same kind, different target, is a different contract.

    Pre-v0.5-reduction this also proved the fingerprint separates KIND from
    target using two different targeted Docker kinds; `systemd_unit_active`
    is now the only targeted kind that can share a contract shape with
    itself (an advanced contract can hold several), so that half of the
    property is proven the only way still constructible. Kind is still part
    of the fingerprint payload by construction (`health_contract_fingerprint`
    hashes `{kind, target}` pairs), not merely by absence of a counterexample.
    """

    web = health_contract_fingerprint(_probes((SYSTEMD, "immich_web.service")))
    server = health_contract_fingerprint(_probes((SYSTEMD, "immich_server.service")))
    assert web != server


@pytest.mark.parametrize(
    "probes, message",
    (
        ((), "at least one probe"),
        (
            _probes((SYSTEMD, "nginx.service"), (SYSTEMD, "nginx.service")),
            "duplicate",
        ),
        (_probes((SYSTEMD, "")), "must not be empty"),
        (_probes((SYSTEMD, "unit name.service")), "whitespace"),
        (_probes((SYSTEMD, "unit\nname.service")), "whitespace"),
        (_probes((SYSTEMD, "unit\x00name")), "control characters"),
        (_probes((SYSTEMD, "unit\x7fname")), "control characters"),
        (_probes((SYSTEMD, "unit​name")), "control characters"),
        (
            _probes((SYSTEMD, "x" * (MAX_HEALTH_PROBE_TARGET_LENGTH + 1))),
            "exceeds",
        ),
    ),
)
def test_malformed_contract_material_is_refused_before_it_is_stored(
    tmp_path: Path, probes, message: str
) -> None:
    _, store, authority, resource = _system(tmp_path)
    _unconfigured(authority, resource.resource_id)
    with pytest.raises(HealthContractError, match=message):
        authority.replace_resource_health_contract(resource.resource_id, probes)
    assert store.resource_health_contract(resource.resource_id) is None
    assert _raw_rows(store) == ([], [])


def test_the_probe_count_bound_is_enforced_at_the_authority(tmp_path: Path) -> None:
    _, store, authority, resource = _system(tmp_path)
    largest = _probes(
        *((SYSTEMD, f"unit-{index}.service") for index in range(MAX_HEALTH_PROBES))
    )
    accepted = authority.replace_resource_health_contract(resource.resource_id, largest)
    assert len(accepted.probes) == MAX_HEALTH_PROBES

    too_many = largest + _probes((SYSTEMD, "one-too-many.service"))
    with pytest.raises(HealthContractError, match="at most"):
        authority.replace_resource_health_contract(resource.resource_id, too_many)
    # The refusal left the previously accepted contract exactly as it was.
    assert store.resource_health_contract(resource.resource_id) == accepted


def test_a_probe_target_is_data_and_never_command_material(tmp_path: Path) -> None:
    """Shell-looking text is stored verbatim as one opaque argument.

    There is nothing to escape here, because nothing ever interpolates a
    target into command text -- the future executor uses fixed argv. What
    this test pins is that the contract layer neither rewrites the operator's
    data nor grows a second, command-shaped field to hold it.
    """

    _, store, authority, resource = _system(tmp_path)
    target = "weird;name&&rm-rf|$(x)`y`.service"
    contract = authority.replace_resource_health_contract(
        resource.resource_id, _probes((SYSTEMD, target))
    )
    assert contract.probes[0].target == target
    _contracts, probe_rows = _raw_rows(store)
    assert [str(row["target"]) for row in probe_rows] == [target]
    assert set(probe_rows[0].keys()) == {"resource_id", "probe_index", "kind", "target"}


# ===========================================================================
# C. AUTHORITY LIFECYCLE
# ===========================================================================


def test_set_read_replace_and_clear_on_one_current_resource(tmp_path: Path) -> None:
    clock, store, authority, resource = _system(tmp_path)
    rid = resource.resource_id

    # A current managed LXC arrives already carrying the built-in default.
    builtin = authority.resource_health_contract(rid)
    assert builtin is not None
    assert (builtin.revision, builtin.probes) == (1, DEFAULT_HEALTH_PROBES)

    # The rest of this test is about the *unconfigured* state itself, which
    # only the low-level clear still produces.
    _unconfigured(authority, rid)
    assert authority.resource_health_contract(rid) is None
    assert _health_contract_view(store, authority, rid) == {
        "status": "unconfigured",
        "revision": None,
        "fingerprint": None,
        "probe_count": None,
        "updated_at": None,
    }

    first = authority.replace_resource_health_contract(rid, DEFAULT_PROBES)
    assert (first.revision, len(first.probes)) == (2, 2)
    assert authority.resource_health_contract(rid) == first

    replaced = authority.replace_resource_health_contract(
        rid, DEFAULT_PROBES + _probes((RUNNING, "redis"))
    )
    assert replaced.revision == 3
    assert replaced.created_at == first.created_at
    assert replaced.fingerprint != first.fingerprint
    published = _health_contract_view(store, authority, rid)
    assert published == {
        "status": "configured",
        "revision": 3,
        "fingerprint": replaced.fingerprint,
        "probe_count": 3,
        "updated_at": replaced.updated_at,
    }

    # Survives a restart: this is durable authority, not process state.
    db_path = store.path
    store.close()
    reopened = InventoryAuthorityStore(db_path, now=clock)
    restarted = InventoryAuthority(reopened, now=clock)
    assert reopened.resource_health_contract(rid) == replaced

    assert restarted.clear_resource_health_contract(rid) is True
    assert restarted.clear_resource_health_contract(rid) is False
    assert reopened.resource_health_contract(rid) is None
    assert _health_contract_view(reopened, restarted, rid)["status"] == "unconfigured"
    assert _raw_rows(reopened) == ([], [])


def test_clearing_leaves_no_probe_rows_behind(tmp_path: Path) -> None:
    _, store, authority, resource = _system(tmp_path)
    authority.replace_resource_health_contract(
        resource.resource_id, DEFAULT_PROBES + _probes((RUNNING, "redis"))
    )
    contracts, probes = _raw_rows(store)
    assert (len(contracts), len(probes)) == (1, 3)
    authority.clear_resource_health_contract(resource.resource_id)
    assert _raw_rows(store) == ([], [])


def test_replacement_never_leaves_a_mixed_revision_probe_set(tmp_path: Path) -> None:
    _, store, authority, resource = _system(tmp_path)
    authority.replace_resource_health_contract(
        resource.resource_id,
        _probes((SYSTEMD, "a.service"), (SYSTEMD, "b.service"), (SYSTEMD, "c.service")),
    )
    replaced = authority.replace_resource_health_contract(
        resource.resource_id, _probes((RUNNING, "redis"))
    )
    contracts, probes = _raw_rows(store)
    assert len(contracts) == 1
    assert [(str(row["kind"]), str(row["target"])) for row in probes] == [
        ("systemd_unit_active", "redis")
    ]
    assert int(contracts[0]["probe_count"]) == 1
    assert store.resource_health_contract(resource.resource_id) == replaced


def test_a_failed_replacement_leaves_the_old_complete_contract_intact(
    tmp_path: Path, monkeypatch
) -> None:
    """Transaction rollback restores the previous contract in full."""

    _, store, authority, resource = _system(tmp_path)
    original = authority.replace_resource_health_contract(
        resource.resource_id, DEFAULT_PROBES
    )
    revision_before = store.backend_instance().published_state_revision

    def explode(self, connection, *, resource_id):
        raise RuntimeError("simulated failure after the probe writes")

    monkeypatch.setattr(
        InventoryAuthority, "_after_resource_health_contract_write", explode
    )
    with pytest.raises(RuntimeError, match="simulated failure"):
        authority.replace_resource_health_contract(
            resource.resource_id, _probes((RUNNING, "redis"))
        )

    assert store.resource_health_contract(resource.resource_id) == original
    assert store.backend_instance().published_state_revision == revision_before
    _contracts, probes = _raw_rows(store)
    assert [str(row["target"]) for row in probes] == ["immich_server", "nginx.service"]


def test_a_failed_clear_leaves_the_contract_intact(tmp_path: Path, monkeypatch) -> None:
    _, store, authority, resource = _system(tmp_path)
    original = authority.replace_resource_health_contract(
        resource.resource_id, DEFAULT_PROBES
    )

    def explode(self, connection, *, resource_id):
        raise RuntimeError("simulated failure during clear")

    monkeypatch.setattr(
        InventoryAuthority, "_after_resource_health_contract_write", explode
    )
    with pytest.raises(RuntimeError, match="simulated failure"):
        authority.clear_resource_health_contract(resource.resource_id)
    assert store.resource_health_contract(resource.resource_id) == original


# ===========================================================================
# D. IDENTITY: THE CONTRACT BELONGS TO ONE EXACT INCARNATION
# ===========================================================================


def test_an_unknown_resource_is_not_an_unconfigured_resource(tmp_path: Path) -> None:
    """"No such resource" and "no contract" are different facts."""

    _, _store, authority, _resource = _system(tmp_path)
    unknown = "11111111-1111-1111-1111-111111111111"
    for call in (
        lambda: authority.resource_health_contract(unknown),
        lambda: authority.replace_resource_health_contract(unknown, DEFAULT_PROBES),
        lambda: authority.clear_resource_health_contract(unknown),
    ):
        with pytest.raises(AuthorityNotFound):
            call()


def test_a_qemu_resource_cannot_hold_a_contract_no_executor_could_honour(
    tmp_path: Path,
) -> None:
    _, store, authority, resource = _system(tmp_path, resource_type="qemu")
    with pytest.raises(AuthorityConflict, match="LXC"):
        authority.replace_resource_health_contract(resource.resource_id, DEFAULT_PROBES)
    assert _health_contract_view(store, authority, resource.resource_id) == {
        "status": "unsupported",
        "revision": None,
        "fingerprint": None,
        "probe_count": None,
        "updated_at": None,
    }


def test_a_vmid_reuse_replacement_inherits_nothing_and_gets_the_builtin_default(
    tmp_path: Path,
) -> None:
    """A new incarnation at the same VMID inherits no operator contract.

    This is the whole reason the contract is keyed by `resource_id`: the
    replacement is a different workload that happens to occupy the same
    locator, and silently handing it the previous workload's definition of
    healthy would be a false claim about a machine nobody has configured.
    What it gets instead is the backend's own built-in default -- a guest
    liveness baseline that claims nothing about a workload at all.
    """

    _, store, authority, original = _system(tmp_path)
    declared = authority.replace_resource_health_contract(
        original.resource_id, DEFAULT_PROBES
    )

    # Two replacements at the same VMID, ending back at an LXC: the final
    # incarnation is an ordinary contract-eligible guest that simply has no
    # contract, not a type this product refuses.
    _rediscover(authority, original.inventory_source_id, resource_type="qemu")
    _rediscover(authority, original.inventory_source_id, resource_type="lxc")

    resources = {item.resource_id: item for item in store.list_resources()}
    successor = next(
        item
        for item in resources.values()
        if item.resource_type == "lxc" and item.resource_id != original.resource_id
    )
    assert successor.vmid == original.vmid

    inherited = authority.resource_health_contract(successor.resource_id)
    assert inherited == store.resource_health_contract(successor.resource_id)
    assert inherited is not None
    # The built-in default on its OWN fresh counter, never the predecessor's
    # Docker/systemd contract and never the predecessor's revision.
    assert inherited.probes == DEFAULT_HEALTH_PROBES
    assert inherited.revision == 1
    assert inherited.fingerprint != declared.fingerprint
    view = InventoryPublication(store, authority).read()
    by_id = {item["resource_id"]: item for item in view.resources}
    assert by_id[successor.resource_id]["health_contract"]["status"] == "configured"
    # The predecessor keeps its own historical row -- ordinary FK provenance,
    # not a contract any replacement can use.
    assert by_id[original.resource_id]["health_contract"]["status"] == "configured"


def test_a_replaced_incarnation_can_no_longer_be_edited(tmp_path: Path) -> None:
    _, store, authority, original = _system(tmp_path)
    authority.replace_resource_health_contract(original.resource_id, DEFAULT_PROBES)
    _rediscover(authority, original.inventory_source_id, resource_type="qemu")
    for call in (
        lambda: authority.resource_health_contract(original.resource_id),
        lambda: authority.replace_resource_health_contract(
            original.resource_id, _probes((RUNNING, "redis"))
        ),
        lambda: authority.clear_resource_health_contract(original.resource_id),
    ):
        with pytest.raises(AuthorityConflict):
            call()
    # And nothing was written by the refusals: the retired incarnation still
    # carries exactly the revision-2 contract it had (revision 1 was the
    # built-in default this replaced), and the QEMU successor gets none.
    contracts, _probe_rows = _raw_rows(store)
    assert [
        (str(row["resource_id"]), int(row["revision"])) for row in contracts
    ] == [(original.resource_id, 2)]


def test_the_same_resource_keeps_its_contract_across_a_node_move_and_rename(
    tmp_path: Path,
) -> None:
    """Node and name are locators and metadata, never contract identity."""

    _, store, authority, resource = _system(tmp_path)
    stored = authority.replace_resource_health_contract(
        resource.resource_id, DEFAULT_PROBES
    )
    _rediscover(
        authority,
        resource.inventory_source_id,
        name="renamed-ct",
        node_name="pve-b",
        node_names=("pve-a", "pve-b"),
        observed_at="2026-08-28T13:00:00+00:00",
    )
    current = store.list_resources()[0]
    assert current.resource_id == resource.resource_id
    assert current.name == "renamed-ct"
    assert authority.resource_health_contract(resource.resource_id) == stored


# ===========================================================================
# E. CONCURRENCY AND STALE EDITORS
# ===========================================================================


def test_a_revision_is_never_reused_after_a_clear(tmp_path: Path) -> None:
    """The compare-and-set witness: clearing must not rewind the counter.

    If a cleared contract let the next one start again at revision 1, an
    operator holding revision 1 of a contract that no longer exists could
    compare-and-set successfully against a completely different contract that
    happened to also be revision 1. That is an ABA: the value they checked
    matched, but not the thing they were checking.
    """

    _, store, authority, resource = _system(tmp_path)
    rid = resource.resource_id

    first = authority.replace_resource_health_contract(
        rid, _probes((SYSTEMD, "a.service"))
    )
    # Revision 2: revision 1 is the built-in default this replaced.
    assert first.revision == 2
    # An ordinary operator opens the editor here and holds revision 2.

    assert authority.clear_resource_health_contract(rid, expected_revision=2) is True
    assert _last_allocated_revision(store, rid) == 2

    second = authority.replace_resource_health_contract(
        rid, _probes((RUNNING, "redis")), expected_revision=0
    )
    assert second.revision == 3

    # The stale editor's revision names a generation that is gone, and must
    # never match again -- not now, and not after any number of later cycles.
    for call in (
        lambda: authority.replace_resource_health_contract(
            rid, _probes((SYSTEMD, "hostile.service")), expected_revision=2
        ),
        lambda: authority.clear_resource_health_contract(rid, expected_revision=2),
    ):
        with pytest.raises(HealthContractRevisionConflict):
            call()
    assert store.resource_health_contract(rid) == second


def test_recreating_identical_material_after_a_clear_is_a_new_generation(
    tmp_path: Path,
) -> None:
    """Same probes, same fingerprint, different durable identity.

    Re-declaring a contract that is still present is idempotent -- nothing
    changed. Re-declaring one that was *deleted* is not a continuation of the
    deleted contract, and a health-execution stage that recorded
    `(revision, fingerprint)` must be able to tell the two generations apart.
    """

    _, store, authority, resource = _system(tmp_path)
    rid = resource.resource_id

    original = authority.replace_resource_health_contract(rid, DEFAULT_PROBES)
    authority.clear_resource_health_contract(rid)
    recreated = authority.replace_resource_health_contract(rid, DEFAULT_PROBES)

    assert recreated.fingerprint == original.fingerprint
    assert recreated.revision > original.revision
    assert (recreated.revision, recreated.fingerprint) != (
        original.revision,
        original.fingerprint,
    )
    assert recreated.probes == original.probes
    # A recreated contract is genuinely new, so its creation time is its own.
    assert store.resource_health_contract(rid) == recreated


def test_revisions_stay_strictly_increasing_across_many_clear_cycles(
    tmp_path: Path,
) -> None:
    _, store, authority, resource = _system(tmp_path)
    rid = resource.resource_id
    seen: list[int] = []
    for index in range(4):
        seen.append(
            authority.replace_resource_health_contract(
                rid, _probes((SYSTEMD, f"unit-{index}.service"))
            ).revision
        )
        seen.append(
            authority.replace_resource_health_contract(
                rid, _probes((RUNNING, f"container-{index}"))
            ).revision
        )
        authority.clear_resource_health_contract(rid)
    # Starting at 2: revision 1 is the built-in default the first cycle
    # replaced.
    assert seen == sorted(set(seen)) == [2, 3, 4, 5, 6, 7, 8, 9]
    assert _last_allocated_revision(store, rid) == 9
    # And every stale revision from every cycle stays stale forever.
    authority.replace_resource_health_contract(rid, DEFAULT_PROBES)
    for stale in seen:
        with pytest.raises(HealthContractRevisionConflict):
            authority.clear_resource_health_contract(rid, expected_revision=stale)


def test_clearing_consumes_no_revision_and_keeps_the_counter(tmp_path: Path) -> None:
    """Clear removes the contract, not the record of what was handed out."""

    _, store, authority, resource = _system(tmp_path)
    rid = resource.resource_id
    authority.replace_resource_health_contract(rid, DEFAULT_PROBES)
    authority.replace_resource_health_contract(rid, _probes((RUNNING, "redis")))
    # 1 is the built-in default; 2 and 3 are the two explicit contracts.
    assert _last_allocated_revision(store, rid) == 3

    authority.clear_resource_health_contract(rid)
    # No contract row and no probe rows -- absence is still what
    # "unconfigured" means -- but the allocation history survives.
    assert _raw_rows(store) == ([], [])
    assert store.resource_health_contract(rid) is None
    assert _last_allocated_revision(store, rid) == 3

    # A second clear is still a no-op and still consumes nothing.
    assert authority.clear_resource_health_contract(rid) is False
    assert _last_allocated_revision(store, rid) == 3
    assert authority.replace_resource_health_contract(rid, DEFAULT_PROBES).revision == 4


def test_a_rolled_back_replacement_consumes_no_revision(
    tmp_path: Path, monkeypatch
) -> None:
    """A revision is allocated in the same transaction it is used by."""

    _, store, authority, resource = _system(tmp_path)
    rid = resource.resource_id
    authority.replace_resource_health_contract(rid, DEFAULT_PROBES)
    assert _last_allocated_revision(store, rid) == 2

    def explode(self, connection, *, resource_id):
        raise RuntimeError("simulated failure after the revision allocation")

    monkeypatch.setattr(
        InventoryAuthority, "_after_resource_health_contract_write", explode
    )
    for _ in range(3):
        with pytest.raises(RuntimeError, match="simulated failure"):
            authority.replace_resource_health_contract(
                rid, _probes((RUNNING, "redis"))
            )
    assert _last_allocated_revision(store, rid) == 2

    monkeypatch.undo()
    assert (
        authority.replace_resource_health_contract(
            rid, _probes((RUNNING, "redis"))
        ).revision
        == 3
    )


def test_sql_refuses_reusing_or_deleting_an_allocated_revision(tmp_path: Path) -> None:
    _, store, authority, resource = _system(tmp_path)
    rid = resource.resource_id
    authority.replace_resource_health_contract(rid, DEFAULT_PROBES)
    authority.replace_resource_health_contract(rid, _probes((RUNNING, "redis")))

    with sqlite3.connect(store.path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        for regressed in (2, 1, 0):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE resource_health_contract_revision_state "
                    "SET last_revision=? WHERE resource_id=?",
                    (regressed, rid),
                )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "DELETE FROM resource_health_contract_revision_state "
                "WHERE resource_id=?",
                (rid,),
            )


def test_sql_refuses_a_contract_row_that_bypasses_the_revision_allocator(
    tmp_path: Path,
) -> None:
    """A contract row must carry the revision the allocator handed out.

    This constrains what the schema can express, not what a trusted
    administrator with direct SQL can reconstruct. The allocator's value and
    the last *used* revision are the same number, so re-inserting a contract
    at exactly the current allocator value is indistinguishable at the SQL
    level; what makes that unreachable is the authority's allocate-then-insert
    order, which is covered by the tests above. What the trigger does buy is
    that no contract row can skip ahead of the allocator or exist without one,
    so a revision can never be conjured out of nothing.
    """

    _, store, authority, resource = _system(tmp_path)
    rid = resource.resource_id
    authority.replace_resource_health_contract(rid, DEFAULT_PROBES)
    authority.clear_resource_health_contract(rid)

    with sqlite3.connect(store.path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        # 2 is the current allocator value (1 was the built-in default), and
        # re-inserting at exactly that value is deliberately indistinguishable
        # at the SQL level -- see the docstring. Everything ABOVE it is not.
        for skipped_ahead in (3, 4, 99):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO resource_health_contracts("
                    "resource_id, revision, fingerprint, probe_count, created_at, "
                    "updated_at) VALUES(?, ?, ?, 1, 'x', 'x')",
                    (rid, skipped_ahead, "a" * 64),
                )

    # A resource that has never had a contract has no allocator row at all,
    # so a hand-written contract row cannot precede one. A QEMU guest is the
    # one that genuinely never gets one: the built-in default is provisioned
    # for managed LXC resources only.
    _, other_store, _other_authority, other = _system(
        tmp_path / "other", resource_type="qemu"
    )
    assert _last_allocated_revision(other_store, other.resource_id) is None
    with sqlite3.connect(other_store.path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO resource_health_contracts("
                "resource_id, revision, fingerprint, probe_count, created_at, "
                "updated_at) VALUES(?, 1, ?, 1, 'x', 'x')",
                (other.resource_id, "a" * 64),
            )


def test_a_replacement_resource_does_not_inherit_the_revision_counter(
    tmp_path: Path,
) -> None:
    _, store, authority, original = _system(tmp_path)
    authority.replace_resource_health_contract(original.resource_id, DEFAULT_PROBES)
    authority.replace_resource_health_contract(
        original.resource_id, _probes((RUNNING, "redis"))
    )
    # 1 built-in default + 2 explicit contracts.
    assert _last_allocated_revision(store, original.resource_id) == 3

    _rediscover(authority, original.inventory_source_id, resource_type="qemu")
    _rediscover(authority, original.inventory_source_id, resource_type="lxc")
    successor = next(
        item
        for item in store.list_resources()
        if item.resource_type == "lxc" and item.resource_id != original.resource_id
    )

    # A different workload at the same VMID starts on its OWN counter: the
    # only revision it has ever been handed is 1, for the built-in default it
    # was just provisioned. The predecessor keeps its own history.
    assert _last_allocated_revision(store, successor.resource_id) == 1
    assert (
        authority.replace_resource_health_contract(
            successor.resource_id, DEFAULT_PROBES
        ).revision
        == 2
    )
    assert _last_allocated_revision(store, original.resource_id) == 3


def test_the_revision_counter_survives_a_rename_and_node_move(tmp_path: Path) -> None:
    _, store, authority, resource = _system(tmp_path)
    rid = resource.resource_id
    authority.replace_resource_health_contract(rid, DEFAULT_PROBES)
    authority.clear_resource_health_contract(rid)
    assert _last_allocated_revision(store, rid) == 2

    _rediscover(
        authority,
        resource.inventory_source_id,
        name="renamed-ct",
        node_name="pve-b",
        node_names=("pve-a", "pve-b"),
        observed_at="2026-08-28T13:00:00+00:00",
    )
    assert store.list_resources()[0].resource_id == rid
    # Same durable resource, so the same counter -- and reconciliation
    # re-provisioned the built-in default on top of it (revision 3) rather
    # than leaving a current managed LXC update-blocked.
    restored = authority.resource_health_contract(rid)
    assert restored is not None
    assert (restored.revision, restored.probes) == (3, DEFAULT_HEALTH_PROBES)
    assert _last_allocated_revision(store, rid) == 3
    assert authority.replace_resource_health_contract(rid, DEFAULT_PROBES).revision == 4
    with pytest.raises(HealthContractRevisionConflict):
        authority.clear_resource_health_contract(rid, expected_revision=2)


def test_concurrent_clear_and_replace_never_duplicate_a_revision(
    tmp_path: Path,
) -> None:
    _, store, authority, resource = _system(tmp_path)
    rid = resource.resource_id
    authority.replace_resource_health_contract(rid, DEFAULT_PROBES)

    def clear():
        return authority.clear_resource_health_contract(rid)

    def replace(index):
        return authority.replace_resource_health_contract(
            rid, _probes((RUNNING, f"container-{index}"))
        ).revision

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(replace, 0), pool.submit(clear), pool.submit(replace, 1)]
        revisions = [
            future.result() for future in futures if not isinstance(future.result(), bool)
        ]

    assert len(set(revisions)) == len(revisions)
    assert min(revisions) > 1
    last = _last_allocated_revision(store, rid)
    assert last == max(revisions)
    current = store.resource_health_contract(rid)
    if current is not None:
        assert current.revision in revisions


def test_compare_and_set_refuses_a_stale_editor(tmp_path: Path) -> None:
    _, store, authority, resource = _system(tmp_path)
    rid = resource.resource_id
    _unconfigured(authority, rid)

    # `expected_revision=0` asserts "there is no contract yet".
    first = authority.replace_resource_health_contract(
        rid, DEFAULT_PROBES, expected_revision=0
    )
    with pytest.raises(HealthContractRevisionConflict, match="expected revision"):
        authority.replace_resource_health_contract(
            rid, _probes((RUNNING, "redis")), expected_revision=0
        )

    second = authority.replace_resource_health_contract(
        rid, _probes((RUNNING, "redis")), expected_revision=first.revision
    )
    assert (first.revision, second.revision) == (2, 3)
    # An editor still holding revision 2 may not discard revision 3.
    with pytest.raises(HealthContractRevisionConflict, match="expected revision"):
        authority.replace_resource_health_contract(
            rid, _probes((SYSTEMD, "other.service")), expected_revision=2
        )
    with pytest.raises(HealthContractRevisionConflict, match="expected revision"):
        authority.clear_resource_health_contract(rid, expected_revision=2)
    # It is still an AuthorityConflict, so an ordinary caller that only
    # distinguishes "refused" keeps working.
    assert issubclass(HealthContractRevisionConflict, AuthorityConflict)
    assert store.resource_health_contract(rid) == second

    assert authority.clear_resource_health_contract(rid, expected_revision=3) is True


def test_an_unconditional_write_still_advances_exactly_one_revision(
    tmp_path: Path,
) -> None:
    _, _store, authority, resource = _system(tmp_path)
    rid = resource.resource_id
    revisions = []
    for index in range(4):
        revisions.append(
            authority.replace_resource_health_contract(
                rid, _probes((SYSTEMD, f"unit-{index}.service"))
            ).revision
        )
    # Starting at 2: revision 1 is the built-in default.
    assert revisions == [2, 3, 4, 5]


def test_concurrent_replacements_serialize_without_a_visible_partial_contract(
    tmp_path: Path,
) -> None:
    """Two writers race; every observer sees one complete contract."""

    _, store, authority, resource = _system(tmp_path)
    rid = resource.resource_id
    authority.replace_resource_health_contract(rid, DEFAULT_PROBES)

    left = _probes(*((SYSTEMD, f"left-{index}.service") for index in range(8)))
    right = _probes(*((RUNNING, f"right-{index}") for index in range(5)))

    def write(probes):
        return authority.replace_resource_health_contract(rid, probes)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [future.result() for future in (pool.submit(write, left), pool.submit(write, right))]

    assert {result.revision for result in results} == {3, 4}
    final = store.resource_health_contract(rid)
    assert final is not None
    assert final.revision == 4
    assert final.fingerprint == health_contract_fingerprint(final.probes)
    assert len(final.probes) in {5, 8}
    contracts, probe_rows = _raw_rows(store)
    assert len(contracts) == 1
    assert len(probe_rows) == len(final.probes)


def test_a_concurrent_clear_and_replace_never_produce_orphan_probes(
    tmp_path: Path,
) -> None:
    _, store, authority, resource = _system(tmp_path)
    rid = resource.resource_id
    authority.replace_resource_health_contract(rid, DEFAULT_PROBES)

    def clear():
        return authority.clear_resource_health_contract(rid)

    def replace():
        return authority.replace_resource_health_contract(
            rid, _probes((RUNNING, "redis"), (RUNNING, "postgres"))
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = (pool.submit(clear), pool.submit(replace))
        for future in futures:
            future.result()

    contracts, probe_rows = _raw_rows(store)
    final = store.resource_health_contract(rid)
    if final is None:
        assert (contracts, probe_rows) == ([], [])
    else:
        assert len(contracts) == 1
        assert len(probe_rows) == int(contracts[0]["probe_count"]) == len(final.probes)


# ===========================================================================
# E2. THE BUILT-IN v0.5 DEFAULT
#
# A package-managed LXC gets `guest_operational` from the backend as a PRODUCT
# decision. It is not inferred from the guest: absence of a workload observer
# is not proof of workload absence, so nothing here inspects Docker, systemd,
# a socket, or a process to choose it.
# ===========================================================================


def test_a_newly_current_managed_lxc_gets_the_builtin_default(
    tmp_path: Path,
) -> None:
    _, store, authority, resource = _system(tmp_path)
    rid = resource.resource_id

    contract = authority.resource_health_contract(rid)
    assert contract is not None
    assert contract.revision == 1
    assert contract.probes == (
        ResourceHealthProbe(kind=GUEST_OPERATIONAL, target=None),
    )
    assert contract.probes[0].target is None
    assert contract.fingerprint == health_contract_fingerprint(DEFAULT_HEALTH_PROBES)
    assert _health_contract_view(store, authority, rid)["status"] == "configured"

    # Durable, and correctly targetless in the real schema.
    contracts, probe_rows = _raw_rows(store)
    assert [(str(row["kind"]), row["target"]) for row in probe_rows] == [
        ("guest_operational", None)
    ]
    assert int(contracts[0]["probe_count"]) == 1


def test_repeated_reconciliation_of_the_default_is_idempotent(
    tmp_path: Path,
) -> None:
    """No revision churn, no republish churn, no second contract."""

    _, store, authority, resource = _system(tmp_path)
    rid = resource.resource_id
    first = authority.resource_health_contract(rid)
    published_before = store.backend_instance().published_state_revision

    for index in range(3):
        _rediscover(
            authority,
            resource.inventory_source_id,
            observed_at=f"2026-08-28T1{index}:00:00+00:00",
        )

    assert authority.resource_health_contract(rid) == first
    assert _last_allocated_revision(store, rid) == 1
    contracts, probe_rows = _raw_rows(store)
    assert (len(contracts), len(probe_rows)) == (1, 1)
    # Reconciliation republishes on its own; what must not happen is a
    # SECOND contract generation per discovery run.
    assert store.backend_instance().published_state_revision > published_before


def test_a_qemu_guest_never_gets_a_default_contract(tmp_path: Path) -> None:
    """The default is a package-managed-LXC product decision, nothing wider."""

    _, store, authority, resource = _system(tmp_path, resource_type="qemu")
    assert _raw_rows(store) == ([], [])
    assert _last_allocated_revision(store, resource.resource_id) is None
    assert (
        _health_contract_view(store, authority, resource.resource_id)["status"]
        == "unsupported"
    )


@pytest.mark.parametrize(
    "probes",
    (
        _probes((HEALTHY, "web")),
        _probes((SYSTEMD, "mariadb.service")),
        _probes((SYSTEMD, "mariadb.service"), (RUNNING, "web")),
    ),
)
def test_an_explicit_advanced_contract_is_never_replaced_by_the_default(
    tmp_path: Path, probes
) -> None:
    """B: the operator's own contract wins, forever, across any number of
    discovery runs."""

    _, store, authority, resource = _system(tmp_path)
    rid = resource.resource_id
    declared = authority.replace_resource_health_contract(rid, probes)

    for index in range(3):
        _rediscover(
            authority,
            resource.inventory_source_id,
            observed_at=f"2026-08-28T1{index}:00:00+00:00",
        )

    assert authority.resource_health_contract(rid) == declared
    assert _last_allocated_revision(store, rid) == declared.revision


def test_reset_restores_the_default_with_compare_and_set(tmp_path: Path) -> None:
    """C: advanced contract -> explicit reset -> the built-in default."""

    _, store, authority, resource = _system(tmp_path)
    rid = resource.resource_id
    advanced = authority.replace_resource_health_contract(
        rid, _probes((HEALTHY, "web"))
    )

    # A stale editor cannot discard the advanced contract by resetting.
    with pytest.raises(HealthContractRevisionConflict):
        authority.reset_resource_health_contract(rid, expected_revision=1)
    assert authority.resource_health_contract(rid) == advanced

    reset = authority.reset_resource_health_contract(
        rid, expected_revision=advanced.revision
    )
    assert reset.probes == DEFAULT_HEALTH_PROBES
    assert reset.revision == advanced.revision + 1
    assert authority.resource_health_contract(rid) == reset

    # Resetting an already-default contract is idempotent: identical material
    # is not a change, so it consumes no revision.
    again = authority.reset_resource_health_contract(rid)
    assert again == reset
    assert _last_allocated_revision(store, rid) == reset.revision

    # And it never leaves the resource unconfigured/update-blocked.
    assert _health_contract_view(store, authority, rid)["status"] == "configured"


def test_reset_reaches_the_default_from_unconfigured_too(tmp_path: Path) -> None:
    _, _store, authority, resource = _system(tmp_path)
    rid = resource.resource_id
    _unconfigured(authority, rid)

    restored = authority.reset_resource_health_contract(rid, expected_revision=0)
    assert restored.probes == DEFAULT_HEALTH_PROBES


def test_reset_refuses_a_resource_that_is_not_a_current_managed_lxc(
    tmp_path: Path,
) -> None:
    _, _store, authority, original = _system(tmp_path)
    _rediscover(authority, original.inventory_source_id, resource_type="qemu")
    with pytest.raises(AuthorityConflict):
        authority.reset_resource_health_contract(original.resource_id)
    with pytest.raises(AuthorityNotFound):
        authority.reset_resource_health_contract(
            "11111111-1111-1111-1111-111111111111"
        )


def test_default_provisioning_runs_no_guest_command_at_all(
    tmp_path: Path, monkeypatch
) -> None:
    """F/G, and the architecture fence for this whole simplification.

    Provisioning the default must never grow back into workload inference.
    The witness is deliberately blunt: the authority is not allowed to spawn
    ANY process while a managed LXC is being reconciled and defaulted, so a
    reintroduced `docker ps`, `systemctl list-unit-files`, `command -v
    docker`, socket probe, or process scan fails this test immediately --
    whatever guest the resource happens to be.

    It says nothing about advanced probes, which still legitimately run
    Docker and systemd commands through the job-bound health boundary.
    """

    import subprocess

    def forbidden(*args, **kwargs):
        raise AssertionError(
            "default health provisioning executed a subprocess: "
            f"{args!r} {kwargs!r}"
        )

    for name in ("run", "Popen", "check_output", "call", "check_call"):
        monkeypatch.setattr(subprocess, name, forbidden)
    monkeypatch.setattr("os.system", forbidden)

    _, store, authority, resource = _system(tmp_path)
    rid = resource.resource_id
    assert authority.resource_health_contract(rid).probes == DEFAULT_HEALTH_PROBES

    # And again on the re-provisioning path, from unconfigured.
    _unconfigured(authority, rid)
    _rediscover(authority, resource.inventory_source_id)
    assert authority.resource_health_contract(rid).probes == DEFAULT_HEALTH_PROBES
    assert store.resource_health_contract(rid) is not None


def test_the_default_makes_no_claim_about_docker_being_absent(
    tmp_path: Path,
) -> None:
    """G: the Docker counterexample is simply not a special case any more.

    A guest that really does run Docker but whose `docker` CLI cannot be
    observed is indistinguishable, to this path, from any other LXC -- and
    that is the point. The default asserts guest liveness, never "there is
    no Docker here", so there is nothing for a hidden runtime to falsify.
    """

    _, store, authority, resource = _system(tmp_path)
    contract = authority.resource_health_contract(resource.resource_id)
    assert contract.probes == DEFAULT_HEALTH_PROBES
    # The stored material carries exactly one kind and no adapter, workload,
    # runtime, absence, or recommendation field of any sort.
    contracts, probe_rows = _raw_rows(store)
    assert set(probe_rows[0].keys()) == {
        "resource_id",
        "probe_index",
        "kind",
        "target",
    }
    assert set(contracts[0].keys()) == {
        "resource_id",
        "revision",
        "fingerprint",
        "probe_count",
        "created_at",
        "updated_at",
    }


# ===========================================================================
# F. THE CONTRACT LAYER TOUCHES NOTHING ELSE
# ===========================================================================


def test_declaring_a_contract_neither_needs_nor_creates_a_package_update_job(
    tmp_path: Path,
) -> None:
    """A contract is configuration; it binds to no job in this stage.

    Job binding is health-EXECUTION work. Nothing here may require a job to
    exist, create one, advance a checkpoint, or terminalize anything.
    """

    _, store, authority, resource = _system(tmp_path)
    authority.replace_resource_health_contract(resource.resource_id, DEFAULT_PROBES)
    authority.clear_resource_health_contract(resource.resource_id)
    assert store.list_package_update_jobs() == ()


def test_contract_writes_republish_without_touching_the_inventory_revision(
    tmp_path: Path,
) -> None:
    _, store, authority, resource = _system(tmp_path)
    before = store.backend_instance()
    authority.replace_resource_health_contract(resource.resource_id, DEFAULT_PROBES)
    after = store.backend_instance()
    assert after.inventory_revision == before.inventory_revision
    assert after.published_state_revision > before.published_state_revision


def test_a_stopped_managed_lxc_still_receives_the_builtin_default(
    tmp_path: Path,
) -> None:
    """The default contract is CONFIGURATION, never a runtime observation.

    The eligibility predicate is exactly "current, package-managed LXC":
    present, active, `lxc`, with a current locator binding and a current
    node. `status` is deliberately NOT part of it. A stopped guest is still
    the resource whose health this contract describes, and gating the
    baseline on a temporary runtime condition would recreate the very dead
    end this pivot removed -- a managed LXC that is UNCONFIGURED, and
    therefore cannot be given an approved update, purely because it happened
    to be down during the reconciliation that would have configured it.

    Running remains a requirement only for scan/update/health EXECUTION
    (`_package_scan_context_is_current`, and the helper's own
    `revalidate_live_target`), never for contract creation.
    """

    _, store, authority, resource = _system(tmp_path)
    rid = resource.resource_id
    source_id = resource.inventory_source_id
    _unconfigured(authority, rid)
    assert authority.resource_health_contract(rid) is None

    _reconcile(
        authority,
        source_id,
        status="stopped",
        observed_at="2026-08-28T10:00:00+00:00",
    )

    assert store.list_resources()[0].status == "stopped"
    contract = authority.resource_health_contract(rid)
    assert contract is not None
    assert contract.probes == DEFAULT_HEALTH_PROBES
    assert contract.fingerprint == health_contract_fingerprint(DEFAULT_HEALTH_PROBES)
    assert _health_contract_view(store, authority, rid)["status"] == "configured"

    # The predicate provisioning shares with the package-scan target proof
    # accepts the stopped resource too: neither is gated on `status`.
    with sqlite3.connect(store.path) as connection:
        connection.row_factory = sqlite3.Row
        row = authority._require_package_scan_target(connection, rid)
    assert str(row["status"]) == "stopped"
