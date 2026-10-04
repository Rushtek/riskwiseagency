"""Construcción del servicio a partir de variables de entorno."""

from __future__ import annotations

import os

from .bookings import BookingService
from .models import load_club_config
from .playtomic import PlaytomicPartnerClient, PlaytomicPublicClient
from .store import BookingStore


def build_service_from_env() -> BookingService:
    club = load_club_config(os.environ.get("CLUB_CONFIG", "club.json"))
    store = BookingStore(os.environ.get("PADEL_DB", "reservas.db"))

    public = partner = None
    if club.playtomic_tenant_id:
        public = PlaytomicPublicClient(club.playtomic_tenant_id, club.tz,
                                       slot_tz=os.environ.get("PLAYTOMIC_SLOT_TZ", "UTC"))
        base, token = os.environ.get("PLAYTOMIC_PARTNER_API_BASE"), os.environ.get("PLAYTOMIC_PARTNER_TOKEN")
        if base and token:
            partner = PlaytomicPartnerClient(
                base, token, club.playtomic_tenant_id, club,
                bookings_path=os.environ.get("PLAYTOMIC_PARTNER_BOOKINGS_PATH", "/v1/bookings"),
            )

    return BookingService(club, store, public=public, partner=partner,
                          csv_path=os.environ.get("PLAYTOMIC_BOOKINGS_CSV") or None)
