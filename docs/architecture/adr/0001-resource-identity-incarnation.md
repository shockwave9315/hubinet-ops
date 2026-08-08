# ADR 0001: identity zasobu i incarnation

Status: **PROPOSED**

Data: 2026-08-08

## Kontekst i decyzja w skrócie

Proxmox VE nie udostępnia jednego, oficjalnie udokumentowanego, niezmiennego
identyfikatora workloadu wspólnego dla QEMU i LXC. `vmid` identyfikuje zajęty
slot w obrębie klastra, lecz może zostać użyty ponownie. Nazwa i node są
zmienne. `digest`, `vmgenid`, znaczniki czasu i pozostałe pola konfiguracyjne nie
spełniają wymagań trwałej identity.

Proponujemy model D, będący uszczegółowieniem modelu C:

```text
backend_instance_id             instalacja Hubinet Ops
inventory_source_id             jawnie skonfigurowane środowisko Proxmox
resource_id                     trwała backendowa inventory/candidate identity
slot locator                    (inventory_source_id, vmid)
resource_type                   immutable property occupant incarnation
locator_generation              kolejny backendowy binding tego samego slotu
continuity_state + evidence     ocena, czy obserwacja należy do incarnation
```

`resource_id` jest losowym, nieprzezroczystym UUID nadanym przez backend. Jest
trwałą identity rekordu inventory/candidate incarnation, a nie „logicznej
usługi”, i nie zależy od nazwy, VMID ani node'a. Dla resource `unverified` nie
jest jednak dowodem, że fizyczna incarnation nie została niewidocznie
zastąpiona między pollingami. `locator_generation` porządkuje historię
wykorzystania slotu, ale sam nie dowodzi replacement. HA może używać
`resource_id` do read-only continuity; destructive authority wymaga dodatkowo
zaakceptowanego security continuity proof.

## Terminologia i granice

- **backend** — konkretna instalacja Hubinet Ops 0.5 i jej nowa baza danych;
- **source** — jeden operatorowo skonfigurowany cluster Proxmox VE albo jeden
  standalone node;
- **slot locator** — cluster-wide slot `(inventory_source_id, vmid)`;
- **occupant type** — immutable dla incarnation `resource_type` (`qemu` albo
  `lxc`), ale nie namespace slotu;
- **resource/incarnation** — jeden workload od utworzenia lub jawnego uznania
  ciągłości do potwierdzonego replacement;
- **observation** — read-only fakty z jednego discovery run;
- **continuity** — backendowa ocena, czy bieżące fakty odnoszą się do tej samej
  incarnation;
- **presence** — osobny stan obecności locatora; nie jest detail status,
  node availability ani poziomem policy.

Node nie jest częścią locatora workloadu. Migracja zmienia bieżącą relację z
node'em, nie identity workloadu.

## Audyt kandydatów Proxmox

Legenda:

- **FACT-DOC** — udokumentowane przez Proxmox;
- **FACT-SOURCE** — zachowanie widoczne w oficjalnym source Proxmox;
- **INFERENCE** — wniosek architektoniczny z faktów;
- **UNKNOWN** — właściwości nie potwierdzono oficjalnym kontraktem.

