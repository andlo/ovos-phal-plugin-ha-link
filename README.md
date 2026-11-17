# ovos-phal-plugin-ha-link

An [OpenVoiceOS](https://openvoiceos.org) PHAL plugin that links the device to
[Home Assistant](https://www.home-assistant.io).

The device connects to Home Assistant itself and carries its message bus both
ways to the [OpenVoiceOS integration](https://github.com/andlo/ha-ovos-integration).
In Home Assistant it shows up as a device, with what the bus can see and set:

- volume, mute and listening on/off: automations can turn listening off at night
- its state (idle, listening, thinking, speaking), the last question and the last answer
- **Say** (`notify.send_message`) and **Ask** (`ovos.ask`)
- the device's own sensors, when a plugin reports them (`ovos.phal.sensor`)

No port is opened on the device and nothing has to reach it from outside, so it
also works behind NAT. Any OVOS install can use it: ovos-installer, raspOVOS,
a Mark II, an OVOS in a VM.

## Install

```bash
pip install --pre ovos-phal-plugin-ha-link
```

In Home Assistant, install the [OpenVoiceOS integration](https://github.com/andlo/ha-ovos-integration)
(0.3.0 or newer), add it, and choose **An OpenVoiceOS device**. That lets
devices connect. Then make a long-lived access token: your profile → Security.

## Configure

In `mycroft.conf`:

```json
{
  "PHAL": {
    "ovos-phal-plugin-ha-link": {
      "url": "http://homeassistant.local:8123",
      "token": "<the long-lived access token>",
      "name": "Kitchen"
    }
  }
}
```

| Setting | |
|---|---|
| `url` | Home Assistant's address |
| `token` | a long-lived access token |
| `name` | the device's name in Home Assistant (default: the host name) |
| `model` | shown on the device page (default: `OpenVoiceOS`) |

Restart OVOS' PHAL service. The device then appears in Home Assistant under
**Settings → Devices & services → Discovered**. Add it there, once. The plugin
keeps the device's id in `~/.local/state/ovos-phal-plugin-ha-link/device_id`,
so it stays the same device.

## How it works

It uses the integration's protocol ([docs/protocol.md](https://github.com/andlo/ha-ovos-integration/blob/master/docs/protocol.md)):
`ovos/connect` over Home Assistant's WebSocket API, then bus messages both
ways. What Home Assistant sends goes on the bus as it is. Of the device's own
messages, only the types Home Assistant asks for (the `forward` list) are
sent. It connects again by itself when Home Assistant restarts.

## Coming

- The device's [control panel](https://github.com/OpenVoiceOS/ovos-control-panel)
  in Home Assistant, through this link, so the panel never has to be opened on
  the network ([ha-ovos-integration#15](https://github.com/andlo/ha-ovos-integration/issues/15)).
- What the device sends out on the network ([#16](https://github.com/andlo/ha-ovos-integration/issues/16)).
- The device as an Assist satellite ([#13](https://github.com/andlo/ha-ovos-integration/issues/13)).

## License

Apache-2.0
