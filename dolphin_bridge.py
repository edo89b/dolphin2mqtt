"""dolphin2mqtt — bridge between the Maytronics MyDolphin Plus cloud and a local
MQTT broker.

The robot is not reachable directly. Maytronics exposes a REST API
(``mbapp18.maytronics.com``) that, after login, hands out temporary AWS STS
credentials; the robot itself talks to AWS IoT Core through a device shadow.
This bridge logs in, fetches those credentials, connects to AWS IoT over
websockets, mirrors the shadow/telemetry onto local MQTT topics, and translates
local ``cmd/*`` messages back into shadow/dynamic updates.

The protocol details (appkey, endpoints, AES token scheme) are reverse
engineered — see https://github.com/sh00t2kill/dolphin-robot.
"""

import base64
import hashlib
import json
import logging
import os
import secrets
import signal
import threading
import time

import paho.mqtt.client as mqtt
import requests
from awscrt import auth, io, mqtt as awsmqtt
from awsiot import mqtt_connection_builder
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger("dolphin-bridge")

# Reverse-engineered constants from the official MyDolphin Plus app. The appkey
# and integration-version are sent as headers on every REST call.
MAYTRONICS_API = "https://mbapp18.maytronics.com/api"
APPKEY = "346BDE92-53D1-4829-8A2E-B496014B586C"
INTEGRATION_VERSION = "1.0.19"
AWS_ENDPOINT = "a12rqfdx55bdbv-ats.iot.eu-west-1.amazonaws.com"
AWS_REGION = "eu-west-1"

# Configuration (see .env.example). Email/password are required; everything else
# has a sensible default.
EMAIL = os.environ["MAYTRONICS_EMAIL"]
PASSWORD = os.environ["MAYTRONICS_PASSWORD"]
MQTT_HOST = os.getenv("MQTT_HOST", "mosquitto")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_USER = os.getenv("MQTT_USER", "")
MQTT_PASS = os.getenv("MQTT_PASS", "")
MQTT_PREFIX = os.getenv("MQTT_PREFIX", "dolphin")
SHADOW_POLL_SECS = int(os.getenv("SHADOW_POLL_SECS", "300"))
# AWS STS credentials are short-lived, so we re-login and reconnect periodically.
CREDENTIALS_REFRESH_SECS = int(os.getenv("CREDENTIALS_REFRESH_SECS", "3000"))

REST_HEADERS = {
    "appkey": APPKEY,
    "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
    "integration-version": INTEGRATION_VERSION,
}


