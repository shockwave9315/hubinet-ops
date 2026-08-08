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
- `Sys.Audit` na `/access` — wymagane tylko dla authoritative ACL topology
  verification przez `GET /access/acl`;
- `Sys.Audit` na `/` tylko jeśli wymagane są `/cluster/status` lub pełna
  cluster/task evidence; jest to szersze niż sam inventory i musi być
  uzasadnione kontraktem implementacji;
- dodatkowe `Pool.Audit`, `Datastore.Audit`, `SDN.Audit` nie są wymagane dla
  minimalnego workload inventory, jeśli tych facts nie pobieramy.

`Sys.Audit` na `/access` jest read-only, ale zwiększa zakres widocznych
security-relevant ACL metadata. To jawny tradeoff konieczny dla authoritative
source-wide completeness; provider bez niego może dostarczać tylko
non-authoritative partial view.

Wbudowany `PVEAuditor` jest read-only i prosty operacyjnie, lecz jego zakres jest
szerszy niż projektowany discovery contract. Nie przyjmujemy go automatycznie.
Powyższa lista nie jest jeszcze finalną minimal permission matrix. Przed
implementacją powstanie dokładna macierz endpoint → ACL path → privilege,
sprawdzona contract i negatywnymi testami. Żadna rola discovery nie może zawierać
`VM.Allocate`, `VM.PowerMgmt`, `VM.Backup`, `VM.Clone`, `Sys.PowerMgmt` ani
pozostałych uprawnień mutacyjnych.

Token nigdy nie jest logowany ani zwracany przez Hubinet Ops API/diagnostics.
Authorization header powstaje wyłącznie w adapterze transportowym.

### ACL topology i effective-permission proof

**FACT-SOURCE:** `GET /access/permissions` pozwala userowi/tokenowi odczytać
własne effective permissions i zwraca mapę `path → privilege → propagate`.
**FACT-SOURCE:** `/cluster/resources` nie zgłasza brakującego `VM.Audit`; pomija
niewidoczne VM/LXC, więc ACL-filtered response może wyglądać dokładnie jak
kompletny, mniejszy inventory.

Effective permissions nie wystarczają samodzielnie. **FACT-DOC:** ACL na deeper
path zastępuje inherited permissions, a `NoAccess` cancels all other roles na
ścieżce. **FACT-SOURCE:** `get_effective_permissions()` pomija paths, których
effective permission map jest pusta. Przykładowe `/vms → VM.Audit propagate`
oraz `/vms/103 → NoAccess` może więc ukryć VM103 zarówno w resources, jak i w
effective permission dump.

**FACT-SOURCE:** `GET /access/acl` zwraca pełną konfigurację ACL, gdy caller ma
`Sys.Audit` na `/access`. Bez tego privilege wynik jest ograniczony do obiektów,
dla których caller może modyfikować permissions, więc nie jest topology proof.

Authoritative source-wide run musi używać tego samego tokenu i wykonać:

```text
ACL topology snapshot/hash BEFORE
+ effective permission snapshot/hash BEFORE
→ fetch cluster-wide baseline and required facts
→ ACL topology snapshot/hash AFTER
+ effective permission snapshot/hash AFTER
```

Permission evaluator musi fail-closed potwierdzić:

- identyczną canonical security-relevant ACL topology przed i po discovery
  window;
- identyczny effective permission snapshot przed i po discovery window;
- effective `VM.Audit` z propagation dla całego `/vms` guest tree;
- wymagane przez kontrakt node permissions dla całego `/nodes` tree oraz
  `Sys.Audit` na `/`, jeśli używane endpointy tego wymagają;
- topology proof potwierdzający brak security-relevant descendant override,
  `NoAccess` lub innej restriction dla discovery identity, które mogłyby ukryć
  dowolny guest albo wymagany node;
- brak pool/per-VM limited scope użytego jako substitute dla source-wide
  visibility.