| Kandydat | QEMU | LXC | Ocena właściwości |
| --- | --- | --- | --- |
| `vmid` / CTID | tak | tak | **FACT-DOC:** wspólny, unikalny cluster-wide numer bieżącego guest. Przeżywa rename i migrację, lecz slot może zostać zwolniony i użyty ponownie. Clone otrzymuje nowy VMID. Nie jest immutable workload ID. |
| nazwa | tak | tak | Konfiguracja edytowalna; rename nie może zmieniać identity. Nie jest identity. |
| bieżący node | tak | tak | **FACT-DOC:** workloady mogą migrować między node'ami. To lokalizacja/relacja, nie identity. |
| `vmgenid` | tak | nie | **FACT-SOURCE:** wartość jest konfigurowalna (`1` generuje, `0` wyłącza), regenerowana przy clone, snapshot rollback i restore. Nie jest immutable ani cross-type. |
| config `digest` | tak | tak | **FACT-SOURCE/FACT-DOC:** hash bieżącej konfiguracji wykorzystywany do ochrony przed równoczesną zmianą. Zmienia się przy config edit, może wrócić do poprzedniej wartości, nie dowodzi stworzenia workloadu. |
| QEMU `meta.ctime` | tak | niepotwierdzone/nie | **FACT-SOURCE:** QEMU zapisuje creation metadata. Aktualny kod clone kopiuje konfigurację i jawnie regeneruje `smbios1.uuid`/`vmgenid`, ale nie pokazuje równoważnej gwarancji nowego `meta.ctime`. LXC config nie eksponuje odpowiednika. Nie jest bezpiecznym cross-type ID. Dokładne zachowanie wszystkich ścieżek restore: **UNKNOWN**. |
| `smbios1.uuid` | tak | nie | **FACT-SOURCE:** QEMU clone regeneruje UUID, ale pole jest elementem konfiguracji i nie istnieje dla LXC. Zachowanie wszystkich restore/config edit jest **UNKNOWN** jako kontrakt identity. Nie używamy. |
| cluster resource `id` | tak | tak | **FACT-SOURCE:** `/cluster/resources` zwraca syntetyczny resource ID oraz `type`, `vmid`, `node`; source buduje dane z listy VM. Brak dokumentowanej gwarancji immutable incarnation. To reprezentacja locatora. |
| tag, description, MAC, disk identity | zależnie | zależnie | Dane konfiguracyjne, kopiowalne lub edytowalne. Mogą być evidence/hint, nigdy samodzielnym identity. |
| snapshot timestamp (`snaptime`) | tak | tak | Dotyczy snapshotu, nie utworzenia workloadu. Nie jest identity. |

### Macierz wymaganych właściwości

| Pole | Immutable | Rename | Migrate | Config edit | Snapshot restore | Clone | Destroy/recreate | Ręczna zmiana | Oficjalny kontrakt identity |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `vmid` | nie | przeżywa | przeżywa | przeżywa | zwykle przeżywa; restore może wybrać ID | nowe ID | może zostać użyty ponownie | operator może zmienić przez migrację/restore do innego ID | nie |
| `vmgenid` | nie | **UNKNOWN** | **UNKNOWN** | możliwa | zmienia się | zmienia się | zwykle nowy, lecz wartość jest konfigurowalna | tak/disable | nie |
| `digest` | nie | zmienia się, jeśli nazwa jest w config | zwykle zależy od treści config; pełny kontrakt **UNKNOWN** | zmienia się | może odtworzyć starą wartość | może być podobny | może być identyczny | pośrednio przez config | nie |
| `meta.ctime` | nieudowodnione | prawdopodobnie przeżywa; **UNKNOWN** jako gwarancja | prawdopodobnie przeżywa; **UNKNOWN** | read-only w API, lecz semantyka wszystkich ścieżek **UNKNOWN** | **UNKNOWN** | brak gwarancji nowej wartości w przeanalizowanej ścieżce | zwykle nowe, ale bez cross-type gwarancji | **UNKNOWN** | nie |

Wniosek architektoniczny: żadne pole nie daje pozytywnego dowodu ciągłości dla
obu typów. Dopasowanie nazwy, `digest` albo fingerprintu konfiguracji oznacza
co najwyżej podobieństwo.

### VMID jest wspólnym slotem QEMU/LXC

**FACT-DOC:** dokumentacja Proxmox nazywa VM i LXC łącznie virtual guests i
stwierdza, że identyfikuje je unikalny numeryczny VMID. Dokumentacja `pct`
dodaje, że CTID musi być unique cluster wide. `pmxcfs` utrzymuje konfiguracje
QEMU i LXC w osobnych katalogach typu, ale jego consistency checks gwarantują
unikalność VMID, nie pary `(type, vmid)`.

**FACT-SOURCE:** `/cluster/nextid` pobiera jedną wspólną `get_vmlist()` i uznaje
numer za zajęty, jeśli występuje w `ids`, niezależnie od `qemu`/`lxc`.
`/cluster/resources` iteruje tę samą listę, a typ opisuje occupant.

