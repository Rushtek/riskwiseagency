"""Modelos de datos y configuración del club."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

# Origen de cada reserva
SOURCE_LOCAL = "local"  # teléfono, WhatsApp, mostrador... (gestionada por el agente)
SOURCE_PLAYTOMIC = "playtomic"


@dataclass
class Court:
    id: str
    name: str
    playtomic_resource_id: str | None = None


@dataclass
class ClubConfig:
    name: str
    timezone: str
    opening_hours: dict[str, tuple[time, time]]
    courts: list[Court]
    slot_minutes: int = 30
    default_duration_minutes: int = 90
    playtomic_tenant_id: str | None = None
    info: str = ""

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def court(self, court_ref: str) -> Court:
        """Busca una pista por id, nombre o id de recurso de Playtomic."""
        ref = court_ref.strip().lower()
        for c in self.courts:
            if ref in (c.id.lower(), c.name.lower(), (c.playtomic_resource_id or "").lower()):
                return c
        # "pista 2" -> id "2"
        digits = "".join(ch for ch in ref if ch.isdigit())
        for c in self.courts:
            if digits and c.id == digits:
                return c
        raise KeyError(f"No existe la pista '{court_ref}'")

    def court_by_resource(self, resource_id: str) -> Court | None:
        return next((c for c in self.courts if c.playtomic_resource_id == resource_id), None)

    def opening_window(self, day: date) -> tuple[datetime, datetime] | None:
        hours = self.opening_hours.get(WEEKDAYS[day.weekday()])
        if not hours:
            return None
        open_t, close_t = hours
        start = datetime.combine(day, open_t, tzinfo=self.tz)
        end = datetime.combine(day, close_t, tzinfo=self.tz)
        if end <= start:  # cierra pasada la medianoche
            end += timedelta(days=1)
        return start, end


def load_club_config(path: str | Path) -> ClubConfig:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    hours = {
        day: (time.fromisoformat(v[0]), time.fromisoformat(v[1]))
        for day, v in raw.get("opening_hours", {}).items()
        if v
    }
    return ClubConfig(
        name=raw["name"],
        timezone=raw.get("timezone", "Europe/Madrid"),
        opening_hours=hours,
        courts=[Court(**c) for c in raw["courts"]],
        slot_minutes=raw.get("slot_minutes", 30),
        default_duration_minutes=raw.get("default_duration_minutes", 90),
        playtomic_tenant_id=raw.get("playtomic_tenant_id"),
        info=raw.get("info", ""),
    )


@dataclass
class Booking:
    source: str
    court_id: str
    start: datetime
    end: datetime
    id: str = ""
    customer_name: str | None = None
    phone: str | None = None
    email: str | None = None
    notes: str = ""
    # True cuando solo sabemos que el hueco está ocupado en Playtomic, sin detalle
    anonymous: bool = False
    extra: dict = field(default_factory=dict)

    def overlaps(self, start: datetime, end: datetime) -> bool:
        return self.start < end and start < self.end

    def to_dict(self, club: ClubConfig) -> dict:
        d = asdict(self)
        d.pop("extra")
        try:
            d["court_name"] = club.court(self.court_id).name
        except KeyError:
            d["court_name"] = self.court_id
        d["start"] = self.start.astimezone(club.tz).strftime("%Y-%m-%d %H:%M")
        d["end"] = self.end.astimezone(club.tz).strftime("%Y-%m-%d %H:%M")
        return {k: v for k, v in d.items() if v not in (None, "", False)}
