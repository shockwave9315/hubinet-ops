# ADR 0002: read-only discovery Proxmox i reconciliation

Status: **PROPOSED**

Data: 2026-08-08

## Decyzja

Przyszły discovery provider będzie transport-neutralnym, ściśle read-only
adapterem backendu:

```text
Proxmox API
  ↓
Hubinet Ops discovery provider
  ↓
normalized inventory snapshot
  ↓
persistent inventory reconciliation
  ↓
Hubinet Ops API
  ↓
Home Assistant Coordinator
```

Home Assistant nie zna endpointów ani credentials Proxmox. Provider nie zawiera
metod mutacyjnych. Discovery dostarcza facts i evidence; nie przyznaje policy,
maintenance ani destructive capabilities.

## Source i endpointy

Jeden `inventory_source_id` reprezentuje jeden cluster lub standalone
environment. Source może mieć kilka operatorowo skonfigurowanych HTTPS
endpointów node'ów w kolejności preferencji. Proxmox `pveproxy` udostępnia API
na TCP 8006 i automatycznie forwarduje żądania do innych node'ów, więc jeden
osiągalny node może obsłużyć cluster-wide discovery.

Failover endpointu jest fail-closed:

1. endpoint musi należeć do jawnie skonfigurowanego source;
2. TLS jest domyślnie weryfikowany przez zaufane CA lub jawnie przypięty
   fingerprint; `verify=false` nie jest rekomendowanym production mode;
3. odpowiedzi z endpointów nie są scalane, dopóki backend nie potwierdzi, że
   odnoszą się do tego samego source;
4. rozbieżność cluster facts, node membership albo continuity evidence kończy
   run jako partial/unavailable, nie jako pusty snapshot;
5. podobna nazwa klastra lub wspólne VMID nie są dowodem wspólnego source.

`/cluster/resources?type=vm` jest preferowanym cluster-wide baseline dla QEMU i
LXC. Source pokazuje, że endpoint filtruje guests przez `VM.Audit`. Node facts
pochodzą z cluster resources oraz, gdy potrzebne, read-only node endpoints.
Config/facts per guest można pobierać dopiero po baseline i grupować; błąd
jednego subrequestu nie zmienia kompletnego baseline w dowód usunięcia.

Zachowanie `/cluster/resources` na standalone node powinno zostać potwierdzone
contract testem względem wspieranych wersji PVE. Oficjalny source używa wspólnej
listy VM/node, ale jawna gwarancja dokumentacyjna dla wszystkich standalone
wariantów jest **UNKNOWN**. Provider może mieć read-only fallback enumerujący
lokalny node, lecz fallback nie może zmieniać semantyki completeness.

## Biblioteka transportowa

### `proxmoxer`

Zalety: dojrzałe mapowanie REST/authentication, używane przez istniejące
integracje i ogranicza własną obsługę szczegółów API. Wady: dodatkowa zależność,
często synchroniczny model wywołań oraz szeroki dynamiczny surface, w którym
łatwiej przypadkowo udostępnić metody mutacyjne.

### Własny wąski HTTP adapter

Zalety: jawny allowlist metod `GET`, prosty async I/O, kontrola timeoutów, TLS,
redakcji i klasyfikacji błędów. Wady: Hubinet Ops musi poprawnie implementować
PVE token auth, kodowanie ścieżek, JSON envelope, retry i wersjonowanie API.

Decyzja projektowa: publiczny provider protocol jest niezależny od biblioteki,
a surface zawiera wyłącznie typowane odczyty potrzebne do snapshotu. Wstępnie
preferujemy wąski async HTTP adapter ze statycznym allowlistem `GET`; wybór
konkretnej biblioteki wymaga osobnego implementation review i testów kontraktu.
Nawet jeśli zostanie wybrany `proxmoxer`, musi pozostać prywatnym transportem za
tym protokołem i nie może być przekazany HA ani warstwie policy.

## Authentication i minimalne privileges

Provider korzysta w przyszłości z API tokenu z privilege separation i expiry.
Proxmox dokumentuje, że effective permissions takiego tokenu są przecięciem
uprawnień usera i tokenu. Ta faza nie tworzy credentiali ani provisioning.

Minimalny projektowany custom role:

- `VM.Audit` na `/vms` z propagation — widoczność QEMU/LXC i ich konfiguracji;
- `Sys.Audit` na `/nodes` z propagation — node status/config;
- `Sys.Audit` na `/` tylko jeśli wymagane są `/cluster/status` lub pełna
  cluster/task evidence; jest to szersze niż sam inventory i musi być
  uzasadnione kontraktem implementacji;
- dodatkowe `Pool.Audit`, `Datastore.Audit`, `SDN.Audit` nie są wymagane dla
  minimalnego workload inventory, jeśli tych facts nie pobieramy.