class MaytronicsApi:
    """Thin client for the Maytronics REST API.

    Handles login, looking up the robot's serials and obtaining the temporary
    AWS IoT credentials used to talk to the device shadow.
    """

    def __init__(self, email: str, password: str):
        self.email = email
        self.password = password
        self.api_token: str | None = None
        # User-facing serial printed on the robot.
        self.serial: str | None = None
        # Internal "motor unit serial" — this is the AWS IoT thing name.
        self.motor_unit_serial: str | None = None
        self.product_info: dict | None = None

    def _post(self, path: str, data: dict, auth_required: bool = False,
              _retry_after_login: bool = True) -> dict:
        """POST helper. Adds the auth token when needed and, on a 401, performs a
        single transparent re-login + retry (tokens expire server-side)."""
        headers = dict(REST_HEADERS)
        if auth_required:
            headers["token"] = self.api_token
        r = requests.post(f"{MAYTRONICS_API}{path}", data=data, headers=headers, timeout=20)
        if auth_required and r.status_code == 401 and _retry_after_login:
            logger.warning("API token expired on %s, re-login and retry", path)
            self.login()
            return self._post(path, data, auth_required=True, _retry_after_login=False)
        r.raise_for_status()
        body = r.json()
        # The API always returns HTTP 200 and signals failures in the body.
        if body.get("Error", {}).get("Status") not in (None, 0, "0"):
            raise RuntimeError(f"Maytronics API error on {path}: {body}")
        return body.get("Data", {})

    def login(self) -> None:
        """Authenticate and capture the session token and the robot serial.

        ``Sernum`` is null until a robot has been associated with the account
        from the mobile app — see the README for the pairing procedure.
        """
        data = self._post("/users/Login/", {"Email": self.email, "Password": self.password})
        self.api_token = data.get("token") or data.get("Token")
        if not self.api_token:
            raise RuntimeError(f"Login: missing token in {data}")
        self.serial = data.get("Sernum") or data.get("SerialNumber")
        if not self.serial:
            raise RuntimeError(
                "Login: no robot associated with this account. "
                "Share the robot from the MyDolphin Plus app to this email."
            )
        logger.info("Login OK serial=%s", self.serial)

    def fetch_motor_unit_serial(self) -> None:
        """Resolve the printed serial into the motor-unit serial (AWS thing name)."""
        data = self._post(
            "/serialnumbers/getrobotdetailsbyrobotsn/",
            {"Sernum": self.serial},
            auth_required=True,
        )
        self.motor_unit_serial = data["eSERNUM"]
        logger.info("Motor-unit-serial=%s", self.motor_unit_serial)

    def fetch_product_info(self) -> None:
        """Fetch model/family metadata, published once on the ``info`` topic."""
        data = self._post(
            "/serialnumbers/getrobotdetailsbymusn/",
            {"Sernum": self.motor_unit_serial},
            auth_required=True,
        )
        self.product_info = data
        logger.info(
            "Robot %s (%s) family=%s",
            data.get("MyRobotName"),
            data.get("PARTNAME"),
            data.get("RobotFamily"),
        )

    def fetch_aws_credentials(self) -> dict:
        """Exchange an encrypted serial token for temporary AWS STS credentials."""
        token = self._encrypt_aws_token()
        data = self._post("/IOT/getToken_DecryptSN/", {"Sernum": token}, auth_required=True)
        return {
            "ak": data["AccessKeyId"],
            "sk": data["SecretAccessKey"],
            "token": data["Token"],
        }

    def _encrypt_aws_token(self) -> str:
        """Build the token the AWS-credentials endpoint expects.

        The motor-unit serial is AES-CBC encrypted with a key derived from the
        first two letters of the email (``md5(email[:2].lower() + "ha")``), then
        ``base64(iv + ciphertext)``. The server rejects '+' in the base64, so we
        retry with fresh random IVs until the output is '+'-free.
        """
        key = hashlib.md5((self.email[:2].lower() + "ha").encode()).digest()
        plaintext = pad(self.motor_unit_serial.encode(), AES.block_size)
        for _ in range(10):
            iv = secrets.token_bytes(16)
            ciphertext = AES.new(key, AES.MODE_CBC, iv).encrypt(plaintext)
            b64 = base64.b64encode(iv + ciphertext).decode()
            if "+" not in b64:
                return b64
        raise RuntimeError("AES token: unable to generate a string without '+' in 10 attempts")


class AwsBridge:
    """Wraps a single AWS IoT Core websocket connection for one robot.

    Subscribes to the device shadow and the ``Maytronics/<MUS>/main`` dynamic
    channel, forwarding every message to the supplied ``on_message`` callback.
    """

    def __init__(self, mus: str, on_message):
        self.mus = mus
        self.on_message = on_message
        self.connection = None
        # awscrt event loop / resolver / bootstrap shared by this connection.
        self._event_loop_group = io.EventLoopGroup(1)
        self._host_resolver = io.DefaultHostResolver(self._event_loop_group)
        self._client_bootstrap = io.ClientBootstrap(self._event_loop_group, self._host_resolver)

    def connect(self, creds: dict) -> None:
        """Open the websocket using SigV4 signing with the temporary creds and
        subscribe to the shadow and dynamic topics."""
        provider = auth.AwsCredentialsProvider.new_static(
            access_key_id=creds["ak"],
            secret_access_key=creds["sk"],
            session_token=creds["token"],
        )
        self.connection = mqtt_connection_builder.websockets_with_default_aws_signing(
            endpoint=AWS_ENDPOINT,
            region=AWS_REGION,
            credentials_provider=provider,
            client_bootstrap=self._client_bootstrap,
            # Random suffix so a reconnect doesn't clash with the old session.
            client_id=f"dolphin-bridge-{secrets.token_hex(4)}",
            clean_session=False,
            keep_alive_secs=30,
            on_connection_interrupted=self._on_interrupted,
            on_connection_resumed=self._on_resumed,
        )
        self.connection.connect().result(timeout=20)
        logger.info("AWS IoT connected")

        # '#' wildcard covers shadow get/update/delete accepted+rejected; the
        # second topic carries dynamic responses (joystick, temperature, ...).
        for topic in (f"$aws/things/{self.mus}/shadow/#", f"Maytronics/{self.mus}/main"):
            sub_future, _ = self.connection.subscribe(
                topic=topic,
                qos=awsmqtt.QoS.AT_LEAST_ONCE,
                callback=self._on_aws_message,
            )
            sub_future.result(timeout=15)
            logger.info("AWS IoT subscribe %s", topic)

    def _on_aws_message(self, topic, payload, **_kwargs):
        # Payloads are usually JSON but fall back to raw text if not.
        try:
            data = json.loads(payload)
        except Exception:
            data = payload.decode("utf-8", errors="replace") if isinstance(payload, bytes) else payload
        try:
            self.on_message(topic, data)
        except Exception as e:
            logger.exception("Error in on_message: %s", e)

    def _on_interrupted(self, connection, error, **_kwargs):
        logger.warning("AWS IoT connection interrupted: %s", error)

    def _on_resumed(self, connection, return_code, session_present, **_kwargs):
        logger.info("AWS IoT connection resumed rc=%s session_present=%s", return_code, session_present)

    def publish(self, topic: str, payload) -> None:
        """Publish to AWS IoT, JSON-encoding dict/list payloads."""
        if isinstance(payload, (dict, list)):
            body = json.dumps(payload)
        elif payload is None:
            body = ""
        else:
            body = payload
        future, _ = self.connection.publish(
            topic=topic,
            payload=body,
            qos=awsmqtt.QoS.AT_LEAST_ONCE,
        )
        future.result(timeout=15)

    def disconnect(self) -> None:
        if self.connection is None:
            return
        try:
            self.connection.disconnect().result(timeout=10)
        except Exception as e:
            logger.warning("Error in AWS IoT disconnect: %s", e)
        finally:
            self.connection = None


