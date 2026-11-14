"""Scryfall's public card data: what Cardmarket's product lists lack (set code, collector number, rarity, images).

Scryfall cards carry their Cardmarket product id, which ties them to the catalog (ledger/catalog_sync.py). The
Default Cards file is ~80 MB of gzip JSON Lines: it is decompressed and parsed one line at a time, keeping only the
fields the catalog uses.
"""

import json
import zlib
from dataclasses import dataclass
from datetime import date

import httpx

from .catalog import normalize_number

BULK_DATA_URL = "https://api.scryfall.com/bulk-data"
SETS_URL = "https://api.scryfall.com/sets"
USER_AGENT = "mtg-ledger/0.1"
BULK_TYPE = "default_cards"


class ScryfallError(Exception):
    pass


@dataclass(frozen=True)
class ScryCard:
    set_code: str
    collector_number: str
    rarity: str
    image_url: str
    image_large_url: str


@dataclass(frozen=True)
class ScrySet:
    code: str
    name: str
    released_at: date | None


def make_client(transport=None):
    return httpx.Client(
        headers={"User-Agent": USER_AGENT, "Accept": "application/json;q=0.9,*/*;q=0.8"},
        timeout=120, follow_redirects=True, transport=transport,
    )


def _lines(response):
    """The decompressed lines of a gzip response body, read as it arrives."""
    inflater = zlib.decompressobj(zlib.MAX_WBITS | 16)
    pending = b""
    for chunk in response.iter_bytes():
        pending += inflater.decompress(chunk)
        *lines, pending = pending.split(b"\n")
        yield from lines
    pending += inflater.flush()
    if pending:
        yield pending


def _images(card):
    images = card.get("image_uris")
    if not images and card.get("card_faces"):
        images = card["card_faces"][0].get("image_uris")
    images = images or {}
    return (images.get("small") or "")[:500], (images.get("normal") or "")[:500]


def fetch_cards(client):
    """{Cardmarket product id: ScryCard} of every Scryfall card that has one; the first entry of an id wins."""
    cards = {}
    try:
        index = client.get(BULK_DATA_URL)
        index.raise_for_status()
        uri = next(d["jsonl_download_uri"] for d in index.json()["data"] if d["type"] == BULK_TYPE)
        with client.stream("GET", uri) as response:
            response.raise_for_status()
            for line in _lines(response):
                if not line.strip():
                    continue
                card = json.loads(line)
                cm_id = card.get("cardmarket_id")
                if not cm_id or cm_id in cards:
                    continue
                cards[cm_id] = ScryCard(
                    card["set"].lower(), normalize_number(card.get("collector_number")), card.get("rarity") or "",
                    *_images(card),
                )
    except (httpx.HTTPError, zlib.error, ValueError, KeyError, TypeError, AttributeError, StopIteration) as exc:
        raise ScryfallError(f"Scryfall cards: {type(exc).__name__}: {exc}") from exc
    return cards


def fetch_sets(client):
    """{set code: ScrySet} of every Scryfall set."""
    sets, url = {}, SETS_URL
    try:
        while url:
            response = client.get(url)
            response.raise_for_status()
            page = response.json()
            for s in page["data"]:
                released = date.fromisoformat(s["released_at"]) if s.get("released_at") else None
                sets[s["code"].lower()] = ScrySet(s["code"].lower(), s["name"], released)
            url = page.get("next_page") if page.get("has_more") else None
    except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise ScryfallError(f"Scryfall sets: {type(exc).__name__}: {exc}") from exc
    return sets