Wbudowany `PVEAuditor` jest read-only i prosty operacyjnie, lecz jego zakres jest
szerszy niż minimalne `VM.Audit` + `Sys.Audit`. Nie przyjmujemy go automatycznie.
Przed implementacją powstanie dokładna macierz endpoint → ACL path → privilege,
sprawdzona negatywnymi testami. Żadna rola discovery nie może zawierać
`VM.Allocate`, `VM.PowerMgmt`, `VM.Backup`, `VM.Clone`, `Sys.PowerMgmt` ani
pozostałych uprawnień mutacyjnych.

Token nigdy nie jest logowany ani zwracany przez Hubinet Ops API/diagnostics.
Authorization header powstaje wyłącznie w adapterze transportowym.

## Normalized discovery snapshot

Snapshot jest value object niezależnym od SDK i powinien zawierać:

```text
DiscoverySnapshot
  run_id
  inventory_source_id
  observed_at
  source_facts
  source_availability
  completeness
  covered_nodes
  failed_scopes
  event_cursor_before / event_cursor_after (optional evidence)
  nodes[]
    external_node_name
    runtime/status facts
    observed_at
    read_result
  resources[]
    locator {inventory_source_id, resource_type, vmid}
    current_node_name
    runtime presence/status
    source facts
    observed config metadata
    observed_at
    per-resource read_result
    continuity evidence[]
```

Snapshot nie zawiera user policy, enrollment, approval, maintenance permission
ani effective destructive capability. `resource_id`, `node_id`, generation i
continuity decision są wynikiem persistent reconciliation, nie surowym faktem
Proxmox. Provider może przekazać candidate evidence (`vmgenid`, `digest`,
`meta.ctime`, task records), wyraźnie opisane typem i provenance.

Zagnieżdżone dane snapshotu muszą być rzeczywiście immutable albo deep-copied
przed transaction boundary. Shallow read-only wrapper nie wystarcza.

## Completeness model

Każdy run ma jedną z klasyfikacji:

- `complete` — autorytatywny source-wide baseline zakończył się sukcesem, a
  wszystkie zakresy wymagane do oceny presence są pokryte;
- `partial` — source odpowiedział, lecz brakuje node'a, strony, zakresu albo
  wystąpił per-resource read error;
- `source_unavailable` — nie uzyskano wiarygodnego baseline;
- `invalid` — odpowiedź narusza schema, source binding lub monotonicity.

Kompletność baseline i kompletność szczegółowych facts są osobne. Pełna lista
locatorów z błędem config read pozwala zachować `present`, ale ustawia
`temporarily_unavailable` dla szczegółów. Częściowa lista locatorów nigdy nie
jest traktowana jak dowód nieobecności.

Run zawiera `covered_nodes` i `failed_scopes`, aby brak node'a lub ACL filtering
nie wyglądał jak empty inventory. Odpowiedź widoczna tylko częściowo z powodu
ACL jest configuration error, nie kompletnym snapshotem source.

## Reconciliation state machine

Presence states:

- `present` — locator występuje w kompletnym, bieżącym baseline; current node
  musi występować w tym samym normalized snapshot;
- `temporarily_unavailable` — locator jest obecny, lecz wymagane status/config
  facts nie zostały odczytane;
- `node_unavailable` — source odpowiada, ale przypisany node jest niedostępny;
  zachowujemy last-known node i resource;
- `missing` — locator nie występuje w udanym, kompletnym baseline, ale brak
  pozytywnego dowodu trwałego removal/replacement;
- `confirmed_removed` — istnieje pozytywny removal proof i autorytatywna
  nieobecność została zatwierdzona w reconciliation.

Dozwolone przejścia (skrót):

```text
present → temporarily_unavailable → present
present → node_unavailable        → present
present → missing                 → present/uncertain
missing → confirmed_removed       tylko z positive removal proof
confirmed_removed → (new resource_id), nigdy z powrotem do starej incarnation
source unavailable/partial        → brak removal transition
```

`missing przez N polli` nie wystarcza do `confirmed_removed`, niezależnie od N.
Długi czas również nie zamienia braku dowodu w dowód.

### Kiedy dokładnie wolno ustawić `confirmed_removed`

Wymagane są łącznie:

1. pozytywny, przypisany do właściwego source i locatora dowód destroy/removal
   (np. kompletna task/event evidence, przyszła audytowana operacja backendu lub
   jawne operator confirmation);
2. kompletny, świeży baseline source po zdarzeniu, w którym locator jest absent;
3. brak gaps/out-of-order cursor pomiędzy evidence i baseline;
4. transaction nadal widzi ten sam expected active binding/continuity revision.

