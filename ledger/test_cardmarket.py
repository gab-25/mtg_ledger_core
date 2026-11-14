import copy
from decimal import Decimal
from unittest import mock

import httpx
from django.test import TestCase

from . import cardmarket, catalog_sync, services
from .models import Card, CardmarketPrice
from .tests import FakeCardTrader

GUIDE = {
    "version": 1, "createdAt": "2026-11-07T02:45:31+0100",
    "priceGuides": [
        {"idProduct": 555001, "idCategory": 1, "avg": 40.5, "low": 35.0, "trend": 42.0, "avg1": 41.0,
         "avg7": 41.5, "avg30": 43.0, "avg-foil": 60.0, "low-foil": None, "trend-foil": 61.25,
         "avg1-foil": 59.0, "avg7-foil": 60.5, "avg30-foil": 62.0},
        {"idProduct": 555201, "idCategory": 1, "trend": 1.2},
        {"idProduct": 999999, "idCategory": 1, "trend": 99},
    ],
}

# CardTrader blueprint id -> Cardmarket ids (FakeCardTrader's catalog has none of its own)
CM_IDS = {1001: [555001], 1201: [555201, 555202], 1002: []}


SINGLES = {"version": 1, "createdAt": "2026-11-14T12:00:09+0100", "products": [
    {"idProduct": 501201, "name": "Counterspell", "idCategory": 1, "categoryName": "Magic Single",
     "idExpansion": 77, "idMetacard": 1, "dateAdded": "2022-01-01 00:00:00"},
]}
NONSINGLES = {"version": 1, "createdAt": "2026-11-14T12:00:14+0100", "products": [
    {"idProduct": 501390, "name": "Dominaria United Draft Booster Box", "idCategory": 5,
     "categoryName": "Magic Display", "idExpansion": 78, "idMetacard": 0, "dateAdded": "2022-09-01 00:00:00"},
]}


def product_lists_client(singles=SINGLES, nonsingles=NONSINGLES):
    files = dict(zip(cardmarket.PRODUCT_LIST_URLS, (singles, nonsingles)))

    def handler(request):
        body = files.get(str(request.url))
        if body is None:
            return httpx.Response(404)
        return httpx.Response(200, json=body)

    return cardmarket.make_client(transport=httpx.MockTransport(handler))


class ProductListTest(TestCase):
    def test_fetch_products_reads_both_lists(self):
        products = cardmarket.fetch_products(product_lists_client())
        self.assertEqual(products, [
            cardmarket.CmProduct(501201, "Counterspell", 1, "Magic Single", 77),
            cardmarket.CmProduct(501390, "Dominaria United Draft Booster Box", 5, "Magic Display", 78),
        ])

    def test_fetch_products_malformed_raises(self):
        with self.assertRaises(cardmarket.CardmarketError):
            cardmarket.fetch_products(product_lists_client(nonsingles={"products": [{"name": "x"}]}))

    def test_fetch_products_http_error_raises(self):
        with self.assertRaises(cardmarket.CardmarketError):
            cardmarket.fetch_products(product_lists_client(singles=None))


class FakeGuide:
    """Cardmarket's price guide file, served through httpx.MockTransport."""

    def __init__(self):
        self.guide = copy.deepcopy(GUIDE)
        self.fail = False
        self.raw = None

    def handler(self, request):
        if str(request.url) != cardmarket.PRICE_GUIDE_URL:
            return httpx.Response(404)
        if self.fail:
            return httpx.Response(500, text="boom")
        if self.raw is not None:
            return httpx.Response(200, content=self.raw)
        return httpx.Response(200, json=self.guide)

    def client(self):
        return cardmarket.make_client(transport=httpx.MockTransport(self.handler))


class CardmarketTestCase(TestCase):
    def setUp(self):
        self.ct = FakeCardTrader()
        for expansion in self.ct.blueprints.values():
            for bp in expansion:
                bp["card_market_ids"] = CM_IDS.get(bp["id"], [])
                bp["image_url"] = f"https://img/{bp['id']}.jpg"
                bp["image"] = {"url": f"/uploads/{bp['id']}.jpg", "show": {"url": f"/uploads/show_{bp['id']}.jpg"}}
        self.guide = FakeGuide()
        self.cm = self.guide.client()
        self.addCleanup(self.cm.close)
        for target, attr, value in (
            (services, "make_client", mock.Mock(side_effect=self.ct.client)),
            (catalog_sync, "BLUEPRINT_PAUSE", 0),
        ):
            patcher = mock.patch.object(target, attr, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def sync(self):
        with self.ct.client() as ct:
            return catalog_sync.sync(self.cm, ct)


class SuggestedPriceTest(CardmarketTestCase):
    def setUp(self):
        super().setUp()
        self.sync()

    def test_non_foil_uses_trend_via_blueprint_link(self):
        card = Card.objects.create(name="Ragavan", set_code="MH2", collector_number="138", ct_blueprint_id=1001)
        self.assertEqual(cardmarket.suggested_price(card, foil=False), Decimal("42.00"))

    def test_foil_uses_trend_foil(self):
        card = Card.objects.create(name="Ragavan", set_code="MH2", collector_number="138", ct_blueprint_id=1001)
        self.assertEqual(cardmarket.suggested_price(card, foil=True), Decimal("61.25"))

    def test_foil_without_foil_trend(self):
        CardmarketPrice.objects.filter(pk=555201).update(trend_foil=None)
        card = Card.objects.create(name="Counterspell", set_code="DMR", collector_number="45", ct_blueprint_id=1201)
        self.assertIsNone(cardmarket.suggested_price(card, foil=True))

    def test_zero_trend_is_no_price(self):
        CardmarketPrice.objects.filter(pk=555201).update(trend_foil=Decimal("0.00"))
        card = Card.objects.create(name="Counterspell", set_code="DMR", collector_number="45", ct_blueprint_id=1201)
        self.assertIsNone(cardmarket.suggested_price(card, foil=True))

    def test_card_without_blueprint_found_by_set_and_number(self):
        card = Card.objects.create(name="Counterspell", set_code="DMR", collector_number="045")
        self.assertEqual(cardmarket.suggested_price(card, foil=False), Decimal("1.20"))

    def test_unknown_printing(self):
        card = Card.objects.create(name="X", set_code="PLST", collector_number="9")
        self.assertIsNone(cardmarket.suggested_price(card, foil=False))

    def test_blueprint_without_cardmarket_id(self):
        card = Card.objects.create(name="Solitude", set_code="MH2", collector_number="32", ct_blueprint_id=1002)
        self.assertIsNone(cardmarket.suggested_price(card, foil=False))
