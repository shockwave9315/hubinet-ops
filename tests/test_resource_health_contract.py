"""Operator-declared per-resource health contracts: schema, authority, races.

The product rule these tests exist to defend is narrow and absolute: a health
contract says what "healthy" would mean for ONE exact resource incarnation,
and the absence of one is *unconfigured*, never a pass. Everything below is
either that rule, the bounds that keep the durable row deterministic, or the
atomicity that stops a half-written contract from ever being readable.

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
RUNNING = HealthProbeKind.DOCKER_CONTAINER_RUNNING
HEALTHY = HealthProbeKind.DOCKER_CONTAINER_HEALTHY


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


# ===========================================================================
# A. SCHEMA
# ===========================================================================


def test_fresh_database_is_schema_v15_with_the_health_contract_tables(
    tmp_path: Path,
) -> None:
    from app.inventory.store import AUTHORITY_SCHEMA_MARKER, AUTHORITY_SCHEMA_VERSION

    assert AUTHORITY_SCHEMA_VERSION == 15
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
        "resource_health_contract_update_immutable",
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
                "VALUES(?, 1, 'docker_container_running', 'redis')",
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
    stored = store.resource_health_contract(resource.resource_id)
    assert stored is not None
    assert stored.revision == 1
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

    Hand-written SQL can still assemble this state by rebuilding the header
    around a shortened probe set. Returning it would understate what the
    operator required, so the read fails closed instead.
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
        "redis",
        "nginx.service",
    ]


def test_the_fingerprint_separates_kind_from_target(tmp_path: Path) -> None:
    """Same target, different required condition, is a different contract."""

    running = health_contract_fingerprint(_probes((RUNNING, "immich_server")))
    healthy = health_contract_fingerprint(_probes((HEALTHY, "immich_server")))
    assert running != healthy
    assert healthy != health_contract_fingerprint(_probes((HEALTHY, "immich_web")))


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

    assert authority.resource_health_contract(rid) is None
    assert _health_contract_view(store, authority, rid) == {
        "status": "unconfigured",
        "revision": None,
        "fingerprint": None,
        "probe_count": None,
        "updated_at": None,
    }

    first = authority.replace_resource_health_contract(rid, DEFAULT_PROBES)
    assert (first.revision, len(first.probes)) == (1, 2)
    assert authority.resource_health_contract(rid) == first

    replaced = authority.replace_resource_health_contract(
        rid, DEFAULT_PROBES + _probes((RUNNING, "redis"))
    )
    assert replaced.revision == 2
    assert replaced.created_at == first.created_at
    assert replaced.fingerprint != first.fingerprint
    published = _health_contract_view(store, authority, rid)
    assert published == {
        "status": "configured",
        "revision": 2,
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
        ("docker_container_running", "redis")
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


def test_a_vmid_reuse_replacement_inherits_no_contract(tmp_path: Path) -> None:
    """A new incarnation at the same VMID starts unconfigured.

    This is the whole reason the contract is keyed by `resource_id`: the
    replacement is a different workload that happens to occupy the same
    locator, and silently handing it the previous workload's definition of
    healthy would be a false claim about a machine nobody has configured.
    """

    _, store, authority, original = _system(tmp_path)
    authority.replace_resource_health_contract(original.resource_id, DEFAULT_PROBES)

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

    assert store.resource_health_contract(successor.resource_id) is None
    assert authority.resource_health_contract(successor.resource_id) is None
    view = InventoryPublication(store, authority).read()
    by_id = {item["resource_id"]: item for item in view.resources}
    assert by_id[successor.resource_id]["health_contract"]["status"] == "unconfigured"
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
    # And nothing was written by the refusals.
    contracts, _probe_rows = _raw_rows(store)
    assert int(contracts[0]["revision"]) == 1


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


def test_compare_and_set_refuses_a_stale_editor(tmp_path: Path) -> None:
    _, store, authority, resource = _system(tmp_path)
    rid = resource.resource_id

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
    assert second.revision == 2
    # An editor still holding revision 1 may not discard revision 2.
    with pytest.raises(HealthContractRevisionConflict, match="expected revision"):
        authority.replace_resource_health_contract(
            rid, _probes((SYSTEMD, "other.service")), expected_revision=1
        )
    with pytest.raises(HealthContractRevisionConflict, match="expected revision"):
        authority.clear_resource_health_contract(rid, expected_revision=1)
    # It is still an AuthorityConflict, so an ordinary caller that only
    # distinguishes "refused" keeps working.
    assert issubclass(HealthContractRevisionConflict, AuthorityConflict)
    assert store.resource_health_contract(rid) == second

    assert authority.clear_resource_health_contract(rid, expected_revision=2) is True


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
    assert revisions == [1, 2, 3, 4]


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

    assert {result.revision for result in results} == {2, 3}
    final = store.resource_health_contract(rid)
    assert final is not None
    assert final.revision == 3
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
