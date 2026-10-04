"""Vista unificada de reservas: las propias del club + las de Playtomic."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timedelta

from .models import SOURCE_LOCAL, SOURCE_PLAYTOMIC, Booking, ClubConfig
from .playtomic import (
    PlaytomicError,
    PlaytomicPartnerClient,
    PlaytomicPublicClient,
    load_bookings_csv,
    subtract_intervals,
)
from .store import BookingStore


class BookingError(ValueError):
    """Error de negocio que se le explica al usuario (pista ocupada, fuera de horario...)."""


class BookingService:
    def __init__(
        self,
        club: ClubConfig,
        store: BookingStore,
        public: PlaytomicPublicClient | None = None,
        partner: PlaytomicPartnerClient | None = None,
        csv_path: str | None = None,
        now: Callable[[], datetime] | None = None,
    ):
        self.club = club
        self.store = store
        self.public = public
        self.partner = partner
        self.csv_path = csv_path
        self._now = now or (lambda: datetime.now(club.tz))
        self.warnings: list[str] = []

    def now(self) -> datetime:
        return self._now().astimezone(self.club.tz)

    # --- Lectura -------------------------------------------------------------

    def bookings_for_day(self, day: date, court_id: str | None = None) -> list[Booking]:
        """Todas las reservas de un día: propias, de Playtomic con detalle y huecos
        ocupados en Playtomic sin detalle (cuando solo hay disponibilidad pública)."""
        window = self.club.opening_window(day)
        day_start = datetime.combine(day, datetime.min.time(), tzinfo=self.club.tz)
        start, end = window if window else (day_start, day_start + timedelta(days=1))
        start, end = min(start, day_start), max(end, day_start + timedelta(days=1))

        local = self.store.between(start, end)
        detailed = self._playtomic_detailed(start, end)
        result = local + detailed
        if window:
            result += self._playtomic_busy_blocks(day, window, local + detailed)
        if court_id:
            result = [b for b in result if b.court_id == court_id]
        return sorted(result, key=lambda b: (b.start, b.court_id))

    def search(self, text: str, from_day: date, days: int = 14) -> list[Booking]:
        """Busca reservas por nombre, teléfono, email o id en ambas fuentes."""
        q = text.strip().lower()
        q_digits = "".join(ch for ch in q if ch.isdigit())
        start = datetime.combine(from_day, datetime.min.time(), tzinfo=self.club.tz)
        end = start + timedelta(days=days)
        found = []
        for b in self.store.between(start, end) + self._playtomic_detailed(start, end):
            haystack = " ".join(filter(None, [b.id, b.customer_name, b.email, b.notes,
                                              *b.extra.get("players", [])])).lower()
            phone_digits = "".join(ch for ch in (b.phone or "") if ch.isdigit())
            if q in haystack or (len(q_digits) >= 6 and q_digits in phone_digits):
                found.append(b)
        return sorted(found, key=lambda b: b.start)

    def free_slots(self, day: date, duration: int, court_id: str | None = None,
                   earliest: datetime | None = None, latest: datetime | None = None) -> list[dict]:
        """Horas de inicio libres (en todas las fuentes) para una duración dada."""
        window = self.club.opening_window(day)
        if not window:
            return []
        open_at, close_at = window
        busy = self.bookings_for_day(day)
        step = timedelta(minutes=self.club.slot_minutes)
        length = timedelta(minutes=duration)
        now = self.now()
        out = []
        for court in self.club.courts:
            if court_id and court.id != court_id:
                continue
            court_busy = [b for b in busy if b.court_id == court.id]
            t = open_at
            while t + length <= close_at:
                ok = (t >= now and (not earliest or t >= earliest) and (not latest or t <= latest)
                      and not any(b.overlaps(t, t + length) for b in court_busy))
                if ok:
                    out.append({"court_id": court.id, "court_name": court.name,
                                "start": t.strftime("%H:%M"), "end": (t + length).strftime("%H:%M")})
                t += step
        return out

    # --- Escritura (solo reservas propias) -------------------------------------

    def create(self, court_id: str, start: datetime, duration: int, customer_name: str,
               phone: str | None = None, notes: str = "") -> Booking:
        court = self.club.court(court_id)
        start = start.astimezone(self.club.tz)
        end = start + timedelta(minutes=duration)
        if start < self.now():
            raise BookingError("No se puede reservar en el pasado.")
        window = self.club.opening_window(start.date())
        if not window or start < window[0] or end > window[1]:
            raise BookingError("La reserva queda fuera del horario del club.")
        # Comprobación con datos frescos de Playtomic para evitar dobles reservas
        self.warnings.clear()
        conflicts = [b for b in self.bookings_for_day(start.date(), court.id) if b.overlaps(start, end)]
        if conflicts:
            c = conflicts[0]
            origen = "Playtomic" if c.source == SOURCE_PLAYTOMIC else "el club"
            raise BookingError(
                f"{court.name} está ocupada de {c.start:%H:%M} a {c.end:%H:%M} (reserva de {origen})."
            )
        if self.warnings and court.playtomic_resource_id:
            raise BookingError("No se ha podido comprobar Playtomic ahora mismo; por seguridad no se "
                               "reserva. Inténtalo de nuevo en unos minutos o llama al club.")
        return self.store.add(Booking(
            source=SOURCE_LOCAL, court_id=court.id, start=start, end=end,
            customer_name=customer_name, phone=phone, notes=notes,
        ))

    def cancel(self, booking_id: str) -> Booking:
        if str(booking_id).upper().startswith("P"):
            raise BookingError("Las reservas hechas en Playtomic se cancelan desde la app de Playtomic "
                               "o desde Playtomic Manager.")
        b = self.store.cancel(booking_id)
        if not b:
            raise BookingError(f"No existe ninguna reserva activa con id {booking_id}.")
        return b

    # --- Playtomic -------------------------------------------------------------

    def _playtomic_detailed(self, start: datetime, end: datetime) -> list[Booking]:
        out: list[Booking] = []
        if self.partner:
            try:
                out += self.partner.bookings(start, end)
            except PlaytomicError as e:
                self.warnings.append(str(e))
        if self.csv_path:
            try:
                out += [b for b in load_bookings_csv(self.csv_path, self.club) if b.overlaps(start, end)]
            except (OSError, PlaytomicError) as e:
                self.warnings.append(f"No se pudo leer el CSV de Playtomic: {e}")
        # Si la misma reserva llega por la API y por el CSV, nos quedamos con una
        seen, unique = set(), []
        for b in out:
            key = (b.court_id, b.start, b.end)
            if key not in seen:
                seen.add(key)
                unique.append(b)
        return unique

    def _playtomic_busy_blocks(self, day: date, window: tuple[datetime, datetime],
                               known: list[Booking]) -> list[Booking]:
        """Bloques ocupados en Playtomic que no corresponden a ninguna reserva conocida."""
        if not self.public:
            return []
        try:
            free = self.public.free_intervals(day)
        except PlaytomicError as e:
            self.warnings.append(str(e))
            return []
        open_at, close_at = window
        open_at = max(open_at, self.now())  # Playtomic no publica huecos pasados
        if open_at >= close_at:
            return []
        out = []
        for court in self.club.courts:
            rid = court.playtomic_resource_id
            if not rid:
                continue
            busy = subtract_intervals([(open_at, close_at)], free.get(rid, []))
            busy = subtract_intervals(busy, [(b.start, b.end) for b in known if b.court_id == court.id])
            for s, e in busy:
                if e - s >= timedelta(minutes=self.club.slot_minutes):
                    out.append(Booking(
                        source=SOURCE_PLAYTOMIC, id="", court_id=court.id, start=s, end=e,
                        anonymous=True, notes="Ocupado en Playtomic (sin detalle del jugador)",
                    ))
        return out
