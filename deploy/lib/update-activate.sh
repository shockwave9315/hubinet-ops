#!/usr/bin/env bash
# Phase U4 -- activation, acceptance, and coherent rollback.
#
# From _update_recheck_source_commit onward this file may mutate managed
# installation state. The activation mutation order in
# update_activate_and_accept is fixed and never reordered (AGENTS.md task
# prompt section 20). update-proxmox-0.5.sh's own EXIT trap calls
# update_rollback_on_failure whenever the process exits non-zero after the
# rollback/recovery boundary has been crossed -- that is, once removal of
# the service's boot activation OR a service stop has been ATTEMPTED (see
# _update_rollback_boundary_crossed in update-recovery.sh) -- never before.
#
# The first mutation of the window is the temporary service-autostart
# guard (_update_disable_service_autostart): hubinet-ops is `disable`d for
# the whole mutation window so a PVE/CT reboot can never boot-activate a
# half-swapped installation, and enablement is restored and positively
# proven again by either _update_restore_service_autostart on success or
# update_rollback_on_failure on every recovery path. The temporary
# disabled/enabled unit-file state is itself ordinary filesystem state
# under /etc/systemd/system (correction pass 10, P1) -- it crosses the
# same CT filesystem durability barrier as every other rollback-critical
# artifact below, on all three sides: immediately after the disable
# request is proven (before the service is stopped or anything else is
# mutated), immediately after the final restore-enable is proven on
# success (before the journal records the run completed), and immediately
# after restore-enable is proven during rollback/recovery (before the old
# service is started again).
#
# Rollback invariant (corrective pass): for every rollback-managed
# artifact (app, venv+requirements, systemd unit), a durable
# "*-activation-attempted" ledger marker is recorded BEFORE that
# artifact's FIRST destructive mutation -- not after its swap completes.
# A destructive mutation is one that removes or overwrites something at a
# LIVE path; a `cp` that only reads the live path to create a rollback
# copy is not destructive and does not itself need a marker first (see
# the systemd unit and PVE host helper steps below, which stage their
# rollback copy with `cp` before the one destructive `mv`).
#
# Rollback itself never trusts "the marker implies the swap fully
# completed" -- each `_update_rollback_*` helper instead inspects the
# actual, bounded set of paths that artifact owns (live path, this run's
# fixed rollback-<UPDATE_RUN_ID> path) and restores based on what it
# actually finds there. This makes rollback correct regardless of exactly
# which destructive step (if any) failed, because a real `mv`/rename is
# atomic -- it either fully happens or leaves both sides exactly as they
# were -- so "does the rollback-<UPDATE_RUN_ID> path exist" is a reliable,
# bounded signal of whether the live path still holds the pre-update
# content or needs restoring. Never guesses at an arbitrary path.

UPDATE_DB_BACKUP_PATH=""
UPDATE_POST_BACKEND_INSTANCE_ID=""
UPDATE_PRE_NFTABLES_CONF=""
UPDATE_DISPLAY_NAME=""
_UPDATE_SERVICE_STATE_DETAIL="unknown"
_UPDATE_SERVICE_ENABLED_DETAIL="unknown"
_UPDATE_SERVICE_READINESS_DETAIL="unknown"

# _update_test_term_checkpoint <name>: test-only self-TERM injection point
# (PR #65 correction pass 13, P1). HUBINET_OPS_TEST_TERM_AT, consulted
# only when HUBINET_OPS_TEST_MODE=1, is a space-separated list of
# checkpoint names -- delivering SIGTERM to this same process at exactly
# the named point lets the hermetic test suite exercise the REAL `trap
# 'exit 143' TERM` / _update_exit_trap machinery at specific points inside
# the mutation window. Unlike every pct-exec-routed CT-side mutation, the
# terminal journal checkpoint and its cleanup in _update_finish_summary
# run entirely on the PVE HOST side, so no fake-command layer can reach
# them the way tests/_bootstrap_fake_pve.py's kill_updater_after_move
# reaches a CT-side `mv` -- this is the same kind of narrow, always-inert-
# in-production seam as HUBINET_OPS_TEST_FAIL_HOST_SYNC above. Inert
# whenever HUBINET_OPS_TEST_MODE is not "1", so production behavior never
# calls this at all.
_update_test_term_checkpoint() {
  local name="$1" needle
  [[ "${HUBINET_OPS_TEST_MODE:-0}" == "1" ]] || return 0
  for needle in ${HUBINET_OPS_TEST_TERM_AT:-}; do
    if [[ "${needle}" == "${name}" ]]; then
      kill -TERM $$
    fi
  done
  # A non-matching needle leaves the `for` loop's own exit status at the
  # failed `[[ ]]` test's (1) -- under this script's `set -e`, a bare
  # nonzero-returning function call trips errexit immediately, well
  # before any actual TERM is ever delivered for a LATER checkpoint name.
  # This must always return 0 when it does not match.
  return 0
}

# Three-valued service-state probe. Return 0 means the service is active
# or transitioning and therefore MUST be treated as potentially running;
# return 1 means systemd positively reported a non-running state; return 2
# means the outer pct call, the systemd read, or its output was not
# trustworthy. UNKNOWN is never permission to mutate rollback-managed
# files.
_update_probe_service_state() {
  local output status
  output="$(pct exec "${VMID}" -- systemctl show hubinet-ops --property=ActiveState --value 2>/dev/null)" \
    && status=0 || status=$?
  if (( status != 0 )); then
    _UPDATE_SERVICE_STATE_DETAIL="unknown (probe exit ${status})"
    return 2
  fi
  case "${output}" in
    active|activating|reloading|deactivating)
      _UPDATE_SERVICE_STATE_DETAIL="${output}"
      return 0
      ;;
    inactive|failed)
      _UPDATE_SERVICE_STATE_DETAIL="${output}"
      return 1
      ;;
    *)
      _UPDATE_SERVICE_STATE_DETAIL="unknown (malformed state: ${output:-empty})"
      return 2
      ;;
  esac
}

_update_wait_until_service_stopped() {
  local waited=0 rc
  while (( waited < BOOTSTRAP_SERVICE_TIMEOUT_SECONDS )); do
    if _update_probe_service_state; then
      : # active or transitioning: keep waiting
    else
      rc=$?
      if (( rc == 1 )); then
        return 0
      fi
      # UNKNOWN may be transient, but is never treated as stopped.
    fi
    sleep 1
    waited=$(( waited + 1 ))
  done
  return 1
}

# _update_wait_until_service_active_and_healthy (correction passes 8/11,
# P2): the bounded readiness proof any freshly started installation must
# pass before target acceptance or rollback completion.
#
# The old code proved `active` in a bounded loop and then issued the
# application health request EXACTLY ONCE. hubinet-ops.service is
# Type=simple, so systemd reports `active` as soon as the process has been
# exec'd -- strictly earlier than the moment uvicorn has actually bound
# 127.0.0.1:8787. A single request fired at that instant can legitimately
# get nothing back from a perfectly healthy installation, and an ordinary
# startup readiness race was therefore classified as a FAILED rollback,
# hard stopping a recovery that had in fact succeeded.
#
# Both required facts are now polled together against the SAME existing
# bounded deadline (BOOTSTRAP_SERVICE_TIMEOUT_SECONDS -- no new timeout is
# invented, and there is no unbounded loop and no fixed pre-probe sleep).
# Health semantics are NOT weakened: success still requires systemd to
# report `active` AND the unauthenticated health endpoint to answer with a
# non-empty body; an empty or failed response is still never a pass. Unit
# enablement, the third required fact, is proven separately and earlier by
# _update_restore_service_autostart, before the service is started at all.
#
# A unit systemd positively reports as `failed` is terminal -- systemd is
# not going to make it active by being waited on -- so that state returns
# early instead of burning the whole deadline. `activating`/`inactive` are
# ordinary transitional answers during a start and are simply retried.
#
# The health request itself must be bounded (PR #65 correction pass 15,
# P1). A bounded OUTER loop does not by itself bound an unbounded INNER
# curl: if the target accepts the TCP connection but stalls before
# sending a usable HTTP response, an unbounded `curl` blocks this call
# forever, the outer loop never regains control to re-check its deadline,
# and "bounded readiness" is false. `--max-time` gives curl its own
# transfer deadline -- computed fresh each iteration as whatever remains
# of THIS SAME BOOTSTRAP_SERVICE_TIMEOUT_SECONDS budget, never a second,
# independent timeout -- so the outer deadline and the inner request
# describe exactly one bounded budget, never two stacked ones. Elapsed
# time is real wall-clock time (bash's own $SECONDS), not a fixed
# per-iteration counter, because a bounded curl call can itself now
# legitimately consume several seconds of the budget.
_update_wait_until_service_active_and_healthy() {
  local start_seconds="${SECONDS}" elapsed=0 state="" health_body="" remaining
  while (( elapsed < BOOTSTRAP_SERVICE_TIMEOUT_SECONDS )); do
    state="$(pct exec "${VMID}" -- systemctl is-active hubinet-ops 2>/dev/null || true)"
    if [[ "${state}" == "active" ]]; then
      remaining=$(( BOOTSTRAP_SERVICE_TIMEOUT_SECONDS - elapsed ))
      (( remaining < 1 )) && remaining=1
      health_body="$(pct exec "${VMID}" -- curl -fsS --max-time "${remaining}" "http://127.0.0.1:8787/r0/v1/health" 2>/dev/null || true)"
      if [[ -n "${health_body}" ]]; then
        _UPDATE_SERVICE_READINESS_DETAIL="active and healthy after ${elapsed}s"
        return 0
      fi
      _UPDATE_SERVICE_READINESS_DETAIL="active, but the unauthenticated health probe has not answered yet"
    elif [[ "${state}" == "failed" ]]; then
      _UPDATE_SERVICE_READINESS_DETAIL="systemd reports the unit as failed"
      return 1
    else
      _UPDATE_SERVICE_READINESS_DETAIL="last service state: ${state:-unknown}"
    fi
    sleep 1
    elapsed=$(( SECONDS - start_seconds ))
  done
  return 1
}

# Three-valued unit-file enablement probe. Return 0 means systemd
# positively reported the unit file as `enabled` -- i.e. systemd WILL
# boot-activate hubinet-ops after a PVE/CT restart; return 1 means systemd
# positively reported `disabled` -- it will NOT; return 2 means the outer
# pct call, the systemd read, or its output was not trustworthy. UNKNOWN is
# never proof of either state: it is never permission to begin the mutation
# window, and never permission to declare an update or a rollback complete.
# Enablement is read from systemd's own explicit unit-file state rather
# than inferred from any command's ambiguous exit status.
_update_probe_service_enabled() {
  local output status
  output="$(pct exec "${VMID}" -- systemctl show hubinet-ops --property=UnitFileState --value 2>/dev/null)" \
    && status=0 || status=$?
  if (( status != 0 )); then
    _UPDATE_SERVICE_ENABLED_DETAIL="unknown (probe exit ${status})"
    return 2
  fi
  case "${output}" in
    enabled)
      _UPDATE_SERVICE_ENABLED_DETAIL="enabled"
      return 0
      ;;
    disabled)
      _UPDATE_SERVICE_ENABLED_DETAIL="disabled"
      return 1
      ;;
    *)
      _UPDATE_SERVICE_ENABLED_DETAIL="unknown (unsupported unit-file state: ${output:-empty})"
      return 2
      ;;
  esac
}

