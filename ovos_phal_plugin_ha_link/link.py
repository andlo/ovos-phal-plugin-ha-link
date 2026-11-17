"""The connection to Home Assistant: its WebSocket API, with the OpenVoiceOS
integration's ovos/* commands (andlo/ha-ovos-integration, docs/protocol.md).

Runs its own asyncio loop in a thread; the plugin hands it bus messages with
send() (thread-safe) and gets HA's with on_bus. Connects again by itself
when HA restarts or the network drops.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
import itertools
import json
import threading
from typing import Any

import aiohttp
from ovos_utils.log import LOG

PROTOCOL = 1
RETRY_SECONDS = 10


class HALink:
    def __init__(self, url: str, token: str, device: dict[str, Any],
                 on_bus: Callable[[dict], None],
                 on_event: Callable[[dict], None] | None = None) -> None:
        self.url = url.rstrip("/").replace("http", "ws", 1) + "/api/websocket"
        self.token = token
        self.device = device            # what ovos/connect says about the device
        self.on_bus = on_bus            # a bus message from HA, for the device
        self.on_event = on_event or (lambda e: None)
        self.forward: set[str] = set()  # the types HA wants (ready event)
        self.ready = False
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._ids = itertools.count(1)
        self._connect_id: int | None = None
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name="ha-link", daemon=True)
        self._stopping = False

    # --- the plugin's side ------------------------------------------------

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stopping = True
        if self._ws is not None:
            asyncio.run_coroutine_threadsafe(self._ws.close(), self._loop)
        self._loop.call_soon_threadsafe(self._loop.stop)

    def send(self, message: dict[str, Any]) -> None:
        """A bus message for HA, if HA asked for this type (thread-safe)."""
        if self.ready and message.get("type") in self.forward:
            self._loop.call_soon_threadsafe(self._send_bus, message)

    def command(self, msg: dict[str, Any]) -> None:
        """Any other command for HA (thread-safe), e.g. the control panel's answers."""
        self._loop.call_soon_threadsafe(self._send, msg)

    # --- the loop ---------------------------------------------------------

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.create_task(self._keep_connected())
        self._loop.run_forever()

    async def _keep_connected(self) -> None:
        while not self._stopping:
            try:
                await self._connect_once()
                if not self._stopping:
                    LOG.warning("ha-link: Home Assistant closed the connection")
            except (aiohttp.ClientError, OSError) as e:
                LOG.warning(f"ha-link: can't reach Home Assistant: {e}")
            except PermissionError as e:
                LOG.error(f"ha-link: {e}")
            self.ready = False
            await asyncio.sleep(RETRY_SECONDS)

    async def _connect_once(self) -> None:
        async with aiohttp.ClientSession() as session, \
                session.ws_connect(self.url, heartbeat=30) as ws:
            self._ws = ws
            hello = await ws.receive_json()
            if hello.get("type") != "auth_required":
                raise aiohttp.ClientError(f"not Home Assistant: {hello}")
            await ws.send_json({"type": "auth", "access_token": self.token})
            auth = await ws.receive_json()
            if auth.get("type") != "auth_ok":
                raise PermissionError(f"Home Assistant refused the token: {auth.get('message')}")
            LOG.info(f"ha-link: logged in to Home Assistant {auth.get('ha_version')}")
            self._connect_id = next(self._ids)
            await ws.send_json({"id": self._connect_id, "type": "ovos/connect",
                                "protocol": PROTOCOL, **self.device})
            async for raw in ws:
                if raw.type != aiohttp.WSMsgType.TEXT:
                    continue
                msg = json.loads(raw.data)
                if msg.get("id") != self._connect_id:
                    if msg.get("type") == "result" and not msg.get("success"):
                        LOG.debug(f"ha-link: HA refused a command: {msg.get('error')}")
                    continue
                if msg.get("type") == "result" and not msg.get("success"):
                    raise PermissionError(f"Home Assistant refused the device: {msg.get('error')}")
                if msg.get("type") == "event":
                    self._on_connect_event(msg["event"])
            self._ws = None

    def _on_connect_event(self, event: dict[str, Any]) -> None:
        match event.get("type"):
            case "pending":
                self.ready = False
                LOG.info("ha-link: waiting to be added in Home Assistant "
                         "(Settings -> Devices & services -> Discovered)")
            case "ready":
                self.forward = set(event.get("forward", []))
                self.ready = True
                LOG.info(f"ha-link: ready, {len(self.forward)} message types go to Home Assistant")
            case "bus":
                self.on_bus(event.get("message") or {})
            case "displaced" | "removed":
                self.ready = False
                LOG.warning(f"ha-link: device {event['type']} in Home Assistant")
        self.on_event(event)

    def _send(self, msg: dict[str, Any]) -> None:
        if self._ws is not None and not self._ws.closed:
            self._loop.create_task(self._ws.send_json({"id": next(self._ids), **msg}))

    def _send_bus(self, message: dict[str, Any]) -> None:
        self._send({"type": "ovos/bus", "device_id": self.device["device_id"],
                    "message": {"type": message["type"],
                                "data": message.get("data") or {},
                                "context": message.get("context") or {}}})
