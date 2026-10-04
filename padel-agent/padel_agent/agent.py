"""Agente conversacional (Claude) que atiende reservas del club de pádel."""

from __future__ import annotations

import json
from datetime import date, datetime

import anthropic
from anthropic import beta_tool

from .bookings import BookingError, BookingService
from .models import WEEKDAYS

MODEL = "claude-opus-5-5"

DIAS = dict(zip(WEEKDAYS, ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]))

SYSTEM_TEMPLATE = """Eres el asistente de reservas de {name}, un club de pádel. Atiendes a jugadores \
y al personal del club, normalmente en español, con un tono cercano y breve (como un buen \
recepcionista por WhatsApp).

Qué puedes hacer con tus herramientas:
- Consultar huecos libres y las reservas de un día.
- Buscar reservas de una persona por nombre, teléfono o email.
- Crear y cancelar reservas del club (teléfono, WhatsApp, mostrador).

Sobre Playtomic: parte de las reservas del club se hacen en Playtomic. Tus herramientas ya \
combinan las reservas del club con las de Playtomic, así que la disponibilidad que ves es la \
real. Algunas reservas de Playtomic aparecen como "Ocupado en Playtomic (sin detalle)": sabes \
que la pista está ocupada pero no quién la reservó. Las reservas con id que empieza por "P" \
son de Playtomic y no puedes cancelarlas ni modificarlas: indica al jugador que lo haga desde \
la app de Playtomic. Las que empiezan por "L" son del club.

Reglas:
- Antes de crear una reserva, confirma con la persona pista, día, hora, duración, nombre y \
teléfono. No inventes datos que no te hayan dado.
- Antes de cancelar, confirma qué reserva es.
- Si una pista está ocupada, ofrece alternativas (otra pista u otra hora cercana).
- Las fechas que te den ("mañana", "el viernes") interprétalas respecto a la fecha actual que \
aparece al principio de cada mensaje, y usa el formato AAAA-MM-DD y HH:MM en las herramientas.
- No compartas teléfonos ni emails de otros clientes con jugadores; solo con el personal del club.
- Si no sabes algo del club que no esté en la información de abajo, dilo y sugiere llamar al club.

Pistas: {courts}
Horario: {hours}
Duración habitual de un partido: {duration} minutos.

Información del club:
{info}"""


def _json(data) -> str:
    return json.dumps(data, ensure_ascii=False)