# _update_disable_service_autostart: the temporary service-autostart guard
# that makes this updater's mutation window survive a PVE host power loss.
#
# Without it, a reachable sequence exists: the old service is stopped, the
# app is moved aside, the target app is activated, the PVE host loses power,
# the CT auto-starts (onboot=1, which this updater never changes), and an
# still-ENABLED hubinet-ops.service is boot-activated by systemd against a
# HALF-SWAPPED installation -- a target app paired with the old venv, or a
# freshly-activated unit paired with an old helper/database -- long before
# any later updater invocation could read the durable journal and roll back.
#
# The minimum existing systemd mechanism prevents that: for the whole
# mutation window the unit file is `disable`d, so systemd never
# boot-activates it. The unit is NOT masked, NOT replaced, and NOT
# permanently disabled; the CT's own onboot flag is untouched; and the
# updater can still `systemctl start` the disabled unit by hand for target
# acceptance, exactly as systemd permits. Normal boot enablement is
# restored only once the target is fully accepted and its installed-source
# marker is coherent (_update_restore_service_autostart), or by rollback.
_update_disable_service_autostart() {
  local rc
  # Re-prove the pre-update contract (an enabled installation) immediately
  # before the first mutation, not only during planning.
  if _update_probe_service_enabled; then
    :
  else
    rc=$?
    die "refusing to begin the update mutation window: hubinet-ops is not provably enabled inside container ${VMID} (${_UPDATE_SERVICE_ENABLED_DETAIL})"
  fi

  # Arm recovery BEFORE the disable request is issued. `systemctl disable`
  # can mutate the unit-file state and still return non-zero, and the
  # process can be SIGKILLed between the request and any success-only
  # marker -- so a marker written afterwards would leave a genuinely
  # disabled installation with no durable record that it must be
  # re-enabled. The EXIT/recovery boundary treats this marker as
  # rollback-armed even though no service stop has been requested yet.
  UPDATE_ROLLBACK_ARMED="1"
  update_journal_record update-service-autostart-disable-attempted "${VMID}"

  run_logged pct exec "${VMID}" -- systemctl disable hubinet-ops \
    || die "failed to request removal of hubinet-ops boot activation inside container ${VMID}; the resulting unit-file state is ambiguous and rollback recovery is required"

  # A zero exit is not proof either: prove the actual unit-file state.
  if _update_probe_service_enabled; then
    die "hubinet-ops is still enabled inside container ${VMID} after the autostart-disable request -- refusing to mutate an installation systemd would boot-activate half-swapped"
  else
    rc=$?
  fi
  (( rc == 1 )) \
    || die "could not positively prove hubinet-ops boot activation disabled inside container ${VMID} (${_UPDATE_SERVICE_ENABLED_DETAIL}) -- refusing to mutate an installation that may still auto-start at boot"

  # Durability barrier (correction pass 10, P1): `systemctl disable` itself
  # only mutates the unit-file symlink state under /etc/systemd/system --
  # ordinary filesystem state, exactly like every other artifact this
  # mutation window depends on. Proving UnitFileState==disabled above only
  # proves the RUNNING KERNEL's view; without this barrier a power loss
  # immediately afterwards could still resurrect an ENABLED unit on the
  # next boot, defeating the whole point of this guard. This must cross
  # the barrier BEFORE the service is stopped or any artifact mutated.
  _update_durability_barrier_ct /etc/systemd/system

  ledger_record update-service-autostart-disabled "${VMID}"
  log_info "hubinet-ops boot activation is temporarily disabled for this update's mutation window (the CT's own onboot setting is unchanged)"
}

# _update_restore_service_autostart: put hubinet-ops back under normal boot
# activation and POSITIVELY prove it. Callers decide what a failure means
# (a success-path die, or a rollback hard stop) -- this helper never exits
# by itself.
#
# mode (PR #65 correction pass 13, P2) selects the systemctl verb:
#
#   enable   (default) -- the ordinary case. Only ADDS the enablement
#            links implied by the CURRENT unit file's [Install] section;
#            it never removes a link that a DIFFERENT unit definition
#            previously installed.
#   reenable -- required whenever the live unit file this call is
#            re-enabling may not be the same [Install] content that was
#            last enabled. `reenable` resets the unit's installed
#            enablement links to exactly what its current [Install]
#            section declares, atomically removing any stale link a
#            differently-configured unit left behind first.
#
# Concretely: rollback restores the OLD unit file onto the live path
# (_update_rollback_unit) only after this run may already have enabled a
# CHANGED target unit (a different WantedBy=/Alias=) during forward
# activation. A plain `enable` on the restored old unit would then add the
# old links back without ever removing the target-only links the earlier
# enable created, leaving stale boot-activation links behind. See
# update_rollback_on_failure's own caller for exactly which case selects
# `reenable`; an update whose unit never changed keeps the simpler
# `enable` behavior unconditionally.
#
# Command-status semantics are MODE-SPECIFIC (PR #65 correction pass 14,
# P2), because a positive UnitFileState=enabled probe alone proves
# different things depending on the mode:
#
#   enable   -- a zero exit from `systemctl enable` is never accepted as
#            proof on its own (it can mutate state and still fail, or the
#            process can be killed before its exit is observed), so a
#            command failure here only logs a warning and falls through
#            to the UnitFileState probe. That probe is sufficient for
#            this mode's whole contract -- "boot activation exists" --
#            and a unit that was already enabled before this call still
#            satisfies it.
#   reenable -- the load-bearing contract is stronger: the stale,
#            differently-configured links left by a superseded unit were
#            actually reset, not merely that SOME enabled state now
#            exists. A pre-existing UnitFileState=enabled (e.g. the
#            TARGET unit this run enabled during forward activation,
#            before rollback restored the old unit file over it) would
#            still read back as enabled even if `reenable` failed and
#            reset nothing -- so the probe cannot distinguish "reenable
#            actually reset the links" from "something was already
#            enabled". Only a successful `systemctl reenable` itself --
#            the bounded, trusted systemd operation -- is what proves the
#            reset happened; a failed one fails this helper closed
#            immediately, before ever consulting the probe.
_update_restore_service_autostart() {
  local mode="${1:-enable}" cmd rc
  case "${mode}" in
    enable) cmd="enable" ;;
    reenable) cmd="reenable" ;;
    *) die "internal error: unsupported autostart-restore mode '${mode}'" ;;
  esac
  # PR #65 correction pass 15, P3: the real command status must be
  # captured BEFORE any negation -- `if ! run_logged ...; then rc=$?; fi`
  # reads `$?` from the NEGATED `!` compound condition, which is always 0
  # inside that branch, not the underlying systemctl command's own exit
  # status. The safety behavior this guards (reenable fails closed on any
  # nonzero) was already correct; only the diagnostic was false. This
  # `&&`/`||` form (same idiom as _update_probe_service_state above)
  # captures the real status either way and never trips `set -e`.
  run_logged pct exec "${VMID}" -- systemctl "${cmd}" hubinet-ops && rc=0 || rc=$?
  if (( rc != 0 )); then
    if [[ "${mode}" == "reenable" ]]; then
      _UPDATE_SERVICE_ENABLED_DETAIL="systemctl reenable returned failure (exit ${rc}); a pre-existing enabled unit-file state is never accepted as proof the stale enablement links were actually reset"
      return 1
    fi
    log_warn "the systemctl ${cmd} request for hubinet-ops returned failure (exit ${rc}) inside container ${VMID}; proving the actual unit-file state before deciding"
  fi
  if _update_probe_service_enabled; then
    log_info "hubinet-ops boot activation is restored (unit-file state: enabled)"
    return 0
  else
    rc=$?
  fi
  return 1
}

_update_read_display_name() {
  local inventory_text line raw
  inventory_text="$(pct exec "${VMID}" -- cat /etc/hubinet-ops/inventory.yaml 2>/dev/null)"
  [[ -n "${inventory_text}" ]] \
    || die "could not read inventory.yaml from container ${VMID}"
  line="$(printf '%s\n' "${inventory_text}" | grep '^  display_name:' | head -n1)"
  raw="${line#*display_name:}"
  raw="$(printf '%s' "${raw}" | sed -E 's/^[[:space:]]*"?//; s/"?[[:space:]]*$//')"
  [[ -n "${raw}" ]] || die "could not read source.display_name from container ${VMID}'s inventory.yaml"
  UPDATE_DISPLAY_NAME="${raw}"
}

_update_capture_pre_mutation_facts() {
  _update_read_display_name
  UPDATE_PRE_NFTABLES_CONF="$(pct exec "${VMID}" -- cat /etc/nftables.conf 2>/dev/null)"
  [[ -n "${UPDATE_PRE_NFTABLES_CONF}" ]] \
    || die "could not read /etc/nftables.conf from container ${VMID} before update"
}

# _update_recheck_source_commit: immediately-before-activation re-check,
# mirroring deploy/lib/bootstrap-deploy.sh::phase8_deploy_source's own
# recheck block exactly (same invariant: the approved target must still be
# HEAD, on a still-clean worktree, right before it is deployed).
_update_recheck_source_commit() {
  local recheck_sha
  recheck_sha="$(git -C "${SOURCE_DIR}" rev-parse HEAD 2>/dev/null)" \
    || die "could not re-verify SOURCE_DIR HEAD before activation"
  [[ "${recheck_sha}" == "${SOURCE_HEAD_SHA}" ]] \
    || die "SOURCE_DIR HEAD changed from the confirmed commit (${SOURCE_HEAD_SHA}) to ${recheck_sha} between confirmation and activation -- refusing to activate an unconfirmed commit"
  local recheck_dirty
  recheck_dirty="$(git -C "${SOURCE_DIR}" status --porcelain 2>/dev/null)"
  [[ -z "${recheck_dirty}" ]] \
    || die "SOURCE_DIR became dirty between confirmation and activation -- refusing to activate"
}

# _update_acquire_maintenance_fence: the EXCLUSIVE product-update
# maintenance fence, taken immediately before the mutation window.
#
# This is what actually makes a Hubinet PRODUCT update and a WORKLOAD
# package-update job mutually exclusive. The Phase U2 probe answer is a poll,
# and a poll cannot: between it and the first mutation an authenticated
# operator may legitimately start a workload update, and a second, later poll
# would only move that window rather than close it -- the update API stays
# live right up to the service stop.
#
# So the backend does it inside its OWN authority writer transaction: it
# proves no workload job is ACTIVE and makes the fence durable in the same
# critical section a workload `start_update` would have to enter to create
# one. Exactly one of the two wins. From the moment this returns, every new
# workload start refuses -- including against the TARGET backend started in
# Step 10, which reads the same durable fence -- until this run releases it.
#
# Released only at a terminal point: a proven successful product update, or a
# proven complete rollback/recovery. A crash anywhere in between leaves the
# fence in place, which is exactly what keeps workload issuance refused while
# this run still owns rollback-capable mutation state.
_update_acquire_maintenance_fence() {
  local output status reason detail
  output="$(pct exec "${VMID}" -- python3 "${UPDATE_FENCE_CT_PATH}" "${UPDATE_RUN_ID}" 2>/dev/null)" \
    && status=0 || status=$?
  (( status == 0 )) && [[ -n "${output}" ]] \
    || die "could not run the product-update maintenance fence tool inside container ${VMID} -- refusing to mutate"

  if _json_bool_field_is_true "${output}" "ok"; then
    UPDATE_FENCE_HELD="1"
    update_journal_record update-maintenance-fence-held "${VMID}"
    log_info "acquired the exclusive product-update maintenance fence (holder ${UPDATE_RUN_ID}); workload package updates are refused until this run completes or is rolled back"
    return 0
  fi

  reason="$(_json_field_from_text "${output}" "reason")"
  detail="$(_json_field_from_text "${output}" "detail")"
  if [[ "${reason}" == "fence_route_absent" ]]; then
    # A backend predating production activation has no fence route and no
    # update worker, so no race handshake with it is possible or needed --
    # read from the endpoint's own 404, never from a transport failure.
    #
    # But "no race with the OLD backend" is NOT "no fence required". The very
    # next thing this run does is activate the new configuration and helpers
    # and start the TARGET backend in Step 10, whose /package-update route is
    # live while Phase U5 acceptance is still running -- and an acceptance
    # failure there rolls product backend and helper material back underneath
    # any workload job issued into that window. So this run establishes the
    # SAME durable fence artifact directly, before the mutation window, and
    # the target backend finds it already present the moment it starts.
    _update_acquire_pre_activation_fence
    return 0
  fi
  die "refusing to mutate: could not take the exclusive product-update maintenance fence (${reason:-unknown}${detail:+: ${detail}}). Nothing has been changed. Let any active workload package update finish, or resolve it through the operator controls (resume or roll back), then run this updater again."
}

