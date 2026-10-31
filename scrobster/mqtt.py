"""Home Assistant devices over MQTT discovery.

scrobSter publishes one device for itself, one per room and one per user, and
Home Assistant builds the entities from them: what each room hears, its cover
art and level, each user's last scrobble, and a switch per room for "scrobble
for me". A room also has an event entity that fires when it starts hearing a song,
for automations.

Everything is pushed, so Home Assistant polls nothing. A change is published
when it happens, from the events in events.py, and the rest of the state, such
as a room's level, is looked at every TICK_SECONDS and sent only when it moved.

Commands come back on `<topic>/<instance>/.../set`. They run through the same
functions as the API, but the broker is the only gate: whoever can publish
there can start and stop listening, change the rates, and choose rooms for any
user. Keep the broker private, as Home Assistant's own Mosquitto add-on is.
Camera URLs and service credentials are never published.

A device that disappears, such as a deleted room, is removed from Home
Assistant too, even when it was deleted while scrobSter was not connected,
because the retained discovery messages on the broker are read back on
connect.
"""
import asyncio
import contextlib
import json
import logging
import secrets
import time

import aiohttp
import aiomqtt

from . import __version__, accounts, config, db, events, rooms, scrobble

log = logging.getLogger("scrobster")

TICK_SECONDS = 5
# Users' history counts are read from the database at most this often, unless
# an event says one changed.
USER_REFRESH_SECONDS = 60
# How long to collect the retained discovery messages after subscribing.
RETAINED_WAIT_SECONDS = 2
RECONNECT_SECONDS = (5, 300)
# Inside an add-on, how often to ask again for a broker that is not there yet,
# such as Mosquitto installed after scrobSter started.
BROKER_RETRY_SECONDS = 300
PROJECT_URL = "https://github.com/benkelly/scrobSter"


# --- where to connect ------------------------------------------------------------

async def broker_settings(warn=True) -> dict | None:
    """The broker to use, or None to stay off.

    MQTT_HOST names one. Inside a Home Assistant add-on with `services:
    mqtt:want`, the Supervisor hands over the broker and its credentials, so
    nothing needs typing.
    """
    if config.MQTT_ENABLED in ("0", "false", "no", "off"):
        return None
    if config.MQTT_HOST:
        return {"host": config.MQTT_HOST, "port": config.MQTT_PORT,
                "username": config.MQTT_USERNAME, "password": config.MQTT_PASSWORD,
                "tls": config.MQTT_TLS}
    if config.SUPERVISOR_TOKEN:
        found = await _supervisor_broker()
        if found:
            return found
    if warn and config.MQTT_ENABLED in ("1", "true", "yes", "on"):
        log.warning("MQTT is turned on but no broker was found: install the Mosquitto"
                    " broker add-on, or set MQTT_HOST")
    return None


async def _supervisor_broker() -> dict | None:
    headers = {"Authorization": f"Bearer {config.SUPERVISOR_TOKEN}"}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
            async with s.get("http://supervisor/services/mqtt", headers=headers) as r:
                if r.status != 200:
                    # 400 means no add-on provides MQTT.
                    log.debug("MQTT: the Supervisor answered %s", r.status)
                    return None
                data = (await r.json()).get("data") or {}
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
        log.warning("MQTT: could not ask the Supervisor for the broker: %s", e)
        return None
    if not data.get("host"):
        return None
    return {"host": data["host"], "port": int(data.get("port") or 1883),
            "username": data.get("username"), "password": data.get("password"),
            "tls": bool(data.get("ssl"))}


def instance_id() -> str:
    """Tells this install's devices from another install's on the same broker.
    Made once and kept, so the entities keep their history across restarts."""
    value = db.get_setting("mqtt_instance")
    if not value:
        value = secrets.token_hex(3)
        db.set_setting("mqtt_instance", value)
    return value


# --- what is published ------------------------------------------------------------

