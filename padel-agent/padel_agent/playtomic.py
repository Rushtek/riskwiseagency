"""Integración con Playtomic.

Hay tres fuentes posibles, de menos a más detalle:

1. ``PlaytomicPublicClient``: la disponibilidad pública del club (la misma que ve
   cualquier jugador en la app). No necesita credenciales. Sabemos qué huecos están
   libres y, por diferencia con el horario del club, cuáles están ocupados, pero
   no quién ha reservado.
2. ``PlaytomicPartnerClient``: la API de Playtomic para clubes/partners. Devuelve el
   detalle de cada reserva (jugador, teléfono...). Hay que pedir el acceso a
   Playtomic; la URL base, el token y la ruta son configurables porque dependen
   del acuerdo con Playtomic.
3. ``load_bookings_csv``: la exportación de reservas de Playtomic Manager (CSV).
   Útil si no hay acceso a la API de partners.
"""

from __future__ import annotations

import csv
import json
import unicodedata
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .models import SOURCE_PLAYTOMIC, Booking, ClubConfig

PUBLIC_API_BASE = "https://api.playtomic.io"

# fetch(url, params, headers) -> JSON decodificado. Inyectable para tests.
Fetcher = Callable[[str, dict[str, str], dict[str, str]], Any]

Interval = tuple[datetime, datetime]


class PlaytomicError(RuntimeError):
    pass