# _update_acquire_pre_activation_fence: the same durable artifact, written
# directly, for an installation whose backend has no fence route yet.
#
# Same holder semantics, same fail-closed durability (fsync, atomic rename,
# directory fsync) as the backend-created fence -- there is exactly one fence
# file, whichever side created it, and the activated target backend reads it
# the same way. A fence another product update already holds is still never
# stolen, and re-running for the same holder is idempotent.
_update_acquire_pre_activation_fence() {
  local output status reason detail
  output="$(pct exec "${VMID}" -- python3 "${UPDATE_FENCE_CT_PATH}" "${UPDATE_RUN_ID}" --pre-activation 2>/dev/null)" \
    && status=0 || status=$?
  (( status == 0 )) && [[ -n "${output}" ]] \
    || die "refusing to mutate: could not establish the product-update maintenance fence on this pre-activation installation. Nothing has been changed."

  if _json_bool_field_is_true "${output}" "ok"; then
    UPDATE_FENCE_HELD="1"
    update_journal_record update-maintenance-fence-held "${VMID}"
    log_info "established the exclusive product-update maintenance fence directly (holder ${UPDATE_RUN_ID}); this installation predates operator-triggered package updates, and the activated target backend will refuse workload starts from the moment it comes up"
    return 0
  fi
  reason="$(_json_field_from_text "${output}" "reason")"
  detail="$(_json_field_from_text "${output}" "detail")"
  die "refusing to mutate: could not establish the exclusive product-update maintenance fence on this pre-activation installation (${reason:-unknown}${detail:+: ${detail}}). Nothing has been changed."
}

# _update_release_maintenance_fence: remove the fence THIS run holds.
#
# Deliberately a plain filesystem removal rather than another authority
# transaction. Releasing only ever widens what is permitted, so it cannot
# race anything into existence and needs no atomicity -- and it must keep
# working in the one case where an API release could not: a failed activation
# update that has rolled back to a pre-activation backend, which has no
# maintenance-fence route at all.
#
# Keyed off the FENCE'''s OWN RECORDED HOLDER, not off this process's memory.
#
# That is deliberately the smallest mechanism that closes the crash edge
# around acquisition. The fence becomes durable before the acquiring call
# returns, so a crash between "the fence exists" and "this run recorded that
# it holds it" is reachable -- and an in-memory flag or a journal marker
# written afterwards would both miss it, orphaning a fence nobody would ever
# release. The fence file already carries the run id that created it, and the
# interrupted run'''s journal already carries the same run id, so recovery can
# match them without any new durable state at all.
#
# The four required behaviours fall straight out of that comparison:
#   absent          -> nothing to release (success -- there is nothing this
#                       run still owes);
#   this run'''s      -> release it (at a terminal point only), and the
#                       result is TRUTHFUL: success is returned only once
#                       removal AND its durability barrier are both proven,
#                       never merely attempted;
#   another run'''s   -> never touched (success -- not this run'''s to
#                       release);
#   unreadable      -> fail closed: FAILURE, left in place, reported.
#
# Correction pass (review finding on PR #74): this used to warn-and-continue
# on a failed `rm` or a skipped durability barrier, and to treat an
# unreadable fence as a successful release -- so a caller could clear its
# own recovery journal believing release had happened when it truthfully had
# not, orphaning the fence (see the caller-side ordering fix at every call
# site below). This now returns non-zero for every case in which this run's
# own release obligation was NOT positively discharged, and every caller
# below is ordered so that failure propagates (via `set -e`) BEFORE the
# journal carrying this run's recovery identity is ever cleared.
_update_release_maintenance_fence() {
  local fence_path="/var/lib/hubinet-ops/product-update-maintenance.fence"
  local raw holder
  raw="$(pct exec "${VMID}" -- cat "${fence_path}" 2>/dev/null)" || raw=""
  if [[ -z "${raw}" ]]; then
    UPDATE_FENCE_HELD="0"
    return 0
  fi
  holder="$(_json_field_from_text "${raw}" "holder")" || holder=""
  if [[ -z "${holder}" ]]; then
    log_warn "the product-update maintenance fence inside container ${VMID} is unreadable; leaving it in place. Workload package updates will keep refusing until it is resolved by hand."
    return 1
  fi
  if [[ "${holder}" != "${UPDATE_RUN_ID}" ]]; then
    log_warn "the product-update maintenance fence inside container ${VMID} is held by another product update (holder ${holder}); leaving it untouched"
    return 0
  fi
  if ! pct exec "${VMID}" -- rm -f "${fence_path}" >/dev/null 2>&1; then
    log_warn "could not remove the product-update maintenance fence inside container ${VMID}; NOT recording it as released. Workload package updates will keep refusing until the next updater invocation retries."
    return 1
  fi
  if ! pct exec "${VMID}" -- sync -f /var/lib/hubinet-ops >/dev/null 2>&1; then
    log_warn "removed the product-update maintenance fence inside container ${VMID} but could not durably prove that removal; NOT recording it as released. The next updater invocation will retry."
    return 1
  fi
  UPDATE_FENCE_HELD="0"
  log_info "released the exclusive product-update maintenance fence; workload package updates are permitted again"
  return 0
}

_update_revalidate_before_mutation() {
  update_ownership_verify "${VMID}" revalidate "${UPDATE_INSTALLATION_RUN_ID}"
  _update_revalidate_plan_fence
  _update_preflight_ct_sync
  # LAST, and immediately before the first mutation: everything above may
  # still refuse this run harmlessly, and taking the fence before those
  # checks would leave workload updates blocked by a run that then declined
  # to proceed.
  _update_acquire_maintenance_fence
}

update_activate_and_accept() {
  log_phase "Phase U4: activate"

  _update_recheck_source_commit
  _update_capture_pre_mutation_facts

  # Immediately-before-mutation fence (correction pass 9, P1, sections 10
  # and 11): the per-VMID flock only serializes legitimate updater
  # invocations -- it does not stop a legitimate PVE operator/tool from
  # removing this CT and restoring another as the same VMID, or restoring
  # a snapshot of THIS SAME installation identity that rolls its live
  # software/database state backward, between planning and mutation. This
  # re-verifies the full ownership chain against the originally-approved
  # installation run-id and re-derives the bounded plan fingerprint,
  # BEFORE the autostart-disable request below (the first mutation of the
  # window) -- so a mismatch fails before autostart is touched, before the
  # service is stopped, and before any live artifact is mutated. It also
  # proves the CT durability barrier itself is usable before entering the
  # mutation window at all.
  _update_revalidate_before_mutation

  # Step 3a -- FIRST mutation of the window: temporarily remove
  # hubinet-ops from boot activation, so no intermediate half-swapped
  # state below can be auto-started by systemd after a PVE/CT reboot.
  # This both arms recovery and journals its own attempted-marker before
  # issuing the disable request; see _update_disable_service_autostart.
  _update_disable_service_autostart

  # Rollback is already armed by the autostart guard above. The stop
  # request may still mutate systemd state and return non-zero (or the
  # process may be interrupted before a success-only marker could be
  # written), so its own attempted-marker is likewise journaled first.
  UPDATE_ROLLBACK_ARMED="1"
  update_journal_record update-service-stop-attempted "${VMID}"
  run_logged pct exec "${VMID}" -- systemctl stop hubinet-ops \
    || die "failed to request a stop of hubinet-ops inside container ${VMID}; resulting service state is ambiguous and rollback recovery is required"
  _update_wait_until_service_stopped \
    || die "could not positively prove hubinet-ops stopped within ${BOOTSTRAP_SERVICE_TIMEOUT_SECONDS}s inside container ${VMID} (last state: ${_UPDATE_SERVICE_STATE_DETAIL})"
  # Diagnostic only; the EXIT rollback boundary is the attempted marker.
  ledger_record update-service-stopped "${VMID}"

  # Step 4 -- activate app payload atomically. The attempted-marker is
  # recorded BEFORE the first destructive move (live app -> rollback),
  # not after the swap completes, so rollback's own state-inspection
  # logic (_update_rollback_app) is armed for every intermediate failure
  # -- including the first move itself failing.
  update_journal_record update-app-activation-attempted "${VMID}"
  run_logged pct exec "${VMID}" -- mv /opt/hubinet-ops/app "/opt/hubinet-ops/app.rollback-${UPDATE_RUN_ID}" \
    || die "failed to move the live application payload aside inside container ${VMID}"
  # Durability barrier (correction pass 9, P1): if power is lost BEFORE
  # this completes, target activation has not yet happened -- recovery may
  # observe either side of the uncommitted rename below, but the old
  # installation remains the only runtime candidate. Once target
  # activation is allowed, the rollback material must already be durable.
  _update_durability_barrier_ct "/opt/hubinet-ops/app.rollback-${UPDATE_RUN_ID}"
  run_logged pct exec "${VMID}" -- mv "${UPDATE_APP_STAGED_PATH}" /opt/hubinet-ops/app \
    || die "failed to activate the staged application payload inside container ${VMID}"
  ledger_record update-app-activated "${VMID}"

  # 5/6. requirements + venv, only if changed. Same attempted-before-
  # first-destructive-move discipline as the app payload above.
  if [[ "${UPDATE_REQUIREMENTS_CHANGED}" == "1" ]]; then
    _update_activate_venv_and_requirements
  fi

  # Step 7 -- systemd unit, only if changed. (P1-B correction pass 2:) a
  # plain `cp` is NOT atomic -- a realistic failure (ENOSPC/EIO/etc mid-
  # write) can leave a PARTIAL destination behind while the live unit
  # remains completely intact, and rollback must never treat mere
  # existence of the canonical rollback-<UPDATE_RUN_ID> path as proof of
  # a complete, trustworthy preserving copy. So the live unit is first
  # copied to a run-owned TEMP candidate; only once that copy has fully
  # succeeded is it atomically renamed (same filesystem, so this rename
  # itself either fully happens or leaves nothing) into the canonical
  # rollback path -- and only THEN is the attempted-marker recorded,
  # immediately before the one actually destructive step (the `mv` of
  # the staged unit onto the live path).
  if [[ "${UPDATE_UNIT_CHANGED}" == "1" ]]; then
    local unit_rollback_path="/etc/systemd/system/hubinet-ops.service.rollback-${UPDATE_RUN_ID}"
    local unit_rollback_tmp_path="/etc/systemd/system/hubinet-ops.service.rollback-tmp-${UPDATE_RUN_ID}"
    run_logged pct exec "${VMID}" -- cp /etc/systemd/system/hubinet-ops.service "${unit_rollback_tmp_path}" \
      || { pct exec "${VMID}" -- rm -f "${unit_rollback_tmp_path}" >/dev/null 2>&1 || true; die "failed to preserve the active systemd unit inside container ${VMID}"; }
    run_logged pct exec "${VMID}" -- mv "${unit_rollback_tmp_path}" "${unit_rollback_path}" \
      || die "failed to finalize the preserved systemd unit inside container ${VMID}"
    # Durability barrier (correction pass 9, P1): the finalized rollback
    # copy must be durable before the attempted-marker is journaled and
    # the live unit is replaced -- this is the exact Codex witness, but
    # not the only artifact requiring the rule (see this file's header).
    _update_durability_barrier_ct "${unit_rollback_path}"
    update_journal_record update-unit-activation-attempted "${VMID}"
    run_logged pct exec "${VMID}" -- mv "${UPDATE_UNIT_STAGED_PATH}" /etc/systemd/system/hubinet-ops.service \
      || die "failed to activate the staged systemd unit inside container ${VMID}"
    ledger_record update-unit-activated "${VMID}"
    run_logged pct exec "${VMID}" -- systemctl daemon-reload \
      || die "systemctl daemon-reload failed inside container ${VMID}"
  fi

  # Step 8 -- PVE host helper, only if changed (host-side, same path).
  if [[ "${UPDATE_HELPER_CHANGED}" == "1" ]]; then
    cp "${UPDATE_HELPER_HOST_PATH}" "${UPDATE_HELPER_HOST_PATH}.rollback-${UPDATE_RUN_ID}" \
      || die "failed to preserve the active PVE host helper before activation"
    # Durability barrier (correction pass 9, P1) -- host-side: the same
    # rule applies to rollback-managed state on the PVE host filesystem,
    # not only inside the CT.
    _update_durability_barrier_host "${UPDATE_HELPER_HOST_PATH}.rollback-${UPDATE_RUN_ID}"
    update_journal_record update-helper-activated "${VMID}"
    mv "${UPDATE_HELPER_STAGED_HOST_PATH}" "${UPDATE_HELPER_HOST_PATH}" \
      || die "failed to activate the staged PVE host helper (same-path atomic rename)"
  fi

  # Step 8b -- the five package-update forced-command boundaries, their
  # root-only operation journals, and the one configuration block that
  # activates the lifecycle. Every artifact created here is journaled before
  # it exists, so a failure after this point removes exactly the new
  # privileged access paths this run created and restores exactly the ones it
  # replaced. See update-boundaries.sh.
  update_boundaries_activate

  # Step 9 -- authority action: preserve, or backup + reset.
  if [[ "${UPDATE_AUTHORITY_ACTION}" == "reset_required" ]]; then
    _update_perform_authority_reset
  fi

  # Step 10 -- start service.
  ledger_record update-service-start-attempted "${VMID}"
  run_logged pct exec "${VMID}" -- systemctl start hubinet-ops \
    || die "failed to start hubinet-ops inside container ${VMID} after activation"
  ledger_record update-service-started "${VMID}"

  _update_wait_until_service_active_and_healthy \
    || die "hubinet-ops did not prove systemd active AND answer its unauthenticated health endpoint within ${BOOTSTRAP_SERVICE_TIMEOUT_SECONDS}s after target activation (${_UPDATE_SERVICE_READINESS_DETAIL})"
  log_pass "target HTTP readiness: systemd active and health endpoint ready"

  log_pass "activation complete"

  log_phase "Phase U5: acceptance"
  _update_accept_discovery
  _update_accept_host_control
  _update_accept_firewall
  log_pass "acceptance"

  _update_write_source_marker

  # Final accepted-target durability barrier (correction pass 9, P1,
  # section 7): acceptance and the installed-source marker are proven, but
  # the new live app/venv/unit/helper/marker content this run activated
  # still only had to survive namespace mutation in the running kernel --
  # never yet a proof it would survive a power loss. An accepted target
  # must not become "journal = completed, rollback artifacts deleted"
  # while its new live content still only exists in cache. Bounded to what
  # this run actually mutated -- never flushed merely for ceremony.
  _update_durability_barrier_ct /opt/hubinet-ops
  if [[ "${UPDATE_UNIT_CHANGED}" == "1" ]]; then
    _update_durability_barrier_ct /etc/systemd/system
  fi
  if [[ "${UPDATE_AUTHORITY_ACTION}" == "reset_required" ]]; then
    _update_durability_barrier_ct /var/lib/hubinet-ops
  fi
  if [[ "${UPDATE_HELPER_CHANGED}" == "1" ]]; then
    _update_durability_barrier_host "${UPDATE_HELPER_HOST_PATH}"
  fi

  # The target is fully accepted, its installed-source marker is coherent,
  # and every live filesystem this run mutated has crossed its durability
  # barrier -- and only now may normal boot activation be restored, and
  # positively proven, before the journal records this run as completed.
  # This leaves exactly one narrow crash window (accepted + coherent
  # marker + durable + re-enabled, journal not yet completed): a reboot
  # there starts the fully accepted TARGET installation, never a mixed
  # one, and a later active-journal recovery may still conservatively roll
  # it back. If the durability barrier above fails, the target is NOT
  # completed: `die` leaves the rollback boundary already crossed, so the
  # existing EXIT-trap recovery performs a coherent rollback exactly as it
  # would for any other activation-window failure.
  _update_restore_service_autostart \
    || die "the target installation was fully accepted, but hubinet-ops boot activation could not be proven restored inside container ${VMID} (${_UPDATE_SERVICE_ENABLED_DETAIL})"

  # Durability barrier (correction pass 10, P1): the just-restored
  # enablement state itself must be durable BEFORE this run is journaled
  # completed -- exactly the same invariant as every other durability
  # barrier in this file, applied to the unit-file-enablement fact rather
  # than to app/venv/unit/authority content. A failure here dies with the
  # rollback boundary already crossed, so the existing EXIT-trap recovery
  # performs a coherent rollback exactly as it would for any other
  # activation-window failure.
  _update_durability_barrier_ct /etc/systemd/system

  _update_finish_summary
}

