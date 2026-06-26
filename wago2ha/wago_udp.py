"""
Protocole UDP Calaos <-> automate Wago.

Format reconstitue depuis Calaos (UDPServer.cpp, WODali.cpp) :

  Entrant (automate -> passerelle), sur changement d'etat d'une entree TOR :
      "WAGO INT <input> <val>"   (entree standard)
      "WAGO KNX <input> <val>"   (entree KNX)
      <val> : "true"/"false" ou 1/0

  Decouverte :
      automate envoie "CALAOS_DISCOVER" -> on repond "CALAOS_IP <ip_locale>"

  Sortant (passerelle -> automate), pilotage DALI via le module 750-641 :
      "WAGO_DALI_GET <line> <address>"
      "WAGO_DALI_SET <line> <group> <address> <value0-100> <fade1-10>"
  La reponse GET contient l'etat du ballast (token[1] == "0" => eteint).

Par defaut l'automate emet vers le port UDP 4646 (port Calaos historique) et
ecoute les commandes DALI sur ce meme port. Tout est configurable.
"""
from __future__ import annotations

import asyncio
import logging
import socket
from typing import Awaitable, Callable

log = logging.getLogger("wago2ha.udp")

# input_number, raw_state(bool), kind("std"|"knx")
InputCallback = Callable[[int, bool, str], Awaitable[None] | None]


def _parse_bool(token: str) -> bool:
    return token.strip().lower() in ("1", "true", "on", "yes")


def _local_ip_for(remote_ip: str) -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((remote_ip, 1))
        return s.getsockname()[0]
    except Exception:  # noqa: BLE001
        return "0.0.0.0"
    finally:
        s.close()


class _CalaosUdpProtocol(asyncio.DatagramProtocol):
    def __init__(self, owner: "WagoUdp") -> None:
        self.owner = owner

    def connection_made(self, transport: asyncio.DatagramTransport) -> None:  # type: ignore[override]
        self.owner.transport = transport

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        try:
            msg = data.decode(errors="ignore").strip()
        except Exception:  # noqa: BLE001
            return
        self.owner._handle_datagram(msg, addr)


class WagoUdp:
    def __init__(
        self,
        plc_host: str,
        listen_port: int = 4646,
        plc_port: int = 4646,
        listen_addr: str = "0.0.0.0",
    ) -> None:
        self.plc_host = plc_host
        self.listen_port = listen_port
        self.plc_port = plc_port
        self.listen_addr = listen_addr
        self.transport: asyncio.DatagramTransport | None = None
        self._input_cb: InputCallback | None = None
        # correlation des reponses DALI GET (file simple par commande)
        self._pending: dict[str, asyncio.Future] = {}
        self._loop: asyncio.AbstractEventLoop | None = None

    def on_input(self, cb: InputCallback) -> None:
        self._input_cb = cb

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        await self._loop.create_datagram_endpoint(
            lambda: _CalaosUdpProtocol(self),
            local_addr=(self.listen_addr, self.listen_port),
            reuse_port=True,
        )
        log.info("Serveur UDP Calaos a l'ecoute sur %s:%s", self.listen_addr, self.listen_port)

    def _handle_datagram(self, msg: str, addr: tuple[str, int]) -> None:
        if msg == "CALAOS_DISCOVER":
            ip = _local_ip_for(addr[0])
            self._send_raw(f"CALAOS_IP {ip}", addr)
            return

        if msg.startswith("WAGO INT ") or msg.startswith("WAGO KNX "):
            parts = msg.split()
            # parts = ["WAGO", "INT"|"KNX", <input>, <val>]
            if len(parts) >= 4:
                kind = "std" if parts[1] == "INT" else "knx"
                try:
                    inp = int(parts[2])
                except ValueError:
                    return
                val = _parse_bool(parts[3])
                log.debug("Entree recue: %s %s = %s", kind, inp, val)
                if self._input_cb and self._loop:
                    res = self._input_cb(inp, val, kind)
                    if asyncio.iscoroutine(res):
                        asyncio.ensure_future(res)
            return

        # Reponses DALI GET : on resout la premiere commande en attente
        if self._pending:
            key, fut = next(iter(self._pending.items()))
            self._pending.pop(key, None)
            if not fut.done():
                fut.set_result(msg)

    def _send_raw(self, text: str, addr: tuple[str, int] | None = None) -> None:
        if not self.transport:
            log.warning("UDP non initialise, envoi ignore: %s", text)
            return
        target = addr or (self.plc_host, self.plc_port)
        self.transport.sendto(text.encode(), target)

    # ----- DALI -----
    def dali_set(self, line: int, group: int, address: int, value: int, fade: int = 1) -> None:
        value = max(0, min(100, value))
        fade = max(1, min(10, fade))
        self._send_raw(f"WAGO_DALI_SET {line} {group} {address} {value} {fade}")

    async def dali_get(self, line: int, address: int, timeout: float = 2.0) -> str | None:
        assert self._loop is not None
        fut: asyncio.Future = self._loop.create_future()
        key = f"{line}:{address}:{id(fut)}"
        self._pending[key] = fut
        self._send_raw(f"WAGO_DALI_GET {line} {address}")
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            self._pending.pop(key, None)
            return None