Token widzący wyłącznie per-VM albo per-pool scope nie może utworzyć
authoritative `complete` inventory. Brak możliwości pobrania lub jednoznacznej
interpretacji własnych effective permissions albo pełnej ACL topology jest
`configuration_error`/`partial`, nie domniemaniem pełnego dostępu. Nie wolno
wtedy wykonywać absence/removal transitions.

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
  acl_topology_hash_before
  acl_topology_hash_after
  permission_snapshot_hash_before
  permission_snapshot_hash_after
  permission_coverage
  covered_nodes
  failed_scopes
  event_cursor_before / event_cursor_after (optional evidence)
  nodes[]
    external_node_name
    runtime/status facts
    observed_at
    read_result
  resources[]
    slot_locator {inventory_source_id, vmid}
    resource_type {qemu | lxc}
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

- `complete` — autorytatywny source-wide baseline zakończył się sukcesem,
  ACL topology i effective-permission proof potwierdzają pełne wymagane
  guest/node coverage, a oba topology/permission hashes przed/po są identyczne;
- `partial` — source odpowiedział, lecz brakuje node'a, strony, zakresu albo
  wystąpił per-resource read error;
- `configuration_error` — token/effective ACL lub provider configuration nie
  pozwala udowodnić authoritative source-wide coverage;
- `source_unavailable` — nie uzyskano wiarygodnego baseline;
- `invalid` — odpowiedź narusza schema, source binding lub monotonicity.

Kompletność baseline i kompletność szczegółowych facts są osobne. Pełna lista
locatorów z błędem config read pozwala zachować `present`, ale ustawia
`temporarily_unavailable` dla szczegółów. Częściowa lista locatorów nigdy nie
jest traktowana jak dowód nieobecności.

`covered_nodes` i `failed_scopes` opisują jawnie zauważone błędy transportu lub
subrequestów, ale nie dowodzą pełnego ACL coverage. Effective-permission proof
bez topology proof również nie może wykluczyć cichego `NoAccess`. Brak pełnej,
jednoznacznie ocenionej topology lub coverage daje `partial`/
`configuration_error`; różne topology albo permission hashes przed/po dają
`invalid`. We wszystkich tych przypadkach nie wolno wykonywać absence/removal
transitions.

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

Każda klasa proof wymaga wspólnych warunków:

1. proof odnosi się do dokładnego `inventory_source_id`, `resource_id`, active
   slot binding i expected binding/continuity revision;
2. późniejszy, kompletny i świeży source baseline potwierdza, że slot
   `(inventory_source_id, vmid)` jest absent;
3. reconciliation transaction nadal widzi ten sam expected binding/revision i
   atomowo zamyka binding, tworzy tombstone oraz ustawia `confirmed_removed`.

Poza nimi akceptowane są trzy rozłączne klasy authoritative removal proof:

#### A. Backend-mediated removal

- zakończona sukcesem typed backend operation, wcześniej związana z expected
  `resource_id`, slot binding i revision;
- późniejszy complete fresh baseline potwierdzający pusty slot.

Durable job/audit backendu jest w tej klasie pozytywnym proof. Nie wymaga
ciągłości zewnętrznego PVE event cursor, ponieważ operacja przeszła własną
autorytatywną ścieżkę backendu. Wymaga natomiast zgodności revision i snapshotu
po operacji.

#### B. Reliable event/task proof

- pozytywnie zidentyfikowany destroy/removal event dla dokładnego slotu i
  occupant;
- ciągły, zaufany cursor/evidence chain obejmujący zdarzenie;
- późniejszy complete fresh baseline potwierdzający pusty slot.

Tylko ta klasa wymaga cursor continuity. Ponieważ oficjalny kontrakt kompletnego,
trwale retencjonowanego PVE event stream pozostaje **UNKNOWN**, klasa B jest
niedostępna, dopóki implementacja nie udowodni takiego kontraktu dla wspieranej
wersji/provider. Zwykła lista recent tasks nie spełnia tego wymagania.

#### C. Explicit operator confirmation

- jawne, audytowane potwierdzenie operatora wskazujące dokładny `resource_id`,
  current slot binding i revision;
- complete fresh baseline potwierdzający pusty slot.

Klasa C nie zależy od event cursor. Potwierdzenie nie zastępuje fresh baseline i
nie może wskazywać jedynie VMID bez resource/binding context.