def build_tools(service: BookingService) -> list:
    club = service.club

    def parse_day(fecha: str) -> date:
        try:
            return date.fromisoformat(fecha)
        except ValueError as e:
            raise BookingError(f"Fecha no válida '{fecha}', usa AAAA-MM-DD") from e

    def with_warnings(payload: dict) -> str:
        if service.warnings:
            payload["avisos"] = list(service.warnings)
        return _json(payload)

    @beta_tool
    def consultar_disponibilidad(fecha: str, duracion_minutos: int = 90, pista: str = "",
                                 desde: str = "", hasta: str = "") -> str:
        """Lista las horas de inicio libres para jugar un día, teniendo en cuenta las reservas del club y las de Playtomic.

        Args:
            fecha: Día en formato AAAA-MM-DD.
            duracion_minutos: Duración del partido en minutos (60, 90 o 120).
            pista: Opcional. Id o nombre de la pista; vacío para todas.
            desde: Opcional. Hora mínima de inicio HH:MM.
            hasta: Opcional. Hora máxima de inicio HH:MM.
        """
        try:
            service.warnings.clear()
            day = parse_day(fecha)
            court_id = club.court(pista).id if pista else None
            to_dt = lambda h: datetime.combine(day, datetime.strptime(h, "%H:%M").time(), tzinfo=club.tz)  # noqa: E731
            slots = service.free_slots(day, duracion_minutos, court_id,
                                       earliest=to_dt(desde) if desde else None,
                                       latest=to_dt(hasta) if hasta else None)
            return with_warnings({"fecha": fecha, "dia_semana": DIAS[WEEKDAYS[day.weekday()]],
                                  "huecos_libres": slots or "No hay huecos libres con esos criterios"})
        except (BookingError, KeyError, ValueError) as e:
            return _json({"error": str(e)})

    @beta_tool
    def ver_reservas_del_dia(fecha: str, pista: str = "") -> str:
        """Muestra todas las reservas de un día (del club y de Playtomic), ordenadas por hora.

        Args:
            fecha: Día en formato AAAA-MM-DD.
            pista: Opcional. Id o nombre de la pista; vacío para todas.
        """
        try:
            service.warnings.clear()
            day = parse_day(fecha)
            court_id = club.court(pista).id if pista else None
            bookings = service.bookings_for_day(day, court_id)
            return with_warnings({"fecha": fecha, "reservas": [b.to_dict(club) for b in bookings]})
        except (BookingError, KeyError) as e:
            return _json({"error": str(e)})

    @beta_tool
    def buscar_reservas(texto: str, desde_fecha: str = "", dias: int = 14) -> str:
        """Busca reservas por nombre, teléfono, email o id, tanto del club como de Playtomic (cuando Playtomic da el detalle).

        Args:
            texto: Nombre, teléfono, email o id de reserva a buscar.
            desde_fecha: Opcional. Primer día de búsqueda AAAA-MM-DD; por defecto hoy.
            dias: Número de días a revisar desde desde_fecha.
        """
        try:
            service.warnings.clear()
            start = parse_day(desde_fecha) if desde_fecha else service.now().date()
            found = service.search(texto, start, dias)
            payload = {"resultados": [b.to_dict(club) for b in found]}
            if not found:
                payload["nota"] = ("Sin resultados. Las reservas de Playtomic sin detalle no se pueden "
                                   "buscar por nombre.")
            return with_warnings(payload)
        except BookingError as e:
            return _json({"error": str(e)})

    @beta_tool
    def crear_reserva(pista: str, fecha: str, hora: str, duracion_minutos: int, nombre: str,
                      telefono: str, notas: str = "") -> str:
        """Crea una reserva del club. Comprueba antes que la pista esté libre también en Playtomic. Úsala solo después de que la persona haya confirmado los datos.

        Args:
            pista: Id o nombre de la pista.
            fecha: Día en formato AAAA-MM-DD.
            hora: Hora de inicio HH:MM.
            duracion_minutos: Duración en minutos.
            nombre: Nombre de quien reserva.
            telefono: Teléfono de contacto.
            notas: Opcional. Comentarios (alquiler de palas, nivel, etc.).
        """
        try:
            start = datetime.combine(parse_day(fecha), datetime.strptime(hora, "%H:%M").time(),
                                     tzinfo=club.tz)
            b = service.create(pista, start, duracion_minutos, nombre, telefono, notas)
            return _json({"ok": True, "reserva": b.to_dict(club)})
        except (BookingError, KeyError, ValueError) as e:
            return _json({"ok": False, "error": str(e)})

    @beta_tool
    def cancelar_reserva(reserva_id: str) -> str:
        """Cancela una reserva del club (id que empieza por L). Las de Playtomic (P...) no se pueden cancelar desde aquí.

        Args:
            reserva_id: Id de la reserva, por ejemplo L12.
        """
        try:
            b = service.cancel(reserva_id)
            return _json({"ok": True, "cancelada": b.to_dict(club)})
        except (BookingError, ValueError) as e:
            return _json({"ok": False, "error": str(e)})

    return [consultar_disponibilidad, ver_reservas_del_dia, buscar_reservas, crear_reserva,
            cancelar_reserva]


class PadelAgent:
    """Conversación con memoria: una instancia por cliente/chat."""

    def __init__(self, service: BookingService, client: anthropic.Anthropic | None = None,
                 model: str = MODEL):
        self.service = service
        self.client = client or anthropic.Anthropic()
        self.model = model
        self.tools = build_tools(service)
        self.system = self._system_prompt()
        self.messages: list = []

    def _system_prompt(self) -> str:
        club = self.service.club
        hours = ", ".join(f"{DIAS[d]} {o:%H:%M}-{c:%H:%M}" for d, (o, c) in club.opening_hours.items())
        courts = ", ".join(f"{c.name} (id {c.id})" for c in club.courts)
        return SYSTEM_TEMPLATE.format(name=club.name, courts=courts, hours=hours or "consultar",
                                      duration=club.default_duration_minutes,
                                      info=club.info or "(sin información adicional)")

    def ask(self, text: str) -> str:
        now = self.service.now()
        stamp = f"[Ahora: {DIAS[WEEKDAYS[now.weekday()]]} {now:%Y-%m-%d %H:%M}]"
        self.messages.append({"role": "user", "content": f"{stamp}\n{text}"})

        runner = self.client.beta.messages.tool_runner(
            model=self.model,
            max_tokens=16000,
            system=self.system,
            tools=self.tools,
            messages=list(self.messages),
            thinking={"type": "adaptive"},
            output_config={"effort": "medium"},
            cache_control={"type": "ephemeral"},
            # Si el modelo rechaza una petición por sus filtros de seguridad, la API la
            # reintenta con el modelo de respaldo recomendado.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        last = None
        for message in runner:
            last = message
            # El runner guarda su propio historial; lo reflejamos para la próxima pregunta
            self.messages.append({"role": "assistant", "content": message.content})
            tool_response = runner.generate_tool_call_response()
            if tool_response is not None:
                self.messages.append(tool_response)

        if last is None:
            return "Perdona, no he podido responder. ¿Puedes repetirlo?"
        if last.stop_reason == "refusal":
            return "Lo siento, no puedo ayudarte con eso. Para cualquier duda, llama al club."
        return "".join(b.text for b in last.content if b.type == "text").strip()
