"""Cardmarket prices: the public daily price guide and the prices it gives to Ledger cards.

Each CardTrader blueprint carries its Cardmarket product ids, which tie it to a row of the price guide. The guide is
downloaded by the nightly catalog sync (ledger/catalog_sync.py).
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

import httpx

from .catalog import normalize_number
from .models import Blueprint, Card, CardmarketPrice

PRICE_GUIDE_URL = "https://downloads.s3.cardmarket.com/productCatalog/priceGuide/price_guide_1.json"
PRODUCT_LIST_URLS = (
    "https://downloads.s3.cardmarket.com/productCatalog/productList/products_singles_1.json",
    "https://downloads.s3.cardmarket.com/productCatalog/productList/products_nonsingles_1.json",
)
USER_AGENT = "mtg-ledger/0.1"

# Price guide key -> CardmarketPrice field
PRICE_FIELDS = {
    "low": "low", "trend": "trend", "avg": "avg", "avg1": "avg1", "avg7": "avg7", "avg30": "avg30",
    "low-foil": "low_foil", "trend-foil": "trend_foil", "avg-foil": "avg_foil",
    "avg1-foil": "avg1_foil", "avg7-foil": "avg7_foil", "avg30-foil": "avg30_foil",
}


class CardmarketError(Exception):
    pass


@dataclass(frozen=True)
class CmProduct:
    """A row of Cardmarket's product lists: a single card, a sealed product or an accessory."""

    id_product: int
    name: str
    id_category: int
    category_name: str
    id_expansion: int


def make_client(transport=None):
    return httpx.Client(
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        timeout=120, follow_redirects=True, transport=transport,
    )


# --- lookups ---------------------------------------------------------------

def blueprint_for(card):
    """The CardTrader printing of a Ledger card: its matched blueprint, else set code + collector number."""
    if card.ct_blueprint_id:
        bp = Blueprint.objects.filter(ct_id=card.ct_blueprint_id).first()
        if bp is not None:
            return bp
    number = normalize_number(card.collector_number)
    if not number:
        return None
    return Blueprint.objects.filter(expansion__code=card.set_code.lower(), collector_number=number).first()


def suggested_price(card, foil):
    """Cardmarket trend to suggest when listing a copy of card (foil trend for foil copies), or None."""
    bp = blueprint_for(card)
    if bp is None or bp.cm_price_id is None:
        return None
    price = CardmarketPrice.objects.filter(pk=bp.cm_price_id).first()
    if price is None:
        return None
    trend = price.trend_foil if foil and not card.is_product else price.trend
    return trend or None  # the guide holds 0.00 for a finish that doesn't exist


def owned_quantities(owner):
    """{blueprint ct_id: copies in owner's collection}, matching cards like blueprint_for()."""
    from django.db.models import Q, Sum

    per_card = list(
        Card.objects.annotate(qty=Sum("items__quantity", filter=Q(items__owner=owner))).filter(qty__gt=0)
        .values_list("ct_blueprint_id", "set_code", "collector_number", "qty")
    )
    known = set(Blueprint.objects.filter(ct_id__in=[c[0] for c in per_card if c[0]]).values_list("ct_id", flat=True))
    wanted = {(s.lower(), normalize_number(n)) for bp, s, n, _ in per_card if bp not in known and normalize_number(n)}
    by_printing = {}
    if wanted:
        rows = Blueprint.objects.filter(
            expansion__code__in={s for s, _ in wanted}, collector_number__in={n for _, n in wanted}
        ).order_by("-ct_id").values_list("expansion__code", "collector_number", "ct_id")
        by_printing = {(s, n): ct_id for s, n, ct_id in rows}
    owned = {}
    for bp, s, n, qty in per_card:
        ct_id = bp if bp in known else by_printing.get((s.lower(), normalize_number(n)))
        if ct_id is not None:
            owned[ct_id] = owned.get(ct_id, 0) + qty
    return owned


# --- download ----------------------------------------------------------------

def _money(value):
    return None if value is None else Decimal(str(value))


def fetch_guide(client):
    """({idProduct: raw values in PRICE_FIELDS order}, createdAt) from today's price guide.

    The values stay as parsed from the JSON: the guide has over 100k rows, and models with a Decimal per field
    would cost hundreds of MB on the worker. price_row() builds the model when it is written.
    """
    try:
        response = client.get(PRICE_GUIDE_URL)
        response.raise_for_status()
        guide = response.json()
        del response
        guide_date = datetime.strptime(guide["createdAt"], "%Y-%m-%dT%H:%M:%S%z")
        prices = {row["idProduct"]: tuple(row.get(key) for key in PRICE_FIELDS) for row in guide["priceGuides"]}
    except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise CardmarketError(f"Cardmarket price guide: {type(exc).__name__}: {exc}") from exc
    return prices, guide_date


def fetch_products(client):
    """[CmProduct] of every MTG single and non-single in Cardmarket's public product lists."""
    products = []
    try:
        for url in PRODUCT_LIST_URLS:
            response = client.get(url)
            response.raise_for_status()
            for p in response.json()["products"]:
                products.append(CmProduct(
                    int(p["idProduct"]), p["name"], int(p["idCategory"]), p.get("categoryName") or "",
                    int(p["idExpansion"]),
                ))
            del response
    except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise CardmarketError(f"Cardmarket products: {type(exc).__name__}: {exc}") from exc
    return products


def price_row(id_product, values, guide_date):
    """The CardmarketPrice of a fetch_guide() entry."""
    return CardmarketPrice(
        id_product=id_product, prices_date=guide_date,
        **{f: _money(v) for f, v in zip(PRICE_FIELDS.values(), values)},
    )