| Proof class | Positive authority | Complete fresh baseline | Cursor continuity |
| --- | --- | --- | --- |
| A: backend-mediated | durable successful typed backend job | wymagany | nie; obowiązuje backend job/binding revision |
| B: event/task | trusted destroy/removal event | wymagany | tak, obowiązkowo |
| C: operator confirmation | explicit audited operator decision | wymagany | nie |

HTTP 404 z pojedynczego config endpointu, niedostępny node, partial listing,
ACL-filtered listing, timeout, source outage ani sam upływ czasu nie należą do
żadnej klasy proof.

### Powrót po braku lub outage

- bez observable gap/conflict zgodne, kompletne obserwacje mogą zachować
  read-only `resource_id`; to observational consistency, nie security proof;
- po rzeczywistym gap (np. `missing`, source outage) mocny continuity proof może
  przywrócić istniejący trusted binding;
- jeśli evidence potwierdza replacement, stary record jest retired/tombstoned,
  a nowy dostaje nowe ID;
- jeśli po observable gap oba wyjaśnienia są możliwe, stary binding trafia do
  quarantine, a bieżący locator może dostać provisional `resource_id` ze stanem
  security `unverified`; żadna policy nie jest kopiowana.

Delete/recreate całkowicie pomiędzy dwoma identycznymi pollingami może być
nierozróżnialne i zachować read-only HA identity. Resource bez zaakceptowanego
continuity anchor pozostaje `unverified` i nie może posiadać destructive policy,
maintenance permission ani aktywnych destructive approvals/jobs.

## Transaction boundary i publikacja

Każdy run wykonuje:

```text
fetch ACL topology + effective permission snapshots BEFORE
→ fetch baseline/facts
→ fetch ACL topology + effective permission snapshots AFTER
→ normalize
→ validate topology/permission stability and coverage, source, schema, time and completeness
→ reconcile in one DB transaction
→ derive presence, continuity and capabilities
→ commit
→ publish committed snapshot to Hubinet Ops API/HA
```

Nie publikujemy surowego albo częściowo reconciled snapshotu. Transaction
sprawdza expected source revision, active locator bindings i monotoniczny czas.
Cursor jest sprawdzany tylko wtedy, gdy provider jawnie deklaruje wspierany
trusted cursor contract albo używana jest klasa proof B. Snapshot starszy od
ostatniego committed run jest odrzucany. Restart backendu ładuje ostatni
committed inventory, tombstones oraz wszystkie dostępne provider cursors;
pamięć procesu nie jest source of truth.

## Failure modes

| Zdarzenie | Klasyfikacja | Wpływ na poprzedni inventory |
| --- | --- | --- |
| wszystkie endpointy timeout | `source_unavailable` | zachowaj, oznacz stale; bez missing/removal |
| osiągalny endpoint innego klastra | `invalid` | odrzuć run; security alert |
| brak jednego node'a | `partial` lub `node_unavailable` | resources node'a zachowane |
| baseline pełny, config read jednego guest fail | baseline complete + per-resource error | locator `present`, facts unavailable |
| `GET /access/acl` niedostępne, ograniczone lub niejednoznaczne | `partial`/`configuration_error` | bez absence/removal transitions |
| topology wykazuje descendant override/`NoAccess` ukrywający scope | `configuration_error` | bez absence/removal transitions |
| topology hash BEFORE ≠ AFTER | `invalid` | odrzuć run; bez absence/removal transitions |
| effective permission proof nie pokrywa całego `/vms`/`/nodes` contract | `partial`/`configuration_error` | bez absence/removal transitions |
| permission hash BEFORE ≠ AFTER | `invalid` | odrzuć run; bez absence/removal transitions |
| token ma tylko per-VM/per-pool visibility | `configuration_error` | read-only partial view może być diagnostyczny, ale nie authoritative inventory |
| pełny baseline bez locatora | `complete` | `missing`, nie `confirmed_removed` |
| pełny baseline + proof klasy A, B albo C | `complete` | `confirmed_removed`, tombstone |
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

### Node routing jest osobną granicą

