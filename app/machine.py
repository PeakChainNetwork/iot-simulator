from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal


ProfileName = Literal["normal", "noisy", "failing", "attacker"]


def _clamp(x: float, lo: float | None = None, hi: float | None = None) -> float:
    if lo is not None:
        x = max(lo, x)
    if hi is not None:
        x = min(hi, x)
    return x


def _first_order_lag(current: float, target: float, dt: float, tau: float) -> float:
    # Stable Euler form of first-order lag: x += (1 - exp(-dt/tau)) * (target - x)
    if tau <= 0:
        return target
    alpha = 1.0 - math.exp(-dt / tau)
    return current + alpha * (target - current)


@dataclass
class MachineState:
    device_id: str
    profile: ProfileName
    t_s: float = 0.0

    rotational_speed: float = 1000.0  # RPM
    power_draw: float = 3.0  # kW
    temperature: float = 30.0  # °C
    pressure: float = 120.0  # PSI
    vibration: float = 1.5  # mm/s

    last_status: str = "ok"

    def as_metrics(self) -> dict[str, Any]:
        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "temperature": round(self.temperature, 3),
            "pressure": round(self.pressure, 3),
            "vibration": round(self.vibration, 3),
            "power_draw": round(self.power_draw, 3),
            "rotational_speed": round(self.rotational_speed, 3),
            "status": self.last_status,
        }