Dlatego slot locator to wyłącznie `(inventory_source_id, vmid)`. Schema nie może
mieć równocześnie aktywnych bindingów LXC101 i QEMU101 w jednym source. Zmiana
typu pod tym samym numerem oznacza replacement occupant tego samego slotu,
nowy `resource_id` i nową `locator_generation`.

### Node i source identity

`/cluster/status` zwraca dla klastra `id="cluster"`, a dla node'a syntetyczne
`id="node/<name>"`; `nodeid` jest numerem z konfiguracji Corosync. Dla standalone
source ten sam oficjalny kod tworzy lokalny wpis z `nodeid=0`. Są to pola statusu
i locator facts, nie globalne immutable UUID.

Proxmox dokumentuje, że hostname powinien być finalny przed utworzeniem klastra
i że cluster name nie może być później zmieniony. Jednocześnie wymaga unikalnej
nazwy klastra tylko dla wielu klastrów w tym samym physical/logical network, a
standalone nie ma Corosync cluster identity. Nie znaleziono oficjalnego
kontraktu globalnie unikalnego, immutable source UUID ani node UUID.

| Obiekt | Kandydat Proxmox | Ocena |
| --- | --- | --- |
| node w cluster | name, Corosync `nodeid`, `node/<name>` | stabilne operational locators w obrębie bieżącej konfiguracji; globalna unikalność, reinstall i remove/rejoin continuity: **UNKNOWN**; nie używamy jako internal identity |
| standalone node | hostname, `node/<name>`, `nodeid=0` | brak odrębnego immutable ID; hostname/certyfikat mogą zmienić się przy reinstall; backend nadaje `node_id` |
| cluster | cluster name, stałe API `id="cluster"`, membership/config version | nazwa jest lokalnie stabilna, lecz nie globalnym UUID; `id` jest stałym literalem, version jest zmienna |
| standalone environment | endpoint, hostname, TLS certificate | wszystkie są zmiennymi transport/facts; brak udokumentowanego environment UUID |

### Node presentation identity a mutation trust

Oficjalna dokumentacja wymaga finalnego hostname przed utworzeniem klastra i
nie wspiera zwykłego rename po jego utworzeniu. Jednocześnie opisuje remove,
reinstall i rejoin node'a pod tym samym hostname/IP oraz aktualizację starego SSH
fingerprintu. Jest to pozytywny dowód, że sama nazwa nie może zachowywać mutation
trust przez taki lifecycle.

Minimalny fail-closed model rozdziela:

- `node_id` — backendową presentation identity używaną w inventory i HA;
- `node_binding_id` + monotonic `binding_revision` — jedną wersję związania
  `node_id` z obserwowaną nazwą/member record;
- `node_attestation` — jawnie zweryfikowane security evidence dokładnego hosta i
  hostd/forced-command endpointu;
- `node_trust_state` — `unverified`, `trusted` albo `revoked`.

Nie przesądzamy pełnego resource-style node incarnation. `node_id` może pozostać
ten sam dla presentation po jawnej decyzji operatora, ale każda security-relevant
zmiana zamyka albo zwiększa revision bindingu i zeruje mutation trust.

| Scenariusz | Presentation | Mutation trust |
| --- | --- | --- |
| rename node | W cluster nie jest normalnie wspierany; standalone/wyjątek traktowany jako nowy candidate binding. | Brak automatycznej continuity; wymagane ponowne binding/attestation. |
| remove/rejoin | Stary `node_id` może pozostać historyczny; rejoin może być pokazany jako candidate tego samego lub nowego node'a. | Stary binding revoked; nowy `node_binding_id`/revision zaczyna `unverified`. |
| reinstall pod tą samą nazwą | Nazwa może być taka sama, ale nie dowodzi host continuity. | Nigdy nie dziedziczy trust; ponowna attestation obowiązkowa. |
| nowy fizyczny node ze starą nazwą | Osobna lub nierozstrzygnięta presentation identity do decyzji operatora. | Nowy untrusted binding; stara attestation nie pasuje. |
| TLS certificate/key rotation | Endpoint facts aktualizują się dopiero po poprawnej weryfikacji TLS policy. | Nie przenosi ani nie nadaje hostd trust; nieoczekiwany fingerprint fail-closed. |
| hostd reinstall/key change | HA presentation może pozostać. | Attestation mismatch/revocation; binding traci `trusted` do re-enrollment. |
| discovery endpoint failover | Dotyczy source transport, nie node identity. | Endpoint musi zostać związany z tym samym source; nie może sam wybrać host mutation route. |
| migracja resource do untrusted node | `via_device` może wskazać bieżący discovered node. | Effective destructive capabilities dla resource stają się `none`; żadna mutacja nie jest routowana. |

