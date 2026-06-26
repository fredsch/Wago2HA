"""Chargement et validation de la configuration YAML."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import yaml

log = logging.getLogger("wago2ha.config")


@dataclass
class PlcConfig:
    host: str
    modbus_port: int = 502
    modbus_unit: int = 1
    udp_listen_port: int = 4646
    udp_plc_port: int = 4646
    udp_listen_addr: str = "0.0.0.0"
    output_write_offset: int = 4096
    output_read_offset: int = 0x200


@dataclass
class MqttConfig:
    host: str = "localhost"
    port: int = 1883
    username: str | None = None
    password: str | None = None
    base_topic: str = "wago2ha"
    discovery_prefix: str = "homeassistant"


@dataclass
class Entity:
    kind: str            # input_switch | input_button | output | light | shutter | sensor ...
    id: str
    name: str
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class Config:
    plc: PlcConfig
    mqtt: MqttConfig
    entities: list[Entity]
    poll_interval_s: int = 120  # lecture des analogiques toutes les 2 min
    suspend_plc_program: bool = False
    suspend_coil: int | None = None  # coil ecrit a True pour suspendre la logique automate


def load_config(path: str) -> Config:
    with open(path, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    plc = PlcConfig(**raw["plc"])
    mqtt = MqttConfig(**(raw.get("mqtt") or {}))

    entities: list[Entity] = []
    for e in raw.get("entities", []):
        e = dict(e)
        kind = e.pop("kind")
        eid = e.pop("id")
        name = e.pop("name", eid)
        entities.append(Entity(kind=kind, id=eid, name=name, params=e))

    cfg = Config(
        plc=plc,
        mqtt=mqtt,
        entities=entities,
        poll_interval_s=int(raw.get("poll_interval_s", 120)),
        suspend_plc_program=bool(raw.get("suspend_plc_program", False)),
        suspend_coil=raw.get("suspend_coil"),
    )
    log.info("Config chargee : %d equipements", len(entities))
    return cfg
