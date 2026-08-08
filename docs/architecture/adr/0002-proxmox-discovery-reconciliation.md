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
environment. Pierwsza implementacja Phase 1 ma twardy constraint:

```text
one inventory_source_id = exactly one active discovery endpoint
automatic discovery endpoint failover = disabled
```

Source może przechowywać dodatkowe endpoint records wyłącznie jako
`candidate`/`inactive`. Nie uczestniczą one w discovery, retry ani automatycznym
failover. Gdy active endpoint jest niedostępny, wynik to `source_unavailable`;
provider nie próbuje kolejnego endpointu.

Canonical HTTPS URL/transport locator jest immutable dla jednego endpoint
record w zwykłym lifecycle. Jedynym wyjątkiem jest controlled migration wersji
canonicalization opisana niżej, która zachowuje endpoint identity i pełną
historię pary przed/po. Zmiana URL zawsze tworzy nowy `endpoint_id` ze statusem
`candidate`; nie istnieje operacja `existing_endpoint.url = new_url`. Relacja
endpointu do `inventory_source_id` również jest immutable, więc record nie może
zostać przeniesiony między sources.

Raw user URL nie uczestniczy bezpośrednio w uniqueness, equality, retained
history lookup ani decyzji replacement. Przed utworzeniem recordu jedna
deterministyczna, wersjonowana canonicalization function musi:

- przyjmować wyłącznie wspierany HTTPS contract i normalizować scheme;
- normalizować casing DNS hostname oraz canonical representation/brackets IPv6;
- traktować brak portu i jawny standardowy port HTTPS `443` jako ten sam
  locator, zachowując każdy jawny non-default port;
- normalizować pustą/root path i trailing slash;
- fail-closed odrzucać userinfo, query, fragment oraz ambiguous/invalid URL;
- nie dodawać magicznie portu PVE `8006`: direct PVE wymaga jawnego `:8006`,
  dopóki późniejszy contract nie zdefiniuje inaczej;
- nie zakładać wsparcia reverse-proxy subpaths bez osobnego evidence/contract.

Dwa raw URLs canonicalizujące się do tego samego transport locatora nie mogą
tworzyć niezależnych endpoint identities ani historii. Ponowne użycie tekstowego
aliasu retained locatora uruchamia ten sam reactivation/source-binding gate.
Versioned contract oraz positive/negative canonicalization tests są warunkiem
implementacji.

Przykładowo `https://PVE.EXAMPLE/` i `https://pve.example:443` są jednym
locatorem, natomiast `https://pve.example:8006/` jest innym, jawnie
non-default locatorem. Tekstowa różnica nie resetuje retained history.

Każdy endpoint record przechowuje canonical locator razem z
`canonicalization_contract_version`, która go wytworzyła. Backend upgrade nie
może reinterpretować istniejącej wartości pod nowym algorytmem. Zmiana wersji
wymaga explicit, audytowanej schema/data migration albo równoważnej controlled
procedure, która zachowuje `endpoint_id`, `inventory_source_id`, source-binding
i retained-history gates, deterministycznie przelicza cały retained namespace i
zapisuje provenance starej/nowej canonical pair.

Migration wykrywa cross-version aliases i collisions przed commit. Ambiguity lub
collision zatrzymuje ją fail-closed: bez automatycznego merge historii, drugiej
endpoint identity albo activation. Canonicalization version nie jest osobnym
namespace pozwalającym ominąć uniqueness. Dopóki retained namespace nie został
jednoznacznie zmigrowany lub objęty osobno zaakceptowanym cross-version lookup
contractem, create/reactivation endpointu pod nową wersją jest blocked. Contract
tests muszą obejmować version upgrade, alias do retained locatora i collision.

Nieudana migration jest atomowa i pozostawia stare stored pairs bez zmian.
Istniejący discovery może działać dalej wyłącznie, jeśli backend nadal potrafi
honorować i weryfikować zapisany stary contract; inaczej source przechodzi w
`configuration_error`. Nowa wersja nie jest furtką do utworzenia aliasu.

Status `active` nie jest zwykłym mutable flag. Jest wynikiem kontrolowanej,
atomowej state transition. Dla istniejącego source zabronione są bez accepted
source-binding procedure:

- `candidate → active` i `inactive → active`;
- direct replacement active endpoint record;
- usunięcie/retire active record i utworzenie innego bezpośrednio jako active;
- zachowanie `endpoint_id` przy zmianie URL/transport targetu;
- przepięcie endpoint record do innego `inventory_source_id`;
- disable/re-enable source użyte do zresetowania activation gate.

Historyczne endpoint records i provenance są retencjonowane, więc delete/recreate
nie zeruje gate. Retire active endpointu musi atomowo wyłączyć discovery dla
source; nie pozwala utworzyć zastępstwa jako active. Wyłączenie i ponowne
włączenie source nie przywraca wyjątku initial creation, a awaria active
endpointu nie upoważnia providera do wyboru zastępstwa.

### Initial source creation a existing source

Jedyny wyjątek Phase 1 dotyczy atomowego initial source creation. Nowy
`inventory_source_id`, który niczego historycznego nie dziedziczy, może zostać
utworzony w jednej transaction razem z dokładnie jednym initial active endpoint
record, wymaganym `source_runtime_health` w stanie
`initial/not_yet_observed`/non-fresh z unset last-success provenance oraz
zwiększeniem globalnego `published_state_revision`. To ustanawia nową backendową
source identity; nie kontynuuje wcześniejszego source ani inventory.

Po commit initial creation source jest `existing`, nawet zanim wykona pierwszy
udany polling. Każde późniejsze żądanie innego transport locatora tworzy inert
candidate. Dopóki source-binding contract pozostaje unresolved, candidate
activation i active endpoint replacement są disabled. Operator, który nie może
przedstawić source-binding proof, musi utworzyć nowy `inventory_source_id` z
własnym initial active endpointem.

Proxmox `pveproxy` udostępnia API na TCP 8006 i może forwardować żądania do
innych node'ów, więc jeden osiągalny endpoint może obsłużyć cluster-wide
discovery. Nie dowodzi to jednak, że dwa niezależnie osiągalne endpointy
reprezentują ten sam `inventory_source_id`. Cluster name, node membership,
VMIDs, hostname, TLS certificate ani endpoint URL nie są samodzielnie
wystarczającym source-binding proof.

```text
stable/unchanged endpoint URL != proven physical source continuity
```

Ten sam URL może po rebuildzie, zmianie DNS, reverse proxy albo infrastruktury
wskazywać inny Proxmox environment. Phase 1 nie rozwiązuje niewidocznego
same-URL repoint. Dla podstawowego read-only inventory jest to świadomie
zaakceptowana observational limitation; stabilny locator, TLS state ani
pozytywny odczyt nie mogą na tej podstawie nadać security continuity lub
destructive trust.

Active endpoint musi być jawnie skonfigurowany i używać TLS weryfikowanego przez
zaufane CA albo jawnie przypięty fingerprint; `verify=false` nie jest
rekomendowanym production mode. Aktywacja candidate endpointu oraz przyszły
multi-endpoint failover wymagają osobnego, zaakceptowanego ADR/contract dla
source binding/attestation. Do tego czasu odpowiedzi z różnych endpointów nie
są scalane ani porównywane jako inventory jednego source.

Security-sensitive TLS trust configuration ma monotonic
`transport_trust_revision` albo równoważną wersję contractu. Zmiany są explicit,
jawnie audytowane i przechodzą kontrolowaną revalidation transition, nie luźny
setter. Każdy discovery run wiąże się z exact `endpoint_id`, canonical transport
locator oraz expected transport-trust revision. Zmiana revision podczas runu
unieważnia commit wyniku pod starym contractem.

Broadening/replacement CA roots, pin/fingerprint albo verification policy,
które może zaufać innemu peerowi, wymaga jawnej revalidation. Nie ustanawia to
source identity, nie dowodzi source binding i nie omija activation gate.
Normalne odnowienie CA-valid certificate przy niezmienionym locatorze i
configured trust policy jest zmianą peer observation, a nie source identity ani
trust-policy revision. Rotation exact pinned certificate/fingerprint jest
security-sensitive controlled transition. Szczegółowy certificate rotation i
source-attestation protocol pozostaje do późniejszego security review.

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
security-relevant ACL metadata. To jawny tradeoff konieczny dla boundary
source-wide completeness; provider bez niego może dostarczać tylko
non-authoritative partial view.

Wbudowany `PVEAuditor` jest read-only i prosty operacyjnie, lecz jego zakres jest
szerszy niż projektowany discovery contract. Nie przyjmujemy go automatycznie.
Powyższa lista nie jest jeszcze finalną minimal permission matrix. Przed
implementacją powstanie dokładna macierz endpoint → ACL path → privilege,
sprawdzona testami kontraktowymi i negatywnymi. Żadna rola discovery nie może
zawierać `VM.Allocate`, `VM.PowerMgmt`, `VM.Backup`, `VM.Clone`,
`Sys.PowerMgmt` ani pozostałych uprawnień mutacyjnych.

Token nigdy nie jest logowany ani zwracany przez Hubinet Ops API/diagnostics.
Authorization header powstaje wyłącznie w adapterze transportowym.

### ACL topology i boundary effective-permission proof

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

Boundary-consistent source-wide run musi używać tego samego tokenu i wykonać:

```text
ACL topology snapshot/hash BEFORE
+ effective permission snapshot/hash BEFORE
→ fetch cluster-wide baseline and required facts
→ ACL topology snapshot/hash AFTER
+ effective permission snapshot/hash AFTER
```

`GET /access/acl` dostarcza topology i enumeruje security-relevant descendant
paths wymagające kontroli. Dla `/vms`, odpowiednich descendant paths oraz
wymaganych paths pod `/nodes` backend pyta Proxmox o effective permissions,
preferencyjnie przez oficjalny `GET /access/permissions?path=<path>` albo inny
równoważny, oficjalnie zweryfikowany kontrakt. **FACT-SOURCE:** endpoint z
parametrem `path` wywołuje upstream `permissions(userid, path)` i zwraca wynik
dla konkretnego principal/path.

