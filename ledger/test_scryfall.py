import gzip
import json
from datetime import date

import httpx
from django.test import SimpleTestCase

from . import scryfall
from .scryfall import ScryCard, ScrySet

FILE_URL = "https://data.scryfall.io/default-cards/default-cards-1.jsonl.gz"


def image(name):
    return {"small": f"https://img/{name}-small.jpg", "normal": f"https://img/{name}-normal.jpg"}


CARDS = [
    {"cardmarket_id": 501201, "set": "dmr", "collector_number": "045", "rarity": "uncommon",
     "image_uris": image("counterspell")},
    {"set": "dmu", "collector_number": "1", "rarity": "common", "image_uris": image("nocm")},  # no Cardmarket id
    {"cardmarket_id": 600001, "set": "neo", "collector_number": "56", "rarity": "rare",
     "card_faces": [{"image_uris": image("front")}, {"image_uris": image("back")}]},
    {"cardmarket_id": 501301, "set": "dmu", "collector_number": "107", "rarity": "mythic",
     "image_uris": image("sheoldred")},
    {"cardmarket_id": 501301, "set": "pdmu", "collector_number": "107s", "rarity": "mythic",
     "image_uris": image("promo")},  # the same Cardmarket id again: the first one wins
]

SETS = [
    {"code": "dmu", "name": "Dominaria United", "released_at": "2022-09-09"},
    {"code": "dmr", "name": "Dominaria Remastered", "released_at": None},
]


class FakeScryfall:
    """Scryfall's bulk-data index, its Default Cards file and the sets list, through httpx.MockTransport."""

    def __init__(self, cards=CARDS, sets=SETS):
        self.cards, self.sets = cards, sets
        self.fail_file = False
        self.raw_file = None

    def handler(self, request):
        url = str(request.url)
        if url == scryfall.BULK_DATA_URL:
            return httpx.Response(200, json={"data": [
                {"type": "oracle_cards", "jsonl_download_uri": "https://data.scryfall.io/oracle.jsonl.gz"},
                {"type": "default_cards", "jsonl_download_uri": FILE_URL},
            ]})
        if url == FILE_URL:
            if self.fail_file:
                return httpx.Response(500, text="boom")
            body = self.raw_file if self.raw_file is not None else "\n".join(json.dumps(c) for c in self.cards)
            return httpx.Response(200, content=gzip.compress(body.encode()),
                                  headers={"Content-Type": "application/gzip"})
        if url == scryfall.SETS_URL:
            half = len(self.sets) // 2
            return httpx.Response(200, json={"data": self.sets[:half], "has_more": True,
                                             "next_page": scryfall.SETS_URL + "?page=2"})
        if url == scryfall.SETS_URL + "?page=2":
            return httpx.Response(200, json={"data": self.sets[len(self.sets) // 2:], "has_more": False})
        return httpx.Response(404)

    def client(self):
        return scryfall.make_client(transport=httpx.MockTransport(self.handler))


class FetchCardsTest(SimpleTestCase):
    def test_fetch_cards_streams_and_keeps_first(self):
        cards = scryfall.fetch_cards(FakeScryfall().client())
        self.assertEqual(cards[501201], ScryCard("dmr", "45", "uncommon", "https://img/counterspell-small.jpg",
                                                 "https://img/counterspell-normal.jpg"))
        self.assertEqual(set(cards), {501201, 600001, 501301})  # the card without a Cardmarket id is skipped
        self.assertEqual(cards[600001].image_url, "https://img/front-small.jpg")  # double-faced: front face
        self.assertEqual(cards[501301].set_code, "dmu")  # duplicate Cardmarket id: the first entry wins

    def test_fetch_cards_http_error_raises(self):
        fake = FakeScryfall()
        fake.fail_file = True
        with self.assertRaises(scryfall.ScryfallError):
            scryfall.fetch_cards(fake.client())

    def test_fetch_cards_malformed_raises(self):
        fake = FakeScryfall()
        fake.raw_file = '{"cardmarket_id": 1, "set": "x"\nnot json'
        with self.assertRaises(scryfall.ScryfallError):
            scryfall.fetch_cards(fake.client())


class FetchSetsTest(SimpleTestCase):
    def test_fetch_sets_follows_pages(self):
        sets = scryfall.fetch_sets(FakeScryfall().client())
        self.assertEqual(sets["dmu"], ScrySet("dmu", "Dominaria United", date(2022, 9, 9)))
        self.assertEqual(sets["dmr"], ScrySet("dmr", "Dominaria Remastered", None))
