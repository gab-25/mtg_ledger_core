import copy
from decimal import Decimal

import httpx
from django.test import TestCase

from . import cardmarket
from .models import Card, CardmarketPrice
from .tests import LedgerTestCase, cm_id, load_catalog

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


class SuggestedPriceTest(LedgerTestCase):
    def setUp(self):
        super().setUp()
        load_catalog()
        CardmarketPrice.objects.create(id_product=cm_id(1001), trend=Decimal("42.00"), trend_foil=Decimal("61.25"))
        CardmarketPrice.objects.create(id_product=cm_id(1201), trend=Decimal("1.20"), trend_foil=Decimal("3.00"))

    def test_non_foil_uses_trend_via_product_link(self):
        card = Card.objects.create(name="Ragavan", set_code="MH2", collector_number="138", cm_product_id=cm_id(1001))
        self.assertEqual(cardmarket.suggested_price(card, foil=False), Decimal("42.00"))

    def test_foil_uses_trend_foil(self):
        card = Card.objects.create(name="Ragavan", set_code="MH2", collector_number="138", cm_product_id=cm_id(1001))
        self.assertEqual(cardmarket.suggested_price(card, foil=True), Decimal("61.25"))

    def test_foil_without_foil_trend(self):
        CardmarketPrice.objects.filter(pk=cm_id(1201)).update(trend_foil=None)
        card = Card.objects.create(name="Counterspell", set_code="DMR", collector_number="45",
                                   cm_product_id=cm_id(1201))
        self.assertIsNone(cardmarket.suggested_price(card, foil=True))

    def test_zero_trend_is_no_price(self):
        CardmarketPrice.objects.filter(pk=cm_id(1201)).update(trend_foil=Decimal("0.00"))
        card = Card.objects.create(name="Counterspell", set_code="DMR", collector_number="45",
                                   cm_product_id=cm_id(1201))
        self.assertIsNone(cardmarket.suggested_price(card, foil=True))

    def test_card_without_product_found_by_set_and_number(self):
        card = Card.objects.create(name="Counterspell", set_code="DMR", collector_number="045")
        self.assertEqual(cardmarket.suggested_price(card, foil=False), Decimal("1.20"))

    def test_unknown_printing(self):
        card = Card.objects.create(name="X", set_code="PLST", collector_number="9")
        self.assertIsNone(cardmarket.suggested_price(card, foil=False))

    def test_product_without_price_row(self):
        card = Card.objects.create(name="Solitude", set_code="MH2", collector_number="32", cm_product_id=cm_id(1002))
        self.assertIsNone(cardmarket.suggested_price(card, foil=False))
