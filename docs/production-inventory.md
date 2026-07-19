# Production inventory

| VMID | Display name | Type / adapter | Criticality | Policy |
|---|---|---|---|---|
| 100 | Home Assistant | QEMU / HAOS | critical | observation-only |
| 101 | Cloudflared | LXC / APT | high | observation-only |
| 102 | MariaDB | LXC / APT | critical | observation-only |
| 103 | MQTT | LXC / APT | critical | observation-only |
| 104 | Nextcloud | LXC / APT | critical | observation-only |
| 105 | AdGuard Home | LXC / APT | critical | observation-only; no guessed service |
| 106 | WeatherHub | LXC / APT + Docker | low | operator scan/update lifecycle; rollback denied |
| 107 | Immich | LXC / APT | high | observation-only; non-Docker deployment |
| 108 | DDNS | LXC / APT | medium | observation-only; no guessed service |
| 109 | Pompa | LXC / APT + Docker | critical | observation-only; Docker names not guessed |
| 110 | Hubinet Ops | LXC / agent_self | high | observation-only/self-health |

Exact IP addresses, services, Docker requirements, monitoring flags, and dashboard paths are versioned in `config/config.example.yaml`. CT106 is the only permitted live lifecycle/update test target.