Discovery może ustalić current node i HA `via_device`, ale nie ustanawia mutation
route. Przed typed host-control backend musi rozwiązać current node do aktywnego
`node_binding_id`, sprawdzić expected binding revision, ważną attestation,
`node_trust_state=trusted` oraz executor/host policy readiness. Node name jest
wyłącznie external locator i nie może być samodzielnym routing credential.

Po remove/rejoin, reinstall, nieoczekiwanej zmianie hostd identity lub nowym
hoście pod starą nazwą binding staje się `unverified`/`revoked`. Migracja
workloadu do takiego node'a może być pokazana read-only, ale jego effective
destructive capabilities spadają do `none`. Endpoint failover discovery nie
przenosi hostd attestation i nie przywraca mutation trust.

## Nierozstrzygnięte kwestie

1. Exact supported PVE versions i contract test standalone `/cluster/resources`.
2. Wybór `proxmoxer` vs wąski async HTTP po security/maintenance review.
3. Minimalna endpoint/ACL matrix dla dodatkowych continuity evidence.
4. Dostępność, pagination i retencja task/event history jako evidence —
   **UNKNOWN** jako niezawodny stream.
5. Mechanizm source binding/failover bez natywnego immutable cluster UUID.
6. Finalny workload continuity proof/enrollment anchor. Dopóki nie zostanie
   zaakceptowany, trusted destructive capabilities są globalnie niedostępne;
   nie blokuje to przyszłego read-only discovery/inventory.
7. Finalny node/hostd attestation protocol i procedura jawnej key rotation.

## Sources / Evidence

Oficjalne źródła Proxmox, odczytane 2026-08-08:

- [pveproxy — HTTPS 8006 i forwarding do innych node'ów](https://github.com/proxmox/pve-docs/blob/master/pveproxy.adoc)
- [pveum — API tokens, privilege separation, role i ACL](https://github.com/proxmox/pve-docs/blob/master/pveum.adoc)
- [pve-manager `Cluster.pm` — cluster-wide resources/status/tasks i permission filters](https://github.com/proxmox/pve-manager/blob/master/PVE/API2/Cluster.pm)
- [pve-access-control `AccessControl.pm` — `GET /access/permissions` i effective permission map](https://github.com/proxmox/pve-access-control/blob/master/src/PVE/API2/AccessControl.pm)
- [pve-access-control `ACL.pm` — `GET /access/acl` i warunek pełnego odczytu ACL](https://github.com/proxmox/pve-access-control/blob/master/src/PVE/API2/ACL.pm)
- [pve-access-control `RPCEnvironment.pm` — obliczanie i filtrowanie effective permissions](https://github.com/proxmox/pve-access-control/blob/master/src/PVE/RPCEnvironment.pm)
- [Proxmox VE API Viewer](https://pve.proxmox.com/pve-docs/api-viewer/)
- [pvecm — multi-master, node remove/reinstall/rejoin i certificate refresh](https://github.com/proxmox/pve-docs/blob/master/pvecm.adoc)

**FACT-DOC:** `pveproxy` forwarduje requests do innych node'ów; token z
separated privileges ma effective ACL jako przecięcie user/token; `VM.Audit`
pozwala czytać VM config, `Sys.Audit` node/cluster status/config. ACL na deeper
path zastępuje inherited permissions, a `NoAccess` anuluje pozostałe role na
danej ścieżce.

**FACT-SOURCE:** `/cluster/resources` filtruje guest entries przez `VM.Audit` na
`/vms/{vmid}`, node facts przez `Sys.Audit` na `/nodes/{node}`, a
`/cluster/status` wymaga `Sys.Audit` na `/`. Brak `VM.Audit` powoduje pominięcie
guest entry bez markeru brakującego scope. `GET /access/permissions` pozwala
userowi/tokenowi odczytać własne effective permissions jako mapę path/privilege/
propagation, ale `get_effective_permissions()` pomija paths z pustą effective
permission map. `GET /access/acl` ujawnia pełną konfigurację ACL tylko callerowi
z `Sys.Audit` na `/access`; bez tego zwraca ograniczony widok.

Pozostałe reguły completeness, reconciliation i failover są decyzjami
architektonicznymi Hubinet Ops, nie obietnicami Proxmox API.