class Bridge:
    """Glue between the local MQTT broker and the AWS IoT side.

    Owns the Maytronics API client, the (recreated-on-refresh) ``AwsBridge`` and
    the local MQTT client, plus the background loops that poll the shadow and
    refresh the AWS credentials.
    """

    def __init__(self):
        self.api = MaytronicsApi(EMAIL, PASSWORD)
        self.aws: AwsBridge | None = None
        self.mus: str | None = None
        self.local = mqtt.Client(client_id="dolphin-bridge", clean_session=True)
        if MQTT_USER:
            self.local.username_pw_set(MQTT_USER, MQTT_PASS)
        self.local.on_message = self._on_local_message
        self.local.on_connect = self._on_local_connect
        self._stop = threading.Event()
        # Serializes AWS reconnects between the refresh loop and command handlers.
        self._aws_lock = threading.Lock()
        # Tracks the last published status to avoid redundant retained writes.
        self._status_online: bool | None = None

    def start(self) -> None:
        # Resolve identity first: login -> motor-unit serial -> product info.
        self.api.login()
        self.api.fetch_motor_unit_serial()
        self.api.fetch_product_info()
        self.mus = self.api.motor_unit_serial

        # LWT marks the bridge offline if the process dies unexpectedly.
        self.local.will_set(self._t("bridge/status"), "offline", retain=True)
        self.local.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
        self.local.loop_start()
        self.local.publish(self._t("info"), json.dumps(self.api.product_info), retain=True)
        # subscribe and bridge/status publish live in _on_local_connect: they run
        # again after automatic reconnection too (clean_session=True => without
        # on_connect, a disconnect would leave the retained "offline" from the LWT
        # and lose the cmd/# subscription).
        logger.info("Local MQTT connection to %s:%s started", MQTT_HOST, MQTT_PORT)

        try:
            self._reconnect_aws()
            self._set_status(True)
        except Exception as e:
            logger.error("Initial AWS connection failed: %s", e)
            self._set_status(False, f"Initial AWS connection failed: {e}")
            raise

        # Background workers: rotate credentials and poll the shadow.
        threading.Thread(target=self._refresh_loop, daemon=True).start()
        threading.Thread(target=self._poll_loop, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        if self.aws:
            self.aws.disconnect()
        try:
            # Explicit offline (the LWT only fires on an ungraceful drop).
            self.local.publish(self._t("bridge/status"), "offline", retain=True)
            self.local.loop_stop()
            self.local.disconnect()
        except Exception:
            pass

    def _t(self, suffix: str) -> str:
        """Build a local topic: ``<prefix>/<motor_unit_serial>/<suffix>``."""
        return f"{MQTT_PREFIX}/{self.mus}/{suffix}" if self.mus else f"{MQTT_PREFIX}/{suffix}"

    def _set_status(self, online: bool, error: str = "") -> None:
        """Publish bridge health, skipping no-op transitions. While offline we
        keep refreshing ``last_error`` so the latest cause is always visible."""
        text = error if error else ("OK" if online else "Unspecified error")
        if self._status_online == online:
            if not online:
                self.local.publish(self._t("bridge/last_error"), text, retain=True)
            return
        self._status_online = online
        self.local.publish(self._t("bridge/status"), "online" if online else "offline", retain=True)
        self.local.publish(self._t("bridge/last_error"), text, retain=True)

    def _reconnect_aws(self) -> None:
        """Fetch fresh credentials and rebuild the AWS connection atomically."""
        with self._aws_lock:
            creds = self.api.fetch_aws_credentials()
            if self.aws is not None:
                self.aws.disconnect()
            self.aws = AwsBridge(self.mus, self._on_aws_message)
            self.aws.connect(creds)
            self._request_shadow()

    def _refresh_loop(self) -> None:
        """Periodically rotate the short-lived AWS credentials; retry on failure."""
        while not self._stop.wait(CREDENTIALS_REFRESH_SECS):
            try:
                logger.info("Refreshing AWS IoT credentials")
                self._reconnect_aws()
                self._set_status(True)
            except Exception as e:
                logger.error("Refresh failed: %s — retry in 60s", e)
                self._set_status(False, f"Credentials refresh failed: {e}")
                time.sleep(60)

    def _poll_loop(self) -> None:
        """Periodically request the shadow so retained state stays fresh."""
        while not self._stop.wait(SHADOW_POLL_SECS):
            try:
                self._request_shadow()
                if self.aws is not None:
                    self._set_status(True)
            except Exception as e:
                logger.error("Shadow polling failed: %s", e)
                self._set_status(False, f"Shadow polling failed: {e}")

    def _request_shadow(self) -> None:
        """Ask AWS IoT to emit the current shadow document."""
        if self.aws is None:
            return
        self.aws.publish(f"$aws/things/{self.mus}/shadow/get", "")

    def _on_aws_message(self, topic: str, data) -> None:
        """Mirror an AWS message onto local MQTT.

        Shadow topics are republished under ``shadow/...``, the dynamic channel
        under ``dynamic``, anything else under ``raw/...``. When the payload is a
        shadow document, the ``state.reported`` section is also exploded into
        per-section ``state/<section>`` topics for easy consumption.
        """
        body = json.dumps(data) if not isinstance(data, str) else data

        if topic.startswith(f"$aws/things/{self.mus}/shadow"):
            local_topic = topic.replace(f"$aws/things/{self.mus}/shadow", self._t("shadow"))
        elif topic == f"Maytronics/{self.mus}/main":
            local_topic = self._t("dynamic")
        else:
            local_topic = self._t(f"raw/{topic.replace('/', '_')}")

        self.local.publish(local_topic, body, retain=True)

        # Flatten reported state into individual topics (e.g. state/systemState).
        if isinstance(data, dict):
            reported = (data.get("state") or {}).get("reported")
            if isinstance(reported, dict):
                self.local.publish(self._t("state"), json.dumps(reported), retain=True)
                for section, value in reported.items():
                    payload = json.dumps(value) if isinstance(value, (dict, list)) else str(value)
                    self.local.publish(self._t(f"state/{section}"), payload, retain=True)

    def _on_local_connect(self, _client, _userdata, _flags, rc) -> None:
        # Runs on first connect and every auto-reconnect: re-subscribe to cmd/#
        # and re-assert the retained status (see the note in start()).
        if rc != 0:
            logger.warning("Local MQTT: connect failed rc=%s", rc)
            return
        if self.mus is None:
            return
        self.local.subscribe(self._t("cmd/#"))
        online = self._status_online is not False
        self.local.publish(
            self._t("bridge/status"),
            "online" if online else "offline",
            retain=True,
        )
        logger.info("Local MQTT (re)connected: status=%s, subscribed to cmd/#",
                    "online" if online else "offline")

    def _on_local_message(self, _client, _userdata, msg) -> None:
        # Only ``<prefix>/<MUS>/cmd/<name>`` messages are commands.
        prefix = self._t("cmd/")
        if not msg.topic.startswith(prefix):
            return
        cmd = msg.topic[len(prefix):]
        try:
            payload = json.loads(msg.payload) if msg.payload else {}
        except json.JSONDecodeError:
            # Tolerate non-JSON payloads (e.g. a bare "on"); handlers cope.
            payload = {}
        logger.info("Command %s payload=%s", cmd, payload)
        try:
            self._dispatch(cmd, payload)
        except Exception as e:
            logger.exception("Command %s failed: %s", cmd, e)
            # Surface the failure on a dedicated error topic.
            self.local.publish(self._t(f"cmd/{cmd}/error"), str(e))

    def _publish_desired(self, desired: dict) -> None:
        """Write a desired-state delta to the device shadow."""
        self.aws.publish(
            f"$aws/things/{self.mus}/shadow/update",
            {"state": {"desired": desired}},
        )

    def _publish_dynamic(self, description: str, content) -> None:
        """Send a request on the dynamic channel (used by joystick/temperature)."""
        self.aws.publish(
            f"Maytronics/{self.mus}/main",
            {"type": "pwsRequest", "description": description, "content": content},
        )

    def _dispatch(self, cmd: str, payload) -> None:
        """Translate a local command into the matching shadow/dynamic update.

        High-level commands map to specific shadow sections; the ``raw_*``
        commands are escape hatches that pass payloads through untouched.
        """
        p = payload if isinstance(payload, dict) else {}

        if cmd == "clean_mode":
            # Accept either {"mode": "..."} or a bare string payload.
            mode = p.get("mode") or (payload if isinstance(payload, str) else None)
            if not mode:
                raise ValueError("clean_mode requires {mode: all|floor|water|ultra|pickup}")
            self._publish_desired({"cleaningMode": {"mode": mode}})

        elif cmd == "power":
            state = p.get("state") or (payload if isinstance(payload, str) else None)
            if state not in ("on", "off"):
                raise ValueError("power requires {state: on|off}")
            self._publish_desired({"systemState": {"pwsState": state}})

        elif cmd == "led":
            # Build only the fields that were provided.
            led = {}
            if "mode" in p:
                led["ledMode"] = str(p["mode"])
            if "intensity" in p:
                led["ledIntensity"] = int(p["intensity"])
            if "enable" in p:
                led["ledEnable"] = bool(p["enable"])
            if not led:
                raise ValueError("led requires one of mode|intensity|enable")
            self._publish_desired({"led": led})

        elif cmd == "reset_filter":
            self._publish_desired({"filterBagIndication": {"resetFbi": True}})

        elif cmd == "cycle_time":
            minutes = p.get("minutes")
            if minutes is None:
                raise ValueError("cycle_time requires {minutes: <int>}")
            self._publish_desired({"cycleInfo": {"cycleTime": int(minutes)}})

        elif cmd == "schedule":
            # Payload is passed through as the full weeklySettings object.
            self._publish_desired({"weeklySettings": p})

        elif cmd == "delay":
            self._publish_desired({"delay": p})

        elif cmd == "joystick":
            self._publish_dynamic("joystick", p)

        elif cmd == "temperature":
            self._publish_dynamic("temperature", p)

        elif cmd == "get_state":
            self._request_shadow()

        elif cmd == "refresh_credentials":
            self._reconnect_aws()

        # --- raw escape hatches -------------------------------------------------
        elif cmd == "raw_desired":
            self._publish_desired(p)

        elif cmd == "raw_dynamic":
            self._publish_dynamic(p.get("description", "custom"), p.get("content", {}))

        elif cmd == "raw_shadow_update":
            self.aws.publish(f"$aws/things/{self.mus}/shadow/update", p)

        elif cmd == "raw_publish":
            topic = p.get("topic")
            if not topic:
                raise ValueError("raw_publish requires {topic, payload}")
            self.aws.publish(topic, p.get("payload", {}))

        else:
            raise ValueError(f"Unknown command: {cmd}")


def main() -> None:
    bridge = Bridge()
    bridge.start()

    # Block until SIGINT/SIGTERM, then shut down cleanly.
    stop_event = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop_event.set())
    try:
        stop_event.wait()
    finally:
        bridge.stop()
        logger.info("Bridge stopped.")


if __name__ == "__main__":
    main()