class Topics:
    def __init__(self, base, prefix, iid):
        self.root = f"{base}/{iid}"
        self.prefix = prefix
        self.iid = iid
        self.status = f"{self.root}/status"
        self.hub = f"{self.root}/state"
        self.ha_status = f"{prefix}/status"
        self.discovery_wildcard = f"{prefix}/device/+/config"
        self.commands = [f"{self.root}/+/set", f"{self.root}/room/+/enabled/set",
                         f"{self.root}/user/+/room/+/set"]

    def room(self, room_id):
        return f"{self.root}/room/{room_id}/state"

    def room_cover(self, room_id):
        return f"{self.root}/room/{room_id}/cover"

    def room_event(self, room_id):
        return f"{self.root}/room/{room_id}/event"

    def user(self, user_id):
        return f"{self.root}/user/{user_id}/state"

    def device_id(self, kind=None, item_id=None):
        return f"scrobster_{self.iid}" + (f"_{kind}_{item_id}" if kind else "")

    def owns(self, device_id):
        mine = self.device_id()
        return device_id == mine or device_id.startswith(f"{mine}_")

    def config(self, device_id):
        return f"{self.prefix}/device/{device_id}/config"

    def states_of(self, device_id) -> list[str]:
        """The retained topics of one of this install's devices, from its id."""
        rest = device_id[len(self.device_id()):].lstrip("_")
        if not rest:
            return [self.hub]
        kind, _, item_id = rest.partition("_")
        if kind == "room":
            return [self.room(item_id), self.room_cover(item_id)]
        return [self.user(item_id)] if kind == "user" else []


def _track(match):
    if not match:
        return None
    return {k: match.get(k) for k in ("artist", "title", "album", "art_url", "offset",
                                      "isrc", "ts", "source", "room")}


def _line(match):
    return f"{match['artist']} - {match['title']}" if match else None


def snapshot(listener, users=None) -> dict:
    """What Home Assistant should show, as plain data. `users` is the users'
    part, which reads the database, from user_snapshot()."""
    now = time.time()
    room_list = []
    for room_id in sorted(listener.rooms):
        room = listener.rooms[room_id]
        match = room.last_match
        playing = bool(match and now - match["ts"] < config.NOW_PLAYING_STOP_SECONDS)
        room_list.append({
            "id": room_id, "name": room.name, "kind": room.kind,
            "enabled": room.enabled, "running": room.running,
            "level_db": None if room.last_level_db is None else round(room.last_level_db),
            "error": room.last_error, "playing": playing,
            "problem": room.outage.since is not None,
            "failing_since": (time.strftime("%Y-%m-%dT%H:%M:%S%z",
                                            time.localtime(room.outage.since))
                              if room.outage.since else None),
            "failures": room.outage.tries,
            "now_playing": _line(match) if playing else "idle",
            "track": _track(match), "art_url": match.get("art_url") if match else None,
        })
    return {
        "hub": {"listening": listener.listening,
                "requests": listener.requests_recently(),
                "rate_limited": listener.rate_limited_for() > 0,
                "gap": config.RECOGNITION_GAP_SECONDS, "interval": config.MATCH_INTERVAL},
        "rooms": room_list,
        "users": users or [],
    }


def user_snapshot() -> list[dict]:
    today = time.localtime()
    midnight = time.mktime((today.tm_year, today.tm_mon, today.tm_mday, 0, 0, 0, 0, 0, -1))
    out = []
    for user in accounts.list_users():
        last = db.recent(user["id"], 1)
        last = last[0] if last else None
        out.append({
            "id": user["id"], "username": user["username"],
            "rooms": rooms.rooms_of(user["id"]),
            "today": db.scrobbled_since(user["id"], int(midnight)),
            "services": sorted(scrobble.enabled_services(
                accounts.get_credentials(user["id"]))),
            "last": None if last is None else {
                **{k: last[k] for k in ("artist", "title", "album", "art_url", "ts",
                                        "room", "source")},
                "services": last["services"]},
        })
    return out


def states(topics, snap) -> dict:
    """{topic: payload} for every retained state topic."""
    out = {topics.hub: snap["hub"]}
    covers = {}
    for room in snap["rooms"]:
        out[topics.room(room["id"])] = {k: v for k, v in room.items() if k != "id"}
        # Its own topic, sent only once there is a cover, because Home
        # Assistant logs an error for an image with no URL.
        if room["art_url"]:
            covers[topics.room_cover(room["id"])] = room["art_url"]
    for user in snap["users"]:
        out[topics.user(user["id"])] = {
            "username": user["username"], "rooms": user["rooms"], "today": user["today"],
            "services": ", ".join(user["services"]) or "none",
            "last_line": _line(user["last"]) or "none", "last": user["last"]}
    return {**{topic: json.dumps(payload, sort_keys=True) for topic, payload in out.items()},
            **covers}