Przyszły job zapisuje expected `resource_id`, resource binding/revision,
`node_binding_id`, node binding revision i `attestation_id`. Bezpośrednio przed
wywołaniem typed host-control backend ponownie sprawdza wszystkie wartości,
bieżącą lokalizację workloadu oraz `node_trust_state=trusted`. HA nie podaje node
route. Routing wyłącznie po external node name jest zabroniony.

## Porównanie modeli

### A. `(instance_id, resource_type, vmid)`

- bezpieczeństwo: nieakceptowalne; VMID reuse daje false continuity;
- HA: proste i stabilne przy migracji, lecz nowy workload odziedziczy Device
  Registry identifier i entity `unique_id`;
- policy: niebezpiecznie wiąże pozwolenie ze slotem;
- multi-node: działa, jeśli node nie jest w identity;
- wiele Proxmoxów: możliwe tylko wtedy, gdy niejednoznaczne `instance_id`
  faktycznie oznacza source;
- reinstall i utrata obserwacji: brak jawnej semantyki;
- DB: najprostsza, ale prostota ukrywa krytyczne ryzyko.
- dodatkowo błędnie modeluje `resource_type` jako namespace, mimo że Proxmox
  traktuje VMID jako wspólny slot QEMU/LXC.

### B. `(instance_id, resource_type, vmid, generation)`

- bezpieczeństwo: lepsze po wykrytym replacement;
- VMID reuse: bezpieczne tylko wtedy, gdy system wie, kiedy zwiększyć
  `generation`;
- delete/recreate między pollingami lub podczas outage: nadal może dać false
  continuity, więc licznik nie rozwiązuje problemu dowodu;
- HA/policy: bezpieczne po poprawnym inkrementowaniu; niebezpieczne przy
  pominiętym replacement;
- DB: umiarkowanie prosta;
- reinstall: utrata licznika bez trwałej bazy może zderzyć identity.
- podobnie jak A pozwala schema wyrazić dwa aktywne occupants jednego slotu,
  jeśli uniqueness obejmuje typ.

### C. backendowy `resource_id`, osobny locator i incarnation

- bezpieczeństwo: pozwala fail-closed oddzielić identity od adresu;
- wykryty VMID reuse: nowa candidate incarnation otrzymuje nowy `resource_id`;
- HA: może wiązać się z `resource_id` dla read-only continuity; destructive
  policy wymaga dodatkowo `security_continuity=trusted`;
- migracja/multi-node: node pozostaje relacją;
- wiele Proxmoxów: locator jest namespaced przez source;
- DB: wymaga historii bindingów, observations i tombstones;
- outage: nadal wymaga jawnej oceny continuity, ale nie wymusza dziedziczenia.

W tym modelu locator C/D to `(inventory_source_id, vmid)`, a `resource_type`
jest immutable property związanej incarnation.

### D. wybrany model: C plus rozdzielenie backend/source i evidence

Model C uzupełniamy o `backend_instance_id`, `inventory_source_id`, historię
`locator_generation`, osobne observational/security continuity states oraz
trwałe evidence. Dzięki temu brak natywnego PVE UUID jest jawny, a nie ukryty za
heurystyką.

Koszt to bardziej rozbudowany schema oraz jawne rozróżnienie dwóch ryzyk:
observable gap może spowodować bezpieczny false split, natomiast niewidoczny
replacement może zachować read-only identity. Security continuity powoduje, że
żaden z tych przypadków nie przenosi destructive authority.

## Backend installation a Proxmox source

