from datetime import date
from decimal import Decimal

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from .models import Blueprint, Card, CardmarketPrice, Category, CollectionItem, Expansion, Listing
from .tests import LedgerTestCase


def catalog():
    """A small local catalog: Cardmarket products, some with prices."""
    m21 = Expansion.objects.create(code="m21", name="Core Set 2021", released_at=date(2020, 7, 3))
    dmr = Expansion.objects.create(code="dmr", name="Dominaria Remastered", released_at=date(2023, 1, 13))
    CardmarketPrice.objects.create(
        id_product=9001, trend=Decimal("12.75"), low=Decimal("10.00"), avg=Decimal("12.50"),
        trend_foil=Decimal("14.34"), prices_date=timezone.now(),
    )
    CardmarketPrice.objects.create(id_product=9007, trend=Decimal("0.50"))
    ugin = Blueprint.objects.create(
        cm_product_id=9001, expansion=m21, name="Ugin, the Spirit Dragon", collector_number="1",
        image_url="https://img/ugin.jpg", image_large_url="https://img/show_ugin.jpg",
    )
    Blueprint.objects.create(cm_product_id=9007, expansion=m21, name="Basri Ket", collector_number="7")
    Blueprint.objects.create(cm_product_id=9045, expansion=dmr, name="Counterspell", collector_number="45")
    single = Category.objects.create(cm_id=1, name="Magic Single Card")
    boxes = Category.objects.create(cm_id=4, name="Booster Boxes")
    Category.objects.create(cm_id=40, name="Playmats")  # no blueprint: not listed
    Blueprint.objects.filter(cm_product_id__in=[9001, 9007, 9045]).update(category=single)
    Blueprint.objects.create(
        cm_product_id=9900, expansion=dmr, name="Dominaria Remastered Draft Booster Box", category=boxes,
    )
    Blueprint.objects.create(cm_product_id=9901, expansion=dmr, name="Dominaria Remastered Playmat")
    return ugin