def _device(topics, device_id, name, model, via=False, area=None):
    device = {"identifiers": [device_id], "name": name, "manufacturer": "scrobSter",
              "model": model, "sw_version": __version__}
    if via:
        device["via_device"] = topics.device_id()
    if area:
        device["suggested_area"] = area
    return device


def _payload(topics, device, state_topic, components):
    for key, component in components.items():
        component.setdefault("unique_id", f"{device['identifiers'][0]}_{key}")
        if component["platform"] != "image":
            component.setdefault("state_topic", state_topic)
        # An entity named None takes the device's name, which reads badly here.
        component.setdefault("name", key.replace("_", " ").capitalize())
    return {"device": device,
            "origin": {"name": "scrobSter", "sw_version": __version__,
                       "support_url": PROJECT_URL},
            "availability_topic": topics.status, "qos": 1, "components": components}


def hub_config(topics) -> dict:
    root = topics.root
    return _payload(topics, _device(topics, topics.device_id(), "scrobSter", "Recognizer"),
                    topics.hub, {
        "listening": {"platform": "switch", "icon": "mdi:ear-hearing",
                      "command_topic": f"{root}/listening/set",
                      "value_template": "{{ 'ON' if value_json.listening else 'OFF' }}"},
        "rate_limited": {"platform": "binary_sensor", "device_class": "problem",
                         "name": "Shazam rate limit", "entity_category": "diagnostic",
                         "value_template": "{{ 'ON' if value_json.rate_limited else 'OFF' }}"},
        "requests": {"platform": "sensor", "name": "Shazam requests (10 min)",
                     "icon": "mdi:counter", "state_class": "measurement",
                     "unit_of_measurement": "requests", "entity_category": "diagnostic",
                     "value_template": "{{ value_json.requests }}"},
        "gap": {"platform": "number", "name": "Seconds between Shazam requests",
                "icon": "mdi:timer-sand", "entity_category": "config", "mode": "box",
                "min": config.RECOGNITION_GAP_RANGE[0], "max": config.RECOGNITION_GAP_RANGE[1],
                "step": 0.5, "unit_of_measurement": "s",
                "command_topic": f"{root}/gap/set", "value_template": "{{ value_json.gap }}"},
        "interval": {"platform": "number", "name": "Seconds between listens",
                     "icon": "mdi:timer-outline", "entity_category": "config", "mode": "box",
                     "min": config.MATCH_INTERVAL_RANGE[0],
                     "max": config.MATCH_INTERVAL_RANGE[1], "step": 1,
                     "unit_of_measurement": "s", "command_topic": f"{root}/interval/set",
                     "value_template": "{{ value_json.interval }}"},
    })


