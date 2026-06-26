"""
Couche MQTT + auto-decouverte Home Assistant.

La passerelle publie un message de configuration "discovery" par entite, puis
publie les etats et s'abonne aux topics de commande. Home Assistant cree alors
automatiquement les entites (cover, light, switch, sensor, binary_sensor, event).
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Awaitable, Callable

import aiomqtt

from .config import MqttConfig

log = logging.getLogger("wago2ha.mqtt")

CommandCallback = Callable[[str, str], Awaitable[None] | None]  # (entity_id, payload)

DEVICE = {
    "identifiers": ["wago2ha_bridge"],
    "name": "Wago2HA",
    "manufacturer": "Wago2HA",
    "model": "750-881 bridge",
}


class MqttHA:
    def __init__(self, cfg: MqttConfig) -> None:
        self.cfg = cfg
        self._client: aiomqtt.Client | None = None
        self._cmd_cb: CommandCallback | None = None
        self._cmd_topics: dict[str, str] = {}  # topic -> entity_id
        self._discovery: list[tuple[str, dict]] = []  # (config_topic, payload)

    def on_command(self, cb: CommandCallback) -> None:
        self._cmd_cb = cb

    def base(self, entity_id: str) -> str:
        return f"{self.cfg.base_topic}/{entity_id}"

    def state_topic(self, entity_id: str) -> str:
        return f"{self.base(entity_id)}/state"

    def command_topic(self, entity_id: str) -> str:
        topic = f"{self.base(entity_id)}/set"
        self._cmd_topics[topic] = entity_id
        return topic

    def register_discovery(self, component: str, entity_id: str, config: dict) -> None:
        """Memorise un message de decouverte HA a publier a la connexion."""
        config = dict(config)
        config.setdefault("unique_id", f"wago2ha_{entity_id}")
        config.setdefault("object_id", entity_id)
        config["device"] = DEVICE
        config.setdefault("availability_topic", f"{self.cfg.base_topic}/status")
        topic = f"{self.cfg.discovery_prefix}/{component}/wago2ha/{entity_id}/config"
        self._discovery.append((topic, config))

    async def run(self, ready: asyncio.Event) -> None:
        """Boucle de connexion avec reconnexion automatique."""
        while True:
            try:
                async with aiomqtt.Client(
                    hostname=self.cfg.host,
                    port=self.cfg.port,
                    username=self.cfg.username,
                    password=self.cfg.password,
                    will=aiomqtt.Will(f"{self.cfg.base_topic}/status", "offline", retain=True),
                ) as client:
                    self._client = client
                    log.info("Connecte au broker MQTT %s:%s", self.cfg.host, self.cfg.port)

                    # Disponibilite + decouverte
                    await client.publish(f"{self.cfg.base_topic}/status", "online", retain=True)
                    for topic, payload in self._discovery:
                        await client.publish(topic, json.dumps(payload), retain=True)

                    # Abonnement aux commandes
                    for topic in self._cmd_topics:
                        await client.subscribe(topic)

                    ready.set()
                    async for message in client.messages:
                        await self._dispatch(message)
            except aiomqtt.MqttError as exc:
                log.warning("MQTT deconnecte (%s), nouvelle tentative dans 5 s", exc)
                self._client = None
                ready.clear()
                await asyncio.sleep(5)

    async def _dispatch(self, message: aiomqtt.Message) -> None:
        topic = str(message.topic)
        entity_id = self._cmd_topics.get(topic)
        if entity_id is None or self._cmd_cb is None:
            return
        payload = message.payload.decode() if isinstance(message.payload, bytes) else str(message.payload)
        res = self._cmd_cb(entity_id, payload)
        if asyncio.iscoroutine(res):
            await res

    async def publish(self, topic: str, payload: str, retain: bool = True) -> None:
        if self._client is None:
            log.debug("MQTT non connecte, publication ignoree: %s", topic)
            return
        await self._client.publish(topic, payload, retain=retain)
