"""The connection to Home Assistant: its WebSocket API, with the OpenVoiceOS
integration's ovos/* commands (andlo/ha-ovos-integration, docs/protocol.md).

Runs its own asyncio loop in a thread; the plugin hands it bus messages with
send() (thread-safe) and gets HA's with on_bus. Connects again by itself
when HA restarts or the network drops.
"""
from __future__ import annotations

import asyncio
import base64
from collections.abc import Callable
import itertools
import json
import re
import threading
from typing import Any

import aiohttp
from ovos_utils.log import LOG

PROTOCOL = 1
RETRY_SECONDS = 10
# a file for HA (the panel's backup) goes in one WebSocket message: HA takes
# 4 MB, and base64 makes it a third bigger
MAX_FILE = 3 * 1024 * 1024 - 64 * 1024


class HALink:
    def __init__(self, url: str, token: str, device: dict[str, Any],
                 on_bus: Callable[[dict], None],
                 on_event: Callable[[dict], None] | None = None,
                 panel_url: str | None = None, panel_token: str | None = None) -> None:
        self.url = url.rstrip("/").replace("http", "ws", 1) + "/api/websocket"
        self.token = token
        self.device = device            # what ovos/connect says about the device
        self.on_bus = on_bus            # a bus message from HA, for the device
        self.on_event = on_event or (lambda e: None)
        self.forward: set[str] = set()  # the types HA wants (ready event)
        self.ready = False
        self.ha_device_id: str | None = None   # HA's own id for this device
        self._results: dict[int, asyncio.Future] = {}
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._ids = itertools.count(1)
        self._connect_id: int | None = None
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name="ha-link", daemon=True)
        self._stopping = False
        # the device's ovos-control-panel, which HA reaches through this link
        self.panel_url = panel_url.rstrip("/") if panel_url else None
        self.panel_token = panel_token
        self._session: aiohttp.ClientSession | None = None

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

    def call(self, msg: dict[str, Any], timeout: float = 10) -> Any:
        """A command for HA, waiting for its result (thread-safe, blocking).
        Raises on an error or when HA isn't there."""
        if self._ws is None or self._ws.closed:
            raise ConnectionError("not connected to Home Assistant")
        return asyncio.run_coroutine_threadsafe(self._call(msg), self._loop).result(timeout)

    async def _call(self, msg: dict[str, Any]) -> Any:
        msg_id = next(self._ids)
        fut = self._loop.create_future()
        self._results[msg_id] = fut
        try:
            await self._ws.send_json({"id": msg_id, **msg})
            return await fut
        finally:
            self._results.pop(msg_id, None)

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
            self.on_event({"type": "lost"})
            await asyncio.sleep(RETRY_SECONDS)

    async def _connect_once(self) -> None:
        async with aiohttp.ClientSession() as session, \
                session.ws_connect(self.url, heartbeat=30) as ws:
            self._ws, self._session = ws, session
            device = dict(self.device)
            if await self._panel_answers():
                device["panel"] = True
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
                                "protocol": PROTOCOL, **device})
            async for raw in ws:
                if raw.type != aiohttp.WSMsgType.TEXT:
                    continue
                msg = json.loads(raw.data)
                if msg.get("type") == "result" and msg.get("id") in self._results:
                    fut = self._results[msg["id"]]
                    if not fut.done():
                        if msg.get("success"):
                            fut.set_result(msg.get("result"))
                        else:
                            fut.set_exception(RuntimeError((msg.get("error") or {}).get("message", "error")))
                    continue
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
                self.ha_device_id = event.get("device_id")
                self.ready = True
                LOG.info(f"ha-link: ready, {len(self.forward)} message types go to Home Assistant")
            case "bus":
                self.on_bus(event.get("message") or {})
            case "panel":
                self._loop.create_task(self._panel(event))
            case "displaced" | "removed":
                self.ready = False
                LOG.warning(f"ha-link: device {event['type']} in Home Assistant")
        self.on_event(event)

    # --- the control panel (ha-ovos-integration#15) ----------------------------

    async def _panel_answers(self) -> bool:
        """Is there a control panel on this device to offer HA?"""
        if not self.panel_url:
            return False
        try:
            async with self._session.get(f"{self.panel_url}/healthz",
                                         timeout=aiohttp.ClientTimeout(total=5)) as r:
                return r.status == 200
        except (aiohttp.ClientError, OSError, TimeoutError):
            LOG.info(f"ha-link: no control panel at {self.panel_url}")
            return False

    async def _panel(self, event: dict[str, Any]) -> None:
        """One request from HA to the panel's API, made here on the device."""
        method = str(event.get("method", "GET")).upper()
        path = str(event.get("path", ""))
        status, body = 400, {"detail": "bad request"}
        if method in ("GET", "POST", "PUT", "DELETE") and path.startswith("/") \
                and ".." not in path and "://" not in path:
            headers = {"Authorization": f"Bearer {self.panel_token}"} if self.panel_token else {}
            try:
                async with self._session.request(
                        method, f"{self.panel_url}/api{path}", headers=headers,
                        json=event.get("body") if method != "GET" else None,
                        timeout=aiohttp.ClientTimeout(total=80 if event.get("binary") else 25)) as r:
                    status = r.status
                    if event.get("binary") and status < 400:
                        body = await self._file(r)
                        if body is None:
                            status, body = 413, {"detail": "the file is too big to send to Home Assistant"}
                    else:
                        try:
                            body = await r.json(content_type=None)
                        except ValueError:
                            body = {"detail": (await r.text())[:500]}
            except (aiohttp.ClientError, OSError, TimeoutError) as e:
                status, body = 502, {"detail": f"the control panel did not answer: {e}"}
        self._send({"type": "ovos/panel/response", "device_id": self.device["device_id"],
                    "request_id": event.get("id"), "status": status, "body": body})

    @staticmethod
    async def _file(r: aiohttp.ClientResponse) -> dict[str, Any] | None:
        """A file answer (the panel's backup) as JSON: base64, with its
        name. None when it wouldn't fit in one of HA's WebSocket messages
        (4 MB)."""
        data = await r.read()
        if len(data) > MAX_FILE:
            return None
        disposition = r.headers.get("Content-Disposition", "")
        match = re.search(r'filename="?([^";]+)"?', disposition)
        return {"content_type": r.headers.get("Content-Type", "application/octet-stream"),
                "filename": match.group(1) if match else None,
                "data": base64.b64encode(data).decode()}

    def _send(self, msg: dict[str, Any]) -> None:
        if self._ws is not None and not self._ws.closed:
            self._loop.create_task(self._ws.send_json({"id": next(self._ids), **msg}))

    def _send_bus(self, message: dict[str, Any]) -> None:
        self._send({"type": "ovos/bus", "device_id": self.device["device_id"],
                    "message": {"type": message["type"],
                                "data": message.get("data") or {},
                                "context": message.get("context") or {}}})
