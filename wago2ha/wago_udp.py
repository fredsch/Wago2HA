"""
Plan de controle UDP Calaos <-> automate Wago (port 4646).

Format reconstitue depuis le PROGRAMME AUTOMATE lui-meme
(calaos_wago/Wago_2.3/wago_881.pro, POU UDPServer / SendInput). Voir
docs/WAGO_PROTOCOL.md pour la reference complete.

Points cles apportes par ce module :

  * HEARTBEAT : on envoie "WAGO_HEARTBEAT" toutes les ~10 s. Tant qu'il arrive
    (timeout automate = 30 s), l'automate passe HEARTBEAT=TRUE, SUSPEND sa
    logique locale (ManageOutput) et pilote ses sorties depuis l'image reseau
    netOutStandard. => HA devient le seul cerveau, SANS modifier le CODESYS.

  * WAGO_SET_SERVER_IP : indique a l'automate vers quelle IP pousser les
    changements d'entrees ("WAGO INT <idx> <0|1>").

  Entrant (automate -> passerelle) :
      "WAGO INT <input> <0|1>"   (entree TOR ; seules les entrees numeriques)
      "WAGO_DALI_GET <status> <niveau>"  (reponse a une requete DALI GET)

  Sortant (passerelle -> automate) :
      "WAGO_HEARTBEAT"
      "WAGO_SET_SERVER_IP a.b.c.d"
      "WAGO_SET_OUTPUT <idx> <0|1>"
      "WAGO_DALI_SET <line> <group> <address> <value0-100> <fade>"
      "WAGO_DALI_GET <line> <shortAddr> <address> 0 0"
      "WAGO_INFO_VOLET_GET <idx>" / "WAGO_INFO_VOLET_SET <idx> <position>"
      "WAGO_SET_OUTTYPE <idx> <type>" / "WAGO_SET_OUTADDR <idx> <a1> <a2> <sameAs>"
"""
from __future__ import annotations

import asyncio
import logging
import socket
from typing import Awaitable, Callable

log = logging.getLogger("wago2ha.udp")

CALAOS_PORT = 4646
HEARTBEAT_INTERVAL = 10.0   # s ; l'automate timeout a 30 s, on garde une marge x3
ENCODING = "latin-1"        # l'automate travaille en ISO-8859-1

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
        # l'automate termine ses trames par un NUL ; on coupe au premier NUL
        raw = data.split(b"\x00", 1)[0]
        try:
            msg = raw.decode(ENCODING, errors="ignore").strip()
        except Exception:  # noqa: BLE001
            return
        self.owner._handle_datagram(msg, addr)