class ListingSuggestionTest(LedgerTestCase):
    def setUp(self):
        super().setUp()
        catalog()
        self.item = CollectionItem.objects.select_related("card").first()
        self.item.card.cm_product_id = 9001
        self.item.card.save()
        self.url = reverse("ledger:api_listing", args=[self.item.pk])

    def suggestion(self):
        return self.client.get(self.url).json()["cm_suggestion"]

    def test_shows_trend_for_non_foil_item(self):
        self.item.foil = False
        self.item.save()
        self.assertEqual(self.suggestion(), "12.75")

    def test_product_suggestion_uses_trend(self):
        Blueprint.objects.filter(cm_product_id=9001).update(category=Category.objects.get(cm_id=4))
        self.item.card.is_product = True
        self.item.card.save()
        self.item.foil = True  # a product is never foil, whatever the row says
        self.item.save()
        self.assertEqual(self.suggestion(), "12.75")

    def test_shows_foil_trend_for_foil_item(self):
        self.item.foil = True
        self.item.save()
        self.assertEqual(self.suggestion(), "14.34")

    def test_foil_item_without_foil_trend_gets_no_suggestion(self):
        CardmarketPrice.objects.filter(pk=9001).update(trend_foil=None)
        self.item.foil = True
        self.item.save()
        self.assertIsNone(self.suggestion())

    def test_unknown_printing_shows_dash_and_no_button(self):
        self.item.card.cm_product_id = None
        self.item.card.set_code = "PLST"
        self.item.card.save()
        self.assertIsNone(self.suggestion())

    def test_suggestion_does_not_change_saved_price(self):
        Listing.objects.filter(item=self.item).delete()
        self.suggestion()
        r = self.client.put(self.url, {"quantity": 1, "price": "3.00", "status": "to_publish"},
                            content_type="application/json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(Listing.objects.get(item=self.item).price, Decimal("3.00"))


class CatalogApiTest(LedgerTestCase):
    def setUp(self):
        super().setUp()
        catalog()
        self.url = reverse("ledger:api_catalog")

    def own(self, quantity, **card):
        item = CollectionItem.objects.select_related("card").first()
        item.quantity = quantity
        item.save()
        for k, v in card.items():
            setattr(item.card, k, v)
        item.card.save()

    def search(self, **params):
        resp = self.client.get(self.url, params)
        self.assertEqual(resp.status_code, 200)
        return resp.json()

    def names(self, **params):
        return [p["name"] for p in self.search(**params)["results"]]

    def test_requires_login(self):
        self.client.logout()
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_search_name_case_insensitive_substring(self):
        data = self.search(q="spirit drag")
        self.assertTrue(data["searched"])
        [ugin] = data["results"]
        self.assertEqual((ugin["name"], ugin["expansion_code"], ugin["collector_number"]),
                         ("Ugin, the Spirit Dragon", "m21", "1"))
        self.assertEqual(ugin["expansion_name"], "Core Set 2021")
        self.assertEqual(ugin["prices"], {"trend": "12.75", "low": "10.00", "avg": "12.50",
                                          "trend_foil": "14.34", "low_foil": None, "avg_foil": None})
        self.assertEqual(ugin["image_url"], "https://img/ugin.jpg")
        self.assertEqual(ugin["image_large_url"], "https://img/show_ugin.jpg")
        self.assertIsNone(ugin["owned_qty"])
        self.assertIsNotNone(data["prices_date"])

    def test_search_box_matches_set_code(self):
        self.assertEqual(self.names(q="M21"), ["Basri Ket", "Ugin, the Spirit Dragon"])

    def test_set_filter(self):
        self.assertEqual(self.names(set="DMR"), ["Counterspell", "Dominaria Remastered Draft Booster Box",
                                                 "Dominaria Remastered Playmat"])

    def test_set_and_number_filter_ignores_leading_zeros(self):
        data = self.search(set="m21", number="007")
        self.assertEqual([p["name"] for p in data["results"]], ["Basri Ket"])
        self.assertEqual(data["filters"], {"q": "", "set": "m21", "number": "7", "category": None, "owned": False})

    def test_category_filter_alone_searches(self):
        data = self.search(category="4")
        self.assertTrue(data["searched"])
        self.assertEqual([p["name"] for p in data["results"]], ["Dominaria Remastered Draft Booster Box"])
        self.assertEqual(data["filters"]["category"], 4)

    def test_bad_category_is_ignored(self):
        self.assertEqual(self.search(category="x")["searched"], False)

    def test_row_has_category_fields(self):
        rows = {p["ct_id"]: p for p in self.search(set="dmr")["results"]}
        box, mat, card = rows[9900], rows[9901], rows[9045]
        self.assertEqual((box["category_id"], box["category_name"], box["is_product"], box["accepts_language"]),
                         (4, "Booster Boxes", True, True))
        self.assertEqual((mat["category_id"], mat["is_product"], mat["accepts_language"]), (None, False, True))
        self.assertEqual((card["category_id"], card["is_product"], card["accepts_language"]), (1, False, True))

    def test_categories_list(self):
        resp = self.client.get(reverse("ledger:api_catalog_categories"))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), [{"id": 4, "name": "Booster Boxes"}, {"id": 1, "name": "Magic Single Card"}])

    def test_old_url_is_gone(self):
        resp = self.client.get("/api/cards/", {"q": "Ugin"})
        self.assertNotEqual(resp.get("Content-Type"), "application/json")

    def test_printing_without_price_shows_dashes(self):
        [card] = self.search(q="counterspell")["results"]
        self.assertIsNone(card["prices"])

    def test_owned_by_blueprint_link(self):
        self.own(3, cm_product_id=9001)
        data = self.search(owned="1")
        self.assertEqual([(p["name"], p["owned_qty"]) for p in data["results"]], [("Ugin, the Spirit Dragon", 3)])
        self.assertTrue(data["filters"]["owned"])

    def test_owned_by_set_and_number(self):
        self.own(2, cm_product_id=None, set_code="DMR", collector_number="045")
        self.assertEqual([(p["name"], p["owned_qty"]) for p in self.search(owned="1")["results"]],
                         [("Counterspell", 2)])

    def test_owned_only_counts_mine(self):
        self.own(3, cm_product_id=9001)
        self.client.force_login(self.other)
        self.assertEqual(self.names(owned="1"), [])
        [ugin] = self.search(q="Ugin")["results"]
        self.assertIsNone(ugin["owned_qty"])

    def test_no_criteria_lists_latest_releases(self):
        data = self.search(q="  ")
        self.assertFalse(data["searched"])
        self.assertEqual(data["filters"], {"q": "", "set": "", "number": "", "category": None, "owned": False})
        # newest expansion (highest CardTrader id) first, then name like a search
        self.assertEqual([(p["expansion_code"], p["name"]) for p in data["results"]], [
            ("dmr", "Counterspell"), ("dmr", "Dominaria Remastered Draft Booster Box"),
            ("dmr", "Dominaria Remastered Playmat"),
            ("m21", "Basri Ket"), ("m21", "Ugin, the Spirit Dragon"),
        ])
        self.assertEqual((data["count"], data["page"], data["num_pages"]), (5, 1, 1))
        self.assertIsNotNone(data["prices_date"])

    def test_latest_releases_rows_match_a_search(self):
        self.own(3, cm_product_id=9001)
        latest = {p["ct_id"]: p for p in self.search()["results"]}
        [ugin] = self.search(q="Ugin")["results"]
        self.assertEqual(latest[9001], ugin)
        self.assertEqual(ugin["owned_qty"], 3)

    def test_latest_releases_paginate(self):
        newest = Expansion.objects.create(code="new", name="Newest Set", released_at=date(2026, 1, 1))
        Blueprint.objects.bulk_create(
            Blueprint(cm_product_id=30000 + i, expansion=newest, name=f"Card {i:02}") for i in range(52)
        )
        first, second = self.search(), self.search(page="2")
        self.assertFalse(second["searched"])
        self.assertEqual((second["count"], second["page"], second["num_pages"]), (57, 2, 2))
        self.assertEqual({p["expansion_code"] for p in first["results"]}, {"new"})
        self.assertEqual([p["expansion_code"] for p in second["results"][-5:]], ["dmr"] * 3 + ["m21"] * 2)

    def test_latest_releases_query_count_does_not_grow_with_rows(self):
        def queries():
            with CaptureQueriesContext(connection) as ctx:
                self.search()
            return len(ctx.captured_queries)

        before = queries()
        m21 = Expansion.objects.get(code="m21")
        Blueprint.objects.bulk_create(Blueprint(cm_product_id=40000 + i, expansion=m21, name=f"Extra {i}") for i in range(20))
        self.assertEqual(queries(), before)

    def test_no_results_message(self):
        data = self.search(q="zzzz")
        self.assertEqual((data["searched"], data["count"], data["results"]), (True, 0, []))

    def test_pager_keeps_filters(self):
        tokens = Expansion.objects.create(code="tm21", name="M21 Tokens")
        Blueprint.objects.bulk_create(
            Blueprint(cm_product_id=20000 + i, expansion=tokens, name=f"Token {i}") for i in range(60)
        )
        data = self.search(set="tm21", page="2")
        self.assertEqual((data["count"], data["page"], data["num_pages"], len(data["results"])), (60, 2, 2, 10))