def room_config(topics, room) -> dict:
    device_id = topics.device_id("room", room["id"])
    model = {"stream": "Camera or stream", "browser": "Browser in the room"}.get(
        room["kind"], "Microphone")
    track = "{{ (value_json.track or {}) | tojson }}"
    return _payload(topics, _device(topics, device_id, f"scrobSter {room['name']}", model,
                                    via=True, area=room["name"]),
                    topics.room(room["id"]), {
        "now_playing": {"platform": "sensor", "icon": "mdi:music",
                        "value_template": "{{ value_json.now_playing }}",
                        "json_attributes_topic": topics.room(room["id"]),
                        "json_attributes_template": track},
        "cover": {"platform": "image", "name": "Cover art",
                  "url_topic": topics.room_cover(room["id"])},
        "playing": {"platform": "binary_sensor", "name": "Music", "icon": "mdi:music-note",
                    "value_template": "{{ 'ON' if value_json.playing else 'OFF' }}"},
        "level": {"platform": "sensor", "name": "Input level", "icon": "mdi:microphone",
                  "unit_of_measurement": "dBFS", "state_class": "measurement",
                  "suggested_display_precision": 0,
                  "value_template": "{{ value_json.level_db }}"},
        "running": {"platform": "binary_sensor", "name": "Recording",
                    "device_class": "running", "entity_category": "diagnostic",
                    "value_template": "{{ 'ON' if value_json.running else 'OFF' }}"},
        "problem": {"platform": "binary_sensor", "name": "Problem",
                    "device_class": "problem",
                    "value_template": "{{ 'ON' if value_json.problem else 'OFF' }}",
                    "json_attributes_topic": topics.room(room["id"]),
                    "json_attributes_template": (
                        "{{ {'failing_since': value_json.failing_since,"
                        " 'failures': value_json.failures,"
                        " 'reason': value_json.error} | tojson }}")},
        "error": {"platform": "sensor", "name": "Last error", "icon": "mdi:alert-circle",
                  "entity_category": "diagnostic",
                  "value_template": "{{ value_json.error or 'none' }}"},
        "enabled": {"platform": "switch", "name": "Listen here", "icon": "mdi:ear-hearing",
                    "entity_category": "config",
                    "command_topic": f"{topics.root}/room/{room['id']}/enabled/set",
                    "value_template": "{{ 'ON' if value_json.enabled else 'OFF' }}"},
        "match": {"platform": "event", "name": "Match", "icon": "mdi:music-circle",
                  "state_topic": topics.room_event(room["id"]), "event_types": ["match"]},
    })


def user_config(topics, user, room_list) -> dict:
    device_id = topics.device_id("user", user["id"])
    components = {
        "last_scrobble": {"platform": "sensor", "icon": "mdi:playlist-music",
                          "value_template": "{{ value_json.last_line }}",
                          "json_attributes_topic": topics.user(user["id"]),
                          "json_attributes_template": "{{ (value_json.last or {}) | tojson }}"},
        "today": {"platform": "sensor", "name": "Scrobbles today", "icon": "mdi:counter",
                  "state_class": "total_increasing", "unit_of_measurement": "scrobbles",
                  "value_template": "{{ value_json.today }}"},
        "services": {"platform": "sensor", "icon": "mdi:link-variant",
                     "entity_category": "diagnostic",
                     "value_template": "{{ value_json.services }}"},
    }
    for room in room_list:
        components[f"room_{room['id']}"] = {
            "platform": "switch", "name": f"Scrobble {room['name']}",
            "icon": "mdi:account-music",
            "command_topic": f"{topics.root}/user/{user['id']}/room/{room['id']}/set",
            "value_template": f"{{{{ 'ON' if {room['id']} in value_json.rooms else 'OFF' }}}}"}
    return _payload(topics, _device(topics, device_id, f"scrobSter {user['username']}",
                                    "User", via=True),
                    topics.user(user["id"]), components)


def configs(topics, snap) -> dict:
    """{device_id: discovery payload} for every device there is now."""
    out = {topics.device_id(): hub_config(topics)}
    for room in snap["rooms"]:
        out[topics.device_id("room", room["id"])] = room_config(topics, room)
    for user in snap["users"]:
        out[topics.device_id("user", user["id"])] = user_config(topics, user, snap["rooms"])
    return out


def with_removals(payload, before: dict) -> dict:
    """A discovery payload that also removes the components published before
    and gone now. Home Assistant drops a component sent with its platform only."""
    gone = {key: {"platform": platform} for key, platform in before.items()
            if key not in payload["components"]}
    if not gone:
        return payload
    return {**payload, "components": {**payload["components"], **gone}}


# --- commands ----------------------------------------------------------------------

def _on(payload: str) -> bool:
    return payload.strip().upper() in ("ON", "1", "TRUE")