# _update_activate_venv_and_requirements (correction pass 8, P1): the
# target virtualenv is BUILT AT ITS FINAL LIVE PATHNAME, never built
# elsewhere and renamed into place.
#
# The previous design created /opt/hubinet-ops/.venv.staged-<runid> while
# the old service was still running and then renamed that whole directory
# onto /opt/hubinet-ops/.venv. A Python virtualenv is not generally
# relocatable: the console entrypoints pip/ensurepip generate embed the
# ABSOLUTE interpreter path of the environment they were created in, so
# the "activated" environment's own bin/pip (and every other generated
# entrypoint) still pointed at a .venv.staged-<runid> pathname that no
# longer existed. Rewriting shebangs is deliberately NOT the fix; building
# at the final pathname is.
#
# The cost is a longer maintenance window when dependencies actually
# change -- accepted, and deliberately not optimized away with wheel
# caches, download stages, or a package mirror. A code-only update (the
# common case) never reaches this function at all: no venv is rebuilt, no
# pip runs, and the existing environment is left exactly as it is.
#
# Ordering, and the invariants each step exists for:
#
#   1. durably journal update-venv-activation-attempted BEFORE the first
#      destructive mutation -- as for every other rollback-managed
#      artifact in this file, so rollback is armed even if the very first
#      move fails, and so a SIGKILL/reboot mid-build is recoverable;
#   2. move the live environment aside to this run's fixed
#      .venv.rollback-<runid> path (atomic rename: it either fully
#      happens or leaves both sides exactly as they were);
#   3. POSITIVELY prove the final live pathname is now absent, through
#      the same three-valued path probe every other proof here uses --
#      UNKNOWN is never permission to build;
#   4. build the new environment directly at that final pathname and
#      install the exact target requirements into it;
#   5. only then swap requirements.txt, so the recorded requirements
#      always describe the environment that is actually installed.
#
# A failure or interruption anywhere in 2-5 leaves at most a PARTIAL
# environment at the live path. That is never resumed: rollback's
# _update_rollback_venv_and_requirements removes the partial target,
# proves the path absent, and restores the preserved old environment (and
# then the preserved requirements.txt), exactly as it already did for a
# failed rename.
_update_activate_venv_and_requirements() {
  local live_venv="/opt/hubinet-ops/.venv"
  local rollback_venv="/opt/hubinet-ops/.venv.rollback-${UPDATE_RUN_ID}"
  local path_state_rc

  update_journal_record update-venv-activation-attempted "${VMID}"
  run_logged pct exec "${VMID}" -- mv "${live_venv}" "${rollback_venv}" \
    || die "failed to move the active virtualenv aside inside container ${VMID}"

  if _update_ct_path_state "${live_venv}"; then
    die "the live virtualenv path ${live_venv} still exists after the pre-update environment was moved aside inside container ${VMID} -- refusing to build a new environment over unknown content"
  else
    path_state_rc=$?
  fi
  (( path_state_rc == 1 )) \
    || die "could not prove the live virtualenv path ${live_venv} absent inside container ${VMID} before building the target environment"

  # Durability barrier (correction pass 9, P1): the preserved old
  # environment must be durable BEFORE the target is built at the final
  # live pathname -- same reasoning as the app payload above.
  _update_durability_barrier_ct "${rollback_venv}"

  run_logged pct exec "${VMID}" -- python3 "${UPDATE_VENV_STAGE_TOOL_CT_PATH}" "${live_venv}" "${UPDATE_REQUIREMENTS_STAGED_PATH}" \
    || die "failed to build the target virtualenv at ${live_venv} inside container ${VMID}; the pre-update environment is preserved at ${rollback_venv} and rollback recovery is required"
  run_logged pct exec "${VMID}" -- chown -R hubinetops:hubinetops "${live_venv}" \
    || die "failed to set ownership on the newly built virtualenv inside container ${VMID}"
  pct exec "${VMID}" -- rm -f "${UPDATE_VENV_STAGE_TOOL_CT_PATH}" >/dev/null 2>&1 \
    || log_warn "could not remove ${UPDATE_VENV_STAGE_TOOL_CT_PATH} inside the container (non-fatal)"

  run_logged pct exec "${VMID}" -- mv /opt/hubinet-ops/requirements.txt "/opt/hubinet-ops/requirements.txt.rollback-${UPDATE_RUN_ID}" \
    || die "failed to move the active requirements.txt aside inside container ${VMID}"
  # Durability barrier (correction pass 9, P1): the preserved old
  # requirements.txt must be durable before the staged target is activated.
  _update_durability_barrier_ct "/opt/hubinet-ops/requirements.txt.rollback-${UPDATE_RUN_ID}"
  run_logged pct exec "${VMID}" -- mv "${UPDATE_REQUIREMENTS_STAGED_PATH}" /opt/hubinet-ops/requirements.txt \
    || die "failed to activate the staged requirements.txt inside container ${VMID}"
  ledger_record update-venv-activated "${VMID}"
}

