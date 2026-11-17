"""ovos-phal-plugin-ha-link: links an OpenVoiceOS device to Home Assistant.

The device connects to Home Assistant itself (Home Assistant's WebSocket
API, the OpenVoiceOS integration's ovos/connect) and carries its bus both
ways, so it shows up in Home Assistant as a device: volume, listening on/off,
state, last question and answer, its sensors, "say" and "ask". No port is
opened on the device, and it works behind NAT.

Configuration, in mycroft.conf:

    "PHAL": {
      "ovos-phal-plugin-ha-link": {
        "url": "http://homeassistant.local:8123",
        "token": "<a long-lived access token>",
        "name": "Kitchen"
      }
    }

The first time, Home Assistant shows the device under Settings -> Devices &
services -> Discovered; add it there.
"""
from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
import json
from pathlib import Path
import socket
import uuid

from ovos_bus_client.message import Message
from ovos_plugin_manager.phal import PHALPlugin
from ovos_utils.log import LOG
from ovos_utils.xdg_utils import xdg_state_home

from .link import HALink

PLUGIN = "ovos-phal-plugin-ha-link"


def _version(package: str) -> str | None:
    try:
        return version(package)
    except PackageNotFoundError:
        return None


def device_id() -> str:
    """Stable per device: made once, kept in the XDG state folder."""
    path = Path(xdg_state_home()) / PLUGIN / "device_id"
    if path.exists():
        return path.read_text().strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    new = f"ovos-{uuid.uuid4()}"
    path.write_text(new)
    return new


class HALinkPlugin(PHALPlugin):
    def __init__(self, bus=None, config=None):
        super().__init__(bus=bus, name=PLUGIN, config=config)
        url, token = self.config.get("url"), self.config.get("token")
        if not url or not token:
            LOG.warning(f"{PLUGIN}: set url and token in mycroft.conf (PHAL -> {PLUGIN}) "
                        "to link this device to Home Assistant")
            self.link = None
            return
        lang = None
        try:
            from ovos_config import Configuration
            lang = Configuration().get("lang")
        except Exception:  # noqa: BLE001 - the language is nice to have
            pass
        info = {
            "device_id": self.config.get("device_id") or device_id(),
            "name": self.config.get("name") or socket.gethostname(),
            "model": self.config.get("model") or "OpenVoiceOS",
            "manufacturer": "OpenVoiceOS",
            "sw_version": _version("ovos-core") or _version(PLUGIN),
        }
        if lang:
            info["lang"] = lang
        # the control panel on this device, offered to HA through the link
        panel_url = self.config.get("panel_url", "http://127.0.0.1:8500")
        panel_token = self.config.get("panel_token")
        if not panel_token:
            try:
                from ovos_config import Configuration
                panel_token = (Configuration().get("webui") or {}).get("access_token")
            except Exception:  # noqa: BLE001
                panel_token = None
        if self.config.get("panel_public_url"):
            info["panel_url"] = self.config["panel_public_url"]
        self.link = HALink(url, token, info, on_bus=self._from_ha, on_event=self._on_link_event,
                           panel_url=panel_url or None, panel_token=panel_token)
        # Home Assistant's Assist for ovos-assist-pipeline-plugin (the pipeline
        # plugin only sends bus messages; this link does the talking)
        self.bus.on("ovos.assist.ping", self._assist_ping)
        self.bus.on("ovos.assist.recognize", self._assist_recognize)
        self.bus.on("ovos.assist.process", self._assist_process)
        # every bus message; the link sends HA only the types it asked for
        self.bus.on("message", self._from_bus)
        self.link.start()

    def _from_bus(self, raw) -> None:
        try:
            message = json.loads(raw) if isinstance(raw, str) else raw
        except ValueError:
            return
        if isinstance(message, dict) and message.get("type"):
            self.link.send(message)

    def _from_ha(self, message: dict) -> None:
        """What Home Assistant asks of the device, put on its bus as it is: no
        session, so OVOS treats it as the device's own."""
        if message.get("type"):
            self.bus.emit(Message(message["type"], message.get("data") or {},
                                  message.get("context") or {}))

    # --- Home Assistant's Assist (ovos-assist-pipeline-plugin) --------------------

    def _on_link_event(self, event: dict) -> None:
        if event.get("type") in ("ready", "lost", "displaced", "removed", "pending"):
            self.bus.emit(Message("ovos.assist.available", {"available": self.link.ready}))

    def _assist_ping(self, message: Message) -> None:
        self.bus.emit(message.reply("ovos.assist.available", {"available": self.link.ready}))

    @staticmethod
    def _ha_lang(lang: str | None) -> str | None:
        """OVOS' da-dk -> HA's da (HA keeps a region only where it matters)."""
        if not lang:
            return None
        base, _, region = lang.replace("_", "-").partition("-")
        return f"{base}-{region.upper()}" if base in ("pt", "zh") and region else base.lower()

    def _assist_recognize(self, message: Message) -> None:
        """Does HA's own sentence matching know this one? Recognising only."""
        data = {"match": False, "intent": None}
        try:
            result = self.link.call({
                "type": "conversation/agent/homeassistant/debug",
                "sentences": [message.data.get("utterance", "")],
                "language": self._ha_lang(message.data.get("lang")),
                "device_id": self.link.ha_device_id,
            }, timeout=3)
            found = (result or {}).get("results", [None])[0]
            if found and found.get("match"):
                data = {"match": True, "intent": (found.get("intent") or {}).get("name")}
        except Exception as e:  # noqa: BLE001 - no match is the safe answer
            LOG.debug(f"{PLUGIN}: assist recognize: {e}")
        self.bus.emit(message.response(data))

    def _assist_process(self, message: Message) -> None:
        """HA carries it out, with its own agent (the one that recognised it)."""
        data = {"speech": None}
        try:
            result = self.link.call({
                "type": "conversation/process",
                "text": message.data.get("utterance", ""),
                "language": self._ha_lang(message.data.get("lang")),
                "agent_id": "conversation.home_assistant",
                "device_id": self.link.ha_device_id,
            }, timeout=15)
            response = (result or {}).get("response") or {}
            data = {"speech": ((response.get("speech") or {}).get("plain") or {}).get("speech"),
                    "response_type": response.get("response_type"),
                    "conversation_id": (result or {}).get("conversation_id")}
        except Exception as e:  # noqa: BLE001
            LOG.warning(f"{PLUGIN}: assist process: {e}")
        self.bus.emit(message.response(data))

    def shutdown(self):
        if self.link is not None:
            self.bus.remove("message", self._from_bus)
            self.link.stop()
        super().shutdown()
