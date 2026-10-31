# A phone as the microphone

The web page can listen through the microphone of the phone or laptop you open
it on. Select **Use this device's mic** on the Listening tab. The page records
12-second clips and posts them to `POST /api/match`. The server identifies each
clip and scrobbles it for the account you signed in with, through the same
duplicate filter as the rooms. See [Two ways to listen](../rooms.md#two-ways-to-listen)
for how this compares with a server microphone.

<img src="../images/phone-mic.png" width="320" alt="A phone using its own microphone: the live meter under the buttons, a match for Here Comes the Sun by The Beatles, and the track shown as playing now via this device">

This guide covers, in order:

1. [Put the page behind HTTPS](#step-1-put-the-page-behind-https)
2. [Open the page and sign in](#step-2-open-the-page-and-sign-in)
3. [Allow the microphone](#step-3-allow-the-microphone)
4. [Keep the screen on](#step-4-keep-the-screen-on)
5. [Get good matches](#step-5-get-good-matches)

Then: [the car, a café, at home](#where-to-use-it), [privacy, data and
battery](#privacy-data-and-battery), and [when something goes
wrong](#when-something-goes-wrong).

This is the whole path of one clip:

```mermaid
flowchart LR
  accTitle: The path of one clip from a phone
  accDescr: Speakers play music. The phone's microphone records a 12-second clip in the browser. The clip travels over HTTPS, through Tailscale, a reverse proxy or Home Assistant ingress, to scrobSter as POST /api/match. scrobSter decodes it to a temporary WAV file, sends only a fingerprint to Shazam, and scrobbles the match to your own services.
  spk(["Speakers<br/>car, café, home"]) -.->|"sound"| phone
  subgraph dev["Your phone"]
    phone["Browser tab<br/>12-second clip<br/>raw audio"]
  end
  subgraph https["HTTPS, one of"]
    ts["tailscale serve"]
    proxy["Reverse proxy<br/>for example Caddy"]
    ingress["Home Assistant<br/>ingress"]
  end
  phone --> ts
  phone --> proxy
  phone --> ingress
  ts -->|"POST /api/match"| app
  proxy -->|"POST /api/match"| app
  ingress -->|"POST /api/match"| app
  subgraph server["Your server"]
    app["scrobSter<br/>clip to WAV<br/>in a temporary file"]
  end
  app -->|"fingerprint only"| shazam["Shazam"]
  app -->|"your account"| services["Last.fm, Libre.fm,<br/>ListenBrainz, Maloja"]
```

## Step 1. Put the page behind HTTPS

Browsers give a page the microphone only in a secure context: an `https://`
address, or `http://localhost`. A plain `http://` address on your network,
such as `http://192.168.1.20:8000`, is not one. On such an address the page
says *This browser only allows microphone access over https, or on
localhost*.

`http://localhost` works only in a browser on the server itself. A phone
cannot use it.

Pick one of these three ways.

### Tailscale

This is the simplest if you already use [Tailscale](https://tailscale.com).
The page stays private to your tailnet, and Tailscale gets the certificate.

1. In the Tailscale admin console, under DNS, turn on MagicDNS and HTTPS
   certificates.
2. On the server, serve port 8000 over HTTPS:

   ```sh
   tailscale serve --bg 8000
   ```

   It prints the address, for example `https://pi.your-tailnet.ts.net`.
3. Install Tailscale on the phone, sign in to the same tailnet, and keep it
   connected.
4. Open that address on the phone.

The phone needs Tailscale on whenever you listen, over Wi-Fi or mobile data.

### A reverse proxy with a real certificate

Use this if you have a domain name. [Caddy](https://caddyserver.com) gets
and renews the certificate by itself. A minimal `Caddyfile`:

```caddy
scrobster.example.com {
    reverse_proxy localhost:8000
}
```

The name must point at the proxy, and the proxy must reach port 8000. Caddy
sends `X-Forwarded-Proto: https`, so scrobSter marks its sign-in cookie
secure. Any proxy that sends that header works the same way. A proxy open to
the internet shows the sign-in page to the internet, so give every account a
strong password.

### Home Assistant ingress

With the [Home Assistant add-on](../home-assistant.md), the page opens
through Home Assistant, as a panel. The microphone then works only if you
reach Home Assistant itself over HTTPS: through Home Assistant Cloud (Nabu
Casa), or your own certificate. `http://homeassistant.local:8123` is not a
secure context.

Two things to know:

- Ingress signs you in as the owner. Clips sent this way scrobble for the
  owner's account, whoever holds the phone.
- This path has not been tested end to end on a phone. If the button does
  nothing or the microphone is blocked inside the Home Assistant app, use
  Tailscale or a proxy instead.

## Step 2. Open the page and sign in

Open the HTTPS address and sign in. The session lasts 30 days, so you sign in
again about once a month on each browser.

The page has a web app manifest, so you can add it to the home screen as an
app:

- **iPhone, Safari:** the Share button, then **Add to Home Screen**.
- **Android, Chrome:** the menu, then **Add to Home screen**, or **Install
  app**.

It opens on its own, without the browser's address bar, with the scrobSter
icon. Add it from the HTTPS address, not from inside Home Assistant: an
ingress address changes, so the icon would stop working.

On an iPhone, the home screen app keeps its own sign-in, apart from Safari's.
Sign in once more inside it. That sign-in also lasts 30 days, and closing the
app does not end it. Signing out in Safari does not sign out the app, and the
other way round. On Android the app shares Chrome's sign-in.

## Step 3. Allow the microphone

Select **Use this device's mic**. The browser asks for the microphone. Allow
it.

Once it is allowed, a menu next to the button lists the microphones, when
there is more than one. Pick the phone's own microphone. The page remembers
the choice in this browser. A live meter under the buttons shows what the
microphone hears.

**iPhone, Safari.** Safari may ask again on later visits. To stop the
question:

- On the page, select **aA** in the address bar, then **Website Settings**,
  and set **Microphone** to **Allow**.
- For every site: Settings > Apps > Safari > Microphone.

If you selected **Don't Allow**, the page says *Microphone blocked*. Change it
in the same places, then reload the page.

**Android, Chrome.** Select the icon left of the address, then
**Permissions**, and turn on **Microphone**. If it was blocked, use
**Reset permissions** there, and reload the page. Chrome also needs the
microphone permission for itself: Settings > Apps > Chrome > Permissions.

## Step 4. Keep the screen on

The page records only while its tab is open, in front, and the screen is on.
A phone stops recording when the screen sleeps.

While the microphone is on, the page asks the browser to keep the screen on,
with the [Screen Wake Lock
API](https://developer.mozilla.org/docs/Web/API/Screen_Wake_Lock_API). It asks
again each time the page comes back to the front, and lets go when you stop
the microphone. This needs HTTPS, and a browser that has it: Chrome on
Android, and Safari from iOS 16.4. In a home screen app on an iPhone it works
from iOS 18.4. When the browser cannot keep the screen on, the note under the
meter says so. Then change the phone's setting while you listen:

- **iPhone:** Settings > Display & Brightness > Auto-Lock > **Never**.
- **Android:** Settings > Display > **Screen timeout**, set to the longest.
  The name differs between makers.

Low Power Mode on an iPhone, and battery saver on some Android phones, can
refuse to keep the screen on. Turn them off while you listen.

Switching to another app, a call or Siri can take the microphone away. The
note says *The microphone was taken away*. The clip being recorded is thrown
away, and the page waits. When the microphone is free again and the page is
in front, it opens the microphone again and goes on: the note says *The
microphone is back*. It does not ask for the microphone while the page is
hidden. If the meter still does not move, select **Stop this device's mic**,
then **Use this device's mic** again.

## Step 5. Get good matches

- **Watch the meter.** Aim for a level near -20 dBFS while music plays. Near
  -91 dBFS means the microphone hears nothing.
- **Get closer, or turn it up.** Level matters most. Shazam tolerates a lot of
  noise, but it fails once the music sits well below the chatter. See
  [Recognition limits](../troubleshooting.md#recognition-limits-measured).
- **Point the bottom of the phone at a speaker.** That is where the main
  microphone is on most phones. Do not cover it.
- **Raw audio is asked for already.** The page turns off echo cancellation,
  noise suppression and automatic gain control, because they remove what
  Shazam needs. Nothing to set.
- **Mic Mode on an iPhone.** If Control Center shows **Mic Mode** while the
  page listens, choose **Standard**. Voice Isolation removes everything except
  speech, and it destroys the fingerprint. Some Android phones have a similar
  voice focus setting: turn it off.
- **Avoid a Bluetooth microphone.** A headset or a car's hands-free microphone
  records at call quality, which matches poorly. Pick the phone's own
  microphone in the menu.

Each cycle takes 15 to 20 seconds: a 12-second clip, the answer, then a short
pause. So a song is usually matched within the first half minute.

## Where to use it

### In the car

First ask where the music comes from.

- **The phone plays it**, over Bluetooth or a cable, from Spotify or another
  app. The microphone would only hear the phone's own playback through the car
  speakers. Use the service's own scrobbling instead: for example, connect
  Spotify to Last.fm or ListenBrainz on their sites. It is exact, and costs no
  battery. Do not run both, or each play is scrobbled twice.
- **The car plays it**: radio, a CD, a USB stick. Then the microphone is the
  way. Put the phone in a holder, on a charger, with the screen on, and with
  its microphone free.

A phone connected to the car over Bluetooth may switch to the car's
hands-free microphone, or to call-quality audio, when the page opens the
microphone. If the music turns muffled, or the menu shows the car, pick the
iPhone or phone microphone in the menu.

Road noise is loud. Keep the music louder than the road, and expect fewer
matches on a motorway.

### In a café

Sit near a speaker. Talk at your table is the main problem: a clip that is
mostly speech does not match. Matches come between conversations, or when the
music is loud enough. The meter tells you whether the phone hears the music
at all.

### At home

A phone works for a room without a server microphone. For a room you use
every day, a [room](../rooms.md) on the server is better: it needs no open
tab, no awake screen and no phone.

## Privacy, data and battery

**Clips go to your server only.** The phone sends each clip to scrobSter over
HTTPS. The server decodes it into a temporary file, computes the fingerprint
locally, and sends only the fingerprint to Shazam. The audio is not kept.
Matches scrobble to the services of the account that sent the clip.

**Mobile data.** The page asks the browser to record at 48 kbit/s: Opus in
Chrome and Firefox, AAC in Safari. Shazam needs no more, because the server
turns every clip into 16 kHz mono first. A 12-second clip measured 61 to
71 kB in Chromium on Linux, with its test microphone. That is about 16 MB per
hour of listening. Safari keeps to the same rate, but this was not measured on
an iPhone. A silent clip is much smaller. Over Tailscale the traffic is the
same.

**Battery.** The screen stays on, the microphone records, and a clip is sent
every 15 seconds or so. Plan for a charger on a long drive. Stop the
microphone when you are done.

## Rate limits

Shazam's limit is shared. Clips from phones and every room on the server ask
Shazam from the same address, and wait their turn: at most one request every
`RECOGNITION_GAP_SECONDS`, 4 by default. A phone adds about one request every
15 seconds, so many phones and rooms at once slow every answer.

As a room does, the server skips a silent clip without asking Shazam: one with
a peak below -70 dBFS, or, when `QUIET_DBFS` is set, a floor below it. The
page then says *Too quiet to identify*. Unlike a room, the phone does not skip
a song it already knows, so every clip with sound asks Shazam. Stop the
microphone when no music plays.

When Shazam limits requests, every room and every phone pauses, from one
minute up to ten. The page then says *Shazam is busy, trying again in 45 s*,
counts down, and records nothing until then. See
[Rooms and cameras](../rooms.md) for the shared limit, and the
**Recognition** card on the Rooms tab for the request count.

## When something goes wrong

The note under the meter says what happened:

| The page says | What to do |
|---|---|
| *This browser only allows microphone access over https, or on localhost* | The address is plain `http://`. See [step 1](#step-1-put-the-page-behind-https). |
| *Microphone blocked* | The permission was refused. See [step 3](#step-3-allow-the-microphone). |
| *Too quiet to identify (input -99 dBFS)* | The microphone hears nothing, so Shazam was not asked. Pick another one in the menu, or check that nothing covers it. |
| *Too quiet to be music* | The clip's floor is below `QUIET_DBFS`, as between two sides of a record. |
| *No match* at a good level | Too much speech or noise, or the recording is not in Shazam. See [Troubleshooting](../troubleshooting.md). |
| *Signed out — sign in again* | The session ended. Follow the link, sign in, and start the microphone again. |
| *Error: the server could not decode the clip* | Check that ffmpeg works on the server. |
| *Shazam is busy, trying again in 45 s* | Shazam is limiting requests. The page waits, then goes on by itself. See [Rate limits](#rate-limits). |
| *The server cannot be reached, trying again in 5 s* | No network, or the server is down. The page waits longer after each failure, up to a minute, and goes on by itself. |
| *The server could not identify the clip* | Shazam could not be reached. The page waits, then tries again. |
| *The microphone was taken away* | A call or another app has the microphone. Bring the page to the front, and it goes on. See [step 4](#step-4-keep-the-screen-on). |
| *This browser cannot keep the screen on* | Turn off auto-lock while you listen. See [step 4](#step-4-keep-the-screen-on). |

For every route the page uses, see the [API](../api.md).