# _update_perform_authority_reset: the backup/remove durability barriers
# required here (correction pass 9, P1, section 5) are deliberately NOT
# separate shell-level `sync` calls beside this function -- they are
# implemented INSIDE hubinet-ops-authority-tool.py itself. `backup`
# reports "ok": true only after its destination has crossed its own
# fsync-based durability barrier, and `remove` reports "ok": true only
# after the containing directory's removal state has been synchronized
# (see that script's own docstring/cmd_backup/cmd_remove). So the ordinary
# ok:true gates already used below are, unchanged, the exact durability
# proof this transition needs: backup ok:true -> durable backup exists;
# THEN reset-attempted is journaled; THEN remove is called.
_update_perform_authority_reset() {
  local backup_dir backup_ct_path tool_output status
  backup_dir="/var/lib/hubinet-ops/update-backups/${UPDATE_RUN_ID}"
  backup_ct_path="${backup_dir}/authority.db"
  run_logged pct exec "${VMID}" -- install -d -o hubinetops -g hubinetops -m 0750 "${backup_dir}" \
    || die "failed to create the authority backup directory inside container ${VMID}"

  tool_output="$(pct exec "${VMID}" -- python3 "${UPDATE_TOOL_CT_PATH}" backup \
    /var/lib/hubinet-ops/authority.db "${backup_ct_path}" \
    "${UPDATE_CURRENT_SCHEMA_MARKER}" "${UPDATE_CURRENT_SCHEMA_VERSION}" "${UPDATE_CURRENT_BACKEND_INSTANCE_ID}" \
    2>/dev/null)" && status=0 || status=$?
  if (( status != 0 )) || ! _json_bool_field_is_true "${tool_output}" "ok"; then
    local reason
    reason="$(_json_field_from_text "${tool_output}" "reason")"
    die "authority database backup failed (${reason:-unknown}) before any live data was removed -- the old authority database remains authoritative and untouched"
  fi
  UPDATE_DB_BACKUP_PATH="${backup_ct_path}"
  ledger_record update-authority-backed-up "${VMID}"

  # Durability barrier (correction pass 10, P1): the helper's own
  # file-plus-immediate-directory fsync (hubinet-ops-authority-tool.py's
  # _fsync_file_and_dir) proves the backup FILE's bytes and its immediate
  # parent directory entry are durable -- but ${backup_dir} (and possibly
  # its own parent, update-backups/) was JUST created by the `install -d`
  # above, in this same run. Fsyncing a file's immediate directory does
  # NOT prove the directory ENTRY LINKING that newly-created directory
  # into ITS OWN parent survived a crash -- an ancestor link is a distinct
  # fact from the leaf file/directory's own durability. A CT filesystem-
  # level barrier over the run's own backup directory closes that
  # ancestry, exactly like every other newly-created durability-critical
  # path in this file (`sync -f` synchronizes the whole containing
  # filesystem, not merely one directory). Only once this barrier passes
  # is the backup treated as destructively usable: the reset-attempted
  # marker is journaled and the live database removed after this point,
  # never before it.
  _update_durability_barrier_ct "${backup_dir}"

  # P1-A correction pass 2: the durable rollback-arming marker must be
  # recorded AFTER the coherent backup is validated but BEFORE the first
  # potentially destructive removal below -- not only after `remove`
  # fully succeeds. cmd_remove's own unlink loop is sequential (db, then
  # -wal, then -shm); an intermediate unlink can succeed on an earlier
  # path and then fail on a later one, so `remove` itself can report
  # "ok": false having already mutated live state. If the attempted-
  # marker were recorded only after a fully successful `remove`, that
  # intermediate failure would die here with NO marker recorded at all,
  # and update_rollback_on_failure would then skip authority restoration
  # entirely (see its own `ledger_has update-authority-reset-attempted`
  # check below) -- leaving old code paired with a missing/partial
  # authority database. Rollback itself never trusts this marker to mean
  # "reset completed"; it always re-proves removal of whatever target/
  # partial state remains before ever copying the validated backup back
  # (see update_rollback_on_failure).
  update_journal_record update-authority-reset-attempted "${VMID}"

  tool_output="$(pct exec "${VMID}" -- python3 "${UPDATE_TOOL_CT_PATH}" remove /var/lib/hubinet-ops/authority.db 2>/dev/null)" \
    && status=0 || status=$?
  (( status == 0 )) && _json_bool_field_is_true "${tool_output}" "ok" \
    || die "failed to remove the old authority database inside container ${VMID} after a validated backup was already made at ${UPDATE_DB_BACKUP_PATH}"
  ledger_record update-authority-reset "${VMID}"
}

_update_accept_discovery() {
  run_logged pct push "${VMID}" "${BOOTSTRAP_SCRIPT_DIR}/lib/hubinet-ops-bootstrap-accept.py" "${CT_ACCEPT_SCRIPT_CT}" \
    || die "failed to push the acceptance script into container ${VMID}"

  local -a accept_args=("${UPDATE_DISPLAY_NAME}" "${BOOTSTRAP_DISCOVERY_TIMEOUT_SECONDS}")
  if [[ "${UPDATE_AUTHORITY_ACTION}" == "preserve" && "${UPDATE_PRE_COMMITTED_SEQUENCE}" =~ ^[0-9]+$ ]]; then
    accept_args+=("${UPDATE_PRE_COMMITTED_SEQUENCE}")
  fi

  local output status
  output="$(pct exec "${VMID}" -- python3 "${CT_ACCEPT_SCRIPT_CT}" "${accept_args[@]}" 2>&1)" && status=0 || status=$?
  pct exec "${VMID}" -- rm -f "${CT_ACCEPT_SCRIPT_CT}" >/dev/null 2>&1 || true

  printf '%s\n' "${output}" | while IFS= read -r line; do log_info "acceptance: ${line}"; done
  local last_line
  last_line="$(printf '%s\n' "${output}" | tail -n1)"
  [[ "${status}" -eq 0 && "${last_line}" == PASS* ]] \
    || die "post-update discovery acceptance failed: ${last_line:-no output}"

  UPDATE_POST_BACKEND_INSTANCE_ID="$(printf '%s' "${last_line}" | sed -n 's/.*backend_instance_id=\([^ ]*\).*/\1/p')"
  [[ -n "${UPDATE_POST_BACKEND_INSTANCE_ID}" ]] \
    || die "post-update acceptance did not report a backend_instance_id"

  if [[ "${UPDATE_AUTHORITY_ACTION}" == "preserve" ]]; then
    [[ "${UPDATE_POST_BACKEND_INSTANCE_ID}" == "${UPDATE_PRE_BACKEND_INSTANCE_ID}" ]] \
      || die "identity regression: backend_instance_id changed from ${UPDATE_PRE_BACKEND_INSTANCE_ID} to ${UPDATE_POST_BACKEND_INSTANCE_ID} on a schema-preserving update"
  else
    [[ "${UPDATE_POST_BACKEND_INSTANCE_ID}" != "${UPDATE_PRE_BACKEND_INSTANCE_ID}" ]] \
      || die "authority reset did not actually produce a new backend_instance_id"
  fi
}

# _update_accept_host_control: re-runs the same deliberately-unknown typed
# probe deploy/lib/bootstrap-host-control.sh's phase8c already performs at
# install time, proving the forced-command boundary (same helper path,
# same pinned key/known_hosts, unchanged or freshly-activated content)
# still rejects an unknown operation exactly as expected.
_update_accept_host_control() {
  local probe_output probe_status probe_request
  probe_request='{"request_version":1,"operation":"probe","target":{},"context":{}}'
  probe_output="$(printf '%s' "${probe_request}" | pct exec "${VMID}" -- runuser -u hubinetops -- \
    ssh -T -p 22 -i "${HOST_CONTROL_CT_PRIVATE_KEY}" \
    -o BatchMode=yes -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes \
    -o "UserKnownHostsFile=${HOST_CONTROL_CT_KNOWN_HOSTS}" \
    -o PasswordAuthentication=no -o KbdInteractiveAuthentication=no \
    -o ForwardAgent=no -o ClearAllForwardings=yes \
    "root@$(_endpoint_host_of_helper)" 2>/dev/null)" && probe_status=0 || probe_status=$?
  (( probe_status == 2 )) \
    && printf '%s' "${probe_output}" | grep -qF 'unknown host-control operation' \
    || die "post-update host-control acceptance failed: the forced-command SSH boundary did not reject the typed probe as expected"
}

# _endpoint_host_of_helper: the host-control SSH target is always this PVE
# host itself (root@<pve-endpoint-host>) -- read from the CT's own pinned
# known_hosts file rather than requiring a bootstrap-only --pve-endpoint
# flag this updater deliberately does not accept.
_endpoint_host_of_helper() {
  local known_hosts_text
  known_hosts_text="$(pct exec "${VMID}" -- cat "${HOST_CONTROL_CT_KNOWN_HOSTS}" 2>/dev/null)"
  printf '%s\n' "${known_hosts_text}" | head -n1 | cut -d' ' -f1
}

_update_accept_firewall() {
  local post_conf
  post_conf="$(pct exec "${VMID}" -- cat /etc/nftables.conf 2>/dev/null)"
  [[ "${post_conf}" == "${UPDATE_PRE_NFTABLES_CONF}" ]] \
    || die "post-update firewall acceptance failed: /etc/nftables.conf is no longer byte-identical to its pre-update content"
  local nft_active
  nft_active="$(pct exec "${VMID}" -- systemctl is-active nftables 2>/dev/null || true)"
  [[ "${nft_active}" == "active" ]] \
    || die "post-update firewall acceptance failed: nftables is not active inside container ${VMID} (${nft_active:-unknown})"
}

# _update_write_source_marker (P2-B): the new marker is fully prepared --
# pushed to a run-owned staged path AND chown'd there -- before the live
# marker is touched at all. Only then is the live marker's swap performed
# via the same attempted-marker-before-first-destructive-mutation /
# state-inspection-rollback discipline as every other rollback-managed
# artifact in this file (see the header comment and _update_rollback_app
# et al.): a failed update must always leave the PRE-UPDATE marker state
# exactly, whether that was an old SHA or no marker at all -- never a NEW
# marker paired with a rolled-back OLD app/db.
_update_write_source_marker() {
  local marker_path="/opt/hubinet-ops/.hubinet-source-commit"
  local marker_staged_path="${marker_path}.staged-${UPDATE_RUN_ID}"
  local marker_rollback_path="${marker_path}.rollback-${UPDATE_RUN_ID}"
  local marker_tmp
  marker_tmp="$(mktemp /tmp/hubinet-ops-update-source-marker.XXXXXX)"
  printf '%s\n' "${SOURCE_HEAD_SHA}" >"${marker_tmp}"
  run_logged pct push "${VMID}" "${marker_tmp}" "${marker_staged_path}" \
    || die "failed to stage the installed-source marker after a successful update"
  rm -f "${marker_tmp}"
  run_logged pct exec "${VMID}" -- chown hubinetops:hubinetops "${marker_staged_path}" \
    || die "failed to set ownership on the staged installed-source marker"

  # P2-B (correction pass 7) -- ordering. NO marker mutation can occur
  # before its precondition has been PROVEN and journaled, so the
  # attempted-marker must not be armed before that proof exists. The old
  # order recorded update-marker-activation-attempted first and only then
  # probed, so an UNKNOWN probe died with "attempted" already in the
  # ledger and neither precondition recorded -- and rollback then hard
  # stopped on a marker mutation that had provably never been armed.
  #
  # Correct order, and the only one this function ever uses:
  #
  #   1. stage + chown the new marker                      (above)
  #   2. probe the old marker
  #   3. EXISTS   -> record update-marker-precondition-exists
  #      ABSENT   -> record update-marker-precondition-absent
  #      UNKNOWN  -> fail BEFORE any marker mutation is armed
  #   4. durably journal update-marker-activation-attempted together with
  #      that proven precondition, in one atomic journal replacement,
  #      BEFORE the first marker `mv`
  #   5. perform the marker mutation
  #
  # The attempted marker is persisted through the DURABLE journal (a
  # single update_journal_record call carries the ledger's whole
  # recovery-relevant set, precondition included), never left only in the
  # ephemeral ledger. An UNKNOWN precondition therefore produces no
  # attempted marker at all: ordinary full artifact rollback proceeds and
  # _update_rollback_marker correctly has nothing to do.
  local path_state_rc
  if _update_ct_path_state "${marker_path}"; then
    ledger_record update-marker-precondition-exists "${VMID}"
    update_journal_record update-marker-activation-attempted "${VMID}"
    run_logged pct exec "${VMID}" -- mv "${marker_path}" "${marker_rollback_path}" \
      || die "failed to move the pre-update installed-source marker aside inside container ${VMID}"
    # Durability barrier (correction pass 9, P1): only when an old marker
    # actually existed is there rollback material to flush -- an absent
    # precondition has nothing to make durable here.
    _update_durability_barrier_ct "${marker_rollback_path}"
  else
    path_state_rc=$?
    (( path_state_rc == 1 )) \
      || die "could not prove whether the pre-update installed-source marker exists inside container ${VMID} -- refusing to mutate it"
    ledger_record update-marker-precondition-absent "${VMID}"
    update_journal_record update-marker-activation-attempted "${VMID}"
  fi
  run_logged pct exec "${VMID}" -- mv "${marker_staged_path}" "${marker_path}" \
    || die "failed to activate the installed-source marker inside container ${VMID}"
  ledger_record update-marker-activated "${VMID}"
}

