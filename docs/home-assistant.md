# Home Assistant

scrobSter is available as a Home Assistant add-on from
https://github.com/benkelly/ha-addons. The add-on runs this same image and takes
the microphone from the Home Assistant audio system, so you choose the input in
the add-on configuration. To set it up step by step, follow the
[getting-started guide](guides/home-assistant.md).

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
| **One per room**, in the Home Assistant area of the same name | Now playing (`Artist - Title`, or `idle`), with the track as attributes; Cover art; Cover colour; Music; Input level in dBFS; Recording; Problem, on while the room keeps failing, with `failing_since` and the reason; Last error; a Listen here switch; and a Match event |
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

## Cover art for a display

Each room's **Cover art** image shows the record it is playing, and goes
unavailable when the music stops: `NOW_PLAYING_STOP_SECONDS` after the last
match, the same moment Now playing reads `idle`. So a dashboard by the hi-fi
never keeps showing the last record. This card shows the cover only while
there is one:

```yaml
type: conditional
conditions:
  - condition: state
    entity: image.scrobster_kitchen_cover_art
    state_not: unavailable
card:
  type: picture-entity
  entity: image.scrobster_kitchen_cover_art
  show_name: false
  show_state: false
```

The Now playing sensor carries the cover's address as its `art_url` attribute,
for a template or a Markdown card. Home Assistant does not take an entity
picture from MQTT attributes, so for a picture use the image entity.

### Without Home Assistant

A display that reads MQTT itself, such as an ESP32 with a screen, a Node-RED
flow or a script on a Pi, can follow a room on two retained topics under
`<MQTT_TOPIC>/<instance>/room/<room id>/`. They are sent again only when the
record changes, not on every match:

| Topic | Payload |
|---|---|
| `now_playing` | JSON: `room`, `playing`, `artist`, `title`, `album`, `art_url` and `colors`, the cover's colours as [the API](api.md#the-covers-colours) gives them. Once the music stops, `playing` is `false` and the rest is `null`, but for `room`. `colors` arrives a moment after the rest, as a second message |
| `playing` | `ON` or `OFF` |
| `cover` | the cover's URL, as plain text, for Home Assistant's image. It is never emptied, so it still holds the last cover after the music stops, or while a record without one plays. A display should take `art_url` from `now_playing` instead |

The instance is a short id kept in the database. To see it, and every room:

```sh
mosquitto_sub -h your-broker -v -t 'scrobster/+/room/+/now_playing'
```

```
scrobster/3f9a1c/room/1/now_playing {"album": "The Man-Machine", "art_url": "https://is1-ssl.mzstatic.com/.../400x400cc.jpg", "artist": "Kraftwerk", "colors": {"brightness": 227, "primary": {"hex": "#e3011b", "rgb": [227, 1, 27]}, "saturated": true, "secondary": null}, "playing": true, "room": "Kitchen", "title": "The Model"}
scrobster/3f9a1c/room/2/now_playing {"album": null, "art_url": null, "artist": null, "colors": null, "playing": false, "room": "Hall", "title": null}
```

The cover URL is the one Shazam gives, on its image server, so the display
needs to reach the internet to load it.

## Match a light to the cover

Each room's **Cover colour** sensor holds the main colour of the record's
cover, as `#rrggbb`, and goes unavailable when the music stops, like Cover
art. Its attributes are what `light.turn_on` takes:

| Attribute | Meaning |
|---|---|
| `rgb_color` | the main colour a light can show, such as `[227, 1, 27]` for Kraftwerk's red |
| `secondary_rgb_color`, `secondary_hex` | another colour on the cover that looks different, or `null` |
| `brightness` | 0 to 255, as light as that colour is on the cover |
| `saturated` | `false` for a cover with no colour to speak of, such as a black and white photo. The colour is then a warm white |

