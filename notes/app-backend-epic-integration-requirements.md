# What the app/backend must expose for Epic integration (direction B)

This documents, per field this compliance tool needs (`epic_compliance/config.py`),
who owns it, and — for anything the app itself must serve — how to actually expose
it. Architecture context: mobile app → app's own backend (photo, AI diagnosis,
storage) → backend writes `DiagnosticReport`/`Observation`/`Media` into Epic via
SMART write scopes. See `notes/decisions.md` ADR-007 (write-back, not a
server-hosted FHIR API) once that ADR lands from the write-back build task.

## Field-by-field

| Field | Owner | What the app/backend does with it |
|---|---|---|
| `FHIR_BASE_URL` | Epic (per organization) | Not exposed by the app. Stored server-side per org once that org activates the app. |
| `SMART_LAUNCH_URL` / `SMART_TOKEN_URL` | Epic | Not exposed. Auto-discovered per org via `{FHIR_BASE_URL}/.well-known/smart-configuration` at launch time — never hardcode. |
| `OAUTH_CLIENT_ID` | Epic issues it to the app at registration | Backend-stored, but **not secret** — SMART's public-client model expects the client_id to be sent in the authorize URL, so it's fine for it to be visible in that redirect. Still keep it in server config, not baked into the mobile app binary, since it's per-org in a multi-tenant setup. |
| `OAUTH_CLIENT_SECRET` | Epic issues it (confidential clients only) | **Backend-only secret.** Store in a secrets manager (AWS Secrets Manager, Vault, etc.) or env injected at deploy time. Never log it, never return it from any API response, never ship it to the mobile app. |
| `OAUTH_REDIRECT_URI` | The app defines and **must serve** | The one field that's a real endpoint the app has to build and expose: a public HTTPS route (e.g. `https://api.yourapp.com/epic/callback`) that: (1) is registered byte-for-byte with Epic at app registration — Epic will refuse to redirect anywhere not on that exact allowlist; (2) accepts the incoming `code` and `state` query params from Epic's auth server; (3) validates `state` (CSRF), then exchanges `code` for a token at `SMART_TOKEN_URL` server-side, never in the mobile app. |
| `RUN_MODE`, `ANTHROPIC_API_KEY` | This compliance tool only | Not related to the app/backend at all — internal to how this tool runs its own checks. |

## How this compliance tool actually receives these values

This tool is a **separate, standalone test harness** — it does not auto-discover
or connect to the app's backend. The values above are copied manually (or via a
shared secrets source you control) into this tool's own `.env` (see
`.env.example`) or entered per-run in the dashboard's config form
(`OVERRIDABLE_FIELDS` in `config.py`). Concretely: whatever `OAUTH_CLIENT_ID`
etc. the app's backend uses against Epic's sandbox is the *same* value you'd put
in this tool's `.env` to test that exact integration.

## Multi-organization note

Once there's more than one Epic customer org, the app's backend needs an org
registry (not yet built — separate from this tool): a table of
`{org_id, fhir_base_url, client_id, client_secret_ref, token_url, launch_url,
status}`, looked up by which org initiated a given SMART launch. This compliance
tool remains single-tenant per run — you'd run it once per org's row when you
want to verify that org's integration specifically.
