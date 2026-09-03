"""The exclusive product-update maintenance fence.

A Hubinet PRODUCT update replaces the backend and its privileged helpers. A
WORKLOAD update mutates packages inside a guest through those helpers. The two
must never overlap: replacing a helper set underneath an in-flight snapshot,
package mutation, or rollback can pair a new backend with a half-replaced host
boundary for an operation already past its write-ahead point.

Checking "is a job active?" once, early, does not achieve that. Between any
such check and the product updater's first mutation, an authenticated operator
can legitimately start an update -- and a second check placed later only moves
the window, it does not close it, because the API stays live between the check
and the service stop. The invariant needs mutual exclusion, not a better-timed
poll.

## The primitive, and why it is this one

The fence is one file beside the authority database, and its whole purpose is
to be a durable fact that survives the backend process restart the product
update performs. It is NOT the synchronization.

The synchronization is the authority store's single ``BEGIN IMMEDIATE`` writer
lock, which both sides already take:

```text
ACQUIRE (backend, on the updater's behalf)   ISSUE (operator start_update)
  BEGIN IMMEDIATE  ---------------------------  BEGIN IMMEDIATE
    is any package-update job ACTIVE?             is the fence present?
      yes -> refuse, ROLLBACK                       yes -> refuse
    write + fsync the fence file                  insert the job row
  COMMIT                                        COMMIT
```

SQLite permits exactly one writer, so these two critical sections are strictly
ordered. Whichever enters first wins and the other observes its result:

- acquire first: the fence file is durable *before* the COMMIT that releases
  the lock, so the issuing transaction cannot start until the fence exists,
  and it refuses;
- issue first: the job row is durable before acquisition can begin, and
  acquisition refuses.

There is no interleaving in which both succeed, and no check-then-act gap: the
existence check is a read performed *inside* the lock, never the lock itself.

A crash between the file write and the COMMIT leaves the fence present with no
committed claim. That is the safe direction -- workload starts refuse, and the
product updater never entered its mutation window because acquisition failed.

## Release

Release deliberately needs none of that. Removing the fence only ever widens
what is permitted, so it cannot race anything into existence, and it is done
by the product updater directly on the filesystem rather than through this
backend. That matters for one real case: a failed activation update rolls back
to a pre-activation backend that has no maintenance-fence route at all, and a
release that depended on the backend answering would strand the fence forever.

The updater releases only at a terminal point -- a proven successful product
update, or a proven complete rollback/recovery -- so a crash anywhere before
that leaves the fence in place, which is exactly what keeps workload issuance
refused while the updater still owns rollback-capable state.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re

from .models import AuthorityInvariantError

#: The fence lives beside the authority database, so it is derived from the
#: installation the backend was actually pointed at. There is no configuration
#: knob for it, and nothing about it names a VMID or a resource.
PRODUCT_UPDATE_FENCE_FILENAME = "product-update-maintenance.fence"

#: A bounded opaque label identifying the product-update run that holds the
#: fence. The updater passes its own run id; nothing here interprets it beyond
#: comparing it for equality, and the charset keeps it a safe filename-free
#: scalar in every message it appears in.
_HOLDER_RE = re.compile(r"^[0-9A-Za-z-]{1,64}$")

MAX_FENCE_BYTES = 4096


class ProductUpdateFenceError(AuthorityInvariantError):
    """The maintenance fence could not be read or written truthfully."""


@dataclass(frozen=True, slots=True)
class ProductUpdateMaintenanceFence:
    """One held fence: who holds it, and since when."""

    holder: str
    acquired_at: str


def product_update_fence_path(authority_db_path: Path) -> Path:
    return Path(authority_db_path).parent / PRODUCT_UPDATE_FENCE_FILENAME


def require_fence_holder(holder: str) -> str:
    if not isinstance(holder, str) or not _HOLDER_RE.fullmatch(holder):
        raise ValueError("product update fence holder is malformed")
    return holder


def read_product_update_fence(
    authority_db_path: Path,
) -> ProductUpdateMaintenanceFence | None:
    """Return the currently held fence, or ``None``.

    Fails CLOSED on anything it cannot read truthfully. A fence file that
    exists but is unreadable, over-long, or malformed is treated as a held
    fence rather than as an absent one: the alternative is letting a corrupt
    byte on disk re-open workload issuance during a product update.
    """

    path = product_update_fence_path(authority_db_path)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ProductUpdateFenceError(
            f"product update maintenance fence is unreadable: {exc}"
        ) from exc
    if len(raw) > MAX_FENCE_BYTES:
        raise ProductUpdateFenceError(
            "product update maintenance fence exceeds its structural bound"
        )
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ProductUpdateFenceError(
            "product update maintenance fence is malformed"
        ) from exc
    if not isinstance(payload, dict):
        raise ProductUpdateFenceError(
            "product update maintenance fence is not an object"
        )
    holder = payload.get("holder")
    acquired_at = payload.get("acquired_at")
    if not isinstance(holder, str) or not isinstance(acquired_at, str):
        raise ProductUpdateFenceError(
            "product update maintenance fence does not name a holder"
        )
    try:
        require_fence_holder(holder)
    except ValueError as exc:
        raise ProductUpdateFenceError(
            "product update maintenance fence names a malformed holder"
        ) from exc
    return ProductUpdateMaintenanceFence(holder=holder, acquired_at=acquired_at)


def write_product_update_fence(
    authority_db_path: Path, fence: ProductUpdateMaintenanceFence
) -> None:
    """Durably create the fence, atomically, before the caller may commit.

    fsynced and renamed into place, then the directory is fsynced too: the
    caller writes this INSIDE its ``BEGIN IMMEDIATE`` critical section, so by
    the time that commit releases the writer lock the fence is already on
    disk and no issuing transaction can have missed it.
    """

    path = product_update_fence_path(authority_db_path)
    temporary = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    payload = json.dumps(
        {"holder": fence.holder, "acquired_at": fence.acquired_at},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    try:
        with open(temporary, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError as exc:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise ProductUpdateFenceError(
            f"product update maintenance fence could not be written: {exc}"
        ) from exc
