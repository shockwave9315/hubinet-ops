# Security model

The model is deny-by-default at three independent boundaries:

- validated resource configuration controls monitoring and operator capabilities;
- the adapter controls which fixed action exists for a resource type;
- the PVE wrapper controls observation, managed-executor, and lifecycle VMID sets plus the VMID→type mapping.

`observation-vmids` contains 100–110, `managed-vmids` contains LXC 101–109 for safe inspect/scan/verify, `maintenance-vmids` contains exactly 106 for mutating APT/snapshot/rollback actions, and `lifecycle-vmids` contains exactly 106. Presence in observation does not grant managed execution, maintenance, or lifecycle. QEMU APT/snapshot/rollback, observation-only LXC maintenance, VM100 lifecycle, and all CT110 managed/lifecycle actions fail closed.

The wrapper parses at most `action vmid [snapshot]`, validates regexes, uses fixed argv for `pct`, `qm`, and `pvesh`, and never calls `eval`. There is no arbitrary shell, console, terminal, MQTT command topic, or Home Assistant command field.

CT110 journal output is limited, sanitized by the same secret filters as errors/events, and represented as a bounded list. Tokens, MQTT passwords, webhook identifiers, SSH keys, authorization headers, and full commands are not published or logged.

Updates require manual approval. Automatic rollback additionally requires an existing snapshot and the per-resource policy; CT106 manual rollback remains disabled in the production inventory.
