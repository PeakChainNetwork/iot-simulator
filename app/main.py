from __future__ import annotations

import asyncio
import json
import logging
import os
import random
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Literal, Optional

import paho.mqtt.client as mqtt
import yaml
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from app.config import settings
from app.machine import MachineModel, ProfileName, attacker_payload


logger = logging.getLogger("iot-simulator")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))

ScenarioName = Literal["none", "spike", "drift", "flood", "bad_payload", "impersonation"]


@dataclass
class ScenarioConfig:
    scenario: ScenarioName = "none"
    enabled: bool = False
    params: dict[str, Any] | None = None


class ScenarioRuleIn(BaseModel):
    scenario: ScenarioName
    enabled: bool = True
    params: dict[str, Any] | None = None


class ScenarioRequest(BaseModel):
    global_: ScenarioRuleIn | None = Field(default=None, alias="global")
    per_device: dict[str, ScenarioRuleIn] | None = None


class SimulatorState:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self.global_rule = ScenarioConfig()
        self.per_device: dict[str, ScenarioConfig] = {}
        self.last_metrics: dict[str, dict[str, Any]] = {}
        self.device_profile: dict[str, ProfileName] = {}
        self.started_at = datetime.now(timezone.utc).isoformat()

        # Short-lived scenario counters (e.g., spike duration)
        self._spike_remaining: dict[str, int] = {}

    async def set_scenarios(self, req: ScenarioRequest) -> None:
        async with self._lock:
            if req.global_ is not None:
                self.global_rule = ScenarioConfig(
                    scenario=req.global_.scenario,
                    enabled=req.global_.enabled,
                    params=req.global_.params,
                )
            if req.per_device is not None:
                for device_id, rule in req.per_device.items():
                    self.per_device[device_id] = ScenarioConfig(
                        scenario=rule.scenario,
                        enabled=rule.enabled,
                        params=rule.params,
                    )

    async def get_rule_for(self, device_id: str) -> ScenarioConfig:
        async with self._lock:
            specific = self.per_device.get(device_id)
            if specific and specific.enabled:
                return specific
            if self.global_rule.enabled:
                return self.global_rule
            return ScenarioConfig()

    async def set_last_metrics(self, device_id: str, metrics: dict[str, Any]) -> None:
        async with self._lock:
            self.last_metrics[device_id] = metrics

    async def snapshot(self) -> dict[str, Any]:
        async with self._lock:
            return {
                "started_at": self.started_at,
                "global_rule": asdict(self.global_rule),
                "per_device": {k: asdict(v) for k, v in self.per_device.items()},
                "profiles": dict(self.device_profile),
                "last_metrics": dict(self.last_metrics),
            }

    async def spike_remaining(self, device_id: str) -> int:
        async with self._lock:
            return self._spike_remaining.get(device_id, 0)

    async def start_spike(self, device_id: str, n: int) -> None:
        async with self._lock:
            self._spike_remaining[device_id] = max(0, int(n))

    async def dec_spike(self, device_id: str) -> None:
        async with self._lock:
            if device_id in self._spike_remaining:
                self._spike_remaining[device_id] -= 1
                if self._spike_remaining[device_id] <= 0:
                    self._spike_remaining.pop(device_id, None)


