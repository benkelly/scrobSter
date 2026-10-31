# Security policy

## Supported versions

Only the latest release gets fixes. A fix ships as a new release, so update to
it: the `latest` image tag, the Helm chart of the same version, or the Home
Assistant add-on once it is bumped.

## Report a vulnerability

Report it privately, through GitHub's private vulnerability reporting:

https://github.com/benkelly/scrobSter/security/advisories/new

Do not open a public issue for it. Say what an attacker can do, and how to
reproduce it. You will get an answer as soon as possible, and credit in the
release notes if you want it.

## What is sensitive

scrobSter holds these secrets. A report that exposes any of them is a security
problem:

- **Service credentials.** ListenBrainz and Maloja tokens, Last.fm session keys,
  and Libre.fm password hashes. Each user's are stored in the SQLite database
  (`DB_PATH`), so treat that file as a secret too.
- **Account passwords**, the administrator's included, and the session cookie.
- **API tokens.** Each user's personal token, and the shared `API_TOKEN`, which
  acts as the owner.
- **Display tokens** read what each room plays now: its name, the song, and
  the cover. Nothing else, so a token found in a display's URL cannot read
  history, accounts, settings or camera URLs. Only their hash is stored. A
  display token that reads more than that is a security problem.
- **Camera URLs.** A stream URL can carry the camera's password. The web page
  and the API show it as `***`, and errors are cleaned of it.
- **The MQTT broker.** Whoever can publish to it can start and stop listening,
  change the rates, and choose rooms for any user. Camera URLs and service
  credentials are never published to it.

Audio stays on your network. Each clip lives in a temporary file, and only the
fingerprint, computed locally, goes to Shazam. A match then goes to the
scrobbling services each user connected, and its ISRC goes to MusicBrainz to
find the IDs.

Home Assistant ingress requests are treated as the owner, from `INGRESS_IP`,
unless `TRUST_INGRESS=0`. That is by design for the add-on. Outside Home
Assistant, set `TRUST_INGRESS=0` if another host could hold that address. The
Helm chart already does.
