from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pytest

from padel_agent.agent import build_tools
from padel_agent.bookings import BookingError, BookingService
from padel_agent.models import ClubConfig, Court
from padel_agent.playtomic import (PlaytomicError, PlaytomicPartnerClient, PlaytomicPublicClient,
                                   load_bookings_csv)
from padel_agent.store import BookingStore

TZ = ZoneInfo("Europe/Madrid")
DAY = date(2026, 10, 10)  # sábado; Madrid = UTC+2


def club():
    return ClubConfig(
        name="Club Test", timezone="Europe/Madrid",
        opening_hours={"sat": (time(9), time(13))},
        courts=[Court("1", "Pista 1", "res-1"), Court("2", "Pista 2", "res-2"), Court("3", "Pista 3")],
        playtomic_tenant_id="tenant-x",
    )


def at(h, m=0):
    return datetime.combine(DAY, time(h, m), tzinfo=TZ)


def fake_availability(url, params, headers):
    """Pista 1: libre 09:00-10:30 y 12:00-13:00 (UTC 07:00 y 10:00) -> ocupada 10:30-12:00.
    Pista 2: no aparece (sin huecos) -> ocupada todo el día."""
    assert params["start_min"] == "2026-10-09T22:00:00"
    return [{"resource_id": "res-1", "start_date": "2026-10-10", "slots": [
        {"start_time": "07:00:00", "duration": 90, "price": "36 EUR"},
        {"start_time": "10:00:00", "duration": 60, "price": "30 EUR"},
    ]}]


def service(tmp_path, **kw):
    c = club()
    kw.setdefault("public", PlaytomicPublicClient("tenant-x", TZ, fetch=fake_availability))
    return BookingService(c, BookingStore(str(tmp_path / "t.db")), now=lambda: at(8), **kw)


def test_playtomic_busy_blocks(tmp_path):
    s = service(tmp_path)
    bookings = s.bookings_for_day(DAY)
    p1 = [(b.start.strftime("%H:%M"), b.end.strftime("%H:%M")) for b in bookings if b.court_id == "1"]
    assert p1 == [("10:30", "12:00")]
    p2 = [b for b in bookings if b.court_id == "2"]
    assert len(p2) == 1 and p2[0].anonymous and p2[0].start == at(9) and p2[0].end == at(13)
    assert not [b for b in bookings if b.court_id == "3"]


def test_free_slots_combines_sources(tmp_path):
    s = service(tmp_path)
    s.create("3", at(10), 90, "Ana", "600111222")
    slots = s.free_slots(DAY, 90)
    p1 = [x["start"] for x in slots if x["court_id"] == "1"]
    assert p1 == ["09:00"]  # 10:30-12:00 ocupado en Playtomic; 12:00 + 90 pasa del cierre
    assert not [x for x in slots if x["court_id"] == "2"]
    p3 = [x["start"] for x in slots if x["court_id"] == "3"]
    assert "10:00" not in p3 and "11:30" in p3 and "09:00" not in p3  # 09:00-10:30 solapa


def test_create_rejects_playtomic_conflict(tmp_path):
    s = service(tmp_path)
    with pytest.raises(BookingError, match="Playtomic"):
        s.create("Pista 1", at(11), 60, "Luis", "600")
    assert s.create("pista 1", at(9), 90, "Luis", "600").id == "L1"
    with pytest.raises(BookingError, match="el club"):
        s.create("1", at(10), 60, "Eva", "600")


def test_create_refuses_when_playtomic_down(tmp_path):
    def broken(*a):
        raise PlaytomicError("timeout")
    s = service(tmp_path, public=PlaytomicPublicClient("t", TZ, fetch=broken))
    with pytest.raises(BookingError, match="comprobar Playtomic"):
        s.create("1", at(9), 60, "Luis", "600")
    assert s.create("3", at(9), 60, "Luis", "600")  # pista sin Playtomic: sí


def test_local_booking_mirrored_in_playtomic_not_duplicated(tmp_path):
    blocked = {"on": False}

    def avail(url, params, headers):
        # Tras crear la reserva local, el club la bloquea en Playtomic Manager (09:00-10:30)
        p1 = ([{"start_time": "08:30:00", "duration": 150}] if blocked["on"]
              else [{"start_time": "07:00:00", "duration": 240}])
        return [{"resource_id": "res-1", "start_date": "2026-10-10", "slots": p1},
                {"resource_id": "res-2", "start_date": "2026-10-10",
                 "slots": [{"start_time": "07:00:00", "duration": 240}]}]
    s = service(tmp_path, public=PlaytomicPublicClient("t", TZ, fetch=avail))
    s.create("1", at(9), 90, "Ana", "600")
    blocked["on"] = True
    p1 = [b for b in s.bookings_for_day(DAY) if b.court_id == "1"]
    assert [b.source for b in p1] == ["local"]


def test_partner_api_and_search(tmp_path):
    def api(url, params, headers):
        assert headers["Authorization"] == "Bearer tok"
        return {"data": [
            {"booking_id": "abc", "resource_id": "res-1", "booking_start_date": "2026-10-10T08:30:00Z",
             "duration": 90, "owner": {"name": "Marta Gil", "phone": "+34 611 22 33 44"},
             "players": [{"name": "Marta Gil"}, {"name": "Pedro Ruiz"}]},
            {"booking_id": "zzz", "resource_id": "res-1", "booking_start_date": "2026-10-10T09:00:00Z",
             "duration": 60, "status": "CANCELED"},
        ]}
    c = club()
    s = service(tmp_path, partner=PlaytomicPartnerClient("https://x", "tok", "t", c, fetch=api))
    s.club = c
    day = s.bookings_for_day(DAY, "1")
    named = [b for b in day if not b.anonymous]
    assert len(named) == 1 and named[0].start == at(10, 30) and named[0].id == "Pabc"
    assert [b.id for b in s.search("pedro", DAY, 1)] == ["Pabc"]
    assert [b.id for b in s.search("611223344", DAY, 1)] == ["Pabc"]
    with pytest.raises(BookingError, match="app de Playtomic"):
        s.cancel("Pabc")


def test_csv_export(tmp_path):
    p = tmp_path / "export.csv"
    p.write_text("Fecha;Hora inicio;Duración;Pista;Cliente;Teléfono;Estado\n"
                 "10/10/2026;09:00;90 min;Pista 2;Jorge López;622000111;Confirmada\n"
                 "10/10/2026;11:00;60;Pista 2;Otro;600;Cancelada\n", encoding="utf-8")
    bs = load_bookings_csv(p, club())
    assert len(bs) == 1 and bs[0].court_id == "2" and bs[0].end == at(10, 30)
    s = service(tmp_path, csv_path=str(p))
    assert [b.customer_name for b in s.search("jorge", DAY, 1)] == ["Jorge López"]


def test_tools_schema_and_calls(tmp_path):
    s = service(tmp_path)
    tools = {t.name: t for t in build_tools(s)}
    schema = tools["crear_reserva"].to_dict()["input_schema"]
    assert set(schema["required"]) == {"pista", "fecha", "hora", "duracion_minutos", "nombre", "telefono"}
    out = tools["crear_reserva"].call({"pista": "3", "fecha": "2026-10-10", "hora": "09:00",
                                      "duracion_minutos": 90, "nombre": "Ana", "telefono": "6"})
    assert '"ok": true' in out
    out = tools["cancelar_reserva"].call({"reserva_id": "L1"})
    assert '"ok": true' in out
    out = tools["consultar_disponibilidad"].call({"fecha": "mañana"})
    assert "error" in out
