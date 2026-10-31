# Documentation

Start with the [README](../README.md): what scrobSter does, the requirements and
the [Quick start](../README.md#quick-start).

<!-- Guides: step-by-step guides in guides/ go here, as a "**Guides**" list above "**Set up**". -->

**Set up**

- **[Install](install.md)**: Docker, a systemd service or Kubernetes with Helm.
- **[Rooms and cameras](rooms.md)**: cameras, USB turntables, finding your
  audio device and the two ways to listen.
- **[Accounts and configuration](configuration.md)**: accounts, every
  environment variable, connecting Last.fm and cover art.
- **[Home Assistant](home-assistant.md)**: the add-on and devices over MQTT.

**Reference**

- **[API](api.md)**: every route, and a REST sensor for Home Assistant.
- **[Architecture](architecture.md)**: diagrams of one room's cycle, and of
  rooms sharing the Shazam rate limit.
- **[Troubleshooting](troubleshooting.md)**: what to check when nothing
  matches, and the measured recognition limits.
- **[Releasing](releasing.md)**: for maintainers, how a version is published and
  verified.