class WagoUdp:
    def __init__(
        self,
        plc_host: str,
        listen_port: int = CALAOS_PORT,
        plc_port: int = CALAOS_PORT,
        listen_addr: str = "0.0.0.0",
        gateway_ip: str | None = None,
        heartbeat: bool = True,
        heartbeat_interval: float = HEARTBEAT_INTERVAL,
    ) -> None:
        self.plc_host = plc_host
        self.listen_port = listen_port
        self.plc_port = plc_port
        self.listen_addr = listen_addr
        self.gateway_ip = gateway_ip
        self.heartbeat_enabled = heartbeat
        self.heartbeat_interval = heartbeat_interval

        self.transport: asyncio.DatagramTransport | None = None
        self._input_cb: InputCallback | None = None
        self._pending: dict[str, asyncio.Future] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._hb_task: asyncio.Task | None = None

    def on_input(self, cb: InputCallback) -> None:
        self._input_cb = cb

    # ----------------------------------------------------------- cycle de vie
    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        await self._loop.create_datagram_endpoint(
            lambda: _CalaosUdpProtocol(self),
            local_addr=(self.listen_addr, self.listen_port),
            reuse_port=True,
        )
        log.info("Serveur UDP Calaos a l'ecoute sur %s:%s", self.listen_addr, self.listen_port)

        # Diriger les evenements d'entrees vers cette passerelle.
        gw = self.gateway_ip or _local_ip_for(self.plc_host)
        self.set_server_ip(gw)
        log.info("IP serveur annoncee a l'automate : %s", gw)

        # Heartbeat => suspend la logique automate et active le pilotage reseau.
        if self.heartbeat_enabled:
            self._hb_task = self._loop.create_task(self._heartbeat_loop())
            log.info("Heartbeat actif (%.0fs) : l'automate suspend sa logique locale.",
                     self.heartbeat_interval)
        else:
            log.warning("Heartbeat DESACTIVE : l'automate gardera sa logique autonome.")

    async def stop(self) -> None:
        if self._hb_task:
            self._hb_task.cancel()
            try:
                await self._hb_task
            except asyncio.CancelledError:
                pass
        if self.transport:
            self.transport.close()

    async def _heartbeat_loop(self) -> None:
        while True:
            self._send_raw("WAGO_HEARTBEAT")
            await asyncio.sleep(self.heartbeat_interval)

    # ----------------------------------------------------------- reception
    def _handle_datagram(self, msg: str, addr: tuple[str, int]) -> None:
        if not msg:
            return

        if msg.startswith("WAGO INT ") or msg.startswith("WAGO KNX "):
            parts = msg.split()
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

        # Reponses correlables (DALI GET, etc.) : on resout la 1ere en attente
        if self._pending:
            key, fut = next(iter(self._pending.items()))
            self._pending.pop(key, None)
            if not fut.done():
                fut.set_result(msg)

    # ----------------------------------------------------------- emission
    def _send_raw(self, text: str, addr: tuple[str, int] | None = None) -> None:
        if not self.transport:
            log.warning("UDP non initialise, envoi ignore: %s", text)
            return
        target = addr or (self.plc_host, self.plc_port)
        # NUL final comme Calaos (l'automate lit iBYTES_RECEIVED octets)
        self.transport.sendto(text.encode(ENCODING) + b"\x00", target)
        log.debug("-> %s", text)

    # ----- routage / suspension
    def set_server_ip(self, ip: str) -> None:
        self._send_raw(f"WAGO_SET_SERVER_IP {ip}")

    # ----- sorties TOR
    def set_output(self, idx: int, state: bool) -> None:
        """Force une sortie TOR par UDP (repli ; en heartbeat, Modbus prime)."""
        self._send_raw(f"WAGO_SET_OUTPUT {idx} {1 if state else 0}")

    # ----- DALI (actionneurs)
    def dali_set(self, line: int, group: int, address: int, value: int, fade: int = 1) -> None:
        value = max(0, min(100, value))   # niveau en pourcent
        fade = max(1, min(10, fade))
        self._send_raw(f"WAGO_DALI_SET {line} {group} {address} {value} {fade}")

    async def dali_get(self, line: int, address: int, timeout: float = 2.0) -> str | None:
        assert self._loop is not None
        fut: asyncio.Future = self._loop.create_future()
        key = f"{line}:{address}:{id(fut)}"
        self._pending[key] = fut
        # format automate : WAGO_DALI_GET <line> <shortAddr> <address> <p4> <p5>
        self._send_raw(f"WAGO_DALI_GET {line} {address} {address} 0 0")
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            self._pending.pop(key, None)
            return None

    # ----- volets : position memorisee dans l'automate (stockage, pas de recalcul)
    def volet_set_position(self, idx: int, position: int) -> None:
        self._send_raw(f"WAGO_INFO_VOLET_SET {idx} {int(position)}")

    async def volet_get_position(self, idx: int, timeout: float = 2.0) -> int | None:
        assert self._loop is not None
        fut: asyncio.Future = self._loop.create_future()
        self._pending[f"volet:{idx}:{id(fut)}"] = fut
        self._send_raw(f"WAGO_INFO_VOLET_GET {idx}")
        try:
            msg = await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            return None
        # reponse : "WAGO_INFO_VOLET <idx> <position>"
        parts = msg.split()
        if len(parts) >= 3 and parts[0] == "WAGO_INFO_VOLET":
            try:
                return int(parts[2])
            except ValueError:
                return None
        return None

    # ----- comportement standalone (repli si la passerelle tombe)
    def set_outtype(self, idx: int, type_code: int) -> None:
        self._send_raw(f"WAGO_SET_OUTTYPE {idx} {type_code}")

    def set_outaddr(self, idx: int, addr1: int, addr2: int, same_as: int = -1) -> None:
        self._send_raw(f"WAGO_SET_OUTADDR {idx} {addr1} {addr2} {same_as}")

    def get_info(self) -> None:
        self._send_raw("WAGO_GET_INFO")