HTTP 404 z pojedynczego config endpointu, niedostępny node, partial listing,
ACL-filtered listing, timeout, source outage ani sam upływ czasu nie spełniają
tych warunków.

### Powrót po braku lub outage

- jeśli mocny continuity proof potwierdza tę samą incarnation, wraca istniejący
  `resource_id`;
- jeśli evidence potwierdza replacement, stary record jest retired/tombstoned,
  a nowy dostaje nowe ID;
- jeśli oba wyjaśnienia są możliwe, stary binding trafia do quarantine, a
  bieżący locator dostaje provisional `resource_id` ze stanem `unverified`;
  żadna policy nie jest kopiowana.

To samo dotyczy delete/recreate między pollingami i długiej przerwy backendu.
Brak obserwowanego `absent` nie oznacza continuity.

## Transaction boundary i publikacja

Każdy run wykonuje:

```text
fetch
→ normalize
→ validate snapshot source, schema, time and completeness
→ reconcile in one DB transaction
→ derive presence, continuity and capabilities
→ commit
→ publish committed snapshot to Hubinet Ops API/HA
```

Nie publikujemy surowego albo częściowo reconciled snapshotu. Transaction
sprawdza expected source revision, active locator bindings i monotoniczny czas/
cursor. Snapshot starszy od ostatniego committed run jest odrzucany. Restart
backendu ładuje ostatni committed inventory, tombstones oraz cursors; pamięć
procesu nie jest source of truth.

## Failure modes

| Zdarzenie | Klasyfikacja | Wpływ na poprzedni inventory |
| --- | --- | --- |
| wszystkie endpointy timeout | `source_unavailable` | zachowaj, oznacz stale; bez missing/removal |
| osiągalny endpoint innego klastra | `invalid` | odrzuć run; security alert |
| brak jednego node'a | `partial` lub `node_unavailable` | resources node'a zachowane |
| baseline pełny, config read jednego guest fail | baseline complete + per-resource error | locator `present`, facts unavailable |
| ACL ukrywa część inventory | `partial`/configuration error | bez removal transitions |
| pełny baseline bez locatora | `complete` | `missing`, nie `confirmed_removed` |
| pełny baseline + pozytywny destroy proof | `complete` | `confirmed_removed`, tombstone |
| out-of-order/stary run | `invalid` | bez zmian |

## Trust boundary

Provider jest read-only nawet wtedy, gdy token przez błąd ma szersze ACL. Kod
transportu musi odrzucać metody inne niż `GET` i mieć zamknięty zestaw ścieżek.
Discovery nie ma dostępu do typed host-control ani forced-command. Późniejsze
mutacje nadal przechodzą:

```text
HA → Hubinet Ops API → backend policy → plans/jobs/locks/audit
   → typed host-control → hostd/forced-command → Proxmox
```

## Nierozstrzygnięte kwestie

1. Exact supported PVE versions i contract test standalone `/cluster/resources`.
2. Wybór `proxmoxer` vs wąski async HTTP po security/maintenance review.
3. Minimalna endpoint/ACL matrix dla dodatkowych continuity evidence.
4. Dostępność, pagination i retencja task/event history jako evidence —
   **UNKNOWN** jako niezawodny stream.
5. Mechanizm source binding/failover bez natywnego immutable cluster UUID.

## Sources / Evidence

Oficjalne źródła Proxmox, odczytane 2026-08-08:

- [pveproxy — HTTPS 8006 i forwarding do innych node'ów](https://github.com/proxmox/pve-docs/blob/master/pveproxy.adoc)
- [pveum — API tokens, privilege separation, role i ACL](https://github.com/proxmox/pve-docs/blob/master/pveum.adoc)
- [pve-manager `Cluster.pm` — cluster-wide resources/status/tasks i permission filters](https://github.com/proxmox/pve-manager/blob/master/PVE/API2/Cluster.pm)
- [Proxmox VE API Viewer](https://pve.proxmox.com/pve-docs/api-viewer/)
- [pvecm — multi-master cluster i migracja](https://github.com/proxmox/pve-docs/blob/master/pvecm.adoc)

**FACT-DOC:** `pveproxy` forwarduje requests do innych node'ów; token z
separated privileges ma effective ACL jako przecięcie user/token; `VM.Audit`
pozwala czytać VM config, `Sys.Audit` node/cluster status/config.

**FACT-SOURCE:** `/cluster/resources` filtruje guest entries przez `VM.Audit` na
`/vms/{vmid}`, node facts przez `Sys.Audit` na `/nodes/{node}`, a
`/cluster/status` wymaga `Sys.Audit` na `/`.

Pozostałe reguły completeness, reconciliation i failover są decyzjami
architektonicznymi Hubinet Ops, nie obietnicami Proxmox API.