# _update_rollback_marker: same state-inspection discipline as
# _update_rollback_app et al. If an old marker existed pre-update, its
# content was moved aside to marker_rollback_path before the live marker
# was touched -- if that path exists, restore it verbatim (whether the
# live marker currently holds nothing or the new SHA, this is correct
# either way, since the swap is an atomic rename). If it does NOT exist,
# either the old-marker-aside move never ran (no marker existed
# pre-update) or it failed before doing anything -- either way there is
# no old marker to restore, so a failed update must leave NO marker
# rather than a new one paired with the rolled-back old app.
_update_rollback_marker() {
  ledger_has update-marker-activation-attempted "${VMID}" || return 0
  local marker_path="/opt/hubinet-ops/.hubinet-source-commit"
  local marker_rollback_path="${marker_path}.rollback-${UPDATE_RUN_ID}"
  local path_state_rc
  if ledger_has update-marker-precondition-exists "${VMID}"; then
    if _update_ct_path_state "${marker_rollback_path}"; then
      _update_remove_ct_path_and_prove_absent "${marker_path}" file "installed-source marker"
      if ! pct exec "${VMID}" -- mv "${marker_rollback_path}" "${marker_path}" >/dev/null 2>&1; then
        _update_rollback_hard_stop "could not restore the pre-update installed-source marker inside container ${VMID}"
      fi
      _update_durability_barrier_ct_or_hard_stop "${marker_path}" "restoring the pre-update installed-source marker"
      return 0
    else
      path_state_rc=$?
    fi
    if (( path_state_rc == 2 )); then
      _update_rollback_hard_stop "could not determine whether the pre-update installed-source marker rollback artifact exists inside container ${VMID}"
    fi
    # The atomic live->rollback move did not happen. Prove the old live
    # marker is still present, then preserve it untouched. Ambiguous with
    # a prior rollback attempt of this same run having already fully
    # restored and consumed the rollback artifact -- both cases require
    # the same action, so (re-)establish the barrier defensively either
    # way (section 6).
    if _update_ct_path_state "${marker_path}"; then
      _update_durability_barrier_ct_or_hard_stop "${marker_path}" "replaying the already-restored installed-source marker"
      return 0
    fi
    path_state_rc=$?
    _update_rollback_hard_stop "the pre-update installed-source marker was known to exist, but its rollback artifact is absent and the live marker is $( (( path_state_rc == 1 )) && printf 'absent' || printf 'unknown' )"
  elif ledger_has update-marker-precondition-absent "${VMID}"; then
    if _update_ct_path_state "${marker_rollback_path}"; then
      _update_rollback_hard_stop "an installed-source marker rollback artifact exists even though the pre-update marker was proven absent"
    else
      path_state_rc=$?
    fi
    (( path_state_rc == 1 )) \
      || _update_rollback_hard_stop "could not prove the installed-source marker rollback path absent"
    _update_remove_ct_path_and_prove_absent "${marker_path}" file "target installed-source marker"
    _update_durability_barrier_ct_or_hard_stop /opt/hubinet-ops "restoring the absent pre-update installed-source marker state"
  else
    # Structurally unreachable since correction pass 7 -- the attempted
    # marker is now journaled only together with a proven precondition
    # (see _update_write_source_marker). Retained as defence in depth: an
    # attempted marker with no precondition would mean rollback cannot
    # know what the pre-update marker state was, and guessing is never an
    # option here.
    _update_rollback_hard_stop "installed-source marker activation was attempted without a recorded, proven precondition"
  fi
}

_update_finish_summary() {
  # Coherence and acceptance have been positively proven. Persist the
  # terminal state before removing rollback material, so a crash between
  # these two steps is cleanup-only on the next invocation, never a false
  # request to roll an already-accepted target back.
  update_journal_checkpoint completed
  # Test-only (PR #74 review finding 2): exercise a real TERM here, after
  # the completed checkpoint is durable but before the fence release this
  # journal still owes -- the fixture a later invocation's own
  # `update_startup_recovery_gate` completed/recovered branch then recovers
  # through.
  _update_test_term_checkpoint before_completed_fence_release
  # Terminal and proven: acceptance passed, the marker is coherent, and the
  # `completed` checkpoint is durable. Only now may workload package updates
  # start again -- releasing any earlier would re-open issuance while this
  # run could still be asked to roll back.
  _update_release_maintenance_fence
  # Test-only (correction pass 13, P1): exercise a real TERM here, after
  # the completed checkpoint is durable but before any rollback artifact
  # is removed -- see _update_test_term_checkpoint's own docstring.
  _update_test_term_checkpoint after_completed_checkpoint

  # Success: clean up rollback material and staged leftovers -- nothing
  # here is managed state a future update depends on. PR #65 correction
  # pass 15, P2: this is now the SAME strict, idempotent, run-owned
  # cleanup startup recovery already performs for a completed/recovered
  # journal (_update_cleanup_recovered_run_artifacts), not a second,
  # divergent, best-effort ("|| true") cleanup contract -- and every step
  # in it is load-bearing: a failure hard-stops (exit 1) with the
  # `completed` journal already durable on disk. The EXIT trap's terminal-
  # checkpoint rule (see update-proxmox-0.5.sh) means that is NEVER
  # reinterpreted as permission to roll back -- the next invocation's
  # existing startup-recovery `completed` path (update_startup_recovery_
  # gate) re-proves enabled+active+healthy, retries this exact cleanup,
  # and only then clears the journal. `completed` must never mean
  # "rollback material may or may not still be lying around forever".
  _update_cleanup_recovered_run_artifacts
  # Test-only (correction pass 13, P1): exercise a real TERM here, after
  # cleanup has fully run but before the journal is cleared.
  _update_test_term_checkpoint after_completed_partial_cleanup
  _update_journal_clear

  cat <<SUMMARY

Hubinet Ops in-place update: PASS

VMID:                 ${VMID}
Installed source:     ${UPDATE_INSTALLED_SHA} -> ${SOURCE_HEAD_SHA}
backend_instance_id:  ${UPDATE_POST_BACKEND_INSTANCE_ID}
Authority action:     ${UPDATE_AUTHORITY_ACTION}
SUMMARY
  if [[ -n "${UPDATE_DB_BACKUP_PATH}" ]]; then
    cat <<BACKUP
Authority DB backup:  ${UPDATE_DB_BACKUP_PATH} (inside container ${VMID}; retained, not auto-deleted)
Home Assistant:       re-enrollment REQUIRED (backend_instance_id changed)
BACKUP
  else
    cat <<PRESERVED
Home Assistant:       no action required (backend_instance_id preserved)
PRESERVED
  fi
}

# ---------------------------------------------------------------------------
# Rollback -- AGENTS.md task prompt sections 21/24/25. Restores the
# coherent PRE-UPDATE installation, including the authority database when a
# destructive reset was already performed. Never leaves old code paired
# with a new/incompatible schema.
# ---------------------------------------------------------------------------

update_rollback_on_failure() {
  local exit_code="$1"
  log_warn "update failed (exit ${exit_code}) after a service stop was attempted -- recovering the coherent pre-update installation"

  # The target service may be running even when a start/stop command
  # returned failure before its success marker was recorded. Always issue
  # a stop request, then prove a definitively non-running state before the
  # first rollback mutation. A warning-and-continue is forbidden here.
  pct exec "${VMID}" -- systemctl stop hubinet-ops >/dev/null 2>&1 \
    || log_warn "rollback stop request returned failure; proving live service state before any rollback mutation"
  _update_wait_until_service_stopped \
    || _update_rollback_hard_stop "could not positively prove hubinet-ops non-running before rollback mutation (last state: ${_UPDATE_SERVICE_STATE_DETAIL})"

  # Undo order below roughly mirrors LIFO (the installed-source marker is
  # always the LAST thing activation touches, so it is undone first here);
  # each restore below is independent and self-contained (state-inspection
  # based, never assumes another artifact's rollback already ran), so the
  # exact relative order does not change correctness -- only that every
  # attempted artifact is restored before the service is started again.
  _update_rollback_marker

  _update_rollback_authority

  _update_rollback_host_helper

  # Removes exactly the privileged access paths THIS run created -- helper,
  # forced-command authorization, and key -- and restores exactly the ones it
  # replaced. A failed activation update must not leave a new key that can
  # reach a root-owned mutation helper behind.
  update_boundaries_rollback

  _update_rollback_unit
  _update_rollback_venv_and_requirements
  _update_rollback_app

  # Every rollback-managed artifact is back at its pre-update content, so
  # the installation is coherent again and may safely be boot-activated.
  # If this run ever ATTEMPTED to remove boot activation, restoring and
  # positively proving it is load-bearing: a rollback that left the unit
  # disabled would silently convert a recovered installation into one that
  # never comes back after the next PVE/CT restart. Never inferred from
  # the enable command's own exit status.
  if ledger_has update-service-autostart-disable-attempted "${VMID}"; then
    # PR #65 correction pass 13, P2: the just-restored unit file
    # (_update_rollback_unit, above) may hold a DIFFERENT [Install]
    # section than whatever unit definition was last enabled during
    # forward activation -- reaching this point at all means the unit was
    # once disabled and is now being re-enabled, but only a unit this run
    # actually swapped (update-unit-activation-attempted) could have been
    # enabled under a changed [Install] section in between. `reenable`
    # resets installed links to exactly the restored old unit's current
    # [Install] section, atomically dropping any stale target-only link;
    # plain `enable` never removes a link a different unit definition
    # installed. An update whose unit never changed keeps the simpler,
    # unmodified `enable` behavior.
    local autostart_restore_mode="enable"
    ledger_has update-unit-activation-attempted "${VMID}" && autostart_restore_mode="reenable"
    _update_restore_service_autostart "${autostart_restore_mode}" \
      || _update_rollback_hard_stop "restored the pre-update installation's files, but could not prove hubinet-ops boot activation re-enabled (${autostart_restore_mode}) inside container ${VMID} (${_UPDATE_SERVICE_ENABLED_DETAIL}) -- it would not start again after a reboot"
    # Durability barrier (correction pass 10, P1): the restored enablement
    # state itself must survive a subsequent power loss BEFORE the old
    # service is started again -- the same invariant as the forward-path
    # barriers in _update_disable_service_autostart and
    # update_activate_and_accept, applied here on the rollback/recovery
    # side. A replay that finds the unit already enabled still runs this
    # unconditionally, re-establishing the barrier before terminal
    # recovery (section 6 discipline, same as every other rollback-
    # restoration barrier in this file).
    _update_durability_barrier_ct_or_hard_stop /etc/systemd/system "restoring hubinet-ops boot activation"
  fi

  run_logged pct exec "${VMID}" -- systemctl start hubinet-ops \
    || _update_rollback_hard_stop "restored the pre-update installation's files, but could not start hubinet-ops inside container ${VMID}"

  _update_wait_until_service_active_and_healthy \
    || _update_rollback_hard_stop "restored the pre-update installation's files, but it did not prove active AND answer its own unauthenticated health probe within ${BOOTSTRAP_SERVICE_TIMEOUT_SECONDS}s (${_UPDATE_SERVICE_READINESS_DETAIL})"

  # Terminal state first, durable and provably retained -- so a crash
  # anywhere below is cleanup-only on the next invocation, never a false
  # request to roll an already-restored installation back again.
  update_journal_checkpoint recovered
  _update_cleanup_recovered_run_artifacts
  _update_test_term_checkpoint before_recovery_fence_release
  # The pre-update installation is fully restored, enabled, running, and
  # healthy, and this run owns no rollback-capable state any more. Only now
  # is it truthful to let workload package updates start again -- and this
  # run's own journal, the only durable record carrying its recovery
  # identity (UPDATE_RUN_ID) that a later invocation could match the fence's
  # recorded holder against, MUST NOT be discarded before that release is
  # positively proven. A release failure here propagates (`set -e`) with the
  # `recovered` journal still on disk, never re-interpreted as permission to
  # roll back (see update-proxmox-0.5.sh's own EXIT-trap terminal-checkpoint
  # rule) -- the next updater invocation retries release and then clears it.
  _update_release_maintenance_fence
  _update_test_term_checkpoint after_recovery_fence_release
  _update_journal_clear
  log_warn "rollback complete -- the pre-update installation is enabled, running again, and healthy (exit ${exit_code})"
}