class SmartSearchTest(LedgerTestCase):
    def setUp(self):
        super().setUp()
        catalog()
        self.url = reverse("ledger:api_catalog")
        m21, dmr = Expansion.objects.get(code="m21"), Expansion.objects.get(code="dmr")
        single, boxes = Category.objects.get(cm_id=1), Category.objects.get(cm_id=4)
        for cm, exp, name, number, category in [
            (9101, m21, "Mother of Runes", "10", single),
            (9102, dmr, "Queen Mother Ramonda", "20", single),
            (9103, dmr, "Lim-Dûl the Necromancer", "30", single),
            (9104, dmr, "Urza's Saga", "123", single),
            (9105, dmr, "Mother of Runes Playmat", "", boxes),
            (9106, dmr, "Mother of Prunes", "40", single),
        ]:
            Blueprint.objects.create(cm_product_id=cm, expansion=exp, name=name, collector_number=number,
                                     category=category)

    def search(self, **params):
        resp = self.client.get(self.url, params)
        self.assertEqual(resp.status_code, 200)
        return resp.json()

    def names(self, **params):
        return [p["name"] for p in self.search(**params)["results"]]

    def test_words_in_any_order_and_prefix(self):
        self.assertEqual(self.names(q="mother rune"), ["Mother of Runes", "Mother of Runes Playmat"])
        self.assertEqual(self.names(q="rune mother"), ["Mother of Runes", "Mother of Runes Playmat"])

    def test_word_must_start_a_word(self):
        self.assertNotIn("Mother of Prunes", self.names(q="mother rune"))

    def test_case_accents_and_punctuation(self):
        self.assertEqual(self.names(q="LIM DUL"), ["Lim-Dûl the Necromancer"])
        self.assertEqual(self.names(q="lim-dûl"), ["Lim-Dûl the Necromancer"])
        self.assertEqual(self.names(q="urzas"), ["Urza's Saga"])
        self.assertEqual(self.names(q="Urza's Saga"), ["Urza's Saga"])

    def test_set_code_in_query(self):
        self.assertEqual(self.names(q="mother m21"), ["Mother of Runes"])
        self.assertEqual(self.names(q="M21"), ["Basri Ket", "Mother of Runes", "Ugin, the Spirit Dragon"])

    def test_number_needs_a_set_code(self):
        self.assertEqual(self.names(q="dmr 123"), ["Urza's Saga"])
        self.assertEqual(self.names(q="123"), [])

    def test_regex_characters_are_harmless(self):
        self.search(q="bolt.*(")

    def test_blank_words_do_not_filter(self):
        self.assertEqual(self.search(q=" -' ")["count"], Blueprint.objects.count())

    def test_exact_name_first_then_cards_before_products(self):
        Blueprint.objects.create(cm_product_id=9107, expansion=Expansion.objects.get(code="m21"), name="Mother",
                                 category=Category.objects.get(cm_id=1))
        names = self.names(q="mother")
        self.assertEqual(names[0], "Mother")
        # same relevance: the card before the product, although the product's expansion is newer
        self.assertEqual(self.names(q="runes"), ["Mother of Runes", "Mother of Runes Playmat"])

    def test_typo_fallback(self):
        self.assertEqual(self.names(q="mothr rnes")[0], "Mother of Runes")

    def test_fallback_respects_filters(self):
        self.assertNotIn("Mother of Runes", self.names(q="mothr rnes", set="dmr"))

    def test_no_fallback_when_words_match(self):
        with CaptureQueriesContext(connection) as ctx:
            self.search(q="mother rune")
        self.assertFalse(any("word_similarity" in q["sql"].lower() for q in ctx.captured_queries))

    def test_nonsense_finds_nothing(self):
        self.assertEqual(self.names(q="zzzxqv"), [])

    def test_hyphenated_set_code(self):
        sld = Expansion.objects.create(code="sld-cvd", name="Secret Lair: CVD")
        Blueprint.objects.create(cm_product_id=9201, expansion=sld, name="Cool Card", collector_number="1")
        self.assertEqual(self.names(q="sld-cvd"), ["Cool Card"])
        self.assertEqual(self.names(q="SLD-CVD cool"), ["Cool Card"])

    def test_exact_name_with_transliterated_letters(self):
        Blueprint.objects.create(cm_product_id=9202, expansion=Expansion.objects.get(code="dmr"), name="Sœur Weiß")
        self.assertEqual(self.names(q="Sœur Weiß"), ["Sœur Weiß"])

    def test_typo_with_set_code(self):
        self.assertEqual(self.names(q="mothr rnes m21"), ["Mother of Runes"])

    def test_number_belongs_to_the_named_set(self):
        Blueprint.objects.create(cm_product_id=9203, expansion=Expansion.objects.get(code="m21"), name="Dmr Golem",
                                 collector_number="1")
        self.assertNotIn("Dmr Golem", self.names(q="dmr 1"))