`backend_instance_id` identyfikuje nową instalację Hubinet Ops oraz jej trwałą
bazę. HA ConfigEntry wiąże się z tym ID. Zmiana URL/IP backendu nie zmienia ID.
Clean reinstall bez przywrócenia dokładnie tej samej bazy tworzy nowe ID i nowe
HA identity; 0.5 nie importuje identity z 0.4.

`inventory_source_id` jest backendowym UUID jednego operatorowo dodanego
środowiska Proxmox. Nazwa klastra, lista endpointów, ich IP/DNS i certyfikaty są
atrybutami/evidence source, nie jego identity. Dwa niezależne Proxmoxy z VMID
101 mają różne `inventory_source_id`.

Proxmox dokumentuje niezmienność nazwy utworzonego klastra oraz stabilność nazw
node'ów po jego utworzeniu, ale nie deklaruje globalnie unikalnego UUID klastra
ani uniwersalnego ID standalone environment. Nazwa klastra jest wymagana jako
unikalna tylko w tym samym fizycznym/logical network. Dlatego source ID nadaje
backend, a przypisanie nowego endpointu do istniejącego source wymaga późniejszej
procedury weryfikacji/confirmation. Endpointów z dwóch sources nie wolno scalać
na podstawie podobnych nazw lub VMID.

## Model continuity

Presence, observational continuity i security continuity są niezależnymi
osiami.

Observational/read-only continuity:

- `consistent` — kolejne kompletne obserwacje są zgodne i nie wystąpił
  observable gap/conflict; nie jest to dowód fizycznej ciągłości;
- `uncertain` — istnieje rzeczywista obserwowalna luka, konflikt albo evidence,
  przez które continuity nie da się rozstrzygnąć;
- `replaced` — istnieje pozytywny dowód, że slot wskazuje innego occupant;
- `retired` — rekord został zakończony i zachowany historycznie.

Security continuity:

- `unverified` — resource nie ma zaakceptowanego continuity anchor/proof;
- `trusted` — operator wykonał enrollment, wymagany proof pozostaje ważny i nie
  ma dyskwalifikującej luki/evidence;
- `revoked` — wcześniejszy trust został jawnie wycofany lub proof przestał być
  ważny.

Pozytywny dowód continuity nie może opierać się tylko na nieprzerwanym VMID,
nazwie, `digest` ani podobieństwie config. Stockowe API PVE nie dostarcza
wspólnego immutable anchor. Przyszły enrollment musi zdefiniować continuity
proof (oraz sposób jego odczytu i ochrony) przed nadaniem `trusted`. ADR nie
przesądza jeszcze mechanizmu enrollment.

Dla `unverified` backend może zachować ten sam `resource_id` pomiędzy zgodnymi,
kompletnymi obserwacjami dla inventory, UX i HA. Sam odstęp czasu między
pollingami nie jest observable gap i nie ustawia automatycznie `uncertain`.
Stockowy polling nie potrafi wykluczyć, że delete/recreate zaszło całkowicie
między dwoma identycznymi obserwacjami. Taka niewidoczna replacement może więc
zachować read-only HA identity; nie może zachować destructive authority.

Pozytywnym dowodem replacement może być:

- obserwowany type change occupant tego samego slotu;
- potwierdzona operacja destroy oraz pełny, późniejszy snapshot bez zasobu;
- jawny mismatch przyszłego continuity anchor;
- wiarygodny, nieprzerwany event/task cursor pokazujący destroy/create;
- jawna, audytowana decyzja operatora.

Task log i config fingerprints są evidence pomocniczym. Retencja i kompletność
task history nie są potwierdzone jako niezawodny, wieczny event stream, więc nie
mogą być jedyną granicą bezpieczeństwa.

Jeżeli wystąpi observable gap/conflict/evidence i continuity nie da się
rozstrzygnąć, system ustawia observational `uncertain`, wycofuje security trust
i może utworzyć nową provisional `resource_id` po ponownym pojawieniu locatora.
Poprzednia candidate incarnation pozostaje w quarantine/tombstone. Późniejsze
jawne resolution może zapisać lineage, ale nie kopiuje automatycznie policy.
Brak observable konfliktu pozwala zachować read-only identity, lecz nigdy nie
podnosi `unverified` do `trusted`.