def http_get_json(url: str, params: dict[str, str], headers: dict[str, str]) -> Any:
    full = f"{url}?{urllib.parse.urlencode(params)}" if params else url
    req = urllib.request.Request(full, headers={"Accept": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:  # red, HTTP 4xx/5xx, JSON inválido...
        raise PlaytomicError(f"Error consultando Playtomic ({url}): {e}") from e


class PlaytomicPublicClient:
    """Disponibilidad pública de un club en Playtomic."""

    def __init__(self, tenant_id: str, club_tz: ZoneInfo, slot_tz: str = "UTC",
                 base_url: str = PUBLIC_API_BASE, fetch: Fetcher = http_get_json):
        self.tenant_id = tenant_id
        self.club_tz = club_tz
        self.slot_tz = ZoneInfo(slot_tz)
        self.base_url = base_url.rstrip("/")
        self.fetch = fetch

    def courts(self) -> list[dict]:
        """Pistas publicadas en Playtomic: [{resource_id, name, ...}]."""
        data = self.fetch(f"{self.base_url}/v1/tenants/{self.tenant_id}", {}, {})
        return [r for r in data.get("resources", []) if r.get("sport_id", "PADEL") == "PADEL"]

    def free_intervals(self, day: date) -> dict[str, list[Interval]]:
        """Huecos libres en Playtomic para un día (hora del club), por resource_id.

        Cada hueco publicado (inicio + duración) se convierte en un intervalo libre;
        luego se fusionan los solapados.
        """
        local_start = datetime.combine(day, time.min, tzinfo=self.club_tz)
        local_end = local_start + timedelta(days=1)
        fmt = "%Y-%m-%dT%H:%M:%S"
        params = {
            "sport_id": "PADEL",
            "tenant_id": self.tenant_id,
            "start_min": local_start.astimezone(self.slot_tz).strftime(fmt),
            "start_max": local_end.astimezone(self.slot_tz).strftime(fmt),
        }
        data = self.fetch(f"{self.base_url}/v1/availability", params, {})
        result: dict[str, list[Interval]] = {}
        for item in data or []:
            rid = item.get("resource_id")
            if not rid:
                continue
            slot_day = date.fromisoformat(item["start_date"][:10])
            intervals = result.setdefault(rid, [])
            for slot in item.get("slots", []):
                start = datetime.combine(slot_day, time.fromisoformat(slot["start_time"]),
                                         tzinfo=self.slot_tz).astimezone(self.club_tz)
                intervals.append((start, start + timedelta(minutes=int(slot["duration"]))))
        return {rid: merge_intervals(iv) for rid, iv in result.items()}


class PlaytomicPartnerClient:
    """API de Playtomic para clubes (detalle de reservas). Requiere credenciales."""

    def __init__(self, base_url: str, token: str, tenant_id: str, club: ClubConfig,
                 bookings_path: str = "/v1/bookings", fetch: Fetcher = http_get_json):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.tenant_id = tenant_id
        self.club = club
        self.bookings_path = bookings_path
        self.fetch = fetch

    def bookings(self, start: datetime, end: datetime) -> list[Booking]:
        fmt = "%Y-%m-%dT%H:%M:%S"
        params = {
            "tenant_id": self.tenant_id,
            "sport_id": "PADEL",
            "start_min": start.astimezone(ZoneInfo("UTC")).strftime(fmt),
            "start_max": end.astimezone(ZoneInfo("UTC")).strftime(fmt),
        }
        data = self.fetch(f"{self.base_url}{self.bookings_path}", params,
                          {"Authorization": f"Bearer {self.token}"})
        items = data.get("data", data.get("bookings", [])) if isinstance(data, dict) else data
        out = []
        for raw in items or []:
            b = booking_from_partner_json(raw, self.club)
            if b and b.overlaps(start, end):
                out.append(b)
        return out


def booking_from_partner_json(raw: dict, club: ClubConfig) -> Booking | None:
    """Normaliza una reserva de la API de partners (tolerante a variaciones de formato)."""
    if raw.get("status", "").upper() in ("CANCELED", "CANCELLED"):
        return None
    rid = raw.get("resource_id") or (raw.get("resource") or {}).get("resource_id")
    court = club.court_by_resource(rid) if rid else None
    start_raw = raw.get("booking_start_date") or raw.get("start_date") or raw.get("start")
    if not start_raw:
        return None
    start = datetime.fromisoformat(str(start_raw).replace("Z", "+00:00"))
    if start.tzinfo is None:
        start = start.replace(tzinfo=ZoneInfo("UTC"))
    end_raw = raw.get("booking_end_date") or raw.get("end_date") or raw.get("end")
    if end_raw:
        end = datetime.fromisoformat(str(end_raw).replace("Z", "+00:00"))
        if end.tzinfo is None:
            end = end.replace(tzinfo=ZoneInfo("UTC"))
    else:
        end = start + timedelta(minutes=int(raw.get("duration", club.default_duration_minutes)))
    owner = raw.get("owner") or raw.get("customer") or raw.get("user") or {}
    if not owner and raw.get("players"):
        owner = raw["players"][0]
    return Booking(
        source=SOURCE_PLAYTOMIC,
        id=f"P{raw.get('booking_id') or raw.get('id') or ''}",
        court_id=court.id if court else str(rid),
        start=start,
        end=end,
        customer_name=owner.get("name") or owner.get("full_name"),
        phone=owner.get("phone"),
        email=owner.get("email"),
        extra={"players": [p.get("name") for p in raw.get("players", []) if p.get("name")]},
    )


# --- Exportación CSV de Playtomic Manager -----------------------------------

_CSV_COLUMNS = {
    "court": ["pista", "court", "recurso", "resource", "espacio"],
    "date": ["fecha", "date", "dia", "day"],
    "start": ["hora inicio", "inicio", "hora", "start time", "start", "hora de inicio"],
    "end": ["hora fin", "fin", "end time", "end", "hora de fin"],
    "duration": ["duracion", "duration", "minutos"],
    "name": ["cliente", "usuario", "jugador", "nombre", "name", "customer", "user", "player"],
    "phone": ["telefono", "phone", "movil", "mobile"],
    "email": ["email", "correo", "e-mail"],
    "id": ["id", "id reserva", "booking id", "reserva"],
    "status": ["estado", "status"],
}


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return " ".join(s.lower().replace("_", " ").split())


def load_bookings_csv(path: str | Path, club: ClubConfig) -> list[Booking]:
    """Lee una exportación CSV de reservas (columnas en español o inglés)."""
    text = Path(path).read_text(encoding="utf-8-sig")
    dialect = csv.Sniffer().sniff(text.splitlines()[0], delimiters=",;\t")
    reader = csv.DictReader(text.splitlines(), dialect=dialect)
    colmap: dict[str, str] = {}
    for header in reader.fieldnames or []:
        n = _norm(header)
        for key, aliases in _CSV_COLUMNS.items():
            if key not in colmap and n in aliases:
                colmap[key] = header
    missing = {"court", "date", "start"} - colmap.keys()
    if missing:
        raise PlaytomicError(f"El CSV no tiene las columnas obligatorias: {sorted(missing)}")

    out = []
    for i, row in enumerate(reader, start=1):
        get = lambda k: (row.get(colmap[k]) or "").strip() if k in colmap else ""  # noqa: E731
        if _norm(get("status")) in ("cancelada", "cancelado", "cancelled", "canceled"):
            continue
        day = _parse_date(get("date"))
        start = datetime.combine(day, time.fromisoformat(get("start")[:5]), tzinfo=club.tz)
        if get("end"):
            end = datetime.combine(day, time.fromisoformat(get("end")[:5]), tzinfo=club.tz)
            if end <= start:
                end += timedelta(days=1)
        else:
            minutes = int("".join(ch for ch in get("duration") if ch.isdigit()) or club.default_duration_minutes)
            end = start + timedelta(minutes=minutes)
        try:
            court_id = club.court(get("court")).id
        except KeyError:
            court_id = get("court")
        out.append(Booking(
            source=SOURCE_PLAYTOMIC, id=f"P{get('id') or f'csv{i}'}", court_id=court_id,
            start=start, end=end, customer_name=get("name") or None,
            phone=get("phone") or None, email=get("email") or None,
        ))
    return out


def _parse_date(s: str) -> date:
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(s[:10], fmt).date()
        except ValueError:
            continue
    raise PlaytomicError(f"Fecha no reconocida en el CSV: '{s}'")


# --- Utilidades de intervalos ------------------------------------------------

def merge_intervals(intervals: list[Interval]) -> list[Interval]:
    merged: list[Interval] = []
    for s, e in sorted(intervals):
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def subtract_intervals(base: list[Interval], remove: list[Interval]) -> list[Interval]:
    out = []
    for s, e in base:
        pieces = [(s, e)]
        for rs, re_ in merge_intervals(remove):
            nxt = []
            for ps, pe in pieces:
                if re_ <= ps or rs >= pe:
                    nxt.append((ps, pe))
                    continue
                if rs > ps:
                    nxt.append((ps, rs))
                if re_ < pe:
                    nxt.append((re_, pe))
            pieces = nxt
        out.extend(pieces)
    return out
