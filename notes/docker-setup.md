# Podman Setup — ONC (g)(10) Test Kit

## Location
`/Users/belleasia/BelleGitRepos/HealthcareApp/onc-certification-g10-test-kit`

## Port Configuration
- **UI accessible at:** `http://localhost:8080` (not 80 — changed in `docker-compose.background.yml`)
- nginx container maps `8080:80` (host:container)
- Inferno app runs internally on port `4567`, proxied by nginx

## Services (docker-compose.yml + docker-compose.background.yml)
| Service | Image/Build | Port | Notes |
|---|---|---|---|
| `inferno` | local build | internal 4567 | main Inferno app |
| `worker` | local build | — | Sidekiq background jobs |
| `nginx` | nginx | 8080→80 | reverse proxy to inferno:4567 |
| `redis` | redis | 6379 | job queue + session store |
| `hl7_validator_service` | infernocommunity/inferno-resource-validator:1.0.78 | internal | FHIR resource validation |

## Setup (first time only)
```bash
cd /Users/belleasia/BelleGitRepos/HealthcareApp/onc-certification-g10-test-kit
./setup.sh   # pulls images, builds, runs DB migration
```

## Start / Stop
```bash
cd /Users/belleasia/BelleGitRepos/HealthcareApp/onc-certification-g10-test-kit

# Start
./run.sh              # builds + starts all services (foreground)
# or
podman compose up -d  # detached

# Stop
podman compose down

# Rebuild after changes
podman compose build && podman compose up
```

## Status check
```bash
podman compose ps
podman compose logs inferno   # app logs
podman compose logs nginx     # access logs
```

## Data persistence
- SQLite DB + test run data: `./data/` directory (gitignored)
- Redis data: `./data/redis/`
- HL7 IGs: `./lib/onc_certification_g10_test_kit/igs/`

## Nginx config
`./config/nginx.conf` — proxies all traffic to `inferno:4567`
`./config/nginx.background.conf` — used by background compose (sets port binding)

## Day 3 status
- Installed and confirmed running on port 8080
- `./setup.sh` completed successfully
- Stack verified working

## Known quirks
- Uses **Podman** (`podman compose`), not Docker
- Port 80 was in conflict; changed to 8080 in `docker-compose.background.yml`
- `run.sh` and `setup.sh` call `docker compose` internally — if they break, run the equivalent `podman compose` commands directly

## This tool's own FHIR validator (ADR-021)

The validator above (1.0.78) belongs to the test kit and is internal to the kit's compose.
This tool runs **its own** container, `epic-compliance-fhir-validator`, using
`infernocommunity/inferno-resource-validator:1.0.84` on port 3500. 1.0.78 rejects valid codes
against today's tx.fhir.org.

- The app starts it on `serve`, `run` and `validator` (podman, falling back to docker).
  It is left running. Turn this off with `VALIDATOR_AUTOSTART=false`.
- An old container on a different image is replaced automatically.
- Check it: `curl localhost:3500/validator/version`, or `python -m epic_compliance validator`.
- Stop it: `podman stop epic-compliance-fhir-validator`.