Proxmox pozostaje source of truth dla obliczania inheritance, deeper-path
replacement, user/group semantics, API-token privilege separation, `NoAccess`
i propagation. Hubinet Ops tylko porównuje wynik upstream evaluation z
discovery contract (`VM.Audit`, `Sys.Audit`, wymagane coverage). Phase 1 nie
może implementować własnego recursive ACL evaluator jako źródła prawdy, chyba
że osobny późniejszy ADR i implementation review udowodnią konieczność oraz
pełną zgodność z upstream. Contract tests muszą sprawdzać tę granicę.

Boundary permission validator musi fail-closed potwierdzić na obu granicach:

- identyczną canonical security-relevant ACL topology przed i po discovery
  window;
- identyczny effective permission snapshot przed i po discovery window;
- effective `VM.Audit` z propagation dla całego `/vms` guest tree;
- wymagane przez kontrakt node permissions dla całego `/nodes` tree oraz
  `Sys.Audit` na `/`, jeśli używane endpointy tego wymagają;
- topology snapshot obejmujący każdy security-relevant descendant path, a dla
  każdego takiego path wynik upstream effective-permission evaluation
  spełniający discovery contract;
- brak pool/per-VM limited scope użytego jako substitute dla source-wide
  visibility.

Token widzący wyłącznie per-VM albo per-pool scope nie może utworzyć
boundary-complete inventory. Brak możliwości pobrania lub jednoznacznej
interpretacji własnych effective permissions albo pełnej ACL topology jest
`configuration_error`/`partial`, nie domniemaniem pełnego dostępu. Nie wolno
wtedy wykonywać absence/removal transitions.

#### Boundary consistency nie jest interval-wide proof

Matching ACL topology i effective permission snapshots BEFORE/AFTER dowodzą
wyłącznie zgodności security state w dwóch próbkowanych punktach granicznych.
Nie wykluczają zmiany `A → B → A` podczas pobierania baseline. W szczególności
czasowy `/vms/103 → NoAccess` może ukryć VM103, mimo że oba końcowe hashes są
identyczne.

```text
boundary-consistent snapshot != interval-wide ACL proof
boundary-consistent complete snapshot != authoritative absence proof
```

Oficjalny, monotonic ACL/config revision albo cursor obejmujący cały discovery
interval nie został zweryfikowany w dokumentacji ani użytych endpointach
Proxmox: **UNKNOWN**. Nie zakładamy jego istnienia. Tak samo boundary evaluation
delegowane do Proxmox nie rozwiązuje ABA.

Dodatnia obserwacja locatora w poprawnym snapshotcie wystarcza do observational
`present`. Brak wcześniej znanego locatora może dać observational `missing` i,
zgodnie z reconciliation, obniżyć observational continuity do `uncertain`.
Sampled snapshot nie dowodzi jednak physical removal, nie zamyka
security-sensitive starej incarnation i nie może nadać ani przenieść
destructive authority. False read-only presence/continuity state wskutek ACL ABA
jest dopuszczalne; identity split bez accepted identity boundary nie jest.
`missing`, source outage, ACL ABA, observable gap, ambiguity ani upływ czasu nie
tworzą nowego/provisional `resource_id`, nie zamykają active bindingu i nie
zwiększają `locator_generation`.

## Normalized discovery snapshot

Snapshot jest value object niezależnym od SDK i powinien zawierać:

```text
DiscoverySnapshot
  run_id
  discovery_run_sequence
  inventory_source_id
  expected_source_config_revision
  endpoint_id
  canonical_transport_locator
  canonicalization_contract_version
  expected_transport_trust_revision
  observed_at
  source_facts
  source_availability
  baseline_completeness
  acl_topology_hash_before
  acl_topology_hash_after
  permission_snapshot_hash_before
  permission_snapshot_hash_after
  permission_coverage
  boundary_consistency
  interval_consistency_evidence (optional; UNKNOWN for stock polling)
  covered_nodes
  failed_baseline_scopes
  detail_summary {ok_count, temporarily_unavailable_count, error_count}
  failed_detail_scopes[]
  event_cursor_before / event_cursor_after (optional evidence)
  nodes[]
    external_node_name
    runtime/status facts
    observed_at
    read_result
  resources[]
    slot_locator {inventory_source_id, vmid}
    resource_type {qemu | lxc}
    current_node_name (optional; absent when current relation is unresolved)
    runtime presence/status
    source facts
    observed config metadata
    observed_at
    detail_status {ok | temporarily_unavailable | error}
    detail_read_result
    continuity evidence[]
```

Snapshot nie zawiera user policy, enrollment, approval, maintenance permission
ani effective destructive capability. `resource_id`, `node_id`, generation i
continuity decision są wynikiem persistent reconciliation, nie surowym faktem
Proxmox. Provider może przekazać candidate evidence (`vmgenid`, `digest`,
`meta.ctime`, task records), wyraźnie opisane typem i provenance.

Snapshotowe `source_availability` jest outcome/faktem jednej próby provider read,
nie trwałym current source health. Dopiero backend stosuje sequence/CAS i
wyprowadza durable/published health bez zmiany retained resource presence.

Przed reconciliation normalized snapshot przechodzi fail-closed validation:

- snapshot `inventory_source_id` musi być exact expected source runu;
- każdy slot `(inventory_source_id, vmid)` może wystąpić najwyżej raz w current
  locator baseline;
- `resource_type` musi być dokładnie `qemu` albo `lxc`;
- równoczesne current entries LXC101 i QEMU101 dla jednego slotu czynią cały run
  `invalid`; nie są positive direct-replacement evidence;
- direct replacement porównuje persisted old occupant z dokładnie jednym current
  occupantem z nowego valid snapshotu;
- external current node names muszą być jednoznaczne w node baseline;
- malformed duplicate locator/node payload oznacza `invalid`, bez reconciliation
  ani publish.

Wymagane są negative contract tests dla duplicate locator, cross-type duplicate,
duplicate node name, wrong source ID oraz unsupported `resource_type`.

Zagnieżdżone dane snapshotu muszą być rzeczywiście immutable albo deep-copied
przed transaction boundary. Shallow read-only wrapper nie wystarcza.

Persistent `discovery_runs` zapisuje przy issuance `discovery_run_sequence` i
expected source/endpoint/canonicalization/transport context. Commit-observed
`source_config_revision`, exact `endpoint_id`, canonical transport locator,
`canonicalization_contract_version` i observed `transport_trust_revision` są
completion fields i powstają wyłącznie wtedy, gdy run osiągnie fail-closed
reconciliation transaction. Provider snapshot niesie expected values; issued
record nie wymaga danych, których fetch jeszcze nie wytworzył.

## Dwie niezależne osie wyniku discovery

### A. Locator/baseline completeness

Każdy finalized run, który osiągnął outcome classification, ma dokładnie jedną
klasyfikację `baseline_completeness`. Issued/incomplete lub jawnie abandoned
przed klasyfikacją run pozostawia to completion field unset:

- `complete` — observational source-wide baseline zakończył się sukcesem,
  boundary ACL topology i effective-permission checks potwierdzają wymagane
  guest/node coverage, a oba topology/permission hashes przed/po są identyczne;
- `partial` — source odpowiedział, lecz sama enumeracja locatorów lub wymagany
  dla niej page, scope, node coverage albo baseline prerequisite jest niepełny;
- `configuration_error` — token/effective ACL lub provider configuration nie
  pozwala udowodnić boundary source-wide coverage;
- `source_unavailable` — nie uzyskano wiarygodnego baseline;
- `invalid` — odpowiedź narusza schema, source binding lub monotonicity.

Ta oś jako jedyna decyduje, czy wolno wyprowadzać observational `present` oraz
`missing`. `complete` baseline pozostaje `complete`, gdy późniejszy opcjonalny
config/runtime/metadata read jednego resource kończy się błędem. Częściowa
lista locatorów nigdy nie jest traktowana jak dowód nieobecności.

Endpoint/subrequest jest `baseline prerequisite` tylko wtedy, gdy jego wynik
jest z góry jawnie wymagany do enumeracji locatorów albo walidacji source-wide
scope, ACL boundary, source binding, schema lub monotonicity. Taki status musi
być częścią wersjonowanego provider contract; nie wolno po błędzie zwykłego
detail read przeklasyfikować go retroaktywnie na baseline prerequisite.

`covered_nodes` i `failed_baseline_scopes` opisują jawnie zauważone błędy
baseline/prerequisites, ale nie dowodzą pełnego ACL coverage.
Effective-permission proof bez topology proof również nie może wykluczyć
cichego `NoAccess`. Brak pełnej, jednoznacznie ocenionej topology lub coverage
daje `partial`/`configuration_error`; różne topology albo permission hashes
przed/po dają `invalid`. We wszystkich tych przypadkach nie wolno wykonywać
`missing`/removal transitions.

`complete` oznacza boundary/snapshot completeness dla read-only inventory. Nie
oznacza interval-wide ACL consistency ani authoritative negative/absence
evidence. Bez osobno zaakceptowanego interval-wide proof nie wolno wyprowadzać
z niego `confirmed_removed`.

### B. Per-resource detail/fact read status

Każdy locator obecny w normalized provider baseline ma niezależny
`detail_status`:

- `ok` — wymagane dla widoku detail facts zostały odczytane i zwalidowane;
- `temporarily_unavailable` — timeout, przejściowy transport/source error albo
  chwilowa niedostępność config/runtime facts;
- `error` — trwały/nieklasyfikowany błąd lub niepoprawny detail payload, który
  wymaga diagnostyki, ale nie podważa samej zaobserwowanej obecności locatora.

Status obejmuje guest config, dodatkowe runtime facts, opcjonalne metadata i
continuity hints, o ile provider contract nie oznaczył konkretnego odczytu jako
baseline prerequisite. Detail failures są zapisywane per observation oraz
agregowane w run jako `detail_error_count`/`failed_detail_scopes`; nie zmieniają
automatycznie `baseline_completeness`.

