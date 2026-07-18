# Hubinet Ops Agent 0.2.0

Wersja 0.2 przenosi decyzję o aktualizacji z przycisku w pushu do osobnego dashboardu Home Assistanta.

## Najważniejsze zmiany

- trwały stan każdego zarządzanego CT w SQLite;
- nowe endpointy stanu i ręcznego odświeżania;
- etap i postęp aktywnego zadania widoczne w HA;
- zatwierdzanie i odrzucanie planu wyłącznie z dashboardu;
- push tylko informuje i otwiera widok konkretnego CT;
- osobny dashboard `Hubinet Ops` z widokami CT101 i CT106;
- Docker-aware healthcheck dla CT106;
- lista kontenerów Docker, ich stan i Docker Health Status;
- wykrywanie dysku, RAM-u, uptime, systemd i adresów IP;
- post-update scan potwierdzający, czy pakiety rzeczywiście zniknęły;
- CT106 dodany do allowlisty bez udostępniania agentowi zwykłego shella;
- endpoint odrzucenia planu;
- migracja bazy 0.1 → 0.2 wykonywana automatycznie.

## Endpointy 0.2

- `GET /health`
- `GET /api/v1/containers`
- `GET /api/v1/state`
- `GET /api/v1/containers/{vmid}/state`
- `POST /api/v1/refresh`
- `POST /api/v1/containers/{vmid}/refresh`
- `POST /api/v1/scan`
- `POST /api/v1/containers/{vmid}/scan`
- `POST /api/v1/plans/approve`
- `POST /api/v1/plans/reject`
- `GET /api/v1/plans`
- `GET /api/v1/jobs`

## CT106 — profil WeatherHub

Healthcheck wymaga:

- `docker.service = active`;
- `containerd.service = active`;
- `weatherhub-weather-api-1` działa i nie jest unhealthy;
- `weatherhub-weather-worker-1` działa i nie jest unhealthy;
- `weatherhub-redis-1` działa i nie jest unhealthy;
- brak nieoczekiwanych failed units;
- co najmniej 3072 MiB wolnego miejsca.

Konfiguracja znajduje się w:

```text
deploy/managed/profiles/ct106-weather.json
```

## Aktualizacja istniejącego CT110

Rozpakuj paczkę na hoście Proxmox i uruchom:

```bash
cd /root/hubinet-ops-0.2
bash deploy/upgrade-0.2-from-pve.sh 110 106
```

Skrypt:

1. robi backup kodu, configu i SQLite w CT110;
2. aktualizuje ograniczony wrapper na PVE;
3. dodaje CT106 do allowlisty;
4. instaluje profil healthchecka w CT106;
5. aktualizuje `hubinet-maint` w CT101;
6. migruje config agenta;
7. uruchamia API 0.2;
8. wykonuje wyłącznie `inspect/refresh` — nie uruchamia `apt upgrade`.

## Home Assistant

Po aktualizacji agenta uruchom na PVE:

```bash
cd /root/hubinet-ops-0.2
bash deploy/install-ha-0.2-from-pve.sh 192.168.4.168 22222 110 192.168.4.200
```

Skrypt:

- robi backup obecnego package i konfiguracji;
- usuwa smoke test;
- instaluje package 0.2;
- instaluje dashboard;
- zachowuje obecny webhook;
- aktualizuje token i URL-e REST;
- wykonuje `ha core check`;
- robi jeden restart Core, ponieważ dodawane są sensory REST i nowy dashboard.

Dashboard będzie dostępny pod:

```text
/hubinet-ops/overview
/hubinet-ops/ct-101
/hubinet-ops/ct-106
```

Dashboard korzysta z kart Mushroom, które są już wymagane przez dostarczony plik Lovelace.

## Bezpieczeństwo

- agent nie przyjmuje dowolnych poleceń powłoki;
- PVE wrapper ma allowlistę akcji oraz VMID-ów;
- argumenty są walidowane;
- REST wymaga Bearer tokenu;
- push nie zatwierdza aktualizacji;
- przycisk `AKTUALIZUJ` znajduje się na dashboardzie i ma dodatkowe potwierdzenie;
- agent ponawia preflight przed zmianą;
- przy włączonym rollbacku tworzy snapshot przed update'em.

## Testy paczki

```text
Python compile: OK
Bash syntax: OK
YAML/JSON parse: OK
pytest: 4 passed
```
