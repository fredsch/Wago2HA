"""
Orchestrateur Wago2HA.

Relie l'automate Wago (Modbus + UDP Calaos) a Home Assistant (MQTT discovery),
et implemente la logique de chaque type d'equipement.
"""
from __future__ import annotations

import asyncio
import json
import logging

from .config import Config, Entity
from .gestures import LongPressDetector, MultiClickDetector
from .mqtt_ha import MqttHA
from .wago_modbus import WagoModbus, pt1000_celsius
from .wago_udp import WagoUdp

log = logging.getLogger("wago2ha.bridge")


def b100_to_255(v: int) -> int:
    return round(max(0, min(100, v)) * 255 / 100)


def b255_to_100(v: int) -> int:
    return round(max(0, min(255, v)) * 100 / 255)


class Bridge:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.modbus = WagoModbus(
            host=cfg.plc.host,
            port=cfg.plc.modbus_port,
            unit=cfg.plc.modbus_unit,
            output_write_offset=cfg.plc.output_write_offset,
            output_read_offset=cfg.plc.output_read_offset,
        )
        self.udp = WagoUdp(
            plc_host=cfg.plc.host,
            listen_port=cfg.plc.udp_listen_port,
            plc_port=cfg.plc.udp_plc_port,
            listen_addr=cfg.plc.udp_listen_addr,
        )
        self.mqtt = MqttHA(cfg.mqtt)

        self.entities: dict[str, Entity] = {e.id: e for e in cfg.entities}
        # routage des entrees UDP : numero d'entree -> liste de (entity_id, handler)
        self._input_routes: dict[int, list] = {}
        self._gesture_detectors: dict[str, object] = {}
        self._shutter_tasks: dict[str, asyncio.Task] = {}
        self._shutter_pos: dict[str, float] = {}

    # ---------------------------------------------------------------- setup
    async def setup(self) -> None:
        await self.modbus.connect()
        self.udp.on_input(self._on_udp_input)
        self.mqtt.on_command(self._on_mqtt_command)
        for e in self.cfg.entities:
            self._setup_entity(e)
        await self.udp.start()

        if self.cfg.suspend_plc_program and self.cfg.suspend_coil is not None:
            ok = await self.modbus.write_output_bit(self.cfg.suspend_coil, True)
            log.info("Suspension du programme automate via coil %s : %s",
                     self.cfg.suspend_coil, "ok" if ok else "echec")

    def _setup_entity(self, e: Entity) -> None:
        handler = getattr(self, f"_setup_{e.kind}", None)
        if handler is None:
            log.warning("Type d'equipement inconnu: %s (%s)", e.kind, e.id)
            return
        handler(e)

    # --------------------------------------------------------- ENTREES TOR
    def _setup_input_switch(self, e: Entity) -> None:
        var = int(e.params["var"])
        self.mqtt.register_discovery("binary_sensor", e.id, {
            "name": e.name,
            "state_topic": self.mqtt.state_topic(e.id),
            "payload_on": "ON", "payload_off": "OFF",
        })

        async def handler(raw: bool) -> None:
            await self.mqtt.publish(self.mqtt.state_topic(e.id), "ON" if raw else "OFF")

        self._input_routes.setdefault(var, []).append(handler)

    def _setup_input_button(self, e: Entity) -> None:
        var = int(e.params["var"])
        mode = e.params.get("mode", "long")  # "long" ou "triple"
        if mode == "triple":
            event_types = ["single", "double", "triple"]
        else:
            event_types = ["single", "long"]

        self.mqtt.register_discovery("event", e.id, {
            "name": e.name,
            "state_topic": self.mqtt.state_topic(e.id),
            "event_types": event_types,
        })

        async def emit_gesture(gesture: str) -> None:
            await self.mqtt.publish(
                self.mqtt.state_topic(e.id),
                json.dumps({"event_type": gesture}),
                retain=False,
            )

        if mode == "triple":
            detector = MultiClickDetector(emit_gesture)
        else:
            detector = LongPressDetector(emit_gesture)
        self._gesture_detectors[e.id] = detector

        async def handler(raw: bool) -> None:
            detector.feed(raw)

        self._input_routes.setdefault(var, []).append(handler)

    async def _on_udp_input(self, inp: int, raw: bool, kind: str) -> None:
        for handler in self._input_routes.get(inp, []):
            res = handler(raw)
            if asyncio.iscoroutine(res):
                await res

    # --------------------------------------------------------- SORTIES TOR
    def _setup_output(self, e: Entity) -> None:
        var = int(e.params["var"])
        as_light = bool(e.params.get("light", False))
        component = "light" if as_light else "switch"
        self.mqtt.register_discovery(component, e.id, {
            "name": e.name,
            "state_topic": self.mqtt.state_topic(e.id),
            "command_topic": self.mqtt.command_topic(e.id),
            "payload_on": "ON", "payload_off": "OFF",
        })

        async def cmd(payload: str) -> None:
            on = payload.strip().upper() == "ON"
            await self.modbus.write_output_bit(var, on)
            await self.mqtt.publish(self.mqtt.state_topic(e.id), "ON" if on else "OFF")

        e.params["_cmd"] = cmd
        e.params["_poll"] = lambda: self._poll_output(e, var)

    async def _poll_output(self, e: Entity, var: int) -> None:
        state = await self.modbus.read_output_bit(var)
        if state is not None:
            await self.mqtt.publish(self.mqtt.state_topic(e.id), "ON" if state else "OFF")

    # ------------------------------------------------------------- VOLETS
    def _setup_shutter(self, e: Entity) -> None:
        self._shutter_pos[e.id] = float(e.params.get("initial_position", 0))
        self.mqtt.register_discovery("cover", e.id, {
            "name": e.name,
            "command_topic": self.mqtt.command_topic(e.id),
            "state_topic": self.mqtt.state_topic(e.id),
            "position_topic": f"{self.mqtt.base(e.id)}/position",
            "set_position_topic": f"{self.mqtt.base(e.id)}/set_position",
            "payload_open": "OPEN", "payload_close": "CLOSE", "payload_stop": "STOP",
            "position_open": 100, "position_closed": 0,
            "state_open": "open", "state_closed": "closed",
            "state_opening": "opening", "state_closing": "closing",
            "device_class": "shutter",
        })
        # topic supplementaire pour la position cible
        pos_topic = f"{self.mqtt.base(e.id)}/set_position"
        self.mqtt._cmd_topics[pos_topic] = e.id  # route aussi vers cette entite
        e.params["_set_position_topic"] = pos_topic

    async def _shutter_command(self, e: Entity, payload: str, is_position: bool) -> None:
        var_up = int(e.params["var_up"])
        var_down = int(e.params["var_down"])
        time_up = float(e.params.get("time_up", 20))
        time_down = float(e.params.get("time_down", time_up))

        # annule un mouvement en cours
        if e.id in self._shutter_tasks:
            self._shutter_tasks[e.id].cancel()

        if is_position:
            try:
                target = float(payload)
            except ValueError:
                return
        else:
            cmd = payload.strip().upper()
            if cmd == "STOP":
                await self._shutter_stop(var_up, var_down)
                await self.mqtt.publish(self.mqtt.state_topic(e.id), "stopped")
                return
            target = 100.0 if cmd == "OPEN" else 0.0

        self._shutter_tasks[e.id] = asyncio.ensure_future(
            self._run_shutter(e, var_up, var_down, time_up, time_down, target)
        )

    async def _shutter_stop(self, var_up: int, var_down: int) -> None:
        await self.modbus.write_output_bit(var_up, False)
        await self.modbus.write_output_bit(var_down, False)

    async def _run_shutter(self, e: Entity, var_up: int, var_down: int,
                           time_up: float, time_down: float, target: float) -> None:
        current = self._shutter_pos.get(e.id, 0.0)
        delta = target - current
        if abs(delta) < 1:
            return
        going_up = delta > 0
        full_time = time_up if going_up else time_down
        duration = abs(delta) / 100.0 * full_time

        # securite : jamais les deux relais en meme temps
        await self._shutter_stop(var_up, var_down)
        await asyncio.sleep(0.3)  # temps mort d'inversion

        await self.mqtt.publish(self.mqtt.state_topic(e.id), "opening" if going_up else "closing")
        await self.modbus.write_output_bit(var_up if going_up else var_down, True)

        start = asyncio.get_running_loop().time()
        try:
            # publie la position estimee pendant la course
            while True:
                elapsed = asyncio.get_running_loop().time() - start
                if elapsed >= duration:
                    break
                est = current + (1 if going_up else -1) * (elapsed / full_time * 100.0)
                self._shutter_pos[e.id] = max(0.0, min(100.0, est))
                await self.mqtt.publish(f"{self.mqtt.base(e.id)}/position",
                                        str(round(self._shutter_pos[e.id])), retain=True)
                await asyncio.sleep(0.5)
            self._shutter_pos[e.id] = target
        except asyncio.CancelledError:
            elapsed = asyncio.get_running_loop().time() - start
            est = current + (1 if going_up else -1) * (elapsed / full_time * 100.0)
            self._shutter_pos[e.id] = max(0.0, min(100.0, est))
            raise
        finally:
            await self.modbus.write_output_bit(var_up if going_up else var_down, False)
            pos = round(self._shutter_pos[e.id])
            await self.mqtt.publish(f"{self.mqtt.base(e.id)}/position", str(pos), retain=True)
            state = "open" if pos >= 99 else "closed" if pos <= 1 else "stopped"
            await self.mqtt.publish(self.mqtt.state_topic(e.id), state)

    # -------------------------------------------------------------- DALI
    def _setup_light_dali(self, e: Entity) -> None:
        self.mqtt.register_discovery("light", e.id, {
            "name": e.name,
            "schema": "json",
            "brightness": True,
            "state_topic": self.mqtt.state_topic(e.id),
            "command_topic": self.mqtt.command_topic(e.id),
        })

        async def cmd(payload: str) -> None:
            data = json.loads(payload)
            line = int(e.params.get("line", 1))
            group = int(e.params.get("group", 0))
            address = int(e.params["address"])
            fade = int(e.params.get("fade", 1))
            if data.get("state") == "OFF":
                self.udp.dali_set(line, group, address, 0, fade)
                await self.mqtt.publish(self.mqtt.state_topic(e.id), json.dumps({"state": "OFF"}), retain=False)
                return
            bri255 = int(data.get("brightness", 255))
            self.udp.dali_set(line, group, address, b255_to_100(bri255), fade)
            await self.mqtt.publish(
                self.mqtt.state_topic(e.id),
                json.dumps({"state": "ON", "brightness": bri255}),
                retain=False,
            )

        e.params["_cmd"] = cmd

    def _setup_light_dali_rgb(self, e: Entity) -> None:
        self.mqtt.register_discovery("light", e.id, {
            "name": e.name,
            "schema": "json",
            "brightness": True,
            "supported_color_modes": ["rgb"],
            "state_topic": self.mqtt.state_topic(e.id),
            "command_topic": self.mqtt.command_topic(e.id),
        })

        async def cmd(payload: str) -> None:
            data = json.loads(payload)
            fade = int(e.params.get("fade", 1))

            def chan(prefix: str, value0_100: int) -> None:
                self.udp.dali_set(
                    int(e.params.get(f"{prefix}line", 1)),
                    int(e.params.get(f"{prefix}group", 0)),
                    int(e.params[f"{prefix}address"]),
                    value0_100, fade,
                )

            if data.get("state") == "OFF":
                for p in ("r", "g", "b"):
                    chan(p, 0)
                await self.mqtt.publish(self.mqtt.state_topic(e.id), json.dumps({"state": "OFF"}), retain=False)
                return

            rgb = data.get("color", {})
            r = int(rgb.get("r", 255)); g = int(rgb.get("g", 255)); b = int(rgb.get("b", 255))
            bri = int(data.get("brightness", 255)) / 255.0
            chan("r", b255_to_100(round(r * bri)))
            chan("g", b255_to_100(round(g * bri)))
            chan("b", b255_to_100(round(b * bri)))
            await self.mqtt.publish(
                self.mqtt.state_topic(e.id),
                json.dumps({"state": "ON", "brightness": int(data.get("brightness", 255)),
                            "color_mode": "rgb", "color": {"r": r, "g": g, "b": b}}),
                retain=False,
            )

        e.params["_cmd"] = cmd

    def _setup_dali_presence(self, e: Entity) -> None:
        self.mqtt.register_discovery("binary_sensor", e.id, {
            "name": e.name,
            "device_class": "occupancy",
            "state_topic": self.mqtt.state_topic(e.id),
            "payload_on": "ON", "payload_off": "OFF",
        })
        e.params["_poll"] = lambda: self._poll_dali_sensor(e, "presence")

    def _setup_dali_lux(self, e: Entity) -> None:
        self.mqtt.register_discovery("sensor", e.id, {
            "name": e.name,
            "device_class": "illuminance",
            "unit_of_measurement": "lx",
            "state_topic": self.mqtt.state_topic(e.id),
        })
        e.params["_poll"] = lambda: self._poll_dali_sensor(e, "lux")

    async def _poll_dali_sensor(self, e: Entity, kind: str) -> None:
        # NOTE: la lecture de capteurs DALI (presence/luminosite) via 750-641
        # n'est pas exposee telle quelle par Calaos. Adapter selon la reponse
        # reelle de WAGO_DALI_GET de votre programme CODESYS.
        line = int(e.params.get("line", 1))
        address = int(e.params["address"])
        res = await self.udp.dali_get(line, address)
        if res is None:
            return
        tokens = res.split()
        if kind == "presence":
            val = "ON" if (len(tokens) > 1 and tokens[1] != "0") else "OFF"
            await self.mqtt.publish(self.mqtt.state_topic(e.id), val)
        else:
            if len(tokens) > 1:
                await self.mqtt.publish(self.mqtt.state_topic(e.id), tokens[1])

    # ----------------------------------------------------- ANALOGIQUES
    def _setup_temperature(self, e: Entity) -> None:
        self.mqtt.register_discovery("sensor", e.id, {
            "name": e.name,
            "device_class": "temperature",
            "unit_of_measurement": "\u00b0C",
            "state_class": "measurement",
            "state_topic": self.mqtt.state_topic(e.id),
        })
        e.params["_poll"] = lambda: self._poll_temperature(e)

    async def _poll_temperature(self, e: Entity) -> None:
        var = int(e.params["var"])
        raw = await self.modbus.read_register(var)
        if raw is None:
            return
        temp = pt1000_celsius(raw)
        a = float(e.params.get("coeff_a", 1.0))
        b = float(e.params.get("coeff_b", 0.0))
        temp = temp * a + b
        await self.mqtt.publish(self.mqtt.state_topic(e.id), f"{temp:.1f}")

    def _setup_analog(self, e: Entity) -> None:
        disc = {
            "name": e.name,
            "state_topic": self.mqtt.state_topic(e.id),
            "state_class": "measurement",
        }
        if "unit" in e.params:
            disc["unit_of_measurement"] = e.params["unit"]
        if "device_class" in e.params:
            disc["device_class"] = e.params["device_class"]
        self.mqtt.register_discovery("sensor", e.id, disc)
        e.params["_poll"] = lambda: self._poll_analog(e)

    async def _poll_analog(self, e: Entity) -> None:
        var = int(e.params["var"])
        raw = await self.modbus.read_register(var)
        if raw is None:
            return
        a = float(e.params.get("coeff_a", 1.0))
        b = float(e.params.get("coeff_b", 0.0))
        value = raw * a + b
        await self.mqtt.publish(self.mqtt.state_topic(e.id), f"{value:.2f}")

    # ----------------------------------------------------- commandes MQTT
    async def _on_mqtt_command(self, entity_id: str, payload: str) -> None:
        e = self.entities.get(entity_id)
        if not e:
            return
        if e.kind == "shutter":
            is_position = (payload.replace(".", "", 1).isdigit())
            await self._shutter_command(e, payload, is_position)
            return
        cmd = e.params.get("_cmd")
        if cmd:
            res = cmd(payload)
            if asyncio.iscoroutine(res):
                await res

    # --------------------------------------------------------- boucle poll
    async def poll_loop(self) -> None:
        while True:
            for e in self.cfg.entities:
                poll = e.params.get("_poll")
                if poll:
                    try:
                        res = poll()
                        if asyncio.iscoroutine(res):
                            await res
                    except Exception as exc:  # noqa: BLE001
                        log.error("Erreur de polling sur %s: %s", e.id, exc)
            await asyncio.sleep(self.cfg.poll_interval_s)

    # -------------------------------------------------------------- run
    async def run(self) -> None:
        await self.setup()
        ready = asyncio.Event()
        mqtt_task = asyncio.ensure_future(self.mqtt.run(ready))
        await ready.wait()
        # premiere lecture immediate des etats
        for e in self.cfg.entities:
            poll = e.params.get("_poll")
            if poll:
                res = poll()
                if asyncio.iscoroutine(res):
                    await res
        poll_task = asyncio.ensure_future(self.poll_loop())
        log.info("Wago2HA demarre.")
        await asyncio.gather(mqtt_task, poll_task)