# _update_ct_path_state: three-valued read-only existence check of a fixed
# live or run-owned rollback path. Return 0=EXISTS, 1=ABSENT, 2=UNKNOWN.
# Both the outer pct success and the helper's explicit JSON answer are
# required. Never infer ABSENT from a failed transport/command.
_update_ct_path_state() {
  local path="$1"
  local marker_path="/opt/hubinet-ops/.hubinet-source-commit"
  local allowed="0" candidate
  for candidate in \
    "${marker_path}" "${marker_path}.rollback-${UPDATE_RUN_ID}" \
    /opt/hubinet-ops/app "/opt/hubinet-ops/app.rollback-${UPDATE_RUN_ID}" \
    /opt/hubinet-ops/.venv "/opt/hubinet-ops/.venv.rollback-${UPDATE_RUN_ID}" \
    /opt/hubinet-ops/requirements.txt "/opt/hubinet-ops/requirements.txt.rollback-${UPDATE_RUN_ID}" \
    /etc/systemd/system/hubinet-ops.service "/etc/systemd/system/hubinet-ops.service.rollback-${UPDATE_RUN_ID}"; do
    if [[ "${path}" == "${candidate}" ]]; then
      allowed="1"
      break
    fi
  done
  [[ "${allowed}" == "1" ]] || return 2

  local output status exists
  output="$(pct exec "${VMID}" -- python3 "${UPDATE_TOOL_CT_PATH}" path-state "${path}" 2>/dev/null)" \
    && status=0 || status=$?
  (( status == 0 )) && _json_bool_field_is_true "${output}" "ok" || return 2
  exists="$(_json_field_from_text "${output}" "exists")" || return 2
  case "${exists}" in
    1) return 0 ;;
    0) return 1 ;;
    *) return 2 ;;
  esac
}

_update_remove_ct_path_and_prove_absent() {
  local path="$1" kind="$2" label="$3" probe_rc
  case "${kind}" in
    file) pct exec "${VMID}" -- rm -f "${path}" >/dev/null 2>&1 || true ;;
    tree) pct exec "${VMID}" -- rm -rf "${path}" >/dev/null 2>&1 || true ;;
    *) _update_rollback_hard_stop "internal error: unsupported rollback removal kind '${kind}'" ;;
  esac
  if _update_ct_path_state "${path}"; then
    _update_rollback_hard_stop "could not remove the live ${label} inside container ${VMID}; the path still exists, so restoration was not attempted"
  else
    probe_rc=$?
  fi
  (( probe_rc == 1 )) \
    || _update_rollback_hard_stop "could not prove the live ${label} path absent inside container ${VMID}; restoration was not attempted"
}

# --- Rollback replayability (P2-A, correction pass 7) ----------------------
#
# A first rollback can legitimately restore several artifacts and then hard
# stop at a LATER terminal step (re-enable, start, or the health proof).
# That path deliberately RETAINS the active journal, so a later updater
# invocation re-enters this SAME rollback for the SAME run id. Rollback is
# therefore not a one-shot operation: every helper below must tolerate
# already-restored state without falsely diagnosing corruption, exactly as
# the app/venv/requirements/marker helpers already do by inspecting the
# bounded set of paths their artifact owns.

# _update_rollback_host_helper: host-side rollback of the PVE package-scan
# helper. Two properties, both bounded to this run's fixed paths (never a
# guessed one):
#
#   - the canonical .rollback-<UPDATE_RUN_ID> copy -- proven to exist
#     before target activation -- is no longer CONSUMED by the restore. It
#     is copied to a run-owned restore temp, and that temp is atomically
#     renamed onto the live path, so a retry after a partial rollback
#     still has the original recovery material. The canonical copy is
#     removed only by _update_cleanup_recovered_run_artifacts at terminal
#     recovery.
#   - an ABSENT canonical copy is not automatically corruption: a prior
#     rollback of this same run may already have restored and consumed it
#     (that is what the previous bare `mv` did). Same bounded state
#     inspection as _update_rollback_app/_update_rollback_venv_and_
#     requirements -- prove the live helper is actually there, and fail
#     closed only if it is not.
_update_rollback_host_helper() {
  ledger_has update-helper-activated "${VMID}" || return 0
  local rollback_path="${UPDATE_HELPER_HOST_PATH}.rollback-${UPDATE_RUN_ID}"
  local restore_tmp="${UPDATE_HELPER_HOST_PATH}.restore-tmp-${UPDATE_RUN_ID}"

  if [[ -e "${rollback_path}" ]]; then
    [[ -f "${rollback_path}" && -s "${rollback_path}" ]] \
      || _update_rollback_hard_stop "the preserved pre-update PVE host helper (${rollback_path}) is not a usable non-empty regular file -- restore ${UPDATE_HELPER_PATH} manually before retrying"
    rm -f -- "${restore_tmp}" \
      || _update_rollback_hard_stop "could not clear the run-owned PVE host helper restore temporary ${restore_tmp}"
    _host_control_install_file 0755 "${rollback_path}" "${restore_tmp}" \
      || _update_rollback_hard_stop "could not stage the preserved pre-update PVE host helper for restoration at ${restore_tmp} -- the preserved copy at ${rollback_path} is untouched"
    if ! mv "${restore_tmp}" "${UPDATE_HELPER_HOST_PATH}" 2>/dev/null; then
      rm -f -- "${restore_tmp}" 2>/dev/null || true
      _update_rollback_hard_stop "could not atomically restore the pre-update PVE host helper onto ${UPDATE_HELPER_PATH} -- the preserved copy at ${rollback_path} is untouched; restore it manually before retrying"
    fi
    _update_durability_barrier_host_or_hard_stop "${UPDATE_HELPER_HOST_PATH}" "restoring the pre-update PVE host helper"
    return 0
  fi

  # The canonical copy is gone. On a recovery retry that is exactly what a
  # previous, already-successful helper restore leaves behind -- never
  # reported as corruption on that evidence alone.
  [[ -f "${UPDATE_HELPER_HOST_PATH}" && -x "${UPDATE_HELPER_HOST_PATH}" ]] \
    || _update_rollback_hard_stop "the preserved pre-update PVE host helper (${rollback_path}) is absent and the live helper ${UPDATE_HELPER_PATH} is not an executable regular file -- restore it manually before retrying"
  # Replay of an already-restored artifact: re-establish the barrier
  # before treating it as terminally restored (section 6).
  _update_durability_barrier_host_or_hard_stop "${UPDATE_HELPER_HOST_PATH}" "replaying the already-restored PVE host helper"
}

# _update_rollback_authority (correction pass 8, P2): restoring the
# pre-update authority database is a REPLAY-SAFE, at-most-once
# destructive operation.
#
# Rollback is not one-shot -- a first rollback attempt can legitimately
# restore several artifacts and then hard stop (or be SIGKILLed) at a
# later terminal step, deliberately RETAINING the active journal so a
# later invocation re-enters this same rollback for the same run id. For
# every other artifact that is harmless: the restore is idempotent
# state-inspection over run-owned paths. The authority database is NOT:
# once the old service has been started again on the restored database it
# may legitimately have written NEW authority state (discovery results,
# scans, approvals). Blindly re-applying the original pre-update backup on
# a replay would silently destroy exactly those post-rollback writes.
#
# So the durable journal now carries one more state fact,
# `update-authority-restored`, recorded only after the restored database
# has been positively inspected, and BEFORE the old service can be started
# (the start happens later in update_rollback_on_failure, and every
# journal record is an atomic durable replacement -- see
# update_journal_checkpoint).
#
#   marker ABSENT  -> first authority rollback. Prove removal of the
#                     target database, copy the validated backup back,
#                     restore owner/mode, positively inspect the result,
#                     then journal the marker.
#   marker PRESENT -> the restore already completed durably. NEVER remove
#                     the live database and NEVER recopy the backup.
#                     Instead re-prove that the live database's durable
#                     IDENTITY LINEAGE is still the restored OLD authority
#                     (same schema marker, same schema version, same
#                     backend_instance_id as the retained backup). Its
#                     CONTENT may legitimately have advanced, so no byte
#                     or hash comparison against the backup is ever made.
#
# Anything else -- a missing, corrupt, unreadable, or differently-
# identified live database -- is a HARD STOP with the journal and the
# validated backup retained. Manual diagnosis is strictly safer than
# automatically overwriting a database whose provenance is uncertain and
# which may hold valuable post-rollback state.
_update_rollback_authority() {
  ledger_has update-authority-reset-attempted "${VMID}" || return 0
  [[ -n "${UPDATE_DB_BACKUP_PATH}" ]] \
    || _update_rollback_hard_stop "an authority reset was attempted for run ${UPDATE_RUN_ID}, but no validated pre-update backup path is recorded -- refusing to touch the live authority database"

  if ledger_has update-authority-restored "${VMID}"; then
    _update_prove_restored_authority_lineage
    log_warn "the pre-update authority database was already restored durably by an earlier rollback attempt of run ${UPDATE_RUN_ID}, and still carries that same authority identity -- preserving every write the restored old service has made since, and NOT re-applying ${UPDATE_DB_BACKUP_PATH}"
    return 0
  fi

  # Fail-closed rollback (P1-B): removal of the NEW/target database must
  # be PROVEN successful (cmd_remove's own ok:true, which already means
  # every one of db/wal/shm was independently re-verified absent -- see
  # hubinet-ops-authority-tool.py's cmd_remove) before the validated OLD
  # backup is ever copied into place. Never warn-then-continue: copying
  # a trusted backup over an uncertain live/sidecar state could silently
  # leave stale WAL/SHM content paired with a just-restored main file.
  # The backup itself is never touched by this block, so it remains
  # available for manual recovery either way.
  # Guarded exactly like _update_perform_authority_reset's own forward
  # `remove` call -- a bare `var="$(cmd)"` with no `&&`/`||` is NOT
  # safe under this file's `set -Eeuo pipefail`: cmd's own non-zero exit
  # would abort the WHOLE script right here (silently, before the
  # fail-closed hard-stop below ever runs) rather than being handled by
  # the very check this line exists to reach.
  local remove_output remove_status
  remove_output="$(pct exec "${VMID}" -- python3 "${UPDATE_TOOL_CT_PATH}" remove /var/lib/hubinet-ops/authority.db 2>/dev/null)" \
    && remove_status=0 || remove_status=$?
  if (( remove_status != 0 )) || ! _json_bool_field_is_true "${remove_output}" "ok"; then
    _update_rollback_hard_stop "could not prove removal of the newly-created target authority database during rollback (tool output: ${remove_output:-none}) -- refusing to copy the pre-update backup over an uncertain live database state. The validated backup at ${UPDATE_DB_BACKUP_PATH} is untouched; resolve the removal failure manually, then restore it by hand"
  fi
  if ! pct exec "${VMID}" -- cp "${UPDATE_DB_BACKUP_PATH}" /var/lib/hubinet-ops/authority.db >/dev/null 2>&1; then
    _update_rollback_hard_stop "could not restore the pre-update authority database backup (${UPDATE_DB_BACKUP_PATH}) to /var/lib/hubinet-ops/authority.db -- the backup itself is preserved and untouched; restore it manually before restarting the service"
  fi
  pct exec "${VMID}" -- chown hubinetops:hubinetops /var/lib/hubinet-ops/authority.db >/dev/null 2>&1 \
    || _update_rollback_hard_stop "could not restore authority database ownership with chown hubinetops:hubinetops inside container ${VMID}"
  pct exec "${VMID}" -- chmod 0640 /var/lib/hubinet-ops/authority.db >/dev/null 2>&1 \
    || _update_rollback_hard_stop "could not restore authority database mode with chmod 0640 inside container ${VMID}"

  # Durability barrier (correction pass 9, P1): the restored copy must be
  # durable BEFORE update-authority-restored is journaled -- that marker
  # must never describe an old DB that only existed in cache.
  _update_durability_barrier_ct_or_hard_stop /var/lib/hubinet-ops/authority.db "restoring the pre-update authority database"

  # Positively inspect what was actually restored before claiming it, and
  # make that fact durable BEFORE the old service is allowed to start and
  # write to it.
  _update_prove_restored_authority_lineage
  update_journal_record update-authority-restored "${VMID}"
}

