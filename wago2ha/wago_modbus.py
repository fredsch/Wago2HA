"""
Client Modbus TCP pour automate Wago (750-881 / famille 750-841).

Cartographie d'adresses reconstituée depuis le code source Calaos (GPLv3) :
  - Lecture d'une ENTREE TOR (bouton)      : FC01 read_coils   @ var
  - Ecriture d'une SORTIE TOR (relais)     : FC05 write_coil   @ var + OUTPUT_WRITE_OFFSET
  - Relecture d'etat d'une SORTIE TOR      : FC01 read_coils   @ var + OUTPUT_READ_OFFSET
  - Lecture registre ANALOGIQUE (temp...)  : FC03 read_holding_registers @ var
  - Ecriture registre ANALOGIQUE           : FC06 write_register @ var + OUTPUT_WRITE_OFFSET

Les offsets correspondent aux constantes Calaos :
  WAGO_841_START_ADDRESS = 4096  -> OUTPUT_WRITE_OFFSET (sorties, automate 750-841/881)
  0x200 (512)                    -> OUTPUT_READ_OFFSET  (image de relecture des sorties)
Ils restent configurables car la cartographie exacte depend du programme
CODESYS Calaos charge dans l'automate.
"""
from __future__ import annotations

import asyncio
import logging

from pymodbus.client import AsyncModbusTcpClient

log = logging.getLogger("wago2ha.modbus")

# Offsets par defaut pour un 750-881 avec le programme CODESYS Calaos
DEFAULT_OUTPUT_WRITE_OFFSET = 4096   # WAGO_841_START_ADDRESS
DEFAULT_OUTPUT_READ_OFFSET = 0x200   # 512


class WagoModbus:
    def __init__(
        self,
        host: str,
        port: int = 502,
        unit: int = 1,
        output_write_offset: int = DEFAULT_OUTPUT_WRITE_OFFSET,
        output_read_offset: int = DEFAULT_OUTPUT_READ_OFFSET,
    ) -> None:
        self.host = host
        self.port = port
        self.unit = unit
        self.output_write_offset = output_write_offset
        self.output_read_offset = output_read_offset
        self._client: AsyncModbusTcpClient | None = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        self._client = AsyncModbusTcpClient(self.host, port=self.port, timeout=3)
        await self._client.connect()
        if self._client.connected:
            log.info("Connecte a l'automate Modbus %s:%s", self.host, self.port)
        else:
            log.warning("Connexion Modbus impossible vers %s:%s", self.host, self.port)

    async def close(self) -> None:
        if self._client:
            self._client.close()

    @property
    def connected(self) -> bool:
        return bool(self._client and self._client.connected)

    async def _ensure(self) -> None:
        if not self.connected:
            await self.connect()

    # ----- Entrees TOR -----
    async def read_input_bit(self, var: int) -> bool | None:
        """Lit l'etat d'une entree TOR (bouton) a l'adresse 'var'."""
        return await self._read_coil(var)

    # ----- Sorties TOR -----
    async def write_output_bit(self, var: int, value: bool) -> bool:
        """Active/desactive une sortie TOR (relais)."""
        async with self._lock:
            await self._ensure()
            addr = var + self.output_write_offset
            try:
                rr = await self._client.write_coil(addr, value, slave=self.unit)
                if rr.isError():
                    log.error("write_coil @%s a echoue: %s", addr, rr)
                    return False
                return True
            except Exception as exc:  # noqa: BLE001
                log.error("Erreur write_output_bit @%s: %s", addr, exc)
                return False

    async def read_output_bit(self, var: int) -> bool | None:
        """Relit l'etat reel d'une sortie TOR via l'image de relecture."""
        return await self._read_coil(var + self.output_read_offset)

    async def _read_coil(self, addr: int) -> bool | None:
        async with self._lock:
            await self._ensure()
            try:
                rr = await self._client.read_coils(addr, count=1, slave=self.unit)
                if rr.isError():
                    log.error("read_coils @%s a echoue: %s", addr, rr)
                    return None
                return bool(rr.bits[0])
            except Exception as exc:  # noqa: BLE001
                log.error("Erreur read_coil @%s: %s", addr, exc)
                return None

    # ----- Registres analogiques -----
    async def read_register(self, var: int) -> int | None:
        """Lit un registre de maintien (holding register) brut, non signe."""
        async with self._lock:
            await self._ensure()
            try:
                rr = await self._client.read_holding_registers(var, count=1, slave=self.unit)
                if rr.isError():
                    log.error("read_holding_registers @%s a echoue: %s", var, rr)
                    return None
                return int(rr.registers[0])
            except Exception as exc:  # noqa: BLE001
                log.error("Erreur read_register @%s: %s", var, exc)
                return None

    async def write_register(self, var: int, value: int) -> bool:
        async with self._lock:
            await self._ensure()
            addr = var + self.output_write_offset
            try:
                rr = await self._client.write_register(addr, value, slave=self.unit)
                return not rr.isError()
            except Exception as exc:  # noqa: BLE001
                log.error("Erreur write_register @%s: %s", addr, exc)
                return False


def raw_to_signed16(raw: int) -> int:
    """Convertit un mot 16 bits non signe en entier signe (complement a deux)."""
    return raw - 0x10000 if raw >= 0x8000 else raw


def pt1000_celsius(raw: int) -> float:
    """
    Module 750-640 + sonde PT1000 : la valeur brute est en dixiemes de degre
    (comme dans Calaos : (short int)raw / 10.0).
    """
    return raw_to_signed16(raw) / 10.0
