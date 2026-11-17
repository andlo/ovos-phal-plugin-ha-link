"""Try the plugin against a running OVOS messagebus and a Home Assistant:

    ovos-messagebus &                     # the bus, on 8181
    HA_URL=http://127.0.0.1:8123 HA_TOKEN=... python tests/try_link.py

Starts the plugin and, on the same bus, a stand-in for ovos-PHAL-plugin-alsa
and the listener (answers mycroft.volume.* and mycroft.mic.*), so the device
gets its volume and listening entities in Home Assistant. Logs what HA asks.
"""
import os
import time

from ovos_bus_client import MessageBusClient
from ovos_bus_client.message import Message

from ovos_phal_plugin_ha_link import HALinkPlugin

bus = MessageBusClient()
bus.run_in_thread()
bus.connected_event.wait(10)

state = {"volume": 0.4, "muted": False, "mic_muted": False}


def volume(message):
    if message.msg_type == "mycroft.volume.set":
        state["volume"] = message.data["percent"]
    if message.msg_type in ("mycroft.volume.mute", "mycroft.volume.unmute"):
        state["muted"] = message.msg_type.endswith(".mute")
    bus.emit(Message("mycroft.volume.get.response",
                     {"percent": state["volume"], "muted": state["muted"]}))
    print("volume:", state)


def mic(message):
    if message.msg_type != "mycroft.mic.get_status":
        state["mic_muted"] = message.msg_type.endswith(".mute")
    bus.emit(Message("mycroft.mic.get_status.response", {"muted": state["mic_muted"]}))
    print("mic muted:", state["mic_muted"])


for t in ("mycroft.volume.get", "mycroft.volume.set", "mycroft.volume.mute", "mycroft.volume.unmute"):
    bus.on(t, volume)
for t in ("mycroft.mic.get_status", "mycroft.mic.mute", "mycroft.mic.unmute"):
    bus.on(t, mic)
bus.on("speak", lambda m: print("speak:", m.data.get("utterance")))

plugin = HALinkPlugin(bus=bus, config={
    "url": os.environ["HA_URL"], "token": os.environ["HA_TOKEN"],
    "name": os.environ.get("NAME", "OVOS link test"),
    "device_id": os.environ.get("DEVICE_ID", "ovos-link-test-0001"),
})
try:
    time.sleep(float(os.environ.get("SECONDS", "120")))
finally:
    plugin.shutdown()
