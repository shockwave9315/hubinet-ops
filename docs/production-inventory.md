# Production inventory

| VMID | Display name | Type / adapter | Criticality | Policy |
|---|---|---|---|---|
| 100 | Home Assistant | QEMU / HAOS | critical | observation-only |
| 101 | Cloudflared | LXC / APT | high | full APT/lifecycle/snapshot control |
| 102 | MariaDB | LXC / APT | critical | full APT/lifecycle/snapshot control |
| 103 | MQTT | LXC / APT | critical | full APT/lifecycle/snapshot control |
| 104 | Nextcloud | LXC / APT | critical | full APT/lifecycle/snapshot control |
| 105 | AdGuard Home | LXC / APT | critical | full control; insufficient health contract is explicit |
| 106 | WeatherHub | LXC / APT + Docker | low | full APT/lifecycle/snapshot control with automatic rollback policy |
| 107 | Immich | LXC / APT | high | full APT/lifecycle/snapshot control; non-Docker deployment |
| 108 | DDNS | LXC / APT | medium | full control; insufficient health contract is explicit |
| 109 | Pompa | LXC / APT + Docker | critical | full control; Docker names are not guessed |
| 110 | Hubinet Ops | LXC / agent_self | high | backend-gated hostd lifecycle/snapshot, dedicated self-update, scoped offline recovery |

Exact IP addresses, services, Docker requirements, monitoring flags, and dashboard paths are versioned in `config/config.example.yaml`. The repository never treats any production resource as a live test target; rollout validation is read-only.
# 0.4.3 CT110 policy

CT110 remains VMID 110 with `agent_self`, but its `monitoring.update_scan` and typed `scan` capability are enabled for PVE-supervised Debian package discovery. This does not authorize the backend to run APT directly. Application release discovery is a separate `self_update` capability fixed to the configured Hubinet Ops repository.
