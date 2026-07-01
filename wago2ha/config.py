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
    # IP que l'automate doit cibler pour pousser les entrees et recevoir le
    # heartbeat. Laisser vide => detection automatique vers `host`.
    gateway_ip: str | None = None
    # Heartbeat : maintient l'automate en "mode distant" (HEARTBEAT=TRUE), ce qui
    # suspend sa logique locale et lui fait piloter ses sorties depuis le reseau.
    # A laisser actif : c'est le mecanisme natif de suspension (aucune modif CODESYS).
    heartbeat: bool = True
    heartbeat_interval_s: float = 10.0
    # Sonde de disponibilite de l'automate (ping UDP WAGO_GET_VERSION) et
    # rafraichissement de la version. 0 => desactive.
    status_interval_s: float = 30.0


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
    )

    # Cles depreciees : la suspension passe desormais par le heartbeat UDP natif.
    if "suspend_plc_program" in raw or "suspend_coil" in raw:
        log.warning(
            "Cles 'suspend_plc_program'/'suspend_coil' depreciees et ignorees : "
            "la suspension du programme automate est assuree par le heartbeat UDP "
            "(plc.heartbeat). Vous pouvez les retirer de votre config."
        )

    log.info("Config chargee : %d equipements", len(entities))
    return cfg