class MqttPublisher:
    def __init__(self, host: str, port: int, username: str | None = None, password: str | None = None) -> None:
        self.host = host
        self.port = port
        self.client = mqtt.Client()
        self._connected = asyncio.Event()
        self._stopping = False

        if username:
            self.client.username_pw_set(username=username, password=password)

        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect

    def _on_connect(self, client: mqtt.Client, userdata: Any, flags: dict[str, Any], rc: int) -> None:
        if rc == 0:
            logger.info("Connected to MQTT broker %s:%s", self.host, self.port)
            self._connected.set()
        else:
            logger.warning("MQTT connect failed rc=%s", rc)
            self._connected.clear()

    def _on_disconnect(self, client: mqtt.Client, userdata: Any, rc: int) -> None:
        self._connected.clear()
        if self._stopping:
            return
        logger.warning("Disconnected from MQTT broker rc=%s", rc)

    async def start(self) -> None:
        # Start Paho network loop in its own thread.
        await asyncio.to_thread(self.client.connect, self.host, self.port, 60)
        self.client.loop_start()
        asyncio.create_task(self._reconnect_loop())

    async def stop(self) -> None:
        self._stopping = True
        self._connected.clear()
        try:
            await asyncio.to_thread(self.client.disconnect)
        finally:
            self.client.loop_stop()

    async def _reconnect_loop(self) -> None:
        backoff = 0.5
        while not self._stopping:
            if self._connected.is_set():
                backoff = 0.5
                await asyncio.sleep(1.0)
                continue
            try:
                await asyncio.to_thread(self.client.reconnect)
                await asyncio.sleep(0.2)
            except Exception as exc:
                logger.warning("Reconnect attempt failed: %s", exc)
                await asyncio.sleep(backoff)
                backoff = min(15.0, backoff * 1.7)

    async def wait_connected(self, timeout_s: float = 10.0) -> bool:
        try:
            await asyncio.wait_for(self._connected.wait(), timeout=timeout_s)
            return True
        except asyncio.TimeoutError:
            return False

    async def publish(self, topic: str, payload: str, qos: int = 1) -> None:
        # Paho publish is thread-safe; still guard against disconnected state.
        if not self._connected.is_set():
            return
        await asyncio.to_thread(self.client.publish, topic, payload, qos)


def load_profiles() -> dict[str, Any]:
    cfg_path = os.path.join(os.path.dirname(__file__), "..", "config", "profiles.yaml")
    cfg_path = os.path.abspath(cfg_path)
    with open(cfg_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)["profiles"]


def choose_initial_profile(device_id: str) -> ProfileName:
    # Deterministic-ish distribution: dev-001 normal, dev-002 noisy, dev-003 failing by default
    if device_id.endswith("001"):
        return "normal"
    if device_id.endswith("002"):
        return "noisy"
    if device_id.endswith("003"):
        return "failing"
    return random.choice(["normal", "noisy"])


def build_topic(device_id: str) -> str:
    return f"factory/{device_id}/telemetry"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def device_loop(
    *,
    device_id: str,
    model: MachineModel,
    state: SimulatorState,
    publisher: MqttPublisher,
    default_rate_hz: float,
) -> None:
    dt = max(0.01, 1.0 / float(default_rate_hz))
    last_t = asyncio.get_event_loop().time()

    while True:
        rule = await state.get_rule_for(device_id)

        # Flood changes publish cadence to 100Hz.
        rate_hz = default_rate_hz
        if rule.enabled and rule.scenario == "flood":
            rate_hz = 100.0
        dt_target = max(0.01, 1.0 / float(rate_hz))

        # Compute dt based on real time to keep dynamics stable even if loop jitters.
        now_t = asyncio.get_event_loop().time()
        dt_sim = max(0.01, min(1.0, now_t - last_t))
        last_t = now_t

        # Scenario application
        if model.state.profile == "attacker":
            metrics: dict[str, Any] = attacker_payload(device_id)
        else:
            if rule.enabled and rule.scenario == "drift":
                params = rule.params or {}
                # drift rates are per-minute; apply proportionally to dt_sim
                temp_per_min = float(params.get("temp_c_per_min", 0.25))
                vib_per_min = float(params.get("vib_mms_per_min", 0.05))
                press_per_min = float(params.get("pressure_psi_per_min", -0.2))
                model.apply_drift(
                    temp_c=temp_per_min * (dt_sim / 60.0),
                    vib_mms=vib_per_min * (dt_sim / 60.0),
                    pressure_psi=press_per_min * (dt_sim / 60.0),
                )

            if rule.enabled and rule.scenario == "spike":
                remaining = await state.spike_remaining(device_id)
                if remaining <= 0:
                    params = rule.params or {}
                    n = int(params.get("duration_messages", random.randint(5, 10)))
                    await state.start_spike(device_id, n)
                # while spike remaining, apply larger impulses
                model.apply_spike(
                    temp_c=float((rule.params or {}).get("temp_c", 18.0)),
                    vib_mms=float((rule.params or {}).get("vib_mms", 2.2)),
                )
                await state.dec_spike(device_id)

            s = model.step(dt_sim)
            metrics = s.as_metrics()

        await state.set_last_metrics(device_id, metrics)

        # bad_payload scenario mutates the payload after metrics are computed
        topic_device_id = device_id
        payload: str
        if rule.enabled and rule.scenario == "impersonation":
            # Publish as unauthorized periodically (every ~10 messages)
            if random.random() < float((rule.params or {}).get("prob", 0.12)):
                topic_device_id = "unauthorized-id"

        if rule.enabled and rule.scenario == "bad_payload":
            mode = str((rule.params or {}).get("mode", random.choice(["corrupt_json", "missing_keys", "oversized"])))
            if mode == "missing_keys":
                payload_obj = {"timestamp": _now_iso(), "temperature": metrics.get("temperature")}
                payload = json.dumps(payload_obj)
            elif mode == "oversized":
                big = "X" * int((rule.params or {}).get("bytes", 200_000))
                payload_obj = {"timestamp": _now_iso(), "note": big, "temperature": metrics.get("temperature"), "pressure": metrics.get("pressure")}
                payload = json.dumps(payload_obj)
            else:
                # corrupt_json
                payload = '{"timestamp": "' + _now_iso() + '", "temperature": '  # intentionally truncated
        else:
            payload = json.dumps(metrics)

        await publisher.publish(build_topic(topic_device_id), payload, qos=1)
        await asyncio.sleep(dt_target)


