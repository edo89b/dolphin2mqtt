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

MAYTRONICS_API = "https://mbapp18.maytronics.com/api"
APPKEY = "346BDE92-53D1-4829-8A2E-B496014B586C"
INTEGRATION_VERSION = "1.0.19"
AWS_ENDPOINT = "a12rqfdx55bdbv-ats.iot.eu-west-1.amazonaws.com"
AWS_REGION = "eu-west-1"

EMAIL = os.environ["MAYTRONICS_EMAIL"]
PASSWORD = os.environ["MAYTRONICS_PASSWORD"]
MQTT_HOST = os.getenv("MQTT_HOST", "mosquitto")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_USER = os.getenv("MQTT_USER", "")
MQTT_PASS = os.getenv("MQTT_PASS", "")
MQTT_PREFIX = os.getenv("MQTT_PREFIX", "dolphin")
SHADOW_POLL_SECS = int(os.getenv("SHADOW_POLL_SECS", "300"))
CREDENTIALS_REFRESH_SECS = int(os.getenv("CREDENTIALS_REFRESH_SECS", "3000"))

REST_HEADERS = {
    "appkey": APPKEY,
    "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
    "integration-version": INTEGRATION_VERSION,
}


class MaytronicsApi:
    def __init__(self, email: str, password: str):
        self.email = email
        self.password = password
        self.api_token: str | None = None
        self.serial: str | None = None
        self.motor_unit_serial: str | None = None
        self.product_info: dict | None = None

    def _post(self, path: str, data: dict, auth_required: bool = False,
              _retry_after_login: bool = True) -> dict:
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
        if body.get("Error", {}).get("Status") not in (None, 0, "0"):
            raise RuntimeError(f"Maytronics API error on {path}: {body}")
        return body.get("Data", {})

    def login(self) -> None:
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
        data = self._post(
            "/serialnumbers/getrobotdetailsbyrobotsn/",
            {"Sernum": self.serial},
            auth_required=True,
        )
        self.motor_unit_serial = data["eSERNUM"]
        logger.info("Motor-unit-serial=%s", self.motor_unit_serial)

    def fetch_product_info(self) -> None:
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
        token = self._encrypt_aws_token()
        data = self._post("/IOT/getToken_DecryptSN/", {"Sernum": token}, auth_required=True)
        return {
            "ak": data["AccessKeyId"],
            "sk": data["SecretAccessKey"],
            "token": data["Token"],
        }

    def _encrypt_aws_token(self) -> str:
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
    def __init__(self, mus: str, on_message):
        self.mus = mus
        self.on_message = on_message
        self.connection = None
        self._event_loop_group = io.EventLoopGroup(1)
        self._host_resolver = io.DefaultHostResolver(self._event_loop_group)
        self._client_bootstrap = io.ClientBootstrap(self._event_loop_group, self._host_resolver)

    def connect(self, creds: dict) -> None:
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
            client_id=f"dolphin-bridge-{secrets.token_hex(4)}",
            clean_session=False,
            keep_alive_secs=30,
            on_connection_interrupted=self._on_interrupted,
            on_connection_resumed=self._on_resumed,
        )
        self.connection.connect().result(timeout=20)
        logger.info("AWS IoT connected")

        for topic in (f"$aws/things/{self.mus}/shadow/#", f"Maytronics/{self.mus}/main"):
            sub_future, _ = self.connection.subscribe(
                topic=topic,
                qos=awsmqtt.QoS.AT_LEAST_ONCE,
                callback=self._on_aws_message,
            )
            sub_future.result(timeout=15)
            logger.info("AWS IoT subscribe %s", topic)

    def _on_aws_message(self, topic, payload, **_kwargs):
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
        self._aws_lock = threading.Lock()
        self._status_online: bool | None = None

    def start(self) -> None:
        self.api.login()
        self.api.fetch_motor_unit_serial()
        self.api.fetch_product_info()
        self.mus = self.api.motor_unit_serial

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

        threading.Thread(target=self._refresh_loop, daemon=True).start()
        threading.Thread(target=self._poll_loop, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        if self.aws:
            self.aws.disconnect()
        try:
            self.local.publish(self._t("bridge/status"), "offline", retain=True)
            self.local.loop_stop()
            self.local.disconnect()
        except Exception:
            pass

    def _t(self, suffix: str) -> str:
        return f"{MQTT_PREFIX}/{self.mus}/{suffix}" if self.mus else f"{MQTT_PREFIX}/{suffix}"

    def _set_status(self, online: bool, error: str = "") -> None:
        text = error if error else ("OK" if online else "Unspecified error")
        if self._status_online == online:
            if not online:
                self.local.publish(self._t("bridge/last_error"), text, retain=True)
            return
        self._status_online = online
        self.local.publish(self._t("bridge/status"), "online" if online else "offline", retain=True)
        self.local.publish(self._t("bridge/last_error"), text, retain=True)

    def _reconnect_aws(self) -> None:
        with self._aws_lock:
            creds = self.api.fetch_aws_credentials()
            if self.aws is not None:
                self.aws.disconnect()
            self.aws = AwsBridge(self.mus, self._on_aws_message)
            self.aws.connect(creds)
            self._request_shadow()

    def _refresh_loop(self) -> None:
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
        while not self._stop.wait(SHADOW_POLL_SECS):
            try:
                self._request_shadow()
                if self.aws is not None:
                    self._set_status(True)
            except Exception as e:
                logger.error("Shadow polling failed: %s", e)
                self._set_status(False, f"Shadow polling failed: {e}")

    def _request_shadow(self) -> None:
        if self.aws is None:
            return
        self.aws.publish(f"$aws/things/{self.mus}/shadow/get", "")

    def _on_aws_message(self, topic: str, data) -> None:
        body = json.dumps(data) if not isinstance(data, str) else data

        if topic.startswith(f"$aws/things/{self.mus}/shadow"):
            local_topic = topic.replace(f"$aws/things/{self.mus}/shadow", self._t("shadow"))
        elif topic == f"Maytronics/{self.mus}/main":
            local_topic = self._t("dynamic")
        else:
            local_topic = self._t(f"raw/{topic.replace('/', '_')}")

        self.local.publish(local_topic, body, retain=True)

        if isinstance(data, dict):
            reported = (data.get("state") or {}).get("reported")
            if isinstance(reported, dict):
                self.local.publish(self._t("state"), json.dumps(reported), retain=True)
                for section, value in reported.items():
                    payload = json.dumps(value) if isinstance(value, (dict, list)) else str(value)
                    self.local.publish(self._t(f"state/{section}"), payload, retain=True)

    def _on_local_connect(self, _client, _userdata, _flags, rc) -> None:
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
        prefix = self._t("cmd/")
        if not msg.topic.startswith(prefix):
            return
        cmd = msg.topic[len(prefix):]
        try:
            payload = json.loads(msg.payload) if msg.payload else {}
        except json.JSONDecodeError:
            payload = {}
        logger.info("Command %s payload=%s", cmd, payload)
        try:
            self._dispatch(cmd, payload)
        except Exception as e:
            logger.exception("Command %s failed: %s", cmd, e)
            self.local.publish(self._t(f"cmd/{cmd}/error"), str(e))

    def _publish_desired(self, desired: dict) -> None:
        self.aws.publish(
            f"$aws/things/{self.mus}/shadow/update",
            {"state": {"desired": desired}},
        )

    def _publish_dynamic(self, description: str, content) -> None:
        self.aws.publish(
            f"Maytronics/{self.mus}/main",
            {"type": "pwsRequest", "description": description, "content": content},
        )

    def _dispatch(self, cmd: str, payload) -> None:
        p = payload if isinstance(payload, dict) else {}

        if cmd == "clean_mode":
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