## Scenariusze lifecycle i zagrożeń

| # | Scenariusz | Wymagane zachowanie |
| --- | --- | --- |
| 1 | rename VM/LXC | Ten sam `resource_id`; zmienia się display name. |
| 2 | migrate `pve-a` → `pve-b` | Ten sam `resource_id` i locator; aktualizacja node relation. |
| 3 | normal reboot | Ten sam `resource_id`; runtime status jest obserwacją. |
| 4 | config edit | Ten sam `resource_id`; nowy `digest`/fingerprint jest faktem, nie identity. |
| 5 | snapshot rollback tego samego workloadu | Intencjonalnie ten sam logical workload, ale QEMU `vmgenid` się zmienia. Rewalidacja continuity proof; przy ambiguity `uncertain`, bez automatycznej policy. |
| 6 | clone do nowego VMID | Nowy locator, nowy `resource_id`, `unverified`; żadna policy nie jest kopiowana. |
| 7 | destroy CT101 | Najpierw `missing`; po pozytywnym potwierdzeniu `confirmed_removed`, `retired` i tombstone. |
| 8 | później nowy CT101 | Nowy `resource_id` i `locator_generation`; stary rekord pozostaje. |
| 9 | LXC101 → delete → QEMU101 | Ten sam slot `(source, 101)` zostaje zajęty ponownie. LXC incarnation jest retired/tombstoned; QEMU dostaje nowy `resource_id`, immutable `resource_type=qemu` i nową `locator_generation`. |
| 10 | delete/recreate między dwoma identycznymi pollingami | Zdarzenie może być observationally indistinguishable. Backend może zachować `resource_id` i read-only HA identity ze stanami `consistent`/`unverified`; nie oznacza to physical continuity. Retained policy może pozostać historycznie, ale effective destructive policy, maintenance permission i nowe destructive approvals/jobs są niedostępne. |
| 11 | backend offline podczas delete/recreate | Znana przerwa w observation jest rzeczywistym gap: observational `uncertain`, security trust revoked/unavailable i fail-closed revalidation. |
| 12 | node chwilowo offline | Jeśli locator nadal występuje w baseline, presence pozostaje `present`, a `node_availability=unavailable`; bez usunięcia i bez zmiany identity. Po powrocie continuity proof decyduje o mutation trust, nie o samym read-only presentation bindingu. |
| 13 | cały source/API niedostępny | Zachowanie last-known inventory, freshness maleje; żadnych missing/removal transitions. |
| 14 | resource missing przez kilka polli i wraca | Liczba polli nie potwierdza removal. Powrót wymaga continuity evidence; przy braku dowodu provisional ID. |
| 15 | `confirmed_removed`, potem locator wraca | Zawsze nowy `resource_id`/generation. Nawet jawny restore tworzy nową incarnation; lineage może wskazać poprzednika. |
| 16 | destructive policy na zastąpionym resource | Policy zostaje przy starym `resource_id`; nowy startuje `discovered`, bez capabilities/maintenance. |
| 17 | backup restore pod starym VMID | Wykryty restore/replacement tworzy nową candidate incarnation, chyba że przyszła jawna semantyka z mocnym proof zostanie zaakceptowana. Restore niewidoczny pomiędzy identycznymi pollingami może zachować read-only `resource_id` jako `unverified`; obecne hints nie wystarczają do destructive trust. |
| 18 | dwa Proxmoxy z VMID 101 | Różne `inventory_source_id`, locators i `resource_id`; brak kolizji. |

## Konsekwencje bezpieczeństwa

Fundamentalny invariant rozdziela trwały zapis intencji operatora od bieżącej
autoryzacji:

```text
stored policy attaches durably to resource_id and its enrollment/history context,
never merely to a Proxmox locator

stored policy allows operation
∩ policy applicability
∩ trusted security continuity
∩ backend capability
∩ exact resource/binding/revision
∩ operation-specific preconditions
= operation eligible
```

