<img src="icon.png" alt="scrobSter" width="120" align="right">

# scrobSter

Self-hosted background-audio scrobbler. It listens to an audio input (mic or line-in), identifies songs with Shazam, and scrobbles them to Last.fm, Libre.fm, ListenBrainz, and Maloja. Point it at a radio, a TV, or the room.

## How it works

Every 15 seconds, ffmpeg records a 12-second chunk from your audio device.
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

Each person has an account and connects their own scrobbling services in
Settings. The room microphone is shared, so every user chooses whether to
receive what it hears. A microphone in a browser is different: that listen
belongs to the person who recorded it, and goes only to them.

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
- **Only an administrator starts or stops the shared microphone.**
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
| `AUDIO_DEVICE` | `:0` (macOS) / `default` (Linux) | ffmpeg input device |
| `CHUNK_SECONDS` | `12` | recorded seconds per match attempt; values above 14 are capped |
| `MATCH_INTERVAL` | `15` | minimum seconds per cycle; increase if rate-limited |
| `RESCROBBLE_MINUTES` | `30` | fallback cooldown, used only when a match has no offset |
| `NOW_PLAYING_STOP_SECONDS` | `180` | clear the playing-now mark after this long with no match |
| `DB_PATH` | `scrobster.db` | SQLite history file |
| `PORT` | `8000` | web/API port |
| `ADMIN_USERNAME` | `admin` | name of the account made on the first start |
| `ADMIN_PASSWORD` | unset | its password. Without one, a random password is logged once |
| `TRUST_INGRESS` | `1` | treat Home Assistant ingress requests as the owner, with no second sign-in |
| `INGRESS_IP` | `172.30.32.2` | the address ingress requests come from |
| `API_TOKEN` | unset | optional shared token that acts as the owner. Each user also has a personal token |
| `LISTEN_ON_START` | `1` | start the capture loop at boot |
| `LASTFM_API_KEY` `LASTFM_API_SECRET` | unset | identifies this application to Last.fm, for every user. Create a key at https://www.last.fm/api/account/create |

These seed the first account only. After that, each user manages their own under
Settings.

| Variable | Purpose |
|---|---|
| `LISTENBRAINZ_TOKEN`, `LISTENBRAINZ_URL` | ListenBrainz |
| `LASTFM_SESSION_KEY`, `LASTFM_USERNAME` | Last.fm, or authorize in the browser instead |
| `LASTFM_PASSWORD_HASH` or `LASTFM_PASSWORD` | Last.fm without the browser step |
| `LIBREFM_USERNAME` + `LIBREFM_PASSWORD_HASH` | Libre.fm |
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

The web page shows the same level as `input <n> dBFS`. It warns you when the
device is silent.

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

## Two ways to listen

**Server microphone (default).** The Python process records the host audio
device with ffmpeg. It runs headless, needs no browser, and survives a closed
tab. Use this for an always-on machine.

**Browser microphone (optional).** Open the page and select *use this device's
mic*. The browser records 12-second clips and posts them to `POST /api/match`.
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

## API

Every route needs a signed-in session, a personal API token, or a request
through Home Assistant ingress.

| Endpoint | Purpose |
|---|---|
| `POST /api/login` `{username, password}` | sign in, sets a session cookie |
| `GET /api/status` | listening state, last match, input level (`level_db`) |
| `POST /api/listen` `{"on": true}` | start or stop the shared microphone. Administrator only |
| `POST /api/match` (raw audio body) | identify one clip and scrobble it for the caller |
| `GET /api/recent?limit=50` | that user's scrobble history |
| `GET /api/me`, `PUT /api/me` | account details, and the room microphone choice |
| `GET/PUT/DELETE /api/services/{name}` | that user's service credentials |
| `GET/POST/DELETE /api/users` | accounts. Administrator only |

Home Assistant REST sensor. Copy the token from Settings:

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

## Troubleshooting

**Every cycle returns no match.** Read `input <n> dBFS` on the web page.

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

## Notes

- The Shazam API is unofficial and can change or block at any time. Keep `MATCH_INTERVAL` at 12 seconds or higher.
- Scrobbles fire at match time, not at track end. This is fine for radio.
- Run the logic self-check with `.venv/bin/python test_scrobster.py`.
