"""Nightly catalog sync (rebuilt from Cardmarket's and Scryfall's public files)."""

import logging

from .models import SyncLog

logger = logging.getLogger(__name__)


class CatalogSyncError(Exception):
    pass


def run_sync():
    """Placeholder until the public-files sync lands: the CardTrader sync is gone."""
    SyncLog.objects.create(message="Catalog sync failed: not implemented yet", level=SyncLog.Level.WARNING)
    return None
