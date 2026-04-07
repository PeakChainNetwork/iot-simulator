from __future__ import annotations

from typing import List

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, case_sensitive=False)

    MQTT_HOST: str = Field(default="mosquitto")
    MQTT_PORT: int = Field(default=1883)

    # Comma-separated list, matching repo conventions (dev-001,dev-002,...)
    SIM_DEVICES: str = Field(default="dev-001,dev-002,dev-003")
    SIM_DEFAULT_RATE_HZ: float = Field(default=1.0, ge=0.01)

    def devices(self) -> List[str]:
        parts = [p.strip() for p in self.SIM_DEVICES.split(",")]
        return [p for p in parts if p]


settings = Settings()