class MachineModel:
    """
    A small, stateful physics-inspired model.

    Rotational speed (RPM) acts as a driver. Power draw, temperature, vibration,
    and pressure are coupled to RPM and each other with realistic lags.
    """

    def __init__(self, device_id: str, profile: ProfileName, profile_cfg: dict[str, Any]) -> None:
        self.state = MachineState(device_id=device_id, profile=profile)
        self.cfg = profile_cfg
        self._rpm_phase = random.random() * 2 * math.pi
        self._temp_phase = random.random() * 2 * math.pi

        # Drift accumulators (used by drift/failing scenarios)
        self._drift_temp_c = 0.0
        self._drift_vib_mms = 0.0
        self._drift_pressure_psi = 0.0

        # Scenario-local short-term impulses
        self._impulses: dict[str, float] = {"temp": 0.0, "vib": 0.0, "press": 0.0, "kw": 0.0, "rpm": 0.0}

    def apply_drift(self, *, temp_c: float = 0.0, vib_mms: float = 0.0, pressure_psi: float = 0.0) -> None:
        self._drift_temp_c += temp_c
        self._drift_vib_mms += vib_mms
        self._drift_pressure_psi += pressure_psi

    def apply_spike(self, *, temp_c: float = 0.0, vib_mms: float = 0.0, pressure_psi: float = 0.0, kw: float = 0.0, rpm: float = 0.0) -> None:
        self._impulses["temp"] += temp_c
        self._impulses["vib"] += vib_mms
        self._impulses["press"] += pressure_psi
        self._impulses["kw"] += kw
        self._impulses["rpm"] += rpm

    def step(self, dt: float) -> MachineState:
        s = self.state
        s.t_s += dt

        rpm_cfg = self.cfg["rpm"]
        kw_cfg = self.cfg["power_draw_kw"]
        t_cfg = self.cfg["temperature_c"]
        p_cfg = self.cfg["pressure_psi"]
        v_cfg = self.cfg["vibration_mms"]
        drift_cfg = self.cfg.get("failing_drift", {})

        # RPM target: within bounds, with small sine-wave micro-oscillation and noise.
        rpm_min = float(rpm_cfg["min"])
        rpm_max = float(rpm_cfg["max"])
        rpm_center = 0.5 * (rpm_min + rpm_max)
        rpm_span = 0.5 * (rpm_max - rpm_min)
        micro_frac = float(rpm_cfg.get("micro_osc_frac", 0.0))
        micro_hz = float(rpm_cfg.get("micro_osc_hz", 0.0))
        rpm_micro = rpm_center * micro_frac * math.sin(2 * math.pi * micro_hz * s.t_s + self._rpm_phase)
        rpm_noise = random.gauss(0.0, float(rpm_cfg.get("noise_std", 0.0)))

        rpm_target = rpm_center + rpm_span * math.sin(2 * math.pi * (micro_hz / 3.0 + 0.002) * s.t_s + self._rpm_phase / 2.0)
        rpm_target += rpm_micro + rpm_noise + self._impulses["rpm"]

        # Noisy profile allows erratic safe spikes
        if "spike_prob" in rpm_cfg and random.random() < float(rpm_cfg["spike_prob"]):
            rpm_target += random.choice([-1.0, 1.0]) * float(rpm_cfg.get("spike_rpm", 0.0))

        rpm_target = _clamp(rpm_target, rpm_min, rpm_max)

        # Slew-rate limit to avoid impossible RPM jumps
        slew = float(rpm_cfg.get("slew_rpm_per_s", 999999.0))
        max_delta = slew * dt
        rpm_next = _clamp(rpm_target, s.rotational_speed - max_delta, s.rotational_speed + max_delta)
        s.rotational_speed = rpm_next

        # Power draw depends on RPM and varies with lag (load factor implicit).
        kw_target = float(kw_cfg["base_kw"]) + float(kw_cfg["per_rpm_kw"]) * s.rotational_speed
        if "spike_prob" in kw_cfg and random.random() < float(kw_cfg["spike_prob"]):
            kw_target += float(kw_cfg.get("spike_kw", 0.0))
        kw_target += random.gauss(0.0, float(kw_cfg.get("noise_std", 0.0))) + self._impulses["kw"]
        s.power_draw = _first_order_lag(s.power_draw, kw_target, dt, float(kw_cfg.get("lag_s", 1.0)))

        # Temperature follows power draw with a slower lag + ambient oscillation.
        ambient = float(t_cfg["ambient_c"])
        per_kw = float(t_cfg["per_kw_c"])
        temp_micro = float(t_cfg.get("micro_osc_amp_c", 0.0)) * math.sin(2 * math.pi * float(t_cfg.get("micro_osc_hz", 0.0)) * s.t_s + self._temp_phase)
        temp_target = ambient + per_kw * s.power_draw + temp_micro
        temp_target += random.gauss(0.0, float(t_cfg.get("noise_std", 0.0))) + self._drift_temp_c + self._impulses["temp"]
        bounds_t = t_cfg.get("bounds", {})
        s.temperature = _first_order_lag(s.temperature, temp_target, dt, float(t_cfg.get("lag_s", 10.0)))
        s.temperature = _clamp(s.temperature, float(bounds_t.get("min", -1e9)), float(bounds_t.get("max", 1e9)))
        if "spike_prob" in t_cfg and random.random() < float(t_cfg["spike_prob"]):
            s.temperature = _clamp(s.temperature + float(t_cfg.get("spike_c", 0.0)), float(bounds_t.get("min", -1e9)), float(bounds_t.get("max", 1e9)))

        # Pressure depends weakly on RPM; failing/drift can push downward.
        press_target = float(p_cfg["base_psi"]) + float(p_cfg["per_rpm_psi"]) * s.rotational_speed
        press_target += random.gauss(0.0, float(p_cfg.get("noise_std", 0.0))) + self._drift_pressure_psi + self._impulses["press"]
        bounds_p = p_cfg.get("bounds", {})
        s.pressure = _first_order_lag(s.pressure, press_target, dt, float(p_cfg.get("lag_s", 5.0)))
        s.pressure = _clamp(s.pressure, float(bounds_p.get("min", -1e9)), float(bounds_p.get("max", 1e9)))
        if "spike_prob" in p_cfg and random.random() < float(p_cfg["spike_prob"]):
            s.pressure = _clamp(s.pressure + float(p_cfg.get("spike_psi", 0.0)), float(bounds_p.get("min", -1e9)), float(bounds_p.get("max", 1e9)))

        # Vibration depends on RPM and bearing looseness, plus micro oscillation.
        bearing = float(v_cfg.get("bearing_factor", 1.0))
        vib_micro = float(v_cfg.get("micro_osc_amp_mms", 0.0)) * math.sin(2 * math.pi * float(v_cfg.get("micro_osc_hz", 0.0)) * s.t_s + self._rpm_phase / 3.0)
        vib_target = (float(v_cfg["base_mms"]) + float(v_cfg["per_rpm_mms"]) * s.rotational_speed) * bearing + vib_micro
        vib_target += random.gauss(0.0, float(v_cfg.get("noise_std", 0.0))) + self._drift_vib_mms + self._impulses["vib"]
        bounds_v = v_cfg.get("bounds", {})
        s.vibration = _first_order_lag(s.vibration, vib_target, dt, float(v_cfg.get("lag_s", 2.0)))
        s.vibration = _clamp(s.vibration, float(bounds_v.get("min", -1e9)), float(bounds_v.get("max", 1e9)))
        if "spike_prob" in v_cfg and random.random() < float(v_cfg["spike_prob"]):
            s.vibration = _clamp(s.vibration + float(v_cfg.get("spike_mms", 0.0)), float(bounds_v.get("min", -1e9)), float(bounds_v.get("max", 1e9)))

        # Built-in failing drift if profile enables it (per-minute drift).
        if drift_cfg.get("enabled"):
            dt_min = dt / 60.0
            self._drift_temp_c += float(drift_cfg.get("temp_c_per_min", 0.0)) * dt_min
            self._drift_vib_mms += float(drift_cfg.get("vib_mms_per_min", 0.0)) * dt_min
            self._drift_pressure_psi += float(drift_cfg.get("pressure_psi_per_min", 0.0)) * dt_min

        # Status: warning sometimes when failing drift present and metrics degrade.
        s.last_status = "ok"
        if drift_cfg.get("enabled") and random.random() < float(drift_cfg.get("warning_prob", 0.0)):
            s.last_status = "warning"

        # Decay impulses so spikes cool off.
        for k in list(self._impulses.keys()):
            self._impulses[k] *= 0.65
            if abs(self._impulses[k]) < 1e-3:
                self._impulses[k] = 0.0

        return s


def attacker_payload(device_id: str) -> dict[str, Any]:
    """
    Produces intentionally impossible physics (for attacker profile/scenario).
    """
    now = datetime.now(timezone.utc).isoformat()
    mode = random.choice(["impossible", "contradictory", "flatline"])
    if mode == "flatline":
        rpm = 0
        temp = random.choice([5, 500, 900])
        return {"timestamp": now, "rotational_speed": rpm, "temperature": temp, "pressure": 0, "vibration": 0, "power_draw": 0, "status": "warning"}
    if mode == "contradictory":
        return {"timestamp": now, "rotational_speed": 0, "temperature": 520, "pressure": 200, "vibration": 35, "power_draw": 0.2, "status": "warning"}
    return {"timestamp": now, "rotational_speed": 2500, "temperature": -40, "pressure": -10, "vibration": 0, "power_draw": 30, "status": "warning"}