def create_app(state: SimulatorState) -> FastAPI:
    app = FastAPI(title="iot-simulator", version="0.1.0")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/scenario")
    async def set_scenario(req: ScenarioRequest) -> dict[str, Any]:
        if req.global_ is None and req.per_device is None:
            raise HTTPException(status_code=400, detail="Provide at least one of: global, per_device")
        await state.set_scenarios(req)
        return {"ok": True}

    @app.get("/state")
    async def get_state() -> dict[str, Any]:
        return await state.snapshot()

    return app


async def main_async() -> None:
    profiles = load_profiles()
    device_ids = settings.devices()

    state = SimulatorState()
    publisher = MqttPublisher(
        settings.MQTT_HOST,
        settings.MQTT_PORT,
        settings.MQTT_USERNAME,
        settings.MQTT_PASSWORD,
    )

    # Create models
    models: dict[str, MachineModel] = {}
    for dev in device_ids:
        profile: ProfileName = choose_initial_profile(dev)
        state.device_profile[dev] = profile
        models[dev] = MachineModel(dev, profile, profiles[profile if profile != "attacker" else "normal"])

    await publisher.start()
    await publisher.wait_connected(timeout_s=10.0)

    # Start device tasks
    tasks = [
        asyncio.create_task(
            device_loop(
                device_id=dev,
                model=models[dev],
                state=state,
                publisher=publisher,
                default_rate_hz=settings.SIM_DEFAULT_RATE_HZ,
            )
        )
        for dev in device_ids
    ]

    # Run until cancelled; FastAPI lifecycle will host this in uvicorn.
    try:
        await asyncio.gather(*tasks)
    finally:
        await publisher.stop()


# FastAPI entrypoint: we start background tasks on startup.
state = SimulatorState()
app = create_app(state)


@app.on_event("startup")
async def _startup() -> None:
    profiles = load_profiles()
    device_ids = settings.devices()

    app.state.profiles = profiles
    app.state.publisher = MqttPublisher(
        settings.MQTT_HOST,
        settings.MQTT_PORT,
        settings.MQTT_USERNAME,
        settings.MQTT_PASSWORD,
    )
    app.state.models = {}

    for dev in device_ids:
        profile: ProfileName = choose_initial_profile(dev)
        state.device_profile[dev] = profile
        app.state.models[dev] = MachineModel(dev, profile, profiles[profile if profile != "attacker" else "normal"])

    await app.state.publisher.start()
    await app.state.publisher.wait_connected(timeout_s=10.0)

    app.state.tasks = [
        asyncio.create_task(
            device_loop(
                device_id=dev,
                model=app.state.models[dev],
                state=state,
                publisher=app.state.publisher,
                default_rate_hz=settings.SIM_DEFAULT_RATE_HZ,
            )
        )
        for dev in device_ids
    ]


@app.on_event("shutdown")
async def _shutdown() -> None:
    tasks = getattr(app.state, "tasks", [])
    for t in tasks:
        t.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)

    publisher: Optional[MqttPublisher] = getattr(app.state, "publisher", None)
    if publisher is not None:
        await publisher.stop()