Stored/retained policy oraz jej revisions pozostają przy starym `resource_id`
po `uncertain`, utracie trust, replacement, retirement i utworzeniu tombstone.
Jest to wymagane dla audytu i nie oznacza, że policy jest effective/applicable.
Policy może być applicable wyłącznie dla dokładnego enrollment/continuity
context, aktualnego expected resource/binding revision, eligible lifecycle oraz
wszystkich operation-specific gates.

Nowy resource nie ma stored policy. Istniejący `unverified`/`revoked` resource
zachowuje wcześniejszą stored policy wyłącznie jako historię. W obu przypadkach:

```text
stored policy = retained, jeśli wcześniej istniała
effective/applicable destructive policy = false
destructive capabilities = none
maintenance permission = none
```

Discovery nigdy nie kopiuje policy, approval, planów, jobs ani locks ze starej
incarnation. Każda przyszła mutacja musi ponownie sprawdzić `resource_id`, aktywny
locator binding, expected continuity revision, `trusted`, policy i runtime
preconditions. Locator jest parametrem wykonawczym wyliczonym dopiero po tych
kontrolach.

Resource ze stanem security continuity `unverified` lub `revoked` może posiadać
wyłącznie historycznie retained policy record; nie może mieć effective
destructive/maintenance policy ani nowych aktywnych destructive approvals/jobs.
Utrata trust blokuje/przerywa istniejące aktywne destructive operations zgodnie
z późniejszym jawnie audytowanym contract. Stabilny `resource_id` ani sama
obecność policy record nie jest podstawą wyjątku.

Granularne flagi `automatic_rollback`, `manual_rollback_allowed` i
`manual_snapshot_restore_allowed` zachowują swoje znaczenie, ale każda jest
tylko jednym wymaganym gate. Na przykład `automatic_rollback=true` bez trusted
continuity, exact binding/revision, backend capability i wymaganych snapshot/job
preconditions nie autoryzuje rollbacku.

Successor po replacement dostaje nowy `resource_id` i nie dziedziczy stored
policy, approvals ani jobs. Ewentualne przyszłe „copy settings” tworzy nową,
jawną i audytowaną policy dla successor resource; nie reaktywuje historycznej
policy poprzednika.

Invariant:

```text
false continuity must never transfer destructive authority
```

## Konsekwencje dla Home Assistant

- ConfigEntry identifier: `backend_instance_id`, nie URL;
- device identifier source/node: namespaced przez `backend_instance_id` i
  backendowe `inventory_source_id`/`node_id`;
- device identifier workloadu: `resource_id`;
- entity `unique_id`: `resource_id` plus typ encji;
- rename: tylko nazwa;
- migrate: `via_device_id` wskazuje nowy node device, identity bez zmian;
- `confirmed_removed`: stare device/entities pozostają unavailable; brak purge w
  tej fazie;
- wykryte VMID reuse/replacement albo observable gap z nierozstrzygniętą
  continuity: nowe/provisional device identity zgodnie z reconciliation;
- niewidoczny delete/recreate pomiędzy identycznymi pollingami może zachować
  read-only HA identity, ponieważ stockowe discovery nie potrafi go rozróżnić;
  nie przenosi to żadnej destructive authority.
- API/HA może prezentować retained policy i historię, lecz backend publikuje
  osobno derived policy applicability/effective capabilities oraz suspended
  reason; frontend nigdy nie wylicza capability z samej obecności policy.

## PHASE 0 AMENDMENT REQUIRED

Obecny kod i kontrakt Phase 0 muszą zostać zmienione **przed implementacją Phase
1**. Wymagany amendment obejmuje łącznie:

- zastąpienie `ResourceIdentity(instance_id, resource_type, vmid)` przez jawne
  `backend_instance_id`, `inventory_source_id` i backendowy `resource_id`;
- oddzielenie locator presence (`present`, `missing`, `confirmed_removed`) od
  per-resource `detail_status` (`ok`, `temporarily_unavailable`, `error`) oraz
  `node_availability`;
- usunięcie mutually-exclusive modelu, w którym `temporarily_unavailable` albo
  `node_unavailable` zastępuje presence: jeśli locator nadal istnieje,
  `temporarily_unavailable` jest detail status, a niedostępny node jest overlay
  przy `presence=present`;
