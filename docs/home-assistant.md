# Home Assistant

scrobSter is available as a Home Assistant add-on from
https://github.com/benkelly/ha-addons. The add-on runs this same image and takes
the microphone from the Home Assistant audio system, so you choose the input in
the add-on configuration.

The app also reads `/data/options.json` directly, which is where the Supervisor
writes add-on options. Each key matches an environment variable, in either case.
No wrapper script is needed.

This is how the add-on connects to Home Assistant:

```mermaid
flowchart TD
    accTitle: The add-on inside Home Assistant
    accDescr: You reach the scrobSter add-on through Home Assistant ingress. The Supervisor gives it its options file and the MQTT broker's address and login. A microphone reaches it through Home Assistant's PulseAudio and ALSA. The add-on sends discovery and states to the Mosquitto broker and takes switch and number commands back, and the broker shows them as devices and entities in Home Assistant.
    you["You, signed in to Home Assistant"] -- ingress --> addon["scrobSter add-on"]
    sup["Supervisor"] -- "/data/options.json" --> addon
    sup -- "broker address and login" --> addon
    mic["Microphone"] --> pulse["Home Assistant audio, PulseAudio"]
    pulse -- "ALSA, via asound.conf" --> addon
    addon -- "discovery and states" --> broker["Mosquitto broker"]
    broker -- "switch and number commands" --> addon
    broker --> ha["Devices and entities in Home Assistant"]
```

Ingress signs you in as the owner, so the page asks for no password.

## Devices over MQTT

scrobSter publishes its rooms and users to Home Assistant through MQTT
discovery, so they appear as devices with no YAML. The add-on finds the
Mosquitto broker by itself. Anywhere else, set `MQTT_HOST`, and the credentials
if the broker needs them.

| Device | Entities |
|---|---|
| **scrobSter** | Listening switch, Shazam rate limit, Shazam requests in the last ten minutes, and the two recognition rates as numbers |
| **One per room**, in the Home Assistant area of the same name | Now playing (`Artist - Title`, or `idle`), with the track as attributes; Cover art; Music; Input level in dBFS; Recording; Problem, on while the room keeps failing, with `failing_since` and the reason; Last error; a Listen here switch; and a Match event |
| **One per user** | Last scrobble, with the track as attributes; Scrobbles today; Services; and a Scrobble switch for every room |

Everything is pushed when it changes, so Home Assistant polls nothing. Rooms and
users appear and disappear as they are added and removed, also when that
happened while scrobSter was not connected. When scrobSter stops, its entities
show as unavailable.

The Match event fires when a room starts hearing a song: a new track, or the
same one after `NOW_PLAYING_STOP_SECONDS` of quiet. It carries the artist,
title, album, cover, and the users who received it, so an automation can react
to a song:

```yaml
automation:
  - alias: Dim the kitchen for Daft Punk
    triggers:
      - trigger: state
        entity_id: event.scrobster_kitchen_match
    conditions:
      - condition: template
        value_template: "{{ trigger.to_state.attributes.artist == 'Daft Punk' }}"
    actions:
      - action: light.turn_on
        target: {entity_id: light.kitchen}
        data: {brightness_pct: 30}
```

**The broker is the gate for commands.** Its switches and numbers act like an
administrator: whoever can publish to the broker can start and stop listening,
change the rates, and choose rooms for any user. Keep the broker private, as
the Mosquitto add-on is. Camera URLs and service credentials are never
published.

Each install has its own instance id, kept in the database, so two installs can
share one broker. Inside scrobSter, every change goes through one event stream
(`scrobster/events.py`). MQTT is one reader of it, and `GET /api/events` serves
it to any other client, such as a script or a future Home Assistant
integration:

```sh
curl -N -H "Authorization: Bearer your-token" http://your-host:8000/api/events
```

Without MQTT, a REST sensor can read `/api/status`. See [API](api.md).
