from __future__ import annotations

from typing import List

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False, extra="ignore")

    MQTT_HOST: str = Field(default="mosquitto")
    MQTT_PORT: int = Field(default=1883)
    MQTT_USERNAME: str = Field(default="iot_client")
    MQTT_PASSWORD: str = Field(default="iot_password")

    # Transport to the broker. Use "websockets" + TLS to reach an exposed
    # broker over WSS (e.g. wss://<host>:443/mqtt); "tcp" for a local broker.
    MQTT_TRANSPORT: str = Field(default="tcp")  # "tcp" | "websockets"
    MQTT_WS_PATH: str = Field(default="/mqtt")
    MQTT_TLS: bool = Field(default=False)

    # MQTT client id. Leave blank to auto-generate a unique one per run, so
    # multiple simulators never evict each other on the broker.
    MQTT_CLIENT_ID: str = Field(default="")

    # Comma-separated list, matching repo conventions (dev-001,dev-002,...)
    SIM_DEVICES: str = Field(default="dev-001,dev-002,dev-003")
    SIM_DEFAULT_RATE_HZ: float = Field(default=1.0, ge=0.01)

    def devices(self) -> List[str]:
        parts = [p.strip() for p in self.SIM_DEVICES.split(",")]
        return [p for p in parts if p]


settings = Settings()