- wdrożenie jednej semantyki `current_node_id`/`last_known_node_id` i HA
  `via_device` opisanej w [ADR 0002](0002-proxmox-discovery-reconciliation.md#node-relation-i-ha-availability);
- dostosowanie mapowania availability w Coordinator/entities tak, aby błąd
  detail lub node availability nie oznaczał automatycznie physical absence;
- zmianę validatorów snapshotu i testów Device Registry/node relation dla
  rozdzielonych osi, rename oraz migracji.

Obecny `docs/architecture/0.5-foundation.md` dokumentuje faktyczny kontrakt
Phase 0 i nie jest w tym PR przepisywany tak, jakby amendment już wdrożono.
Trusted operations muszą osobno wymagać zaakceptowanego security continuity
proof. Niniejsza faza nie modyfikuje kodu ani testów.

## Nierozstrzygnięte kwestie

1. Konkretny, odporny na kopiowanie/rollback mechanizm continuity proof dla
   późniejszego enrollment. Dopóki nie zostanie zaakceptowany, trusted
   destructive capabilities są globalnie niedostępne; read-only
   discovery/inventory może powstać niezależnie.
2. Semantyka QEMU `meta.ctime` we wszystkich wersjach i ścieżkach backup/restore
   pozostaje **UNKNOWN**, ale niezależnie nie rozwiązuje LXC.
3. Gwarancje retencji i kompletności task/event history są **UNKNOWN**; traktujemy
   ją tylko jako evidence.
4. Procedura bezpiecznego przypięcia alternatywnego endpointu do istniejącego
   source wymaga osobnego design review.
5. Czy operator będzie mógł jawnie scalić false split w HA bez utraty historii;
   automatycznego merge nie projektujemy.
6. Konkretny node/hostd attestation protocol, key rotation i operatorowa
   procedura ponownego nadania `trusted` pozostają do osobnego review.

## Sources / Evidence

Oficjalne źródła Proxmox, odczytane 2026-08-08:

- [pvecm — cluster, migracja, nazwy node/cluster](https://github.com/proxmox/pve-docs/blob/master/pvecm.adoc)
- [pmxcfs — cluster-wide guest configuration i VMID](https://github.com/proxmox/pve-docs/blob/master/pmxcfs.adoc)
- [pve-manager `Cluster.pm` — wspólne `/cluster/resources` i `/cluster/nextid`](https://github.com/proxmox/pve-manager/blob/master/PVE/API2/Cluster.pm)
- [qemu-server `QemuServer.pm` — schema `vmgenid`, `meta`, restore](https://github.com/proxmox/qemu-server/blob/master/src/PVE/QemuServer.pm)
- [qemu-server `QemuConfig.pm` — snapshot rollback i `vmgenid`](https://github.com/proxmox/qemu-server/blob/master/src/PVE/QemuConfig.pm)
- [qemu-server `API2/Qemu.pm` — create/clone oraz regeneracja UUID/`vmgenid`](https://github.com/proxmox/qemu-server/blob/master/src/PVE/API2/Qemu.pm)
- [pve-container `LXC/Config.pm` — config digest i brak odpowiednika `vmgenid`](https://github.com/proxmox/pve-container/blob/master/src/PVE/LXC/Config.pm)
- [pct(1) — CTID, clone, migrate i digest](https://pve.proxmox.com/pve-docs/pct.1.html)
- [qm(1) — VMID i QEMU config contract](https://pve.proxmox.com/pve-docs/qm.1.html)
- [Migrate to Proxmox VE — wspólna identity virtual guests przez VMID](https://pve.proxmox.com/wiki/Migrate_to_Proxmox_VE)

Repozytoria GitHub są oficjalnymi read-only mirrors; źródłem nadrzędnym jest
również [git.proxmox.com](https://git.proxmox.com/). Wnioski o braku przydatności
danego pola jako identity są inference architektonicznym opartym na wskazanym
kontrakcie i source, a nie deklaracją dodatkowych gwarancji API Proxmox.
