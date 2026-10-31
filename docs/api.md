# API

Every route needs a signed-in session, a personal API token, or a request
through Home Assistant ingress. The display routes below also take a display
token.

| Endpoint | Purpose |
|---|---|
| `POST /api/login` `{username, password}` | sign in, sets a session cookie |
| `GET /api/status` | version, listening state, last match, profile links, and `rooms`: each room's state, level and last match, and `skipped`, the cycles that did not ask Shazam because the song was known, and `failing_since`, the unix time a room started failing, or `null`. `floor_db` is the last clip's median level, and `quiet` says it was below `QUIET_DBFS`. `level_db` and `last_error` describe the first room. `mqtt` says whether the broker is connected |
| `GET /api/events` | what happens, as it happens, as [server-sent events](https://developer.mozilla.org/docs/Web/API/Server-sent_events). Each event is named by its kind: `match`, `scrobble`, `cleared`, `listening`, `rooms`, `member`, `users`, `services`, `rates`. You see what the rest of the API would show you. `resync` means events were lost, so read `/api/status` again |
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
| `GET /api/displays`, `POST /api/displays` `{name}`, `DELETE /api/displays/{id}` | display tokens. `POST` answers with the token, the only time it is shown. Administrator only |

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

The token is under Settings, hidden until you select Show:

![Settings, Your API token: the token hidden, with Show, Copy and Replace token buttons](images/settings-token.png)

For a sensor per room, such as "what is playing in the kitchen", use this
`value_template` instead:

```yaml
    value_template: >-
      {% set room = value_json.rooms | selectattr('name', 'eq', 'Kitchen') | first %}
      {{ room.last_match.title if room and room.last_match else 'none' }}
```

## Displays

A screen by the hi-fi reads these with a display token, made under Settings,
Displays. The token reads these routes and nothing else. Send it as
`Authorization: Bearer <token>`, or as `?token=<token>` for a client that cannot
set a header. Only a display token is read from the URL, never a personal one.

| Endpoint | Purpose |
|---|---|
| `GET /display?room=1&idle=clock` | a full-screen page: the cover, title, artist and album. Without `room`, whichever room matched last. `idle` is `clock` or `blank`. Add `&token=` on a screen that is not signed in |
| `GET /api/rooms/{id}/now` | `{room_id, room, playing, artist, title, album, since, heard, art, art_key}`. The song fields are `null` while nothing plays. `since` is when the room started hearing the song, `heard` its last match, `art` the room's cover route, and `art_key` changes with the cover |
| `GET /api/now` | the same, for whichever room that plays matched last, or `room_id: null` |
| `GET /api/rooms/{id}/cover?size=600&idle=204` | the cover itself, fetched once per song and kept in memory. It carries an `ETag`, so asking again with `If-None-Match` costs a `304`. `size` is 32 to 1200 pixels square: Apple's covers are fetched at that size, any other is served as it is |
| `GET /api/cover` | the same, for the room `/api/now` picks |
| `GET /api/events` | a display token sees only `match` events with their `room_id`, and `rooms` and `listening`. Read `now` again on each |

A room plays from its match until `NOW_PLAYING_STOP_SECONDS` pass without
another, its last clip is silence or below `QUIET_DBFS`, or it stops listening.
While nothing plays, or the song has no cover, `idle` chooses what the cover
route answers: `204` (the default), `404`, or `image`, a black PNG of `size`
pixels, 400 without one.
