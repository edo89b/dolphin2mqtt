# dolphin2mqtt

Bridge between the Maytronics MyDolphin Plus cloud and a local MQTT broker. The
robot is only reachable through the vendor cloud: the bridge logs in to the
Maytronics REST API, obtains temporary AWS credentials, mirrors the robot's AWS
IoT device shadow onto local retained topics and turns local `cmd/<name>`
messages into shadow updates. The protocol is reverse engineered (upstream
reference: <https://github.com/sh00t2kill/dolphin-robot>).

## Tech Stack

- Python 3.11 (Docker image `python:3.11-slim`), a single module, no framework.
- `requests` 2.32.3: Maytronics REST API.
- `awscrt` 0.20.10 + `awsiotsdk` 1.21.5: AWS IoT Core over websockets, SigV4
  signing with the STS credentials handed out by the REST API.
- `paho-mqtt` 1.6.1: local broker. The code uses the 1.x API (constructor and
  `on_connect(client, userdata, flags, rc)`); a 2.x upgrade needs the
  `CallbackAPIVersion` migration, not just a version bump.
- `pycryptodome` 3.20.0: AES-CBC token for the AWS credentials endpoint.
- Docker Compose, one service. All versions are pinned in `requirements.txt`.

## Directory Structure

```
dolphin2mqtt/
├── dolphin_bridge.py - the whole bridge (MaytronicsApi, AwsBridge, Bridge)
├── Dockerfile - python:3.11-slim image running dolphin_bridge.py
├── docker-compose.yml - service dolphin2mqtt on the external mqtt_net network
├── requirements.txt - pinned dependencies
├── .env.example - configuration template (copy to .env, gitignored)
├── README.md - user guide: account registration, command payloads
├── LICENSE - MIT
├── scripts/
│   └── doc_check.py - documentation staleness check
└── .githooks/
    └── pre-commit - runs the check before every commit
```

Inside `dolphin_bridge.py`:
- `MaytronicsApi`: REST login, serial lookup, AWS token (`_encrypt_aws_token`).
- `AwsBridge`: one AWS IoT websocket connection (subscribe, publish, disconnect).
- `Bridge`: local MQTT client, the two background loops (credential refresh,
  shadow poll), AWS-to-local mirroring (`_on_aws_message`) and command
  translation (`_dispatch`).

## Setup & Commands

```bash
cp .env.example .env                  # then fill in the account and broker
docker network create mqtt_net        # only if the external network does not exist yet
docker compose config --services      # validates the compose file: prints dolphin2mqtt
docker compose up -d --build          # build and start (also after every code change)
docker compose logs -f dolphin2mqtt   # follow the bridge log
python3 -m py_compile dolphin_bridge.py   # syntax check, touches nothing
python3 scripts/doc_check.py          # documentation staleness check
```

- The compose file expects an existing external network `mqtt_net` shared with
  the broker; `MQTT_HOST` is the broker's name on that network. Without such a
  network, replace the `networks:` blocks with the default bridge network.
- The account must be a password account with the robot paired to it: the
  step-by-step procedure (curl calls and app pairing) is in `README.md`.
- There is no test suite and no mock of the cloud. Verification means running
  against a real account and broker and checking the topics listed below. The
  only read-only command is `get_state`; every other command acts on the robot.
- Do not run a second instance (e.g. `python3 dolphin_bridge.py` from a shell)
  against the same broker while the container is up: the local client id is
  fixed (`dolphin-bridge`), so the two instances keep kicking each other off.

## Coding Conventions

- One module, three classes with distinct roles (see above). Keep it that way:
  REST code in `MaytronicsApi`, AWS transport in `AwsBridge`, MQTT glue and
  command mapping in `Bridge`.
- Naming: `PascalCase` classes, `snake_case` functions, `_leading_underscore`
  for internals, `UPPER_CASE` module constants. Type hints on signatures.
- Configuration comes only from environment variables read at import time:
  required ones with `os.environ[...]` (fail fast), optional ones with
  `os.getenv(..., default)`. A new variable goes into `.env.example` (with a
  comment), the README table and the table below in the same commit.
- Protocol constants (`MAYTRONICS_API`, `APPKEY`, `INTEGRATION_VERSION`,
  `AWS_ENDPOINT`, `AWS_REGION`) mirror the official app. Change them only
  against the upstream reverse-engineering project, never by guesswork.
- New command: one `elif` branch in `Bridge._dispatch` that validates the
  payload, raises `ValueError` with the expected shape on bad input, and calls
  `_publish_desired` (shadow `state.desired`) or `_publish_dynamic` (dynamic
  channel). Document it in the README command table and in the census below.
- Every state topic is published retained; only `cmd/<name>/error` is not.
- AWS reconnects go through `_reconnect_aws`, which holds `_aws_lock`.
- Logging: module logger `dolphin-bridge`, lazy `%s` arguments, never log the
  password, the API token or the STS credentials.
- Comments, docstrings, log and error messages in English (public repository).
- Anti-patterns: hardcoding a serial, an email or a broker address; adding a
  retained command topic; swallowing a command error without publishing it.

## Git Workflow and operating rules

- Single branch `main`, pushed to `origin` on GitHub. No CI.
- Commit messages in English, imperative subject, optional lowercase scope
  prefix (e.g. `compose: move MQTT bridge to internal mqtt_net ...`).
- Commits, pushes and releases are done by the maintainer only; nothing is
  pushed without an explicit decision.
- Public repository: never commit `.env`, real emails, serial numbers, IP
  addresses or hostnames, in files or in commit messages. `.env.example` holds
  placeholders only.
- Commands move a real robot through the vendor cloud: use `get_state` to test
  the command path. Never publish a command with the retain flag: the bridge
  subscribes to `cmd/#` on every connect and would replay it at each reconnect.
- Deploying a change means `docker compose up -d --build` on the host that runs
  the bridge; the image does not follow the repository by itself.
- Documentation-code coherence: a change that makes a sentence of the
  documentation false fixes it in the same commit. When the staleness check
  fails, fix the document, do not silence the check. The check catches broken
  references (names that no longer exist), not descriptions that became false.
- Staleness check: `python3 scripts/doc_check.py` (manual run). The versioned
  hook `.githooks/pre-commit` runs it on every commit once enabled, once per
  clone: `git config core.hooksPath .githooks`. Markers for legitimate
  exceptions: `<!-- doc-check:ignore -->` (line), `-start`/`-end` (block),
  `-file` (whole document).

## Key Files & Directories

- `dolphin_bridge.py`: all runtime code.
- `.env.example`: every supported variable; the real `.env` is gitignored.
- `docker-compose.yml`: restart policy `always`, json-file logs capped at 3 x 10 MB.
- `README.md`: account registration and command payloads for users.
- Tests: none. Documentation: this file and `README.md`.

## Related documents

- [README.md](README.md): account registration, pairing, command payloads.
- Upstream protocol reference: <https://github.com/sh00t2kill/dolphin-robot>.

## Integrations (census)

Before adding or changing a read or a write towards the cloud or the broker,
check this census; every new read or write is added here in the same commit,
before the code.

REST API `https://mbapp18.maytronics.com/api` (form-encoded POST, headers
`appkey` + `integration-version`, `token` once logged in; on HTTP 401 one
re-login and retry):

| # | Endpoint | Purpose |
|---|---|---|
| 1 | `/users/Login/` | API token and the printed serial (`Sernum`, null if no robot is paired) |
| 2 | `/serialnumbers/getrobotdetailsbyrobotsn/` | motor unit serial (`eSERNUM`), the AWS thing name (MUS) |
| 3 | `/serialnumbers/getrobotdetailsbymusn/` | product details, published once on `info` |
| 4 | `/IOT/getToken_DecryptSN/` | temporary AWS credentials; the request carries the AES token |

AWS IoT Core (`AWS_ENDPOINT`, region `eu-west-1`, websockets, QoS 1): subscribes
to `$aws/things/<MUS>/shadow/#` and `Maytronics/<MUS>/main`; requests the shadow
at startup, after every reconnect and every `SHADOW_POLL_SECS`; rebuilds the
connection with fresh credentials every `CREDENTIALS_REFRESH_SECS`.

Local topics, all under `<MQTT_PREFIX>/<MUS>/`:

| # | Topic | Retained | Content |
|---|---|---|---|
| 1 | `bridge/status` | yes | `online` / `offline` (also the LWT) |
| 2 | `bridge/last_error` | yes | `OK` or the text of the last failure |
| 3 | `info` | yes | product details JSON (endpoint 3) |
| 4 | `shadow/...` | yes | mirror of `$aws/things/<MUS>/shadow/...` |
| 5 | `state` | yes | the `state.reported` object of each shadow document |
| 6 | `state/<section>` | yes | one topic per reported section (JSON; scalars via `str()`, so `True`/`False`) |
| 7 | `dynamic` | yes | mirror of `Maytronics/<MUS>/main` |
| 8 | `raw/<topic>` | yes | any other AWS topic (unused with the current subscriptions) |
| 9 | `cmd/<name>` | no | commands in, JSON payload |
| 10 | `cmd/<name>/error` | no | error text of a failed command |

Reported sections seen in practice (they vary by model): `systemState`
(`pwsState`, `robotState`, `rTurnOnCount`), `isConnected` (`connected`),
`cycleInfo` (`cleaningMode.mode`, `cleaningMode.cycleTime`,
`cycleStartTimeUTC`), `nextCycleInfo`, `cleaningModes`, `filterBagIndication`
(`state`), `led`, `robotError` and `pwsError` (`errorCode`), `debug`.

Writes towards the cloud (payloads in the README):

| # | Command | Writes |
|---|---|---|
| 1 | `clean_mode` | desired `cleaningMode.mode` |
| 2 | `power` | desired `systemState.pwsState` (`on`/`off`) |
| 3 | `led` | desired `led.ledMode` / `ledIntensity` / `ledEnable` |
| 4 | `reset_filter` | desired `filterBagIndication.resetFbi = true` |
| 5 | `cycle_time` | desired `cycleInfo.cycleTime` |
| 6 | `schedule`, `delay` | desired `weeklySettings` / `delay`, payload passed as is |
| 7 | `joystick`, `temperature` | `pwsRequest` on the dynamic channel |
| 8 | `get_state` | empty message on `shadow/get` (read only) |
| 9 | `refresh_credentials` | new AWS credentials and reconnect |
| 10 | `raw_desired`, `raw_dynamic`, `raw_shadow_update`, `raw_publish` | unvalidated pass-through, `raw_publish` to any AWS topic |

## Known issues and cloud quirks

1. **Error-topic feedback loop (bridge bug).** `cmd/<name>/error` sits inside the
   bridge's own `cmd/#` subscription: the bridge reads its error back as the
   unknown command `<name>/error`, publishes `cmd/<name>/error/error`, and so
   on until the broker drops the connection (seen: a `clean_mode` with an empty
   payload produced about 200 levels in 0.2 s, then a reconnect). Until the code
   is fixed, never send an empty or invalid command.
2. **Payload parsing.** A payload that is not valid JSON becomes `{}`: a bare
   string must be a JSON string (`"all"` with the quotes), plain `all` fails.
3. **Credential refresh retry.** A failed refresh logs "retry in 60s" but the
   next attempt comes 60 s plus `CREDENTIALS_REFRESH_SECS` later. The AES token
   step fails now and then (no `+`-free output in 10 random IVs): expect a
   sporadic `offline` with that `last_error`, cleared by the next successful
   shadow poll while the old connection still works.
4. **`robotState` is not connectivity.** `notConnected` also appears while the
   robot accepts commands normally; use `isConnected.connected`.
5. **Latched values.** `robotError.errorCode`, `pwsError.errorCode`,
   `cycleInfo.cycleStartTimeUTC` and `cycleInfo.cleaningMode` keep their last
   value for days: do not use them as live state.
6. **Cleaning mode.** It is never reported back at the top level, so the shadow
   delta for `cleaningMode` never clears; the app is the only confirmation. A
   new mode applies at the next cycle start: to apply it now send
   `clean_mode`, then `power` `off`, wait about 10 s, then `power` `on`.
7. **`pwsState` stuck on `on`.** After a cycle the cloud can leave
   `pwsState = on` with the robot idle; a `power` `off` command clears it.

## Environment Variables and secrets

All variables live in `.env` (gitignored, loaded by `env_file`); the reference
with placeholders is `.env.example`.

| # | Variable | Default | Meaning |
|---|---|---|---|
| 1 | `MAYTRONICS_EMAIL` | required | account email; its first two letters also derive the AES key |
| 2 | `MAYTRONICS_PASSWORD` | required | account password (password account, not OTP-only) |
| 3 | `MQTT_HOST` / `MQTT_PORT` | `mosquitto` / `1883` | local broker |
| 4 | `MQTT_USER` / `MQTT_PASS` | empty | broker credentials; empty user means anonymous |
| 5 | `MQTT_PREFIX` | `dolphin` | first topic level |
| 6 | `SHADOW_POLL_SECS` | `300` | shadow request interval |
| 7 | `CREDENTIALS_REFRESH_SECS` | `3000` | AWS credentials rotation interval |
| 8 | `LOG_LEVEL` | `INFO` | Python logging level |

- `DOLPHIN_IPV4` / `DOLPHIN_IPV6` in `.env.example` are leftovers of the former
  macvlan setup: nothing reads them.
- Secrets are the Maytronics password and the broker password: only in `.env`,
  never in the repository, in logs or in chat. The STS credentials and the API
  token live in memory only. `APPKEY` is the public key of the official app,
  not a secret.
- Give the bridge its own broker user: it publishes under `<MQTT_PREFIX>/<MUS>/`
  (including `cmd/<name>/error`) and subscribes to `<MQTT_PREFIX>/<MUS>/cmd/#`.
