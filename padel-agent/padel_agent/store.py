"""Almacén SQLite de las reservas propias del club (las que no vienen de Playtomic)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from .models import SOURCE_LOCAL, Booking

SCHEMA = """
CREATE TABLE IF NOT EXISTS bookings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    court_id TEXT NOT NULL,
    start TEXT NOT NULL,
    end TEXT NOT NULL,
    customer_name TEXT NOT NULL,
    phone TEXT,
    email TEXT,
    notes TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'confirmed',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bookings_start ON bookings(start);
"""


def _utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


class BookingStore:
    def __init__(self, path: str = "reservas.db"):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def add(self, b: Booking) -> Booking:
        cur = self.conn.execute(
            "INSERT INTO bookings (court_id, start, end, customer_name, phone, email, notes, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (b.court_id, _utc_iso(b.start), _utc_iso(b.end), b.customer_name, b.phone, b.email,
             b.notes, _utc_iso(datetime.now(timezone.utc))),
        )
        self.conn.commit()
        b.id = f"L{cur.lastrowid}"
        return b

    def cancel(self, booking_id: str) -> Booking | None:
        b = self.get(booking_id)
        if b:
            self.conn.execute("UPDATE bookings SET status='cancelled' WHERE id=?", (_row_id(booking_id),))
            self.conn.commit()
        return b

    def get(self, booking_id: str) -> Booking | None:
        row = self.conn.execute(
            "SELECT * FROM bookings WHERE id=? AND status='confirmed'", (_row_id(booking_id),)
        ).fetchone()
        return _to_booking(row) if row else None

    def between(self, start: datetime, end: datetime) -> list[Booking]:
        rows = self.conn.execute(
            "SELECT * FROM bookings WHERE status='confirmed' AND start < ? AND end > ? ORDER BY start",
            (_utc_iso(end), _utc_iso(start)),
        ).fetchall()
        return [_to_booking(r) for r in rows]


def _row_id(booking_id: str) -> int:
    return int(str(booking_id).lstrip("Ll"))


def _to_booking(row: sqlite3.Row) -> Booking:
    return Booking(
        source=SOURCE_LOCAL,
        id=f"L{row['id']}",
        court_id=row["court_id"],
        start=datetime.fromisoformat(row["start"]),
        end=datetime.fromisoformat(row["end"]),
        customer_name=row["customer_name"],
        phone=row["phone"],
        email=row["email"],
        notes=row["notes"] or "",
    )
