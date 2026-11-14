"""Nightly catalog sync: the card database from Cardmarket's and Scryfall's public files, no token needed.

Cardmarket's product lists give every MTG product (singles, sealed, accessories) and its category; Scryfall gives the
set, collector number, rarity and images of the singles, tied by their Cardmarket id; Cardmarket's price guide gives
the prices. Then the Ledger's cards are aligned with the catalog (catalog.reconcile_cards). Outcome in the SyncLog.
"""

import logging
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from itertools import batched

from . import cardmarket, catalog, scryfall
from .cardmarket import PRICE_FIELDS, CardmarketError
from .models import CARD_CATEGORY_ID, Blueprint, CardmarketPrice, Category, Expansion, SyncLog
from .scryfall import ScryfallError

BATCH = 2000

logger = logging.getLogger(__name__)


class CatalogSyncError(Exception):
    pass


@dataclass
class SyncResult:
    expansions: int
    blueprints: int
    priced: int
    skipped: int
    reconciled: catalog.ReconcileResult
    guide_date: datetime


def _main_sets(products, cards):
    """{Cardmarket expansion id: the Scryfall set most of its singles belong to} (ties: the shortest code)."""
    counts = defaultdict(Counter)
    for p in products:
        card = cards.get(p.id_product)
        if p.id_category == CARD_CATEGORY_ID and card is not None:
            counts[p.id_expansion][card.set_code] += 1
    return {exp: min(c.items(), key=lambda kv: (-kv[1], len(kv[0]), kv[0]))[0] for exp, c in counts.items()}


def _rows(products, cards):
    """([(cm id, name, set code, number, rarity, image, large image, Cardmarket category)], skipped products).

    A product without a Scryfall card (sealed, accessories, unmatched singles) goes to the main set of its Cardmarket
    expansion, without a number; when that expansion has no matched single, it is skipped.
    """
    main = _main_sets(products, cards)
    rows, skipped = [], 0
    for p in products:
        card = cards.get(p.id_product)
        if card is not None:
            rows.append((p.id_product, p.name, card.set_code, card.collector_number, card.rarity, card.image_url,
                         card.image_large_url, p.id_category))
            continue
        code = main.get(p.id_expansion)
        if code is None:
            skipped += 1
            continue
        rows.append((p.id_product, p.name, code, "", "", "", "", p.id_category))
    return rows, skipped


def _write(rows, categories, sets, prices, guide_date):
    """Write the catalog and the prices in one transaction; expansions and categories left empty go."""
    from django.db import transaction

    codes = {r[2] for r in rows}
    with transaction.atomic():
        Expansion.objects.bulk_create(
            [Expansion(code=code, name=sets[code].name if code in sets else code.upper(),
                       released_at=sets[code].released_at if code in sets else None) for code in sorted(codes)],
            update_conflicts=True, unique_fields=["code"], update_fields=["name", "released_at"],
        )
        Category.objects.bulk_create(
            [Category(cm_id=i, name=name) for i, name in categories.items()],
            update_conflicts=True, unique_fields=["cm_id"], update_fields=["name"],
        )
        expansion_pk = dict(Expansion.objects.filter(code__in=codes).values_list("code", "pk"))
        category_pk = dict(Category.objects.values_list("cm_id", "pk"))
        # Models are built and written one batch at a time: all of them at once do not fit in the worker.
        for chunk in batched(rows, BATCH):
            Blueprint.objects.bulk_create(
                [
                    Blueprint(cm_product_id=cm, name=name[:200], expansion_id=expansion_pk[code],
                              collector_number=number[:40], rarity=rarity[:40], image_url=small,
                              image_large_url=large, category_id=category_pk[category])
                    for cm, name, code, number, rarity, small, large, category in chunk
                ],
                update_conflicts=True, unique_fields=["cm_product"],
                update_fields=["expansion", "name", "collector_number", "rarity", "image_url", "image_large_url",
                               "category"],
            )
        for chunk in batched(prices.items(), BATCH):
            CardmarketPrice.objects.bulk_create(
                [cardmarket.price_row(id_product, values, guide_date) for id_product, values in chunk],
                update_conflicts=True, unique_fields=["id_product"],
                update_fields=[*PRICE_FIELDS.values(), "prices_date"],
            )
        Category.objects.filter(blueprints__isnull=True).delete()
        Expansion.objects.filter(blueprints__isnull=True).delete()


def sync(cm_client, sf_client):
    """Rebuild the catalog from the public files, then align the Ledger's cards with it.

    Everything is downloaded before anything is written.
    """
    prices, guide_date = cardmarket.fetch_guide(cm_client)
    products = cardmarket.fetch_products(cm_client)
    sets = scryfall.fetch_sets(sf_client)
    cards = scryfall.fetch_cards(sf_client)
    rows, skipped = _rows(products, cards)
    del cards
    categories = {p.id_category: p.category_name for p in products}
    del products
    _write(rows, categories, sets, prices, guide_date)
    return SyncResult(
        expansions=len({r[2] for r in rows}), blueprints=len(rows), priced=sum(1 for r in rows if r[0] in prices),
        skipped=skipped, reconciled=catalog.reconcile_cards(), guide_date=guide_date,
    )


def _summary(result):
    r = result.reconciled
    message = (
        f"Catalog sync: {result.expansions} expansions, {result.blueprints} printings, {result.priced} priced "
        f"(guide {result.guide_date:%Y-%m-%d}), {r.linked} cards linked, {r.unlinked} not linked"
    )
    if result.skipped:
        message += f", {result.skipped} products without set"
    if r.clashes:
        message += f", {r.clashes} duplicates to merge by hand"
    return message


def run_sync(cm_client=None, sf_client=None):
    """sync() with its outcome written to the SyncLog. Returns the result, or None when it could not run."""
    own_cm, own_sf = cm_client is None, sf_client is None
    cm_client = cm_client or cardmarket.make_client()
    sf_client = sf_client or scryfall.make_client()
    try:
        result = sync(cm_client, sf_client)
    except (CatalogSyncError, CardmarketError, ScryfallError) as exc:
        SyncLog.objects.create(message=f"Catalog sync failed: {exc}"[:300], level=SyncLog.Level.WARNING)
        return None
    except Exception as exc:
        # Unexpected (database, bug): still visible in the Activity log, and the task is marked failed.
        logger.exception("Catalog sync failed")
        SyncLog.objects.create(
            message=f"Catalog sync failed: {type(exc).__name__}: {exc}"[:300], level=SyncLog.Level.WARNING
        )
        raise
    finally:
        if own_cm:
            cm_client.close()
        if own_sf:
            sf_client.close()
    level = SyncLog.Level.WARNING if result.reconciled.clashes else SyncLog.Level.OK
    SyncLog.objects.create(message=_summary(result)[:300], level=level)
    return result