async def handle_command(listener, topics, topic: str, payload: str) -> bool:
    """Act on one command. False when the topic or the value means nothing."""
    parts = topic[len(topics.root) + 1:].split("/")
    if parts == ["listening", "set"]:
        if _on(payload):
            listener.start()
        else:
            await listener.stop()
        return True
    if len(parts) == 2 and parts[0] in ("gap", "interval") and parts[1] == "set":
        low, high = (config.RECOGNITION_GAP_RANGE if parts[0] == "gap"
                     else config.MATCH_INTERVAL_RANGE)
        try:
            value = float(payload)
        except ValueError:
            return False
        if not low <= value <= high:
            return False
        if parts[0] == "gap":
            listener.save_rates(gap=value)
        else:
            listener.save_rates(interval=int(value))
        return True
    if len(parts) == 4 and parts[0] == "room" and parts[2:] == ["enabled", "set"]:
        try:
            rooms.update_room(int(parts[1]), enabled=_on(payload))
        except (ValueError, LookupError):
            return False
        await listener.sync_rooms()
        return True
    if len(parts) == 5 and parts[0] == "user" and parts[2] == "room" and parts[4] == "set":
        try:
            user_id, room_id = int(parts[1]), int(parts[3])
        except ValueError:
            return False
        if accounts.get_user(user_id) is None or rooms.get_room(room_id) is None:
            return False
        rooms.set_member(room_id, user_id, _on(payload))
        return True
    return False


# --- the connection ----------------------------------------------------------------

class Publisher:
    def __init__(self, listener):
        self.listener = listener
        self.connected = False

    async def run(self):
        """Keep a connection to the broker for as long as scrobSter runs."""
        settings = await self._find_broker()
        if settings is None:
            return
        topics = Topics(config.MQTT_TOPIC, config.MQTT_DISCOVERY_PREFIX, instance_id())
        wait = RECONNECT_SECONDS[0]
        while True:
            try:
                await self._session(settings, topics)
            except aiomqtt.MqttError as e:
                log.warning("MQTT: %s, connecting again in %ss", e, wait)
            except Exception:
                log.exception("MQTT: publishing failed, connecting again in %ss", wait)
            self.connected = False
            await asyncio.sleep(wait)
            wait = min(wait * 2, RECONNECT_SECONDS[1])
            if not config.MQTT_HOST:
                # The Supervisor's broker may have moved or changed its password.
                settings = await broker_settings(warn=False) or settings

    async def _find_broker(self):
        """The broker, or None to stay off. Inside an add-on, keep asking: the
        Mosquitto add-on may be installed after scrobSter started."""
        settings = await broker_settings()
        while settings is None and config.SUPERVISOR_TOKEN \
                and config.MQTT_ENABLED not in ("0", "false", "no", "off"):
            await asyncio.sleep(BROKER_RETRY_SECONDS)
            settings = await broker_settings(warn=False)
            if settings:
                log.info("MQTT: Home Assistant now offers a broker")
        return settings

    async def _session(self, settings, topics):
        will = aiomqtt.Will(topics.status, "offline", qos=1, retain=True)
        async with aiomqtt.Client(
                settings["host"], settings["port"], username=settings.get("username"),
                password=settings.get("password"), identifier=f"scrobster-{topics.iid}",
                will=will,
                tls_params=aiomqtt.TLSParameters() if settings.get("tls") else None) as client:
            log.info("MQTT: connected to %s:%s, publishing to Home Assistant",
                     settings["host"], settings["port"])
            self.connected = True
            with events.subscribe() as sub:
                session = _Session(self.listener, client, topics, sub)
                try:
                    await session.run()
                except asyncio.CancelledError:
                    # A clean disconnect sends no will, so say it ourselves.
                    with contextlib.suppress(aiomqtt.MqttError, asyncio.TimeoutError):
                        await asyncio.shield(client.publish(
                            topics.status, "offline", qos=1, retain=True))
                    raise