How the colours are picked is in [the API](api.md#the-covers-colours).

The entity ids below are for a room named Kitchen. Home Assistant makes them
from the device and entity names, so check yours under Settings, Devices &
services, Entities, and search for `cover colour`.

This automation keeps the lamp as it was before the music, follows each
record, and puts the lamp back once the room's **Music** sensor turns off,
`NOW_PLAYING_STOP_SECONDS` after the last match. The lamp is saved once per
listening session, so the moment between two records when Cover colour is
unavailable, while the next cover's colours are worked out, does not save the
coloured lamp over it:

```yaml
automation:
  - alias: Kitchen lamp follows the record
    mode: queued
    triggers:
      # No to or from, so a new record with the same colour but another
      # brightness or second colour also triggers it.
      - trigger: state
        entity_id: sensor.scrobster_kitchen_cover_colour
        id: colour
      - trigger: state
        entity_id: binary_sensor.scrobster_kitchen_music
        from: "on"
        to: "off"
        id: stopped
    actions:
      - choose:
          - conditions:
              - condition: trigger
                id: colour
              - condition: template
                value_template: >-
                  {{ trigger.to_state is not none
                     and trigger.to_state.state not in ['unavailable', 'unknown'] }}
              - condition: state
                entity_id: binary_sensor.scrobster_kitchen_music
                state: "on"
            sequence:
              # Once, when the music starts: the scene is there until the
              # lamp is put back.
              - if:
                  - condition: template
                    value_template: "{{ states.scene.kitchen_lamp_before_music is none }}"
                then:
                  - action: scene.create
                    data:
                      scene_id: kitchen_lamp_before_music
                      snapshot_entities: [light.kitchen_lamp]
              - action: light.turn_on
                target: {entity_id: light.kitchen_lamp}
                data:
                  rgb_color: "{{ state_attr('sensor.scrobster_kitchen_cover_colour', 'rgb_color') }}"
                  transition: 2
                  # Optional: as light as the cover. Leave it out to keep the
                  # lamp's brightness, or use brightness_pct for a fixed one.
                  brightness: "{{ state_attr('sensor.scrobster_kitchen_cover_colour', 'brightness') }}"
          - conditions:
              - condition: trigger
                id: stopped
              - condition: template
                value_template: "{{ states.scene.kitchen_lamp_before_music is not none }}"
            sequence:
              - action: scene.turn_on
                target: {entity_id: scene.kitchen_lamp_before_music}
                data: {transition: 2}
              # Put back once, then forgotten, so the next record saves the
              # lamp afresh.
              - action: scene.delete
                target: {entity_id: scene.kitchen_lamp_before_music}
```

To leave the lamp alone for a black and white cover, add this to the
conditions of the first choice:

```yaml
              - condition: template
                value_template: "{{ state_attr('sensor.scrobster_kitchen_cover_colour', 'saturated') }}"
```

When scrobSter restarts, Music goes unavailable rather than off, so the lamp
keeps its colour and follows the record again once scrobSter is back. A scene
made by `scene.create` does not survive a restart of Home Assistant, so after
one the lamp is left as it is when the music stops.

**WLED** works the same way: its Home Assistant integration makes each strip,
and each segment, a light, so use its entity in `light.turn_on`. For two
colours, give two segments one each, with `secondary_rgb_color` for the
second, or keep the primary when there is no second colour:

```yaml
      - action: light.turn_on
        target: {entity_id: light.wled_shelf_segment_1}
        data:
          rgb_color: >-
            {{ state_attr('sensor.scrobster_kitchen_cover_colour', 'secondary_rgb_color')
               or state_attr('sensor.scrobster_kitchen_cover_colour', 'rgb_color') }}
          transition: 2
```

A two-colour effect on one segment, such as WLED's Gradient, takes both
colours through WLED's JSON API. A `rest_command` sends them:

```yaml
rest_command:
  wled_shelf_colours:
    url: http://wled-shelf.local/json/state
    method: post
    content_type: application/json
    payload: >-
      {"on": true, "transition": 20,
       "seg": [{"col": [{{ primary | tojson }}, {{ secondary | tojson }}]}]}
```

and the automation calls it in place of `light.turn_on`:

```yaml
      - action: rest_command.wled_shelf_colours
        data:
          primary: "{{ state_attr('sensor.scrobster_kitchen_cover_colour', 'rgb_color') }}"
          secondary: >-
            {{ state_attr('sensor.scrobster_kitchen_cover_colour', 'secondary_rgb_color')
               or state_attr('sensor.scrobster_kitchen_cover_colour', 'rgb_color') }}
```

WLED counts `transition` in tenths of a second, so 20 is two seconds.