# _update_prove_restored_authority_lineage: the live authority database
# must be a coherent, recognizable authority whose durable identity is the
# OLD (pre-update) one. The expected identity is read from the retained,
# already-validated backup itself rather than from planning-phase
# variables, because a startup-recovery invocation deliberately never runs
# Phase U2 and therefore has no UPDATE_CURRENT_* facts at all.
#
# Only IDENTITY is compared -- schema marker, schema version, and
# backend_instance_id. Content is expected to differ once the restored old
# service has run: this function must never compare whole-database bytes
# or hashes, or a legitimate post-rollback write would look like
# corruption.
_update_prove_restored_authority_lineage() {
  local backup_output backup_status live_output live_status
  local field expected actual

  backup_output="$(pct exec "${VMID}" -- python3 "${UPDATE_TOOL_CT_PATH}" inspect "${UPDATE_DB_BACKUP_PATH}" 2>/dev/null)" \
    && backup_status=0 || backup_status=$?
  if (( backup_status != 0 )) || ! _json_bool_field_is_true "${backup_output}" "ok"; then
    _update_rollback_hard_stop "could not read the pre-update authority identity from the retained backup ${UPDATE_DB_BACKUP_PATH} (tool output: ${backup_output:-none}) -- the live authority database was not touched"
  fi

  live_output="$(pct exec "${VMID}" -- python3 "${UPDATE_TOOL_CT_PATH}" inspect /var/lib/hubinet-ops/authority.db 2>/dev/null)" \
    && live_status=0 || live_status=$?
  if (( live_status != 0 )) || ! _json_bool_field_is_true "${live_output}" "ok"; then
    _update_rollback_hard_stop "the live authority database at /var/lib/hubinet-ops/authority.db is missing, corrupt, or unrecognized (tool output: ${live_output:-none}) -- refusing to overwrite an uncertain database automatically. The validated pre-update backup at ${UPDATE_DB_BACKUP_PATH} is retained untouched; diagnose and restore it by hand"
  fi

  for field in marker schema_version backend_instance_id; do
    expected="$(_json_field_from_text "${backup_output}" "${field}")" || expected=""
    actual="$(_json_field_from_text "${live_output}" "${field}")" || actual=""
    [[ -n "${expected}" && "${expected}" == "${actual}" ]] \
      || _update_rollback_hard_stop "the live authority database's ${field} (${actual:-unknown}) is not the pre-update authority's (${expected:-unknown}) -- this is not the database this rollback restored, so it is NOT overwritten automatically. The validated pre-update backup at ${UPDATE_DB_BACKUP_PATH} is retained untouched; diagnose manually"
  done
}

# _update_rollback_unit: state-inspection restore, not marker-implies-
# complete. If the systemd unit activation was ever attempted, the only
# question that matters is whether this run's fixed rollback copy
# (.service.rollback-<UPDATE_RUN_ID>) exists: if it does, the live unit
# might currently hold either the pre-update or the newly-activated
# content, and unconditionally restoring the rollback copy over it is
# correct either way (the destructive `mv` that consumes staged-> live is
# atomic, so live never holds a partial mix); if it does not exist, either
# the preserving copy never needed restoring or a PRIOR rollback of this
# same run already restored it and consumed the artifact -- so there is
# nothing left to restore, and this must not hard stop merely because it
# is a retry (P2-A, correction pass 7: the implementation now matches what
# this contract already said). UNKNOWN still fails closed, and a real
# restore still treats daemon-reload as load-bearing.
_update_rollback_unit() {
  ledger_has update-unit-activation-attempted "${VMID}" || return 0
  local live_path="/etc/systemd/system/hubinet-ops.service"
  local rollback_path="${live_path}.rollback-${UPDATE_RUN_ID}"
  local path_state_rc
  if _update_ct_path_state "${rollback_path}"; then
    _update_remove_ct_path_and_prove_absent "${live_path}" file "systemd unit"
    if ! pct exec "${VMID}" -- mv "${rollback_path}" "${live_path}" >/dev/null 2>&1; then
      _update_rollback_hard_stop "could not restore the pre-update systemd unit inside container ${VMID}"
    fi
    # Rollback restoration must also become durable (correction pass 9,
    # P1, section 6), before daemon-reload lets systemd act on it.
    _update_durability_barrier_ct_or_hard_stop "${live_path}" "restoring the pre-update systemd unit"
    pct exec "${VMID}" -- systemctl daemon-reload >/dev/null 2>&1 \
      || _update_rollback_hard_stop "systemctl daemon-reload failed after restoring the pre-update unit inside container ${VMID}; refusing to restart under an unproven loaded unit definition"
    return 0
  else
    path_state_rc=$?
  fi
  (( path_state_rc == 1 )) \
    || _update_rollback_hard_stop "could not determine whether the pre-update systemd unit rollback artifact exists inside container ${VMID}"
  # ABSENT. Nothing to restore -- but the live unit must positively exist,
  # or this installation cannot be started again at all.
  #
  # (Correction pass 8, P2:) reaching this branch does NOT mean the
  # systemd MANAGER is already running the old unit definition. The
  # reachable witness is a prior rollback of this same run that restored
  # the old unit file onto the live path (consuming the rollback artifact)
  # and was then SIGKILLed BEFORE its daemon-reload -- systemd then still
  # holds the TARGET unit definition in memory even though the file on
  # disk is the old one. "The file is already back" is a fact about the
  # filesystem and is never proof about the manager, so whenever unit
  # activation was ever attempted, a daemon-reload is unconditionally
  # required before the restored old service may be started.
  if _update_ct_path_state "${live_path}"; then
    # A replay finding this artifact already restored still must
    # (re-)establish its durability barrier before treating it as
    # terminally restored -- "it exists" is never equivalent to "it
    # survived the next power loss" (correction pass 9, P1, section 6).
    _update_durability_barrier_ct_or_hard_stop "${live_path}" "replaying the already-restored systemd unit"
    pct exec "${VMID}" -- systemctl daemon-reload >/dev/null 2>&1 \
      || _update_rollback_hard_stop "systemctl daemon-reload failed inside container ${VMID} while replaying the unit rollback of run ${UPDATE_RUN_ID}; the pre-update unit file is in place but systemd may still hold the target definition, so restarting under an unproven loaded unit definition is refused"
    return 0
  else
    path_state_rc=$?
  fi
  _update_rollback_hard_stop "the pre-update systemd unit rollback artifact is absent and the live unit is $( (( path_state_rc == 1 )) && printf 'absent' || printf 'unknown' ) inside container ${VMID}"
}

# _update_rollback_venv_and_requirements: same state-inspection discipline
# as _update_rollback_unit, applied independently to the venv and to
# requirements.txt (the two moves are sequential but neither implies the
# other reached its own rollback-copy step; see the intermediate-state
# enumeration in this file's header comment).
_update_rollback_venv_and_requirements() {
  ledger_has update-venv-activation-attempted "${VMID}" || return 0

  local rollback_venv="/opt/hubinet-ops/.venv.rollback-${UPDATE_RUN_ID}"
  local path_state_rc
  if _update_ct_path_state "${rollback_venv}"; then
    _update_remove_ct_path_and_prove_absent /opt/hubinet-ops/.venv tree "virtualenv"
    if ! pct exec "${VMID}" -- mv "${rollback_venv}" /opt/hubinet-ops/.venv >/dev/null 2>&1; then
      _update_rollback_hard_stop "could not restore the pre-update virtualenv inside container ${VMID}"
    fi
    _update_durability_barrier_ct_or_hard_stop /opt/hubinet-ops/.venv "restoring the pre-update virtualenv"
  else
    path_state_rc=$?
    (( path_state_rc == 1 )) \
      || _update_rollback_hard_stop "could not determine whether the pre-update virtualenv rollback artifact exists inside container ${VMID}"
    if _update_ct_path_state /opt/hubinet-ops/.venv; then
      # Replay of an already-restored artifact: re-establish the barrier
      # before treating it as terminally restored (section 6).
      _update_durability_barrier_ct_or_hard_stop /opt/hubinet-ops/.venv "replaying the already-restored virtualenv"
    else
      path_state_rc=$?
      _update_rollback_hard_stop "the virtualenv rollback artifact is absent and the live pre-update virtualenv is $( (( path_state_rc == 1 )) && printf 'absent' || printf 'unknown' )"
    fi
  fi

  local rollback_requirements="/opt/hubinet-ops/requirements.txt.rollback-${UPDATE_RUN_ID}"
  if _update_ct_path_state "${rollback_requirements}"; then
    _update_remove_ct_path_and_prove_absent /opt/hubinet-ops/requirements.txt file "requirements.txt"
    if ! pct exec "${VMID}" -- mv "${rollback_requirements}" /opt/hubinet-ops/requirements.txt >/dev/null 2>&1; then
      _update_rollback_hard_stop "could not restore the pre-update requirements.txt inside container ${VMID}"
    fi
    _update_durability_barrier_ct_or_hard_stop /opt/hubinet-ops/requirements.txt "restoring the pre-update requirements.txt"
  else
    path_state_rc=$?
    (( path_state_rc == 1 )) \
      || _update_rollback_hard_stop "could not determine whether the pre-update requirements.txt rollback artifact exists inside container ${VMID}"
    if _update_ct_path_state /opt/hubinet-ops/requirements.txt; then
      _update_durability_barrier_ct_or_hard_stop /opt/hubinet-ops/requirements.txt "replaying the already-restored requirements.txt"
    else
      path_state_rc=$?
      _update_rollback_hard_stop "the requirements.txt rollback artifact is absent and the live pre-update file is $( (( path_state_rc == 1 )) && printf 'absent' || printf 'unknown' )"
    fi
  fi
}

# _update_rollback_app: same state-inspection discipline as
# _update_rollback_unit, applied to the application payload directory.
_update_rollback_app() {
  ledger_has update-app-activation-attempted "${VMID}" || return 0
  local rollback_path="/opt/hubinet-ops/app.rollback-${UPDATE_RUN_ID}"
  local path_state_rc
  if _update_ct_path_state "${rollback_path}"; then
    :
  else
    path_state_rc=$?
    if (( path_state_rc == 2 )); then
      _update_rollback_hard_stop "could not determine whether the pre-update application rollback artifact exists inside container ${VMID}"
    fi
    if _update_ct_path_state /opt/hubinet-ops/app; then
      # Replay of an already-restored artifact: re-establish the barrier
      # before treating it as terminally restored (section 6).
      _update_durability_barrier_ct_or_hard_stop /opt/hubinet-ops/app "replaying the already-restored application payload"
      return 0
    fi
    path_state_rc=$?
    _update_rollback_hard_stop "the application rollback artifact is absent and the live pre-update application is $( (( path_state_rc == 1 )) && printf 'absent' || printf 'unknown' )"
  fi
  _update_remove_ct_path_and_prove_absent /opt/hubinet-ops/app tree "application payload"
  if ! pct exec "${VMID}" -- mv "${rollback_path}" /opt/hubinet-ops/app >/dev/null 2>&1; then
    _update_rollback_hard_stop "could not restore the pre-update application payload inside container ${VMID}"
  fi
  _update_durability_barrier_ct_or_hard_stop /opt/hubinet-ops/app "restoring the pre-update application payload"
}

_update_rollback_hard_stop() {
  log_warn "ROLLBACK COULD NOT BE COMPLETED: $*"
  log_warn "Preserving every rollback/backup artifact and active journal ${UPDATE_JOURNAL_PATH:-unknown} for manual recovery. Context: VMID=${VMID:-unknown}, run=${UPDATE_RUN_ID:-unknown}, authority_backup=${UPDATE_DB_BACKUP_PATH:-none}. Do not begin a new update or assume the service is safe until this is resolved by hand."
  exit 1
}
