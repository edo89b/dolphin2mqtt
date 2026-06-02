# dolphin2mqtt

Bridge between the Maytronics MyDolphin Plus cloud (REST + AWS IoT) and a local
Mosquitto broker.

## Configuration

Copy the example environment file and fill in your values:

```bash
cp .env.example .env
```

| Variable | Description |
|---|---|
| `MAYTRONICS_EMAIL` / `MAYTRONICS_PASSWORD` | MyDolphin Plus account (see below) |
| `MQTT_HOST` / `MQTT_PORT` | Local MQTT broker |
| `MQTT_USER` / `MQTT_PASS` | MQTT credentials (leave empty for anonymous) |
| `MQTT_PREFIX` | Topic prefix (default `dolphin`) |
| `SHADOW_POLL_SECS` | Shadow polling interval |
| `CREDENTIALS_REFRESH_SECS` | AWS STS credentials refresh interval |
| `LOG_LEVEL` | `INFO`, `DEBUG`, ... |

The provided `docker-compose.yml` attaches the container to an external macvlan
network with a static IP. If you don't use macvlan, remove the `networks:`
blocks (and the `DOLPHIN_IPV4` / `DOLPHIN_IPV6` variables) to use default bridge
networking.

## Registering an account usable by the bridge

The official MyDolphin Plus app can create **OTP-only** accounts (login via a
code sent by email): the `mbapp18.maytronics.com` REST API exposes no OTP
endpoint, so these accounts **cannot be used** by the bridge. You need an account
created directly via the API with a classic password, and then associate the
robot with that account from the app.

The commands below are the ones used while setting up this deployment — replace
email / password / first name / last name with your own.

### 1. Check that the email is not already in use

```bash
curl -X POST 'https://mbapp18.maytronics.com/api/users/isEmailExists/' \
  -H 'appkey: 346BDE92-53D1-4829-8A2E-B496014B586C' \
  -H 'Content-Type: application/x-www-form-urlencoded; charset=utf-8' \
  --data-urlencode 'Email=you@example.com'
```

Expected response: `"Data":{"isEmailExists":false}`.

> Note: this API is unreliable for OTP accounts — it may return `false` even for
> emails that already exist on the app side. Proceed to step 2 and read the error
> message.

### 2. Register the account with a password

```bash
curl -X POST 'https://mbapp18.maytronics.com/api/users/register/' \
  -H 'appkey: 346BDE92-53D1-4829-8A2E-B496014B586C' \
  -H 'Content-Type: application/x-www-form-urlencoded; charset=utf-8' \
  --data-urlencode 'Email=you@example.com' \
  --data-urlencode 'Password=YourPassword' \
  --data-urlencode 'FirstName=Name' \
  --data-urlencode 'LastName=Surname'
```

- `Status: 1` + `Alert: Succeed` → registration OK, go to step 3.
- `Alert: Email Exists` → the email is registered but probably in an OTP-only /
  unverified state. **Don't insist**: use a different email (even a secondary
  alias, e.g. `+dolphin@gmail.com`, or a separate Outlook/iCloud address). Gmail
  `+` aliases work for registration **but not always** in the later flows.

### 3. Verify login with the new password

```bash
curl -X POST 'https://mbapp18.maytronics.com/api/users/Login/' \
  -H 'appkey: 346BDE92-53D1-4829-8A2E-B496014B586C' \
  -H 'Content-Type: application/x-www-form-urlencoded; charset=utf-8' \
  --data-urlencode 'Email=you@example.com' \
  --data-urlencode 'Password=YourPassword'
```

Expected response: `Status: 1`, with a `Data.token` present and (for now)
`Data.Sernum: null`.

### 4. Associate the robot with the account from the mobile app

There is no REST endpoint to "claim" the robot: pairing is done **only** from
the mobile app over Bluetooth.

1. Log out of the MyDolphin Plus app (if you were logged in with another account).
2. Log in with the email and password you just registered.
3. **Add a robot** / **+** in the app.
4. Get close to the robot, turn it on, follow the BT + Wi-Fi procedure.
5. If the app complains that the robot is already registered to another account,
   do a Wi-Fi/BT reset from the power supply (usually the power button held down,
   see your model's manual). The previous association will be lost.

### 5. Verify that the robot is associated

Repeat the login from step 3. Now `Data.Sernum` must contain the robot's serial.

```bash
curl -s -X POST 'https://mbapp18.maytronics.com/api/users/Login/' \
  -H 'appkey: 346BDE92-53D1-4829-8A2E-B496014B586C' \
  -H 'Content-Type: application/x-www-form-urlencoded; charset=utf-8' \
  --data-urlencode 'Email=you@example.com' \
  --data-urlencode 'Password=YourPassword' | python3 -c "import json,sys; d=json.load(sys.stdin)['Data']; print('Sernum:', d.get('Sernum'))"
```

### 6. Run the bridge

Fill in `MAYTRONICS_EMAIL` and `MAYTRONICS_PASSWORD` in `.env`, then:

```bash
docker compose up -d --build
docker compose logs -f
```

## MQTT configuration and topics

All local topics live under `dolphin/<motor_unit_serial>/...`.

Available commands (`dolphin/<MUS>/cmd/<name>` with a JSON payload):

| cmd | payload |
|---|---|
| `clean_mode` | `{"mode":"all\|floor\|water\|ultra\|pickup"}` |
| `power` | `{"state":"on\|off"}` |
| `led` | `{"mode":1-3,"intensity":0-100,"enable":true}` |
| `reset_filter` | `{}` |
| `cycle_time` | `{"minutes":120}` |
| `schedule` | `{...weeklySettings...}` |
| `delay` | `{...}` |
| `joystick` | `{...}` (dynamic topic) |
| `temperature` | `{...}` (dynamic topic) |
| `get_state` | `{}` — force a shadow get |
| `refresh_credentials` | `{}` — renew STS |
| `raw_desired` | free-form payload → `state.desired` |
| `raw_dynamic` | `{"description":"...","content":{...}}` |
| `raw_shadow_update` | free-form payload → `$aws/things/<MUS>/shadow/update` |
| `raw_publish` | `{"topic":"...","payload":...}` — escape hatch |

## References

- Reference project: <https://github.com/sh00t2kill/dolphin-robot>
- OTP issue: <https://github.com/sh00t2kill/dolphin-robot/issues/199>