Po reconciliation published `detail_status` ma dodatkową wartość
`not_applicable`:

```text
presence=present
  → detail_status ∈ {ok, temporarily_unavailable, error}

presence∈{missing, confirmed_removed, not_current}
  → detail_status=not_applicable
```

`not_applicable` oznacza, że dla bieżącego published absence/terminal state nie
istnieje current detail read. Nie oznacza error, timeout ani braku retained
last-known facts. Normalized provider entry obecny w current baseline nigdy nie
używa tej wartości. Validator/contract tests muszą odrzucać niezgodne kombinacje
presence/detail status w obu kierunkach.

Przykład kontraktowy:

```text
boundary-complete baseline: VM100, VM101, VM102
VM101 detail/config read: timeout

VM101.presence = present
VM101.detail_status = temporarily_unavailable
baseline_completeness = complete
```

Pozostałe locatory są normalnie reconciled względem pełnego baseline. Jeżeli
wcześniej znany VM103 nie występuje na liście, może przejść do observational
`missing` mimo błędu detail VM101. Nadal obowiązuje ACL ABA: takie `missing` nie
jest authoritative absence proof i nie umożliwia polling-only
`confirmed_removed`.

## Reconciliation state machine

State machine używa bez wyjątków taxonomy i valid-state matrix z
[ADR 0001](0001-resource-identity-incarnation.md#kanoniczny-model-stanu-i-continuity):

- `presence`: `present`, `missing`, `confirmed_removed`, `not_current`;
- `lifecycle`: `active`, `quarantined`, `retired`;
- `observational_continuity`: `consistent`, `uncertain`, `replaced`;
- `security_continuity`: `unverified`, `trusted`, `revoked`;
- `detail_status` i `node_availability` są niezależnymi osiami.

Reconciled `detail_status` używa `ok|temporarily_unavailable|error` wyłącznie dla
`presence=present`, a dla `missing|confirmed_removed|not_current` zawsze
`not_applicable`.

`retired` jest wyłącznie lifecycle; nigdy nie jest observational continuity.
Locator presence i availability/detail status są niezależne:

- `present` — locator występuje w boundary-complete, bieżącym baseline; detail
  status może być `ok`, `temporarily_unavailable` albo `error`;
- `node_availability` — osobna oś `available`, `unavailable`, `unresolved` albo
  `not_applicable`; niedostępny node jest overlay i nie zmienia `present`;
- `missing` — locator nie występuje w udanym, boundary-complete baseline, ale
  brak pozytywnego dowodu trwałego removal/replacement; jest to observational
  negative, które może wynikać także z niewykrytego ACL ABA;
- `confirmed_removed` — istnieje pozytywny removal proof i autorytatywna
  nieobecność slotu została zatwierdzona w reconciliation;
- `not_current` — terminalna reprezentacja starej incarnation po positive direct
  replacement; nie twierdzi, że slot jest absent, ponieważ current occupantem
  jest successor.

Dozwolone przejścia (skrót):

```text
present + detail_status ok ↔ temporarily_unavailable/error
present + node_availability unavailable → present + node_availability available
present → missing                  → same resource_id/binding + present/uncertain/quarantined
missing → confirmed_removed       tylko z positive removal authority
                                  + accepted authoritative absence evidence
confirmed_removed + późniejszy powrót locatora → new resource_id,
                                             nigdy stara incarnation
nonterminal old + active binding + expected resource_continuity_revision
  + positive replacement evidence + boundary-valid current successor observation
  → atomic old not_current/retired/replaced + new resource_id/generation/present
baseline source_unavailable/partial → brak `missing`/removal transition
```

`missing przez N polli` nie wystarcza do `confirmed_removed`, niezależnie od N.
Długi czas również nie zamienia braku dowodu w dowód.

Powrót locatora po ambiguity sam nie jest granicą identity. Bez positive
replacement evidence i bez wcześniejszego `confirmed_removed` reconciler nie
zamyka active bindingu, nie zwiększa `locator_generation` i nie tworzy nowego
active/provisional `resource_id`.

### Direct replacement bez pustego slotu

Direct replacement jest osobnym przejściem od removal/absence. Dowodzi, że
stary resource nie zajmuje już slotu, ale nie dowodzi, że slot był albo jest
pusty:

```text
replacement proof != absence proof
confirmed_removed != replaced
```

Positive direct replacement evidence może pochodzić wyłącznie z:

1. boundary-valid/current observation pokazującej zmianę immutable occupant
   `resource_type` dla tego samego `(inventory_source_id, vmid)`, czyli
   `lxc → qemu` albo `qemu → lxc`;
2. future accepted continuity-anchor contract, którego mismatch jednoznacznie
   wyklucza starą incarnation;
3. trusted destroy/create event chain, ale dopiero po zaakceptowaniu contractu
   gwarantującego contiguous event/cursor semantics; dla stockowego PVE taki
   kontrakt pozostaje **UNKNOWN**;
4. explicit audited operator replacement decision związana z exact starym
   `resource_id`, active locator binding i expected revision. Taka decyzja
   klasyfikuje replacement, ale nie nadaje successorowi trusted continuity ani
   destructive authority.

Rename, name change, zwykła zmiana config `digest`, runtime/config/detail
mismatch, pojedynczy HTTP error ani upływ czasu nie są positive replacement
evidence. Gdy evidence jest niejednoznaczne, obowiązuje
`uncertain`/quarantine na istniejącym `resource_id` i bindingu; nie wolno
wykonywać direct handoff, tworzyć provisional identity ani inkrementować
generation tylko z powodu config change lub ambiguity.

Input direct replacement to dowolny istniejący **nonterminal** old resource z
exact active locator bindingiem, nie tylko resource aktualnie publikowany jako
`presence=present`. Dopuszczalne old states obejmują co najmniej:

- `presence=present`, `lifecycle=active`;
- `presence=present`, `lifecycle=quarantined`;
- `presence=missing`, `lifecycle=quarantined`.

We wszystkich przypadkach wymagane są expected `resource_id`, active
`binding_id`, `locator_generation`, `resource_continuity_revision`, positive
replacement evidence i dokładnie jeden successor occupant w boundary-valid
current snapshot. Ambiguity sama nie spełnia tego warunku.

Direct replacement jest jedną reconciliation transaction. W tej samej granicy
backend:

1. weryfikuje expected old `resource_id`;
2. weryfikuje exact active locator binding;
3. weryfikuje expected `locator_generation` i current
   `resource_continuity_revision`;
4. weryfikuje positive replacement evidence oraz bieżącą, boundary-valid
   obserwację successor occupanta;
5. zamyka stary binding przez `valid_to` i `closure_reason=replaced`;
6. ustawia starej incarnation `presence=not_current`,
   `observational_continuity=replaced` i `lifecycle=retired`;
7. zwiększa old `resource_continuity_revision`, revokuje wcześniejszy trust,
   jeżeli istniał (wcześniejsze `unverified` pozostaje historycznie
   `unverified`), suspenduje policy applicability i ustawia effective destructive
   authorization/maintenance permission na `none`;
8. zapisuje retained terminal/tombstone history;
9. inkrementuje `locator_generation` i tworzy nowy `resource_id`;
10. tworzy dokładnie jeden nowy active binding dla tego samego slotu;
11. inicjalizuje successor jako `presence=present`,
   `security_continuity=unverified`, level `discovered`, bez inherited stored
   policy, approvals, jobs ani locks;
12. zapisuje optional lineage old → successor oraz evidence/provenance/audit;
13. wykonuje commit i dopiero potem publikuje oba stany.

Nie istnieje committed ani published moment z dwoma active bindings dla slotu.
Nie jest wymagany pośredni `missing`, `confirmed_removed` ani authoritative
proof, że slot stał się pusty. Jeśli stara incarnation była `trusted`, positive
replacement natychmiast suspenduje jej effective policy, ustawia destructive
capabilities i maintenance permission na `none`, a aktywne destructive
operations są fail-closed blokowane/przerywane według późniejszego audytowanego
operation contract.

Published `resources[]` jest identity-keyed przez `resource_id`, nie przez VMID.
Po handoff widok może równocześnie zachować old
`resource_id=A`/generation 4/`not_current`/`retired` i opublikować successor
`resource_id=B`/generation 5/`present`/`active` dla tego samego VMID. Jest to
historia dwóch occupants, nie dwa current bindings. API, Coordinator i HA nie
mogą budować identity map przez `resources_by_vmid[vmid]`; current occupant jest
rozwiązywany przez exact active locator binding. Contract tests muszą obejmować
old i successor z tym samym VMID oraz ich różne Device Registry/entity identity.

### Node relation i HA availability

To jest docelowy kontrakt wymagany przez Phase 0 Amendment. `detail_status`
pozostaje niezależny; dla absence albo terminal state nie ma bieżącego detail
read, a retained facts są stale/historyczne.

| Przypadek | `presence` | `detail_status` | `node_availability` | `current_node_id` | `last_known_node_id` | HA `via_device` | HA availability |
| --- | --- | --- | --- | --- | --- | --- | --- |
| A. Locator present, current assignment znany, node dostępny | `present` | `ok`, `temporarily_unavailable` albo `error` | `available` | wymagany, node istnieje w tym samym snapshotcie | `null` | current node | Presence i facts dostępne zależnie od `detail_status`; detail error wyłącza tylko zależne encje. |
| B. Locator present, current assignment znany, node niedostępny | `present` | `ok`, `temporarily_unavailable` albo `error` | `unavailable` | wymagany, node istnieje w tym samym snapshotcie | `null` | current node | Presence może pozostać dostępne; node/runtime/detail-dependent entities są unavailable lub jawnie stale. |
| C. Locator present, current relation nierozstrzygnięta | `present` | `ok`, `temporarily_unavailable` albo `error` | `unresolved` | `null` | poprzedni node, jeśli był znany | last-known node dla presentation, jeśli istnieje; inaczej brak relacji | Presence może pozostać dostępne; location/node-dependent entities są unavailable do resolution. |
| D. Locator missing | `missing` | `not_applicable` | `not_applicable` | `null` | ostatni znany node, jeśli istniał | zachowaj last-known presentation relation | Resource entities unavailable; retained facts są stale/historyczne; brak purge. |
| E. Confirmed removed | `confirmed_removed` | `not_applicable` | `not_applicable` | `null` | ostatni znany node, jeśli istniał | zachowaj last-known presentation relation/history | Resource entities unavailable; retained facts są historyczne; brak automatycznego delete/purge. |
| F. Stara incarnation po direct replacement | `not_current` | `not_applicable` | `not_applicable` | `null` | ostatni znany node starej incarnation, jeśli istniał | zachowaj last-known presentation relation/history | Stare entities unavailable i retained; successor ma osobne device/entities oraz własną current node relation. |

`current_node_id` oznacza wyłącznie aktualną, wiarygodnie resolved relację
inventory. `last_known_node_id` jest presentation/history hint i nigdy security
mutation route. Mutacja używa wyłącznie current location ponownie związanej z
trusted `node_binding_id`, exact revision i ważną attestation. W szczególności:

Każde non-null `current_node_id` i `last_known_node_id` musi wskazywać node record
obecny w tym samym published snapshot; last-known może wskazywać retained/offline
node. Validator nie może akceptować dangling `via_device` relation.

Phase 1 nie purguje node recordu, dopóki jakikolwiek published current albo
last-known resource go referencjonuje. Offline/history node pozostaje retained w
published nodes, jeśli jest potrzebny do zachowania tej relacji. Future node
purge wymaga osobnego atomowego reference/HA lifecycle contract; nie jest
projektowany w tym ADR.

```text
node availability failure != resource physical absence
last_known_node_id != security mutation route
```

### Kiedy dokładnie wolno ustawić `confirmed_removed`

Ta ścieżka dotyczy wyłącznie autorytatywnego potwierdzenia pustego slotu.
Positive replacement przy slocie zajętym przez successor używa osobnego direct
replacement transition i nigdy nie jest zapisywany jako `confirmed_removed`.

Każda klasa proof wymaga wspólnych warunków:

1. proof odnosi się do dokładnego `inventory_source_id`, `resource_id`, active
   `binding_id`, `locator_generation` i expected
   `resource_continuity_revision`;
2. istnieje osobno zaakceptowane authoritative absence evidence, które dowodzi,
   że slot `(inventory_source_id, vmid)` jest absent z klasą bezpieczeństwa
   wystarczającą do zamknięcia incarnation; boundary-complete fresh baseline
   jest wymaganym consistency check, lecz sam nie spełnia tego warunku;
3. reconciliation transaction nadal widzi ten sam expected active binding,
   generation i resource continuity revision, a następnie atomowo zamyka binding,
   zwiększa `resource_continuity_revision`, tworzy tombstone, ustawia
   `presence=confirmed_removed` i `lifecycle=retired`.

Confirmed removal zachowuje ostatnie znaczące
`observational_continuity=consistent|uncertain`; nie ustawia wartości `retired`
na tej osi. Policy applicability, destructive capabilities i maintenance
permission są `none`, active destructive execution fail closed, a audit/history
pozostają. Wcześniejsze `security_continuity=trusted` przechodzi do `revoked`;
resource, który nigdy nie był trusted, może zachować historyczne `unverified`.

Poza nimi rozróżniamy trzy klasy positive removal authority:

#### A. Backend-mediated removal

- zakończona sukcesem typed backend operation, wcześniej związana z expected
  `resource_id`, active `binding_id`, `locator_generation` i
  `resource_continuity_revision`;
- późniejszy boundary-complete fresh baseline pokazujący pusty slot;
- osobno zaakceptowane authoritative absence evidence.

Durable job/audit backendu jest w tej klasie pozytywnym proof. Nie wymaga
ciągłości zewnętrznego PVE event cursor, ponieważ operacja przeszła własną
autorytatywną ścieżkę backendu. Jest positive removal authority, ale sama wraz z
pollingowym snapshotem nie dowodzi interval-wide absence. Wymaga zgodności
revision oraz osobno zaakceptowanego absence proof po operacji.

#### B. Reliable event/task proof

- pozytywnie zidentyfikowany destroy/removal event dla dokładnego slotu i
  occupant;
- ciągły, zaufany cursor/evidence chain obejmujący zdarzenie;
- późniejszy boundary-complete fresh baseline pokazujący pusty slot;
- authoritative absence evidence obejmujące brak późniejszego ponownego zajęcia
  slotu.

Tylko ta klasa wymaga cursor continuity. Ponieważ oficjalny kontrakt kompletnego,
trwale retencjonowanego PVE event stream pozostaje **UNKNOWN**, klasa B jest
niedostępna, dopóki implementacja nie udowodni takiego kontraktu dla wspieranej
wersji/provider. Dopiero zaakceptowany stream obejmujący zarówno removal, jak i
brak późniejszego recreate mógłby dostarczyć absence side. Zwykła lista recent
tasks nie spełnia tego wymagania.

#### C. Explicit operator confirmation

- jawne, audytowane potwierdzenie operatora wskazujące dokładny `resource_id`,
  current `binding_id`, `locator_generation` i `resource_continuity_revision`;
- boundary-complete fresh baseline pokazujący pusty slot;
- osobno zaakceptowane authoritative absence evidence lub attestation.

Klasa C nie zależy od event cursor. Ogólne potwierdzenie operatora nie zastępuje
absence proof ani fresh baseline i nie może wskazywać jedynie VMID bez
resource/binding context. Jeżeli przyszły kontrakt uzna szczególną, audytowaną
source/host-side attestation operatora za authoritative absence proof, musi to
zostać zaakceptowane oddzielnie.

| Proof class | Positive removal authority | Boundary-complete fresh baseline | Authoritative absence side |
| --- | --- | --- | --- |
| A: backend-mediated | durable successful typed backend job | wymagany consistency check | osobny accepted proof; event cursor nie jest wymagany przez positive side |
| B: event/task | trusted destroy/removal event | wymagany consistency check | trusted contiguous cursor/stream obejmujący brak recreate; obecnie **UNKNOWN** |
| C: operator confirmation | explicit audited operator decision | wymagany consistency check | osobny accepted proof/attestation; event cursor nie jest wymagany przez positive side |

Możliwe przyszłe klasy authoritative absence evidence obejmują oficjalnie
udokumentowany monotonic ACL/config revision pokrywający cały interval,
transactional/linearizable source snapshot albo trusted host/source-side
absence attestation. Żadna nie jest obecnie wybrana ani potwierdzona dla
stockowego PVE polling. Dlatego polling-only automatic `confirmed_removed`
pozostaje niedostępne.

HTTP 404 z pojedynczego config endpointu, niedostępny node, partial listing,
ACL-filtered listing, timeout, source outage ani sam upływ czasu nie należą do
żadnej klasy proof.

### Powrót po braku lub outage

- bez observable gap/conflict zgodne, kompletne obserwacje mogą zachować
  read-only `resource_id`; to observational consistency, nie security proof;
- po rzeczywistym gap (np. `missing`, source outage) accepted continuity
  resolution/re-enrollment zachowuje ten sam `resource_id` i active locator
  binding, jeśli nadal są ważne, aktualizuje lifecycle/observational state,
  może ustawić `security_continuity=trusted`, zwiększa
  `resource_continuity_revision` i ponownie wyprowadza effective policy oraz
  capabilities; locator binding sam nie posiada trust state;
- jeśli evidence pozytywnie potwierdza innego current occupanta, atomowy direct
  replacement zamyka stary binding jako `replaced`, zachowuje terminal history,
  a successor dostaje nowe ID/generation bez `confirmed_removed`;
- jeśli po observable gap oba wyjaśnienia są możliwe, istniejący `resource_id`,
  active locator binding i `locator_generation` pozostają dla read-only
  reconciliation. Gdy locator jest ponownie widoczny, `presence=present`,
  observational continuity=`uncertain`, lifecycle=`quarantined`, a security
  continuity jest `revoked` po utracie wcześniejszego trust albo pozostaje
  `unverified`. Policy applicability=false, destructive capabilities i
  maintenance permission są `none`; aktywne destructive operations fail closed.
  Stored policy/history pozostają przy istniejącym `resource_id`, a monotonic
  `resource_continuity_revision` zwiększa się, aby unieważnić wcześniejsze
  approvals/jobs bez zmiany `binding_id` ani `locator_generation`.

Quarantine nie jest terminal history ani tombstone. Nie zamyka bindingu i nie
tworzy successor lineage. Tombstone/termination powstaje dopiero po accepted
terminal transition: `confirmed_removed` z authoritative absence proof albo
direct replacement z positive replacement evidence. Nowy current `resource_id`
dla tego samego slotu powstaje tylko na jednej z tych dwóch granic: jako
successor direct replacement albo przy powrocie locatora po wcześniejszym
`confirmed_removed`.

Delete/recreate całkowicie pomiędzy dwoma identycznymi pollingami może być
nierozróżnialne i zachować read-only HA identity. Resource bez zaakceptowanego
continuity anchor pozostaje `unverified`. Może zachować historycznie stored
policy record, lecz effective destructive policy, maintenance permission i
destructive capabilities są `none`; nie wolno tworzyć nowych destructive
approvals/jobs.

## Transaction boundary i publikacja

Każdy run wykonuje:

```text
atomic transaction:
  require no active discovery owner for this inventory_source_id
  increment durable source.last_issued_run_sequence
  → capture expected source_config_revision + exact endpoint/canonicalization/TLS revisions
    + provider contract/version
  → persist issued run with returned discovery_run_sequence, expected context
    and no required completion fields
  → set exact active_discovery_run_id/fencing ownership to this run
  → increment global published_state_revision because last_issued_run_sequence is exposed
→ commit issuance/context
→ only now begin provider I/O
→ fetch ACL topology + per-path effective permission snapshots BEFORE
→ fetch locator baseline and declared baseline prerequisites
→ fetch per-resource optional detail/facts
→ fetch ACL topology + per-path effective permission snapshots AFTER
→ normalize
→ validate exact source + locator/node uniqueness + schema
→ validate exact source/endpoint/canonicalization/transport-trust revisions
→ validate boundary topology/permission equality and baseline completeness
→ record independent per-resource detail statuses
→ classify outcome
→ if authoritative inventory success:
    one atomic DB transaction:
    → require run is nonterminal and exact current active discovery owner
    → revalidate exact current source/endpoint/canonicalization/TLS context
    → finalize run exactly once + update completion provenance
    → reconcile inventory
    (w tym optional atomic direct old-binding → successor-binding handoff)
    → update committed-inventory and source-health tokens
    → persist fixed last-successful timestamp + freshness deadline
      bound to exact committed run/context
    → increment inventory revision + published-state revision
    → derive presence, continuity, freshness and capabilities
    → release active discovery ownership
    → commit everything or nothing
    → publish committed inventory + source state to Hubinet Ops API/HA
→ else failed/partial/unavailable/invalid:
    one atomic DB transaction:
    → require run is nonterminal and exact current active discovery owner
    → finalize run exactly once + max-update completion provenance
    → revalidate exact applicability/current source/endpoint/canonicalization/TLS context
    → if newest applicable: CAS-update run-health provenance,
      current health/freshness/origin/reason and invalidate mutation freshness
    → increment published-state revision for every published-field change
    → no resource reconciliation
    → release active discovery ownership
    → commit everything or nothing
    → publish source state with retained last committed inventory
```

Nie publikujemy surowego albo częściowo reconciled snapshotu. Monotonic
`source_config_revision` jest concurrency tokenem source configuration, nie
source identity. Inkrementuje się przy każdej zmianie provider/discovery
settings, credential material/version lub secret reference, discovery-relevant
lifecycle/config state, active discovery-route transition albo wymaganych
provider contract parameters, w tym configured freshness duration. Controlled
canonicalization migration active
endpointu również zwiększa revision. Pure display-label change, inert candidate
metadata i nowe observed facts nie zmieniają znaczenia bieżącego runu, więc nie
zwiększają revision.

Run zapisuje expected `source_config_revision` przed fetch. Reconciliation
transaction oraz failed-run transaction, która miałaby zastosować health,
sprawdzają fail-closed wewnątrz swojej atomic boundary:

```text
current source_config_revision == expected source_config_revision
exact active endpoint_id == expected endpoint_id
stored canonical locator/version == expected canonical locator/version
current transport_trust_revision == expected transport_trust_revision
active_discovery_run_id == run.run_id
run lifecycle is nonterminal
discovery_run_sequence > inventory_sources.last_committed_run_sequence
discovery_run_sequence > source_runtime_health.last_health_run_sequence
```

Każdy mismatch klasyfikuje run jako invalid/stale: bez reconciliation ani
inventory commit oraz bez zmiany current health nowego contextu. Run może zostać
one-time finalized/audytowany i podnieść completion provenance tylko przy exact
active ownership; jeśli jest ono publikowane, ta sama transaction zwiększa
`published_state_revision` i zwalnia ownership. Source
configuration mutation, active route/canonicalization/TLS transition, issuance,
successful reconciliation oraz failed-run health application serializują się w
tej samej backendowej transaction/lock/CAS boundary. Dzięki monotonic revision
także zmiana source
configuration `A → B → A` podczas runu pozostawia inny numer. Exact endpoint,
canonicalization oraz TLS checks są oddzielne i nie mogą zostać zastąpione przez
source revision. Applicability check wykonany przed transaction nie jest
security boundary. Mismatch odrzuca health/reconciliation application także
wtedy, gdy zmiana nastąpiła po odczytach AFTER, lecz przed transaction commit.

### `discovery_run_sequence`

Backend nadaje przed fetch monotoniczny `discovery_run_sequence` osobno dla
każdego `inventory_source_id`. Sequence jest trwałym issuance/audit tokenem, nie
source identity, czasem ani samodzielnym proof kolejności physical observation
windows; może mieć luki po failed/crashed runs i nie zwiększa
`source_config_revision`. Wall-clock timestamps służą wyłącznie do
observability/audit i nigdy nie są concurrency authority.

Phase 1 wymaga per-source single-flight: najwyżej jeden run jednego source może
jednocześnie posiadać durable active ownership, wykonywać provider I/O i być
eligible do finalization/reconciliation lub health application. Observation
windows jednego source nie mogą się nakładać. Discovery różnych
`inventory_source_id` może działać równolegle.

Każdy source ma durable monotonic issuance state
`last_issued_run_sequence` albo równoważny DB sequence. Allocation wykonuje w
jednej transaction:

```text
require source.active_discovery_run_id is unset
→ atomic increment source.last_issued_run_sequence
→ capture exact expected source/endpoint/canonicalization/TLS context
  + provider contract/version
→ persist issued run with the returned sequence and expected context only
→ set source.active_discovery_run_id = run.run_id
→ increment global published_state_revision because last_issued_run_sequence is exposed
→ commit issuance/context
→ dopiero potem rozpocznij pierwsze provider I/O
```

Issuance/context transaction serializuje się z controlled source configuration,
endpoint i transport-trust transitions, więc persisted expected context nie może
łączyć pól z różnych revisions. Dwa triggery konkurujące o wolny source ownership
nie mogą oba przejść issuance: dokładnie jeden ustanawia active run; drugi nie
rozpoczyna I/O i może zostać queued/coalesced/rejected bez projektowania tutaj
pełnej kolejki.

Wartość jest unique per source, strictly increasing, never reused i trwała przez
restart/crash. Nie wolno wyliczać jej jako `last_committed_run_sequence + 1`,
opierać na timestampie ani ponownie użyć po nieudanym runie. Luki są prawidłowe.
Unique constraint obejmuje `(inventory_source_id, discovery_run_sequence)`.
Jeżeli implementacja kiedykolwiek dopuści concurrent discovery tego samego
source, będzie wymagać osobnego accepted ordering contract opartego na provider
cursor, linearizable observation token albo równoważnym proof. Sam issuance
sequence nie wystarczy.

Issuance i completion to dwa etapy lifecycle jednego recordu. Od utworzenia
immutable są: `run_id`, source, sequence, issued/start timestamp, expected
source/endpoint/canonicalization/TLS context oraz provider contract/version.
Finish/completed timestamp, observation interval, outcome,
`baseline_completeness`, ACL/permission BEFORE/AFTER provenance, detail summary,
normalized snapshot hash, optional commit-observed context i terminal/failure
reason są początkowo unset. Kontrolowana finalization zapisuje wyłącznie
faktycznie znane completion fields dokładnie raz; druga albo sprzeczna
finalization jest odrzucana, a pola po finalization są immutable/audit-retained.

Run może pozostać `issued`/incomplete po crash, a durable active ownership nie
jest automatycznie uznawane za zwolnione przez utratę procesu. Restart ładuje ten
stan i przed issue następnego runu dla source wykonuje controlled recovery: w
jednej transaction sprawdza exact ownera, one-time terminalizuje go jako
`abandoned` z rzeczywistym restart/fence reason i terminalization timestampem,
pozostawia niezaobserwowane observation i snapshot fields unset, zapisuje
completion audit i — jeśli abandonment outcome jest applicable — fail-closed
aktualizuje health/freshness oraz published revision bez resource reconciliation,
a następnie zwalnia ownership. Sequence pozostaje zużyty i nie jest reużywany.

Ten sam controlled abandon/cancel/fence contract obowiązuje dla timeoutu albo
hung workera. Dopiero po terminalnym commit i release można issue kolejny run.
Late completion starego workera nie spełnia już nonterminal-state ani exact
active-ownership checks, więc nie może ponownie finalizować recordu, reconcile
inventory, aktualizować health ani publikować transitions. Successful
reconciliation finalizuje run i zwalnia ownership w tej samej transaction co
inventory commit; failure przed reconciliation pozostawia commit-observed fields
unset. Minimalny lifecycle może używać `issued`/`running`/`completed`/`abandoned`
albo równoważnego zamkniętego kontraktu bez projektowania workflow engine.

Każdy `discovery_runs` record zapisuje przydzielony sequence. Reconciliation
commit wymaga:

```text
source.active_discovery_run_id == run.run_id
run lifecycle is nonterminal
run.discovery_run_sequence > source.last_committed_run_sequence
run.discovery_run_sequence > source_runtime_health.last_health_run_sequence
```

W tej samej transaction/CAS boundary backend reconciliuje inventory, ustawia
canonical `inventory_sources.last_committed_run_sequence` oraz niezależne
`source_runtime_health.last_health_run_sequence=run.discovery_run_sequence` i
successful outcome, one-time finalizuje run i zwalnia ownership. Exact ownership
i sequence/CAS guards odrzucają late/fenced albo stale workera nawet przy zgodnym
`source_config_revision`.

`source_config_revision` chroni znaczenie konfiguracji, a sequence porządkuje
issuance i stan committed/health. Faktyczny observation ordering zapewnia jednak
obowiązkowe single-flight: dla dwóch normalnie zakończonych runów jednego source
run N terminalizuje się i zwalnia ownership **przed** pierwszym provider I/O runu
N+1. Wyższy sequence odpowiada późniejszemu eligible observation window tylko
dlatego, że overlapping eligible runs są zabronione, a nie z właściwości samego
licznika.

Wymagane concurrency contract tests:

```text
A issued/active → B trigger → B does not begin provider I/O
A terminal commit/release → B may be issued and begin provider I/O
A hangs → controlled abandon/fence A → issue B → late A application rejected
A active → context transition → wait for A terminalization or fence A
  → new-context B begins only after release; no overlapping eligible windows
crash with A active → restart recovery abandons/fences A → A sequence remains consumed
  → issue B → stale A cannot apply state
source X active + source Y active → parallel provider I/O allowed
two same-source issuance attempts racing for free ownership
  → exactly one issues/owns; loser does not allocate or begin I/O
allocate N → crash before reconciliation → after fence next allocation > N
issue run → completion fields may remain unset
successful completion → finalizes and releases exact ownership exactly once
failed completion → finalizes, updates health and releases exact ownership atomically
second conflicting finalization → rejected
newest applicable failed finalization + health/published update → atomic all-or-nothing
context changes before failed-run transaction → finalize audit, reject health application
source A/B concurrent published commits → distinct global published_state_revision
API read during N→N+1 commit → complete N or complete N+1, never torn view
```

Cursor jest sprawdzany tylko wtedy, gdy provider jawnie deklaruje wspierany
trusted cursor contract albo używana jest klasa proof B. Snapshot niespełniający
exact active ownership lub committed/health sequence guards jest odrzucany.
Restart backendu ładuje ostatni committed inventory,
`inventory_sources.last_issued_run_sequence`, durable active-run ownership,
`source_runtime_health.last_health_run_sequence`,
`inventory_sources.last_committed_run_sequence`, tombstones oraz wszystkie
dostępne provider cursors; pamięć procesu nie jest source of truth. Osierocony
active run musi przejść controlled abandon/fence i release przed nowym issuance.

### Last committed inventory a current source health/freshness

Backend utrzymuje dwa niezależne durable outcomes:

1. **last committed inventory** — ostatni authoritative snapshot, który przeszedł
   reconciliation i może być zachowany do read-only presentation;
2. **current source observation health/freshness** — bieżący presentation i
   security state wraz z origin/reason; może pochodzić z najnowszego applicable
   completed runu, controlled context transition, time expiry albo initial state.

Canonical durable ownership jest pojedyncze:

- `inventory_sources` posiada `source_config_revision`,
  `last_issued_run_sequence`, `last_committed_run_sequence` i durable active-run
  ownership;
- `source_runtime_health` posiada completion/health provenance, current
  health/freshness/origin/reason i last-successful/deadline/context metadata;
- published source view agreguje oba recordy, ale nie tworzy drugiej
  authoritative kopii żadnego monotonic tokenu.

Conceptual source state publikuje co najmniej:

- `last_issued_run_sequence`;
- `latest_completed_run_sequence` i redacted outcome, wybierane przez najwyższy
  completed sequence niezależnie od wall-clock finish order;
- `last_health_run_sequence` jako najwyższy sequence, którego run-derived health
  outcome został kiedykolwiek skutecznie zastosowany, oraz odpowiadający
  `last_run_health_outcome`; exact context znajduje się w powiązanym run record i
  nie musi być current contextem;
- `last_committed_run_sequence`;
- `last_successful_observed_at`;
- fixed `freshness_valid_until`/`fresh_until` oraz exact committed run/context
  provenance, do których deadline należy;
- current health/freshness, np. `healthy/fresh`, `stale`,
  `source_unavailable`, `partial/degraded`, `configuration_error`, invalid
  current-context observation albo `not_yet_observed`;
- `current_health_origin` i `current_health_reason`, gdzie origin rozróżnia co
  najmniej `discovery_run(sequence)`, `controlled_context_transition`,
  `time_expiry` oraz `initial/not_yet_observed`;
- exact `source_config_revision`, endpoint/canonical locator/version i
  `transport_trust_revision`, pod którymi last inventory został committed, oraz
  bieżący source/transport context do porównania.

Nazwy finalnych API/DB enumów pozostają implementation contract, lecz
rozdzielenie tych danych jest obowiązkowe.

Pola `last_issued_run_sequence` i `last_committed_run_sequence` w published view
pochodzą z canonical `inventory_sources`; nie należą do
`source_runtime_health`. Każdy zakończony run może atomowo podnieść
completion-audit token, jeśli jego
sequence jest większy od `latest_completed_run_sequence`; to nie nadaje mu prawa
do inventory reconciliation ani current-health update. Jeśli completion token/
outcome jest publikowany, jego zmiana zwiększa w tej samej transaction
`published_state_revision`, nawet dla old-context runu bez zmiany current health.

Successful authoritative inventory run wykonuje jedną atomic transaction, która
sprawdza nonterminal run i exact active ownership, ponownie waliduje exact
current context, one-time finalizuje run i completion provenance, reconciliuje
resources, aktualizuje `last_committed_run_sequence`, successful
observation/freshness, fixed freshness deadline należący do exact run/contextu i
`last_health_run_sequence`, zwiększa `inventory_revision` i
`published_state_revision`, zwalnia ownership, po czym commit/publikuje wszystko
albo nic.

Failed, partial, unavailable albo invalid **newest applicable** run wykonuje
jedną atomic transaction obejmującą nonterminal/exact active ownership check,
one-time finalization, completion-audit max, exact applicability validation,
health CAS, `last_run_health_outcome`, current health/freshness/origin/reason,
mutation-freshness invalidation, `published_state_revision` i release ownership.
W tej transaction:

- nie wykonuje resource reconciliation;
- nie tworzy `missing`, removal ani replacement transition;
- nie zmienia resource identity/presence;
- zachowuje redacted run audit i completion provenance;
- aktualizuje health/outcome, jeśli jego sequence jest większy od
  `last_health_run_sequence`;
- publikuje/wyprowadza stale/degraded source state razem z zachowanym last
  committed inventory;
- wszystko commit albo nic commit — crash nie może pozostawić finalized failed
  runu przy nadal fresh source health ani przy nadal active ownership.

Health update używa CAS:

```text
run.discovery_run_sequence > source_runtime_health.last_health_run_sequence
```

W tej samej transaction backend ponownie sprawdza:

```text
run.expected_source_config_revision == current source_config_revision
run.expected_endpoint_id == exact current active endpoint_id
run.expected_canonical_locator/version == current canonical locator/version
run.expected_transport_trust_revision == current transport_trust_revision
source.active_discovery_run_id == run.run_id
run lifecycle is nonterminal
```

To jest applicability/ownership check; wcześniejszy check przed transaction nie
wystarcza i nie może tworzyć TOCTOU window. Run ze starym contextem jest
auditowalny: jeśli nadal jest active ownerem, może zostać finalized, podnieść
completion-audit provenance i atomowo zwolnić ownership, lecz nie może zmienić
current health nowego contextu. Jeśli został wcześniej controlled fenced,
late worker nie może wykonać drugiej finalization ani żadnej state application.

Controlled source config/active route/canonicalization/TLS trust transition
wykonuje jedną atomic transaction, która zmienia odpowiedni context/revision,
ustawia current health jako stale, origin `controlled_context_transition` i
jawny reason, ustawia mutation freshness na false oraz zwiększa
`published_state_revision`. Transaction serializuje się z issuance, successful
reconciliation oraz failed-run health application. Przy active runie
implementacja wybiera jedną z dwóch fail-closed ścieżek: czeka na jego terminal
finalization/release albo atomowo terminalizuje go jako
cancelled/abandoned/invalid z rzeczywistym reason, fences worker i zwalnia
ownership. Dopiero wtedy może issue run nowego contextu; eligible observation
windows nie mogą się nakładać. Sama context transition bez fencing nie zmienia
run-derived provenance. Jeżeli ta sama transaction terminalizuje active run,
może zgodnie z one-time-finalization contract zaktualizować prawdziwy completion
audit i published revision, lecz nie tworzy fake observation ani successful
run-health outcome; current health origin pozostaje
`controlled_context_transition`. Żadna ścieżka nie cofa sequence. Run-derived
health pola zachowują provenance ostatniego zastosowanego runu, również gdy
należał do poprzedniego contextu. Current source of truth to current
health/origin/reason wraz z current context. Poprzedni inventory jest not
mutation-fresh do nowego udanego commitu; po nim current origin staje się
`discovery_run(sequence)`.

Initial source creation atomowo tworzy source, dokładnie jeden initial active
endpoint oraz wymagany `source_runtime_health` record z
`current_health_origin=initial/not_yet_observed`, jawnie non-fresh health i
`last_successful_observed_at=null`, unset freshness deadline provenance, a active
discovery ownership jest unset;
`last_committed_run_sequence` pozostaje null
albo używa jednoznacznego initial sentinel. Ta sama transaction zwiększa
`published_state_revision`, ponieważ source pojawia się w API. Nie ma effective
destructive capabilities. Initial health ani późniejsza controlled context
transition nie resetują monotonic sequences.

Dla prostych fail-closed semantics successful inventory commit także wymaga, aby
nie istniał wyższy `last_health_run_sequence`. W Phase 1 normalne runy jednego
source są uporządkowane przez ownership:

```text
A seq10 issued/active → A provider I/O → A terminal commit/release
→ dopiero teraz B seq11 może zostać issued i rozpocząć provider I/O

A seq10 fenced/abandoned → B seq11 issued/active
→ late worker A nie spełnia ownership/nonterminal checks i nie zmienia state
```

Normalny `A terminal commit/release → B issue/fetch/commit` pozostaje dozwolony.
Completion time ani wall clock nie są observation-ordering authority; sam wyższy
issuance sequence nie uprawnia do commit, jeśli exact ownership nie obowiązuje.

Każdy newest applicable run, który nie kończy się authoritative successful
inventory commit i podważa current-state confidence, ustawia retained inventory
jako not mutation-fresh. Obejmuje to `source_unavailable`, partial/degraded,
`configuration_error` oraz invalid current-context run, np. schema violation,
duplicate locator/node lub boundary mismatch. Audit-only invalid run ze starym
contextem nie jest applicable i nie nadpisuje current health. Freshness nie jest
wiązane z przypadkową nazwą enumu: istnieje tylko po authoritative successful
applicable commit i do najwcześniejszego z fixed deadline, controlled context
transition albo nowszego applicable health outcome unieważniającego confidence.

### Freshness deadline i revisioned expiry

Authoritative successful applicable commit wylicza według jawnego
freshness-duration contract i atomowo utrwala fixed
`last_successful_observed_at`, `freshness_valid_until` oraz exact committed
run/source/endpoint/canonicalization/transport-trust provenance. Deadline należy
do tej revision i nie przesuwa się przy API read. Nowy successful applicable run
może ustanowić nowy deadline. Exact duration/TTL pozostaje późniejszym explicit
configuration/operation contract, ale Phase 1 nie może reprezentować fresh jako
bezterminowego. Zmiana configured duration jest controlled discovery-relevant
configuration transition zwiększającą `source_config_revision`; nie interpretuje
ponownie ani nie wydłuża deadline istniejącego commitu.

Po przekroczeniu current deadline backend wykonuje controlled transition:

```text
current health: healthy/fresh → stale/non-fresh
current_health_origin = time_expiry
current_health_reason = freshness_deadline_elapsed
mutation freshness = false
freshness-dependent destructive/maintenance effective capabilities = none/ineligible
resource inventory/presence/identity unchanged
inventory_revision unchanged
published_state_revision = next globally allocated revision
```

Od chwili deadline poprzednia revision jest wyłącznie historycznym committed
view z fixed facts; nie może być traktowana ani zwrócona jako authoritative
current fresh state. Jeżeli timer nie zmaterializował jeszcze expiry, pierwsza
granica publikacji lub użycia musi wykonać transition przed zwróceniem/oceną
state.

Expiry transaction nie wykonuje reconciliation, missing, removal ani replacement
transition i nie zmienia retained policy/history. Ponownie wyprowadza effective
capabilities i zapisuje health, reason/origin, capability result oraz globalny
published token atomowo. Powrót stale → fresh jest dozwolony wyłącznie przez nowy
authoritative successful applicable inventory commit z nowym deadline i nową
published revision. Ponowny read, retry timera ani cofnięcie zegara nie odwracają
committed expiry.

Expiry jest CAS/fenced względem exact current freshness provenance. W tej samej
transaction backend sprawdza co najmniej:

```text
expected last_committed_run_sequence == current last_committed_run_sequence
expected freshness_valid_until == current freshness_valid_until
expected committed source_config_revision == current/committed source_config_revision
expected committed endpoint/canonical locator/version == current/committed route contract
expected committed transport_trust_revision == current/committed transport trust
current health is still fresh for that exact provenance
current time is at or beyond the fixed deadline
```

Jeżeli run N+1 ustanowił już nowy deadline albo context się zmienił, spóźniony
timer runu N jest no-op. Expiry serializuje się z successful/failed run
finalization, single-flight ownership transitions i controlled source/transport
context transitions. Nie jest discovery runem i nie tworzy fake run sequence ani
observation.

Timer/expiry worker może przyspieszać notification, ale nie jest correctness
boundary. Przed zwróceniem authoritative published source freshness lub effective
capabilities backend sprawdza deadline; jeśli już minął, najpierw atomowo commit
expiry, a dopiero potem składa consistent published view. Każda przyszła
destructive/maintenance eligibility decision wykonuje niezależnie ten sam
deadline check we własnej fail-closed decision boundary — nie polega na tym, że
poller, timer albo wcześniejszy HA/API read wykonał transition. Jeśli expiry
commit nie może zostać wykonany, API nie zwraca state jako authoritative fresh,
a mutation eligibility odmawia operacji.

Przykład:

```text
22:00 successful commit, fresh_until=22:05
poller i timer zatrzymują się
22:30 API read albo mutation eligibility check
→ najpierw atomic stale/time_expiry transition + published_state_revision++
→ dopiero potem zwróć/wykorzystaj non-fresh state
```

Wall-clock-derived display age nie jest security authority. Gdy anomalia czasu
uniemożliwia bezpieczną ocenę deadline, backend nie może przedłużyć mutation
freshness: stosuje fail-closed non-fresh transition/decision według późniejszego
clock implementation contract. Raz committed expiry nie jest cofane przez
backward clock change.

Wymagane freshness/expiry contract tests:

```text
successful run → fixed last_successful_observed_at + fresh_until → fresh
deadline passes without a new run → atomic stale/time_expiry transition
  → published_state_revision increases; inventory_revision unchanged
poller/timer stalls → later API read commits expiry before returning authoritative view
poller/timer stalls → direct mutation eligibility check independently enforces expiry
run10 timer fires after run11 successful/new deadline → run10 expiry CAS is no-op
time expiry → no missing/replacement/removal; identity unchanged
  → freshness-dependent capabilities become none/ineligible
read published_state_revision N twice → identical fixed timestamps/deadline
  → no canonical backend age changes within N
new successful applicable run after expiry → fresh only with new deadline
  + new published_state_revision
```

Każda committed zmiana dowolnego API-visible pola ma monotoniczny
published-state change token. Inventory reconciliation zwiększa
`inventory_revision` i `published_state_revision`; health/completion-only CAS
albo controlled context transition zwiększa `published_state_revision` bez
wymuszania nowego `inventory_revision`. Dotyczy to m.in. zmian
`latest_completed_run_sequence/outcome`, `last_health_run_sequence/outcome`,
fixed last-success/deadline timestamps, health reason/origin,
current/committed source context, initial/time-expiry/context-transition state,
resource/node/policy i derived capabilities.

Samo porównanie health enum jest niewystarczające: `seq11 source_unavailable →
seq12 source_unavailable` zmienia published provenance i musi zwiększyć
`published_state_revision`. Old-context run podnoszący tylko publikowany
completion-audit max również zwiększa token. `inventory_revision` i
`published_state_revision` są durable, atomic, strictly increasing i never
reused; globalna alokacja jest serializowana, więc concurrent source A/B commits
nie mogą otrzymać tego samego resulting published revision. Revision update i
publikowane dane należą zawsze do tej samej atomic transaction.

Dzięki temu HA/cache/push-refresh widzi `healthy → source_unavailable/stale`
mimo identycznego `resources[]`, także przy time expiry bez nowego discovery
runu. Globalny published timestamp nie zastępuje per-source
`last_successful_observed_at`, fixed `freshness_valid_until`, run sequence ani
committed context provenance.
Canonical global ownerem obu published-view tokens jest `backend_instance`;
source i health records nie przechowują niezależnych authoritative kopii.

Revisioned published source view zawiera fixed timestamps/deadline i committed
freshness state/origin/reason. Nie zawiera canonical wall-clock-mutating
`inventory_freshness/age`. HA/UI może wyliczyć display-only age jako
`now - last_successful_observed_at`, lecz ta wartość nie należy do equality/
immutability contract `published_state_revision=N`, nie jest security authority
i nie uczestniczy w mutation/capability decision. Dwa odczyty revision N zwracają
identyczne fixed freshness facts. Opcjonalny dynamiczny age z backendu musiałby
być jawnie non-revisioned presentation metadata poza tym snapshot contract;
preferowany kontrakt to fixed timestamps i client-side age.

`published_state_revision=N` identyfikuje dokładnie jeden logicznie spójny
committed backend view. API assembly musi używać consistent DB read snapshot/
transaction albo równoważnej granicy i zwracać revision N wraz ze wszystkimi
`sources[]`, `nodes[]`, `resources[]`, policy i derived capabilities ze stanu N.
Torn view — revision N, część danych N i część po concurrent commit N+1 — jest
zabroniony. Consumer dostaje kompletne N albo kompletne N+1. To nie definiuje
HTTP caching protocol.

Published API rozróżnia osiągalność Hubinet backendu od freshness każdego
Proxmox source. Przy `source_unavailable`/degraded/configuration error albo
applicable invalid current-context observation, a także po `time_expiry`:

- HA nie usuwa devices ani nie wyprowadza false resource `missing`;
- last-known read-only facts mogą pozostać jako stale/historyczne;
- encje zależne od current facts są unavailable lub jawnie stale zgodnie z
  source-health overlay;
- resource presence i identity pozostają stanem last committed inventory.

```text
Hubinet backend reachable != Proxmox source inventory fresh
source unavailable != resource missing
```

## Failure modes

| Zdarzenie | Klasyfikacja | Wpływ na poprzedni inventory |
| --- | --- | --- |
| active endpoint timeout/unavailable | `source_unavailable` | zachowaj inventory, bez missing/removal; jeśli newest applicable sequence, CAS-update source health/freshness; nie próbuj candidate endpointu |
| candidate endpoint osiągalny | nie uczestniczy w run | brak automatic failover do czasu accepted source-binding contract |
| operator żąda nowego URL dla existing source | utwórz inert `candidate` z nowym `endpoint_id` | active record/URL bez zmian; activation disabled bez source-binding proof |
| active endpoint retired albo source disable/re-enable | discovery disabled/last-known zachowane | gate nie resetuje się; brak direct replacement |
| TLS trust/pinning zmienione | audytowana transport-security revalidation | nie ustanawia source continuity ani nie aktywuje innego peer/source |
| `transport_trust_revision` zmienione podczas runu | `invalid` | nie commituj snapshotu odczytanego pod starym trust contract; old inventory pozostaje last-known, nie mutation-fresh |
| `source_config_revision` zmienione podczas runu | `invalid`/stale | bez reconciliation/commit; old inventory pozostaje last-known, nie mutation-fresh; następny run używa nowej revision |
| canonicalization version migration nieukończona, ambiguous lub collision | `configuration_error`/blocked | bez create/reactivation aliasu i bez commit runu o niezweryfikowanym endpoint provenance |
| wrong snapshot source ID, unsupported resource type albo duplicate locator/node | `invalid` | bez reconciliation/publish; LXC101+QEMU101 w jednym snapshotcie nie jest replacement evidence |
| brak node scope wymaganego do locator baseline | `baseline_completeness=partial` | bez `missing` transitions |
| locator obecny, optional node/detail facts niedostępne | baseline bez zmian + `node_availability=unavailable`/detail error | locator zachowany jako `present` |
| baseline pełny, config read jednego guest fail | `baseline_completeness=complete`; per-resource `detail_status=temporarily_unavailable/error` | locator `present`; facts unavailable; inne locatory reconciled normalnie |
| reconciled resource jest `missing`, `confirmed_removed` albo `not_current` | `detail_status=not_applicable` | brak current detail read; retained facts mogą pozostać stale/historyczne, nie jest to error |
| `GET /access/acl` niedostępne, ograniczone lub niejednoznaczne | `baseline_completeness=partial`/`configuration_error` | bez `missing`/removal transitions |
| upstream per-path effective evaluation dla path z topology nie spełnia discovery contract | `configuration_error` | bez absence/removal transitions |
| topology hash BEFORE ≠ AFTER | `invalid` | odrzuć run; bez absence/removal transitions |
| effective permission proof nie pokrywa całego `/vms`/`/nodes` contract | `baseline_completeness=partial`/`configuration_error` | bez `missing`/removal transitions |
| permission hash BEFORE ≠ AFTER | `invalid` | odrzuć run; bez absence/removal transitions |
| token ma tylko per-VM/per-pool visibility | `configuration_error` | read-only partial view może być diagnostyczny, ale nie authoritative inventory |
| boundary-complete baseline bez locatora, także potencjalne ACL ABA | observational `missing`/`uncertain` | zachowaj existing `resource_id`, active binding i generation; nie `confirmed_removed`, nie identity split |
| locator wraca po `missing`/outage, continuity ambiguous, bez replacement/removal proof | `present` + `uncertain`/`quarantined` | zachowaj existing `resource_id`, active binding i generation; fail-closed authority; bez provisional ID/tombstone |
| boundary-complete baseline + proof klasy A, B albo C, bez authoritative absence evidence | `baseline_completeness=complete` | najwyżej `missing`/`uncertain`; bez zamknięcia incarnation |
| proof klasy A, B albo C + accepted authoritative absence evidence | zgodnie z kontraktem proof | `confirmed_removed`, tombstone |
| nonterminal old active binding + bieżący successor + positive replacement evidence | direct replacement | atomowo `not_current`/`retired`/`replaced` old, nowy `resource_id` i generation; old może wcześniej być present albo missing/quarantined |
| A issued/active, B trigger dla tego samego source | single-flight owner A | B nie alokuje overlapping eligible runu ani nie rozpoczyna provider I/O; może być queued/coalesced/rejected |
| A terminal commit/release, następnie B trigger | sequential eligible runs | B może zostać issued i rozpocząć fetch dopiero po release A |
| A hangs, potem controlled abandon/fence i issue B | A terminal `abandoned`, B active | sequence A zużyty; late A nie może finalizować, reconcile, aktualizować health ani publikować |
| source X active i source Y active | niezależne ownership | równoległe provider I/O dozwolone dla różnych sources |
| issue run, przed fetch | `issued` record | completion fields pozostają unset; issuance nie wymaga fake outcome/timestamps/hash |
| successful completion exact active ownera | jedna atomic transaction | ownership check + finalization + completion provenance + reconciliation + commit/health provenance + oba global revisions + release; wszystko albo nic |
| newest applicable failed completion exact active ownera | jedna atomic transaction bez resource reconciliation | ownership check + finalization + completion max + exact applicability + health CAS/outcome/state + freshness invalidation + published revision + release; wszystko albo nic |
| second conflicting finalization | rejected | immutable completion audit i brak jakiejkolwiek health/publish mutation |
| allocate N, crash przed fetch/reconciliation | incomplete issued active run | restart atomowo abandons/fences N i zwalnia ownership; następny issued sequence jest `> N`; brak reuse i fake observation data |
| newest applicable partial/unavailable/invalid run | degraded source health | CAS-update health/outcome, bez resource reconciliation/identity/presence transitions; retained inventory pozostaje last-known |
| context zmienia się przy active A | wait albo controlled fence A | B nowego contextu nie zaczyna I/O przed terminal release A; late fenced A nie może apply state |
| context zmienia się między pre-check a failed-run transaction | old-context run nieapplicable | finalizacja/completion audit i release dozwolone tylko dla exact active ownera; transaction revalidation blokuje current health overwrite |
| controlled source/transport context transition po healthy seq10 | current health stale, origin `controlled_context_transition` | jedna transaction zmienia context, freshness i published revision; last run outcome/sequence pozostają provenance seq10 |
| fixed freshness deadline mija bez nowego runu | `stale`, origin `time_expiry` | CAS-fenced health/capability transition; bez resource reconciliation, `inventory_revision` bez zmian, `published_state_revision++` |
| timer starego run10 odpala po successful run11 | stale expiry provenance mismatch | no-op; nie starzeje nowego deadline ani capabilities |
| poller/timer zatrzymany, późniejszy API read lub mutation check | deadline guard | najpierw commit expiry, potem zwróć/użyj non-fresh state; brak optimistic fallback |
| seq11 i seq12 oba `source_unavailable` | ten sam health enum, inne provenance | oba applicable published updates zwiększają unique `published_state_revision` |
| old-context run podnosi tylko `latest_completed_run_sequence` | current health bez zmian | published completion field zmienia się, więc ta sama transaction zwiększa `published_state_revision` |
| concurrent source A/B published commits | dwie globalne revisions | resulting `published_state_revision` muszą być różne i strictly ordered |
| API read przecina concurrent N→N+1 commit | consistent read snapshot | zwróć kompletne N albo kompletne N+1; torn view odrzucony |
| initial source creation | `not_yet_observed`/non-fresh | atomowo source + initial active endpoint + health record + published revision; destructive capabilities `none` |

## Trust boundary

Provider jest read-only nawet wtedy, gdy token przez błąd ma szersze ACL. Kod
transportu musi odrzucać metody inne niż `GET` i mieć zamknięty zestaw ścieżek.
Discovery nie ma dostępu do typed host-control ani forced-command. Późniejsze
mutacje nadal przechodzą:

```text
HA → Hubinet Ops API → backend policy → plans/jobs/locks/audit
   → typed host-control → hostd/forced-command → Proxmox
```

### Source freshness jest przyszłym mutation gate

Retained trusted `resource_id`, applicable policy i osiągalność samego Hubinet
backendu nie wystarczają do destructive/maintenance operation. Effective
capabilities wymagają sufficiently fresh committed inventory:

- committed pod bieżącym `source_config_revision`;
- związany z nadal exact active endpointem, canonicalization contract i
  `transport_trust_revision`;
- current time jest bezpiecznie oceniony jako wcześniejszy niż fixed
  `freshness_valid_until` exact committed run/contextu;
- bez nowszego applicable non-authoritative outcome podważającego current-state
  assumptions, w tym `source_unavailable`, partial/degraded,
  `configuration_error` albo invalid current-context run;
- mieszczący się także w jawnym, operation-specific freshness requirement.

Zmiana source configuration/transport trust, expiry deadline lub każdy nowszy
applicable current source outcome podważający confidence zachowuje last-known
presentation inventory, ale odbiera mu status mutation-fresh. Mutation path
sprawdza deadline samodzielnie i w razie expiry najpierw wykonuje fenced stale
transition; nie może polegać na pollerze, timerze ani wcześniejszym API read. Nie
ma optimistic fallback. Dokładna source freshness duration i dodatkowy
operation-specific TTL pozostają przyszłym implementation/operation contract,
lecz muszą być explicit i fail-closed przed włączeniem destructive operations.
Użycie silniejszej niezależnej live host/source-side attestation podczas stale
discovery wymaga osobnego accepted operation-specific security contract.

### Node routing jest osobną granicą

Discovery może ustalić current node i HA `via_device`, ale nie ustanawia mutation
route. Przed typed host-control backend musi rozwiązać current node do aktywnego
`node_binding_id`, sprawdzić expected binding revision, ważną attestation,
`node_trust_state=trusted` oraz executor/host policy readiness. Node name jest
wyłącznie external locator i nie może być samodzielnym routing credential.

Po remove/rejoin, reinstall, nieoczekiwanej zmianie hostd identity lub nowym
hoście pod starą nazwą binding staje się `unverified`/`revoked`. Migracja
workloadu do takiego node'a może być pokazana read-only, ale jego effective
destructive capabilities spadają do `none`. Przyszły, jawnie zaakceptowany
endpoint failover discovery nie może przenosić hostd attestation ani przywracać
mutation trust.

Semantyka presentation node relation i availability jest zdefiniowana w
[macierzy wyżej](#node-relation-i-ha-availability). W szczególności
`node_availability=unavailable` nie zmienia locator presence, a
`last_known_node_id` nie może zostać przekazane jako host route.

## Nierozstrzygnięte kwestie

1. Exact supported PVE versions i contract test standalone `/cluster/resources`.
2. Wybór `proxmoxer` vs wąski async HTTP po security/maintenance review.
3. Minimalna endpoint/ACL matrix dla dodatkowych continuity evidence.
4. Dostępność, pagination i retencja task/event history jako evidence —
   **UNKNOWN** jako niezawodny stream.
5. Monotonic ACL/config revision, transactional snapshot lub inny interval-wide
   consistency/absence proof — **UNKNOWN** dla stockowego polling API.
6. Mechanizm source binding/attestation bez natywnego immutable cluster UUID.
   Dopóki nie zostanie zaakceptowany, tylko initial source creation może nadać
   status active; późniejsza aktywacja/replacement pozostają wyłączone, dokładnie
   jeden endpoint jest active, a automatic failover pozostaje wyłączony.
7. Finalny workload continuity proof/enrollment anchor. Dopóki nie zostanie
   zaakceptowany, trusted destructive capabilities są globalnie niedostępne;
   nie blokuje to przyszłego read-only discovery/inventory.
8. Finalny node/hostd attestation protocol i procedura jawnej key rotation.

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
z `Sys.Audit` na `/access`; bez tego zwraca ograniczony widok. Parametr `path`
endpointu `GET /access/permissions` zleca Proxmox obliczenie effective
permissions dla konkretnego principal/path.

W użytych oficjalnych endpointach i dokumentacji nie zweryfikowano monotonic
ACL/config revision ani cursor gwarantującego interval-wide consistency. Ten
brak pozostaje **UNKNOWN**, a nie negatywnym twierdzeniem o wszystkich wersjach
Proxmox.

Pozostałe reguły completeness, reconciliation i failover są decyzjami
architektonicznymi Hubinet Ops, nie obietnicami Proxmox API.
