# Documentation

Start with the [README](../README.md): what scrobSter does, the requirements and
the [Quick start](../README.md#quick-start).

**Guides**, step by step

- **[Home Assistant](guides/home-assistant.md)**: from adding the add-on
  repository to a room that scrobbles and a dashboard card.
- **[Kubernetes with Helm](guides/kubernetes.md)**: secrets, storage,
  HTTPS for phones, and a sound card on one node.
- **[Proxmox VE](guides/proxmox.md)**: a USB turntable passed through to a
  VM or an LXC container.
- **[A phone as the microphone](guides/phone.md)**: HTTPS, permissions,
  and good matches on an iPhone or Android.

**Set up**

- **[Install](install.md)**: Docker, a systemd service or Kubernetes with Helm.
- **[Rooms and cameras](rooms.md)**: cameras, USB turntables, finding your
  audio device and the two ways to listen.
- **[Accounts and configuration](configuration.md)**: accounts, every
  environment variable, connecting Last.fm and cover art.
- **[Home Assistant](home-assistant.md)**: the add-on and devices over MQTT.
- **[A display by the hi-fi](displays.md)**: show what a room plays on a
  tablet, a Raspberry Pi, a Home Assistant dashboard or an ESP32 with ESPHome.

**Reference**

- **[API](api.md)**: every route, and a REST sensor for Home Assistant.
- **[Architecture](architecture.md)**: diagrams of one room's cycle, and of
  rooms sharing the Shazam rate limit.
- **[Troubleshooting](troubleshooting.md)**: what to check when nothing
  matches, and the measured recognition limits.
- **[Releasing](releasing.md)**: for maintainers, how a version is published and
  verified.
