# Agente de reservas para club de pádel (con Playtomic)

Asistente con IA (Claude) que atiende a jugadores y al personal del club: consulta
huecos libres, muestra las reservas del día, busca reservas de una persona y crea o
cancela reservas. **Combina las reservas propias del club con las de Playtomic**, así
que la disponibilidad que da es la real y no reserva una pista que ya está cogida en
Playtomic. Ejemplo de conversación:

```
Tú: ¿Hay pista mañana por la tarde para 4, hora y media?
Asistente: Mañana sábado tienes libre la Pista 1 a las 17:00 y a las 18:30, y la
Pista 3 a las 19:00. La Pista 2 está ocupada toda la tarde (reservas de Playtomic).
¿Cuál te reservo? Necesito tu nombre y un teléfono.
```

## Cómo encuentra las reservas de Playtomic

Hay tres niveles; puedes activar uno o varios:

| Fuente | Qué da | Qué necesitas |
|---|---|---|
| **Disponibilidad pública** | Qué horas están ocupadas en cada pista (sin saber quién reservó) | Solo el `tenant_id` del club |
| **API de Playtomic para clubes** | Cada reserva con nombre, teléfono, jugadores | Credenciales que da Playtomic al club |
| **Exportación CSV** de Playtomic Manager | Igual que la API, pero desde un fichero exportado | Exportar el CSV (manual o programado) |

- Con **solo la disponibilidad pública** el agente ya evita dobles reservas y ve
  la ocupación real, pero las reservas de Playtomic salen como
  *"Ocupado en Playtomic (sin detalle)"* y no se pueden buscar por nombre.
- Para **buscar "¿a qué hora juega Marta?"** en reservas hechas en Playtomic hace
  falta la API para clubes o el CSV.

**Cómo saber el `tenant_id`**: abre la página del club en playtomic.io; la URL
termina en el identificador (`https://playtomic.io/nombre-del-club/<tenant_id>`).
Los `playtomic_resource_id` de cada pista salen de
`https://api.playtomic.io/v1/tenants/<tenant_id>` (campo `resources`).

### Importante: evitar dobles reservas en el otro sentido

El agente comprueba Playtomic antes de crear una reserva, pero **Playtomic no se
entera de las reservas que crea el agente**. Hasta tener la API de clubes con
escritura, el personal debe bloquear esas reservas en Playtomic Manager (o dejar
alguna pista solo para reservas del club, como la *Pista 3* del ejemplo, con
`playtomic_resource_id: null`). Si una reserva del club ya está bloqueada en
Playtomic, el agente no la duplica.

Si Playtomic no responde en el momento de reservar una pista que está en
Playtomic, el agente **no** reserva y pide intentarlo más tarde.

## Instalación

Requiere Python 3.10+ y una clave de la API de Claude (console.anthropic.com).

```bash
cd padel-agent
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp club.example.json club.json   # edita pistas, horario, precios y tenant_id
cp .env.example .env             # pon ANTHROPIC_API_KEY y, si las tienes, las credenciales de Playtomic
python -m padel_agent.cli
```

## Configuración

- `club.json`: nombre, zona horaria, horario por día, pistas (y su id en
  Playtomic), duración habitual y un texto libre `info` con precios, normas de
  cancelación, servicios, etc. El agente usa ese texto para responder dudas.
- `.env`: ver `.env.example`.
  - `PLAYTOMIC_SLOT_TZ`: zona horaria en la que Playtomic devuelve las horas de
    los huecos (por defecto `UTC`). Si al probar ves las reservas desplazadas
    1–2 horas, cámbiala a `Europe/Madrid`.
  - `PLAYTOMIC_PARTNER_*`: URL, token y ruta de la API de Playtomic para clubes.
    El formato exacto depende del acceso que dé Playtomic; el lector es tolerante
    a los nombres de campo habituales (`booking_start_date`, `resource_id`,
    `owner`, `players`...). Si difieren, se ajusta en `booking_from_partner_json`.
  - `PLAYTOMIC_BOOKINGS_CSV`: ruta al CSV exportado (columnas en español o inglés:
    Fecha, Hora inicio, Hora fin o Duración, Pista, Cliente, Teléfono, Estado...).

## Estructura

```
padel_agent/
  agent.py      Agente Claude: instrucciones y herramientas (disponibilidad, reservas, búsqueda...)
  bookings.py   Vista unificada club + Playtomic, huecos libres, alta/cancelación con control de conflictos
  playtomic.py  Disponibilidad pública, API de clubes y lector de CSV
  store.py      Reservas propias del club (SQLite)
  models.py     Pistas, horario y reservas
  cli.py        Chat por terminal
tests/          Pruebas con Playtomic simulado (pytest)
```

Pruebas: `pip install -r requirements-dev.txt && pytest`

## Detalles técnicos

- Modelo `claude-opus-5-5` con razonamiento adaptativo y esfuerzo `medium`.
- Caché de prompts activada (instrucciones y herramientas se reutilizan entre mensajes).
- Respaldo automático en el servidor (`fallbacks: "default"`): si el modelo
  rechaza una petición por sus filtros de seguridad, la API la reintenta con
  otro modelo.
- `PadelAgent` guarda la conversación: crea una instancia por cliente/chat.

## Siguientes pasos posibles

- Conectarlo a **WhatsApp** (WhatsApp Business API / Twilio) o a un chat en la web:
  basta con llamar a `PadelAgent.ask()` desde el webhook, una instancia por número.
- Recordatorios automáticos el día antes y avisos de huecos libres de última hora.
- Escritura en Playtomic (crear bloqueos) si la API de clubes lo permite.
