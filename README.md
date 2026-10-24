<img src="scrobster/static/icon.png" alt="scrobSter" width="120" align="right">

# scrobSter

[![Artifact Hub](https://img.shields.io/endpoint?url=https://artifacthub.io/badge/repository/scrobster)](https://artifacthub.io/packages/search?repo=scrobster)

Self-hosted background-audio scrobbler. It listens to audio inputs (a mic, a line-in, or the audio of your security cameras), identifies songs with Shazam, and scrobbles them to Last.fm, Libre.fm, ListenBrainz, and Maloja. Point it at a radio, a TV, or every room in the house.

<img width="1012" height="991" alt="image" src="https://github.com/user-attachments/assets/14454c0a-fd00-415c-8560-9b9a88de72b6" />

## How it works

Every 15 seconds, ffmpeg records a 12-second chunk in each room. A room is one
input: a microphone on the server, or a camera's RTSP or RTSPS stream. See
Rooms and cameras.
[shazamio](https://github.com/shazamio/shazamio) builds an audio signature
locally and asks the Shazam API for a match.

A match does two things. It marks the track as playing now, so your profile
shows the live "scrobbling now" line while the song continues. It also scrobbles
the track once per play. A track that keeps playing is not scrobbled again, but
a track that restarts is, because scrobSter compares the match offset. See
Recognition limits.

When the music stops, the mark is removed. After `NOW_PLAYING_STOP_SECONDS` with
no match, scrobSter clears it, and it also clears it when you stop the listener.
ListenBrainz and Maloja are cleared through an API call. Last.fm has no method to
clear a mark, so its own copy disappears a few minutes later.

The default of 180 seconds is deliberate. Matches drop out during quiet
passages, so a shorter limit would make the mark flicker during a song.

A web page shows the current match and the history. The JSON API serves mobile
clients and Home Assistant.

### Cover art and MusicBrainz IDs

Shazam returns a cover and usually an ISRC with each match. scrobSter uses both
where a service can take them:

- **ListenBrainz and Maloja** receive MusicBrainz IDs in `additional_info`.
  scrobSter finds them from the ISRC, once per track. ListenBrainz uses the
  release ID to show that release's cover from the Cover Art Archive. A release
  ID is sent only when a release title matches Shazam's album, because a wrong
  release would show the wrong cover.
- **Maloja** receives Shazam's cover as an uploaded picture, for an album that
  Maloja shows a placeholder for. It never replaces a picture Maloja already
  has, and it tries each album once per run.
- **Last.fm and Libre.fm** receive neither. Their API has no artwork upload, so
  missing Last.fm art still has to be added by hand on the album page.

All of this is best effort. If MusicBrainz or Maloja fails, the scrobble still
goes through without it.

Each person has an account and connects their own scrobbling services in
Settings. A room is shared, so every user chooses, room by room on the Rooms
tab, whether to receive what it hears. A microphone in a browser is different: that listen
belongs to the person who recorded it, and goes only to them.

## Rooms and cameras

Rooms have their own tab. An administrator adds rooms there. Each room has a
name, which the history shows, and one input:

- **A microphone on this server.** Pick a device, or leave the default, which
  is `AUDIO_DEVICE`.
- **A camera or stream.** Give its URL: `rtsp://`, `rtsps://`, `http://` or
  `https://`. ffmpeg asks an RTSP camera for its audio only, over TCP, so the
  video is never sent.

**Test level** records three seconds and reports the peak, before you save. A
camera with its audio turned off says so.

Each room runs its own capture loop, and the Rooms tab shows each room's level
and what it hears, live. Every user turns on **scrobble for me** for the rooms
they want. A room scrobbles for nobody until then, except for the
administrator who added it. A song that two rooms hear at once is still one play for
someone who receives both.

Tips for cameras:

- **Turn the audio on**, in the camera and in the stream you use. A camera's
  low-resolution substream is fine if it carries audio.
- **Choose AAC at 16 kHz or more** where the camera offers it. G.711 at 8 kHz
  loses everything above 4 kHz, and Shazam's fingerprint uses up to about
  5.5 kHz, so it matches less often.
- **Use a restream** if you have one, for example go2rtc or Frigate at
  `rtsp://host:8554/<camera>`. Many cameras accept only a few connections.
- **UniFi Protect** shares an RTSPS URL per camera, on port 7441. Its
  self-signed certificate is accepted.
- **The camera password stays on the server.** The page shows it as `***`,
  and leaving `***` in place keeps it when you edit the URL. Errors are
  cleaned of it before anyone sees them.
- **Audio stays on your network.** Each clip lives in a temporary file. Only
  the fingerprint, computed locally, goes to Shazam.

**Shazam's rate limit is shared.** Every room asks from the same address, so
requests take turns: at most one per `RECOGNITION_GAP_SECONDS`, whatever the
number of rooms. A silent room asks nothing. A 429 pauses every room, from one
minute up to ten.

An administrator tunes both rates under **Recognition** on the Rooms tab, and a
change applies at once. The card counts the requests of the last ten minutes,
says when Shazam last limited them, and warns when the rooms that are on would
want requests faster than the limit allows. A rate chosen there outranks
`RECOGNITION_GAP_SECONDS` and `MATCH_INTERVAL`, until **Back to the configured
rates**.

An upgrade from 0.2 keeps working as before. The microphone becomes the first
room, named "Server mic", with the device chosen in Settings. Everyone who
received the microphone receives that room. Rename it after the room it is in
with **Edit** on the Rooms tab.

## Requirements

- Python 3.10 - 3.13
- ffmpeg on PATH
- An audio input device

## Quick start

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Put your settings in a `.env` file next to the code. Git ignores this file, and
your secrets stay out of the shell history.

```sh
# .env
AUDIO_DEVICE=:1
ADMIN_PASSWORD=choose-something-long
```

Then start it:

```sh
.venv/bin/python -m scrobster
```

A real environment variable always overrides the file, so Docker and systemd
work as usual.

Open http://localhost:8000 and sign in as `admin`. Connect your scrobbling
services under Settings.

Without `ADMIN_PASSWORD`, the first start makes a random password and writes it
to the log once. Read it there, then change it in Settings.

## Accounts

The first start creates one administrator. An administrator adds more accounts
under Users, and each person connects their own services.

- **Service credentials belong to a user** and are set in the web page. The
  variables below only seed the first account, on the very first start.
- **The Last.fm API key and secret stay in the environment.** They identify this
  application, not a person, so one registration serves everybody. Each user
  then authorizes Last.fm in the browser under Settings, which stores a session
  key for that user alone.
- **Only an administrator starts or stops listening**, and adds or changes rooms.
- **Each user gets an API token** in Settings, for Home Assistant and scripts.

Open http://localhost:8000. Toggle listening from the page.

Test recognition with a file first:

```sh
.venv/bin/python -m scrobster.listener some-song.wav
```

## Configuration (environment variables)

A service is enabled when all of its variables are set.

| Variable | Default | Purpose |
|---|---|---|
| `AUDIO_BACKEND` | `avfoundation` (macOS) / `alsa` (Linux) | ffmpeg input format |
| `AUDIO_DEVICE` | `:0` (macOS) / `default` (Linux) | ffmpeg input device for a microphone room that names no device of its own, the first room included |
| `CHUNK_SECONDS` | `12` | recorded seconds per match attempt; values above 14 are capped |
| `MATCH_INTERVAL` | `15` | minimum seconds per cycle in each room, 12 to 600; increase if rate-limited. The Rooms tab can change it |
| `RECOGNITION_GAP_SECONDS` | `4` | minimum seconds between two Shazam requests, across every room and browser clip, 1 to 60. The Rooms tab can change it |
| `RESCROBBLE_MINUTES` | `30` | fallback cooldown, used only when a match has no offset |
| `NOW_PLAYING_STOP_SECONDS` | `180` | clear the playing-now mark after this long with no match |
| `DB_PATH` | `scrobster.db` | SQLite history file |
| `PORT` | `8000` | web/API port |
| `ADMIN_USERNAME` | `admin` | name of the account made on the first start |
| `ADMIN_PASSWORD` | unset | its password. Without one, a random password is logged once |
| `TRUST_INGRESS` | `1` | treat Home Assistant ingress requests as the owner, with no second sign-in |
| `INGRESS_IP` | `172.30.32.2` | the address ingress requests come from |
| `API_TOKEN` | unset | optional shared token that acts as the owner. Each user also has a personal token |
| `LISTEN_ON_START` | `1` | start listening in every room that is turned on at boot |
| `LASTFM_API_KEY` `LASTFM_API_SECRET` | unset | identifies this application to Last.fm, for every user. Create a key at https://www.last.fm/api/account/create |
| `MQTT_ENABLED` | `auto` | Home Assistant devices over MQTT. `auto` connects when `MQTT_HOST` is set or the Supervisor offers a broker. `0` turns it off. See Home Assistant |
| `MQTT_HOST` `MQTT_PORT` | unset, `1883` | the broker |
| `MQTT_USERNAME` `MQTT_PASSWORD` | unset | its credentials |
| `MQTT_TLS` | `0` | connect with TLS |
| `MQTT_TOPIC` | `scrobster` | where states and commands go, under `<topic>/<instance>/` |
| `MQTT_DISCOVERY_PREFIX` | `homeassistant` | Home Assistant's discovery prefix |

These seed the first account only. After that, each user manages their own under
Settings.

| Variable | Purpose |
|---|---|
| `LISTENBRAINZ_TOKEN`, `LISTENBRAINZ_URL` | ListenBrainz |
| `LASTFM_SESSION_KEY`, `LASTFM_USERNAME` | Last.fm, or authorize in the browser instead |
| `LASTFM_PASSWORD_HASH` or `LASTFM_PASSWORD` | Last.fm without the browser step |
| `LIBREFM_USERNAME` + `LIBREFM_PASSWORD` or `LIBREFM_PASSWORD_HASH` | Libre.fm. A password is hashed on the first start and only the hash is kept |
| `MALOJA_URL`, `MALOJA_KEY` | Maloja |

## Connect Last.fm

Create an API account at https://www.last.fm/api/account/create. Put the key and
the secret in `.env`, then authorize once:

```sh
.venv/bin/python -m scrobster.auth          # prints a URL, open it and allow access
.venv/bin/python -m scrobster.auth <token>  # prints LASTFM_SESSION_KEY
```

Copy the session key into `.env`. scrobSter never stores your password. You can
revoke the key in your Last.fm account settings.

## Find your audio device

The easy way: sign in as an administrator, open Rooms, and edit a room, or
add one. The form lists the devices ffmpeg can see, and **Test level** records
three seconds from the one you pick and reports the peak, so a silent device is
caught before you choose it. A room's own device outranks `AUDIO_DEVICE`. The
steps below do the same by hand.

Step 1. List the devices.

macOS:

```sh
ffmpeg -f avfoundation -list_devices true -i ""
# use the audio index, for example AUDIO_DEVICE=":1"
```

Linux:

```sh
arecord -l
# use for example AUDIO_DEVICE="hw:1,0" or AUDIO_DEVICE="default"
```

Step 2. Confirm that the device hears sound. Do not skip this step. A silent
device captures a file of the correct size, so size alone proves nothing.

```sh
ffmpeg -f avfoundation -i ":1" -t 5 -ac 1 -ar 44100 /tmp/t.wav -y
ffmpeg -i /tmp/t.wav -af volumedetect -f null - 2>&1 | grep mean_volume
```

Play music, then read the result. A value near -91 dB is digital silence, and
that device is the wrong one. A value near -20 dB is good.

The Rooms tab shows each room's level while it listens, updated a few times a
second while the room records, and warns you when a room is silent.

Index 0 is not always the microphone. On a Mac with virtual audio software, index
0 is often a loopback device that stays silent until you route audio through it.

## Docker (Linux host)

Images are published to GitHub Packages for `linux/amd64` and `linux/arm64`, so
a Raspberry Pi works.

```sh
docker run -d --name scrobster \
  --device /dev/snd --group-add audio \
  -p 8000:8000 \
  -v scrobster-data:/data \
  -e LISTENBRAINZ_TOKEN=your-token \
  ghcr.io/benkelly/scrobster:latest
```

Or use the compose file in this repository:

```sh
docker compose up -d
```

Tags: `latest` for the main branch, `1.2.3` and `1.2` for a release, and a short
commit SHA for every build.

Notes:

- `--group-add audio` is required. The container runs as an unprivileged user,
  and reading the sound card needs the host audio group.
- Docker Desktop on macOS cannot reach the microphone. Run bare on macOS.
- `SSL_CERT_FILE` is only for macOS. scrobSter ignores it when the file is
  missing, so the same `.env` works in a container.

## Kubernetes (Helm)

The chart is published next to the image, with the same version:

```sh
helm install scrobster oci://ghcr.io/benkelly/charts/scrobster \
  --namespace music --create-namespace \
  --set secretEnv.ADMIN_PASSWORD=choose-a-password \
  --set secretEnv.LISTENBRAINZ_TOKEN=your-token
```

It runs one pod, never two, because the SQLite database on its volume and the
queue to Shazam both belong to one process. Settings go in `env`, and anything
secret in `secretEnv`, or in a Secret of your own named by `existingSecret`.
Every variable in [Configuration](#configuration-environment-variables) works.
`ingress.enabled` publishes the web page, and the volume is kept when the
release is uninstalled. See
[`charts/scrobster/values.yaml`](charts/scrobster/values.yaml) for the rest.
Values are checked against
[`charts/scrobster/values.schema.json`](charts/scrobster/values.schema.json), so
a misspelt key or a wrong type stops the install instead of being ignored.

Cameras need nothing extra: add one on the **Rooms** tab with its RTSP or RTSPS
address. To record from a node's own sound card, set `hostSound.enabled=true`
and pin the pod to that node with `nodeSelector`. Reaching `/dev/snd` needs a
privileged container.

The chart sets `TRUST_INGRESS=0`. A pod can hold Home Assistant's ingress
address, and the app would otherwise sign that pod's requests in as the owner.

CI installs the chart into a throwaway [kind](https://kind.sigs.k8s.io/)
cluster on every pull request and signs in through the service.

The chart is listed on
[Artifact Hub](https://artifacthub.io/packages/search?repo=scrobster). CI
pushes [`charts/scrobster/artifacthub-repo.yml`](charts/scrobster/artifacthub-repo.yml)
to the `artifacthub.io` tag next to the chart, and its `repositoryID` proves
the listing belongs to this repository.

## Two ways to listen

**Server microphone (default).** The Python process records the host audio
device with ffmpeg. It runs headless, needs no browser, and survives a closed
tab. Use this for an always-on machine.

**Browser microphone (optional).** Open the page and select *use this device's
mic*. Once the browser has permission, a menu next to the button lets you pick
which microphone, and the page remembers it. A live meter under the buttons
shows what that microphone hears, with a mark holding the loudest of the last
second, so you can aim a phone without waiting for a clip to be sent. The
browser records 12-second clips and posts them to `POST /api/match`.
The server identifies and scrobbles them through the same path, so both inputs
share one history and one duplicate filter. Use this for a phone or a laptop in
another room.

The browser asks for raw audio, because echo cancellation, noise suppression,
and automatic gain control remove the detail that Shazam needs.

The tab must stay open and awake. A phone stops recording when the screen
sleeps.

Browsers permit microphone access only in a secure context. `http://localhost`
works. A plain `http://` address on your network does not. To use a phone, put
the app behind HTTPS, for example with a reverse proxy or Tailscale.

## Home Assistant

scrobSter is available as a Home Assistant add-on from
https://github.com/benkelly/ha-addons. The add-on runs this same image and takes
the microphone from the Home Assistant audio system, so you choose the input in
the add-on configuration.

The app also reads `/data/options.json` directly, which is where the Supervisor
writes add-on options. Each key matches an environment variable, in either case.
No wrapper script is needed.

### Devices over MQTT

scrobSter publishes its rooms and users to Home Assistant through MQTT
discovery, so they appear as devices with no YAML. The add-on finds the
Mosquitto broker by itself. Anywhere else, set `MQTT_HOST`, and the credentials
if the broker needs them.

| Device | Entities |
|---|---|
| **scrobSter** | Listening switch, Shazam rate limit, Shazam requests in the last ten minutes, and the two recognition rates as numbers |
| **One per room**, in the Home Assistant area of the same name | Now playing (`Artist - Title`, or `idle`), with the track as attributes; Cover art; Music; Input level in dBFS; Recording; Last error; a Listen here switch; and a Match event |
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
(`scrobster/events.py`), and MQTT is one reader of it.

## API

Every route needs a signed-in session, a personal API token, or a request
through Home Assistant ingress.

| Endpoint | Purpose |
|---|---|
| `POST /api/login` `{username, password}` | sign in, sets a session cookie |
| `GET /api/status` | version, listening state, last match, profile links, and `rooms`: each room's state, level and last match. `level_db` and `last_error` describe the first room. `mqtt` says whether the broker is connected |
| `GET /api/levels` | `{room_id: dBFS}`: the live peak of each room that is listening, or `null` between two recordings |
| `POST /api/listen` `{"on": true}` | start or stop listening in every room that is turned on. Administrator only |
| `POST /api/match` (raw audio body) | identify one clip and scrobble it for the caller |
| `GET /api/recent?limit=50` | that user's scrobble history |
| `GET /api/me` | account details, with the rooms that scrobble for you |
| `GET /api/services`, `PUT/DELETE /api/services/{name}` | that user's service credentials |
| `GET /api/rooms` | the rooms. An administrator also sees each input, with a camera password masked |
| `POST /api/rooms`, `PUT/DELETE /api/rooms/{id}` `{name, kind, device, backend, url, enabled}` | add, change or remove a room. `kind` is `device` or `stream`. Administrator only |
| `PUT /api/rooms/{id}/member` `{"on": true}` | receive what that room hears, or stop |
| `GET/PUT/DELETE /api/recognition` `{gap, interval}` | how often Shazam is asked, with the request count of the last ten minutes. `DELETE` goes back to the environment. Administrator only |
| `GET /api/audio` | the devices ffmpeg can see, and the environment's default. Administrator only |
| `POST /api/audio/test` `{kind, device, backend, url, room_id}` | record three seconds and report the peak level. Administrator only |
| `GET/POST/DELETE /api/users` | accounts. Administrator only |

Home Assistant REST sensor, if you do not use MQTT. Copy the token from Settings:

```yaml
sensor:
  - platform: rest
    name: scrobSter
    resource: http://your-host:8000/api/status
    headers:
      Authorization: !secret scrobster_token
    value_template: "{{ value_json.last_match.title if value_json.last_match else 'none' }}"
    json_attributes: [listening, last_match, level_db]
    scan_interval: 30
```

Put `scrobster_token: Bearer your-token-here` in `secrets.yaml`.

For a sensor per room, such as "what is playing in the kitchen", use this
`value_template` instead:

```yaml
    value_template: >-
      {% set room = value_json.rooms | selectattr('name', 'eq', 'Kitchen') | first %}
      {{ room.last_match.title if room and room.last_match else 'none' }}
```

## Troubleshooting

**Every cycle returns no match.** Read the room's level on the Rooms tab.
The browser microphone has its own live meter.

- Near -91 dB: the device is silent. Pick a different `AUDIO_DEVICE`.
- Near -20 dB but still no match: the microphone hears the music, but the sound
  is too poor to identify. Raise the volume, or move the microphone closer.
- On macOS, set Control Center > Mic Mode to **Standard**. Voice Isolation
  removes everything except speech, and it destroys the fingerprint.

**To scrobble music that plays on this computer**, capture the output instead of
the microphone. Install a loopback device, for example
[Background Music](https://github.com/kyleneideck/BackgroundMusic) or
[BlackHole](https://github.com/ExistentialAudio/BlackHole). Set it as the system
output, then set `AUDIO_DEVICE` to its index. The audio stays digital, so the
match rate is much better than a microphone.

**Other problems.**

- `certificate verify failed` on macOS with a python.org build: run with `SSL_CERT_FILE=$(.venv/bin/python -m certifi)`.
- ffmpeg capture fails on macOS: give your terminal microphone permission in System Settings > Privacy & Security.
- HTTP 429 from Shazam: your IP is rate-limited. Increase `MATCH_INTERVAL`.

## Recognition limits (measured)

These numbers come from tests against known audio on this project. They explain
the defaults.

- **A longer window does not help. It breaks matching.** Windows of 8 to 14
  seconds matched. Windows of 16 and 20 seconds failed every time, although
  their fingerprints were larger. `CHUNK_SECONDS` is therefore capped at 14.
- **Shazam tolerates a lot of noise.** A known track still matched under heavy
  pink noise and strong artificial reverb, even when the noise was almost as
  loud as the music.
- **Level is what matters.** Matching failed only when the music sat about
  14 dB below the noise, or lower.

So a missed song usually means one of these:

- The sound at that moment was speech, not music.
- The music was very quiet or very distant.
- The recording is not in the Shazam database, for example a live version.
- Microphone processing changed the sound. See Troubleshooting.

The listener skips the request when the input is silent, so silence costs no
API quota.

## Releasing

The version lives in one place, `__version__` in `scrobster/__init__.py`, and
`GET /api/status` reports it.

To cut a release, bump that number in the pull request. When it merges, CI
publishes the image as `ghcr.io/benkelly/scrobster:<version>`, moves the
matching `x.y` tag, pushes the git tag `v<version>` and opens a GitHub release
with generated notes. A merge that does not change the number publishes only
`latest` and the commit tag, so ordinary work needs no bump. The Helm chart is
published as `oci://ghcr.io/benkelly/charts/scrobster:<version>` whenever that
version is not there yet, with the same number for the chart and the app.

### Verifying

CI signs the image and the chart by digest with keyless
[cosign](https://github.com/sigstore/cosign), so a signature proves the artifact
was pushed by this repository's workflow on `main`. The identity keeps the
repository's own case, `benkelly/scrobSter`:

```sh
cosign verify ghcr.io/benkelly/scrobster:<version> \
  --certificate-identity-regexp '^https://github\.com/benkelly/scrobSter/\.github/workflows/ci\.yml@refs/heads/main$' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com

cosign verify ghcr.io/benkelly/charts/scrobster:<version> \
  --certificate-identity-regexp '^https://github\.com/benkelly/scrobSter/\.github/workflows/ci\.yml@refs/heads/main$' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
```

The image also carries an SPDX SBOM and SLSA provenance from BuildKit:

```sh
docker buildx imagetools inspect ghcr.io/benkelly/scrobster:<version> \
  --format '{{ json .SBOM }}'
docker buildx imagetools inspect ghcr.io/benkelly/scrobster:<version> \
  --format '{{ json .Provenance }}'
```

The Home Assistant add-on pins the exact version, so bump
[ha-addons](https://github.com/benkelly/ha-addons) after the release, never
before: the add-on cannot pull a tag that has not been built.

## Notes

- The Shazam API is unofficial and can change or block at any time. Keep `MATCH_INTERVAL` at 12 seconds or higher.
- Scrobbles fire at match time, not at track end. This is fine for radio.
- Run the logic self-check with `.venv/bin/python test_scrobster.py`.
- `requirements.txt` pins exact versions, so a rebuild cannot pull a shazamio that breaks matching. Dependabot opens weekly pull requests for the pins, the GitHub Actions and the base image; CI tests each one.
