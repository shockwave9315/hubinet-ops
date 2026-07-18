from __future__ import annotations

from pathlib import Path

path = Path("app/service.py")
text = path.read_text(encoding="utf-8")

old = '''        state = self.get_state(vmid)
        prior_operation = state["operation_status"]
        state.update(
            {
                "update_status": "scanning",
                "job_stage": "scanning",
            }
        )
'''
new = '''        state = self.get_state(vmid)
        prior_operation = state["operation_status"]
        prior_stage = state["job_stage"]
        state.update(
            {
                "update_status": "scanning",
                "job_stage": "scanning",
            }
        )
'''
if text.count(old) != 1:
    raise SystemExit("scan start target not unique")
text = text.replace(old, new)

old = '''        except ExecutorError as exc:
            state.update(
                {
                    "update_status": "unknown",
                    "job_stage": "idle" if prior_operation == "idle" else state["job_stage"],
                    "last_error": sanitize_text(exc, limit=2000),
                    "last_scan": utc_now(),
                    "operation_status": prior_operation,
                }
            )
'''
new = '''        except ExecutorError as exc:
            state = self.get_state(vmid)
            state.update(
                {
                    "update_status": "unknown",
                    "job_stage": prior_stage,
                    "last_error": sanitize_text(exc, limit=2000),
                    "last_scan": utc_now(),
                    "operation_status": prior_operation,
                }
            )
'''
if text.count(old) != 1:
    raise SystemExit("scan failure target not unique")
text = text.replace(old, new)

old = '''        count = max(0, int(data.get("pending_count", 0) or 0))
        data = dict(data)
        data["packages"] = list(data.get("packages") or [])[:200]
        state.update(
            {
                "updates": data,
                "pending_updates": count,
                "update_status": "update_available" if count else "up_to_date",
                "job_stage": "idle" if prior_operation == "idle" else state["job_stage"],
                "last_scan": utc_now(),
                "operation_status": prior_operation,
            }
        )
'''
new = '''        count = max(0, int(data.get("pending_count", 0) or 0))
        data = dict(data)
        data["packages"] = list(data.get("packages") or [])[:200]
        state = self.get_state(vmid)
        state.update(
            {
                "updates": data,
                "pending_updates": count,
                "update_status": "update_available" if count else "up_to_date",
                "job_stage": prior_stage,
                "last_scan": utc_now(),
                "operation_status": prior_operation,
            }
        )
'''
if text.count(old) != 1:
    raise SystemExit("scan success target not unique")
text = text.replace(old, new)

old = '''        self._execute(
            "repair",
            int(job["vmid"]),
            int(policy.repair_timeout_seconds),
            emit,
        )
'''
new = '''        self._execute(
            "repair",
            int(job["vmid"]),
            max(900, int(policy.repair_timeout_seconds)),
            emit,
        )
'''
if text.count(old) != 1:
    raise SystemExit("repair timeout target not unique")
text = text.replace(old, new)

old = '''def _fingerprint(data: dict[str, Any]) -> str:
    blob = json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()
'''
new = '''def _fingerprint(data: dict[str, Any]) -> str:
    # Match the managed executor contract: only the ordered package plan is
    # fingerprinted. Volatile fields such as scanned_at must not invalidate approval.
    packages = list(data.get("packages") or [])
    blob = json.dumps(packages, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()
'''
if text.count(old) != 1:
    raise SystemExit("fingerprint target not unique")
text = text.replace(old, new)

old = '''    packages = {
        str(item.get("name", ""))
        for item in data.get("packages", [])
        if isinstance(item, dict)
    }
'''
new = '''    packages = {
        str(item.get("name", "")).split(":", 1)[0]
        for item in data.get("packages", [])
        if isinstance(item, dict)
    }
'''
if text.count(old) != 1:
    raise SystemExit("risk package target not unique")
text = text.replace(old, new)

path.write_text(text, encoding="utf-8")
