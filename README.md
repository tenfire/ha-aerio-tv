# AerioTV for Home Assistant

A HACS-compatible Home Assistant integration for controlling the local companion remote built into [AerioTV for Android](https://github.com/jonzey231/AerioTV-Android).

AerioTV remains the playback authority. Home Assistant connects directly to each Android TV over the trusted LAN using AerioTV's authenticated WebSocket protocol; no cloud service or IPTV credentials are required.

## Current features

- One config entry and media-player entity per AerioTV device
- Multiple TVs supported
- Automatic discovery using `_aeriotv._tcp.local.`
- Manual host and advertised-port fallback
- On-screen six-digit pairing
- Durable per-TV authentication token across Home Assistant restarts
- Local push state for availability, play/pause, current channel ID, live status, and live rewind position
- Play, pause, play/pause, and seek controls
- Optional Dispatcharr channel browser with groups, logos, and current-programme labels
- Channel selection from Home Assistant's media picker using stable Dispatcharr channel UUIDs
- Privacy-preserving diagnostics with credentials, endpoint, device identity, and channel identity redacted

## Optional Dispatcharr channel browsing

AerioTV's companion protocol does not expose its channel catalogue, groups, or logos. The integration therefore does not duplicate IPTV credentials or scrape application storage.

Install and configure the separate [Dispatcharr Home Assistant integration](https://github.com/tenfire/ha-dispatcharr) to add its catalogue to the AerioTV media picker. The boundary is intentionally soft:

- Dispatcharr owns catalogue retrieval, grouping, search, artwork, and refresh.
- AerioTV owns pairing, device availability, transport controls, and playback.
- The integrations communicate through documented stable media identifiers rather than importing each other's Python internals.
- With Dispatcharr integration `0.1.5` or newer, AerioTV shows the current channel name and logo on its media-player entity. Dispatcharr `0.1.6` or newer also supplies the current programme title. Protected artwork is fetched through Home Assistant's signed media-image proxy rather than made public.

This keeps both integrations independently installable and avoids two competing channel caches.

## Requirements and limitations

- AerioTV must be installed on an Android TV device.
- AerioTV currently advertises its companion server only while the TV app is in the foreground.
- The companion server uses an ephemeral port, so automatic discovery is strongly recommended.
- Manual setup requires both the TV address and the currently advertised port.
- Communication uses AerioTV's existing unencrypted `ws://` protocol. Authentication does not encrypt traffic: a device able to capture LAN traffic can replay the bearer token and control the TV. Isolate untrusted clients, keep the TV and Home Assistant on a trusted network, and never expose the companion port to the internet.
- Dispatcharr browsing is optional. Without it, pairing and all ordinary AerioTV controls continue to work.
- A selected Dispatcharr channel must also exist in the catalogue loaded by the AerioTV Android app. Both sides use the same stable Dispatcharr UUID; AerioTV does not receive or play Dispatcharr's stream URL from Home Assistant.

## Installation with HACS

Until this repository is included in the default HACS catalogue:

1. Open **HACS → Integrations**.
2. Open the menu and select **Custom repositories**.
3. Add `https://github.com/tenfire/ha-aerio-tv` with category **Integration**.
4. Install **AerioTV**.
5. Restart Home Assistant.
6. Open **Settings → Devices & services** and add **AerioTV**.

## Pairing a TV

1. Open AerioTV on the Android TV and leave it in the foreground.
2. In Home Assistant, select the discovered AerioTV device.
3. Continue when prompted. AerioTV displays a six-digit pairing code on the TV.
4. Enter that code in Home Assistant.

Home Assistant stores only the random token returned after successful pairing. Pairing codes and tokens are not exposed as entity attributes or in diagnostics.

### Manual setup

If discovery is unavailable, enter the TV's host and the ephemeral port advertised by `_aeriotv._tcp.local.`. The port can change when AerioTV restarts, so manual setup is less durable than discovery.

## Media-player behavior

The entity reports unavailable whenever the foreground-only companion server cannot be reached. If Home Assistant starts while AerioTV is closed, the config entry and unavailable entity still load immediately while reconnection continues in the background. While connected, it receives state pushes from AerioTV instead of polling rapidly.

Supported controls currently include:

- play
- pause
- play/pause
- seek when AerioTV reports a seekable playback window
- browse and select Dispatcharr channels when that integration is loaded

Open the media browser for the AerioTV entity and select **Dispatcharr**. Dispatcharr owns the displayed folders, channel names, logos, and current-programme labels. AerioTV receives only the selected stable channel UUID and switches through its native `disp:<uuid>` command.

## Troubleshooting

### The TV is not discovered

- Confirm AerioTV is open in the foreground.
- Confirm Home Assistant and the TV are on networks that permit multicast DNS.
- Check that client isolation or firewall rules do not block LAN traffic between them.

### The entity is unavailable

AerioTV stops its companion server when the Android TV app leaves the foreground. Reopen AerioTV. Discovery will update the saved host and ephemeral port for the same stable device ID.

### Pairing fails

- Enter the currently displayed six-digit code.
- A wrong attempt rotates the code on the TV; use the new code.
- AerioTV limits wrong-code attempts per connection.
- If a saved token is revoked, Home Assistant starts reauthentication. Open AerioTV, continue the reauthentication flow, and enter the new code; the existing device and entity are preserved.

### Reporting a problem

Download diagnostics from **Settings → Devices & services → AerioTV** and attach them to the issue. The stored pairing token is redacted automatically. Do not publish network addresses, pairing codes, or tokens.

## Development

```bash
uv venv --python 3.13
uv pip install -r requirements_test.txt
uv run ruff check custom_components/aeriotv tests
uv run ruff format --check custom_components/aeriotv tests
uv run pytest -q
```

CI also runs Home Assistant Hassfest and HACS validation.

## Security

See [SECURITY.md](SECURITY.md). This community project is not affiliated with Home Assistant or the AerioTV maintainers.

## License

MIT — see [LICENSE](LICENSE).