class _Session:
    """One connection: read the retained devices, publish, then follow."""

    def __init__(self, listener, client, topics, sub):
        self.listener = listener
        self.client = client
        self.topics = topics
        self.sub = sub
        self.sent = {}  # topic -> payload, retained, as the broker has it
        self.components = {}  # device id -> {component key: platform}
        self.users = None
        self.users_at = 0.0
        self.resend = False
        self.heard = {}  # room id -> (track key, ts) of its last match

    async def run(self):
        reader = asyncio.create_task(self._read())
        try:
            await self.client.subscribe(self.topics.discovery_wildcard)
            await asyncio.sleep(RETAINED_WAIT_SECONDS)
            await self.client.unsubscribe(self.topics.discovery_wildcard)
            for topic in self.topics.commands + [self.topics.ha_status]:
                await self.client.subscribe(topic, qos=1)
            await self._publish(full=True)
            await self.client.publish(self.topics.status, "online", qos=1, retain=True)
            while True:
                if reader.done():
                    reader.result()  # raises what ended the connection
                    return
                happened = await self.sub.wait(TICK_SECONDS)
                await self._follow(happened)
        finally:
            reader.cancel()

    async def _read(self):
        async for message in self.client.messages:
            topic = message.topic.value
            payload = message.payload.decode(errors="replace") if message.payload else ""
            if topic.startswith(f"{self.topics.prefix}/device/"):
                self._remember(topic, payload)
            elif topic == self.topics.ha_status:
                # Home Assistant restarted. Everything is retained, but its
                # documentation asks for the discovery to be sent again.
                self.resend = payload == "online"
            else:
                try:
                    if not await handle_command(self.listener, self.topics, topic, payload):
                        log.warning("MQTT: ignored %r on %s", payload[:40], topic)
                except Exception:
                    log.exception("MQTT: command on %s failed", topic)

    def _remember(self, topic, payload):
        """A retained discovery message from before, ours or another install's."""
        device_id = topic.split("/")[-2]
        if not self.topics.owns(device_id) or not payload:
            return
        try:
            components = json.loads(payload).get("components") or {}
        except (ValueError, AttributeError):
            components = {}
        self.components[device_id] = {key: c.get("platform") for key, c in components.items()
                                      if isinstance(c, dict) and c.get("platform")}
        self.sent[self.topics.config(device_id)] = payload

    async def _follow(self, happened):
        kinds = {event["kind"] for event in happened}
        for event in happened:
            if event["kind"] == "match" and event.get("room_id") is not None:
                await self._send_match(event)
        users_changed = bool(kinds & {"users", "member", "services", "scrobble", "rooms"})
        if self.sub.dropped:
            self.sub.dropped = 0
            users_changed = True
        full = bool(kinds & {"users", "rooms"}) or self.resend
        if self.resend:
            self.resend = False
            self.sent = {}
        await self._publish(full=full, users=users_changed)

    def starts_song(self, room_id, match) -> bool:
        """A match goes on every cycle while a song plays. The event fires once,
        when the room starts hearing it."""
        before = self.heard.get(room_id)
        self.heard[room_id] = (match.get("track_key"), match["ts"])
        return not (before and before[0] == match.get("track_key")
                    and match["ts"] - before[1] < config.NOW_PLAYING_STOP_SECONDS)

    async def _send_match(self, event):
        match = event["match"]
        if not self.starts_song(event["room_id"], match):
            return
        names = {u["id"]: u["username"] for u in (self.users or [])}
        payload = {"event_type": "match", **(_track(match) or {}),
                   "users": [names.get(u, u) for u in event["users"]]}
        await self.client.publish(self.topics.room_event(event["room_id"]),
                                  json.dumps(payload), qos=1)

    async def _publish(self, full=False, users=False):
        now = time.monotonic()
        if users or self.users is None or now - self.users_at > USER_REFRESH_SECONDS:
            self.users = user_snapshot()
            self.users_at = now
        snap = snapshot(self.listener, self.users)
        if full:
            await self._publish_configs(snap)
        for topic, payload in states(self.topics, snap).items():
            await self._send(topic, payload)

    async def _publish_configs(self, snap):
        current = configs(self.topics, snap)
        for device_id, payload in current.items():
            payload = with_removals(payload, self.components.get(device_id, {}))
            await self._send(self.topics.config(device_id), json.dumps(payload, sort_keys=True))
            self.components[device_id] = {key: c["platform"] for key, c
                                          in payload["components"].items() if len(c) > 1}
        for device_id in [d for d in self.components if d not in current]:
            # Removing the config removes the device and its entities.
            await self._send(self.topics.config(device_id), "")
            for topic in self.topics.states_of(device_id):
                await self._send(topic, "", force=True)
            del self.components[device_id]

    async def _send(self, topic, payload, force=False):
        if self.sent.get(topic) == payload and not force:
            return
        await self.client.publish(topic, payload, qos=1, retain=True)
        if payload:
            self.sent[topic] = payload
        else:
            self.sent.pop(topic, None)
