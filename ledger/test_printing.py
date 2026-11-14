from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.urls import reverse

from decimal import Decimal

from . import catalog, services
from .catalog import merge_cards, normalize_number
from .models import Blueprint, Card, CollectionItem, Expansion, Listing, SyncLog
from .tests import LedgerTestCase, cm_id, load_catalog


class PrintingModelTest(LedgerTestCase):
    def test_two_printings_same_name_coexist(self):
        Card.objects.create(name="Fable of the Mirror-Breaker", set_code="NEO", collector_number="141")
        Card.objects.create(name="Fable of the Mirror-Breaker", set_code="NEO", collector_number="356")
        self.assertEqual(Card.objects.filter(name="Fable of the Mirror-Breaker").exclude(collector_number="").count(), 2)

    def test_same_set_number_rejected(self):
        Card.objects.create(name="Fable of the Mirror-Breaker", set_code="NEO", collector_number="141")
        with self.assertRaises(IntegrityError), transaction.atomic():
            Card.objects.create(name="Other", set_code="NEO", collector_number="141")

    def test_legacy_rows_still_unique_by_name(self):
        Card.objects.create(name="X", set_code="M10")
        with self.assertRaises(IntegrityError), transaction.atomic():
            Card.objects.create(name="X", set_code="M10")

    def test_str_shows_number(self):
        card = Card(name="Fable of the Mirror-Breaker", set_code="NEO", collector_number="356")
        self.assertEqual(str(card), "Fable of the Mirror-Breaker (NEO #356)")


class PrintingFormCsvTest(LedgerTestCase):
    def test_catalog_add_targets_printing(self):
        load_catalog()
        resp = self.client.post(reverse("ledger:api_collection"), {
            "blueprint": cm_id(1101), "condition": "NM", "language": "EN", "quantity": 1, "price": "10.00",
        }, content_type="application/json")
        self.assertEqual(resp.status_code, 201)
        self.assertTrue(Card.objects.filter(set_code="NEO", collector_number="356", cm_product_id=cm_id(1101)).exists())

    def test_csv_number_column_roundtrip(self):
        upload = SimpleUploadedFile("c.csv", b"name,set,number,quantity\nFable of the Mirror-Breaker,neo,141,2\n")
        self.client.post(reverse("ledger:api_import"), {"file": upload})
        self.assertTrue(Card.objects.filter(name="Fable of the Mirror-Breaker", set_code="NEO",
                                            collector_number="141").exists())
        exported = self.client.get(reverse("ledger:api_export")).content.decode()
        self.assertIn("quantity,number", exported.splitlines()[0])
        self.assertIn(",141", exported)

    def test_normalize_number(self):
        self.assertEqual(normalize_number("0187"), "187")
        self.assertEqual(normalize_number("012a"), "12A")
        self.assertEqual(normalize_number(" 7 "), "7")
        self.assertEqual(normalize_number(""), "")


class ResolvePrintingTest(LedgerTestCase):
    def resolve(self, cards):
        return services.resolve_blueprints(self.ct.client(), cards).unmatched

    def test_resolve_by_number_picks_showcase(self):
        card = Card.objects.create(name="Fable of the Mirror-Breaker", set_code="NEO", collector_number="356")
        self.assertEqual(self.resolve([card]), [])
        card.refresh_from_db()
        self.assertEqual(card.ct_blueprint_id, 1101)

    def test_resolve_legacy_fills_number(self):
        card = Card.objects.get(name="Counterspell", set_code="DMR")
        self.resolve([card])
        card.refresh_from_db()
        self.assertEqual(card.ct_blueprint_id, 1201)
        self.assertEqual(card.collector_number, "45")

    def test_resolve_merges_into_existing_printing(self):
        legacy = Card.objects.get(name="Counterspell", set_code="DMR")  # demo: NM/IT x4 @0.80, listed
        printing = Card.objects.create(name="Counterspell", set_code="DMR", collector_number="45",
                                       ct_blueprint_id=1201)
        CollectionItem.objects.create(owner=self.user, card=printing, condition="NM", language="IT", quantity=2)
        self.assertEqual(self.resolve([legacy]), [])
        self.assertFalse(Card.objects.filter(pk=legacy.pk).exists())
        item = CollectionItem.objects.get(card=printing, condition="NM", language="IT", foil=False)
        self.assertEqual(item.quantity, 6)
        self.assertTrue(Listing.objects.filter(item=item).exists())  # the legacy listing follows the copies

    def test_merge_refuses_two_live_listings(self):
        legacy = Card.objects.get(name="Counterspell", set_code="DMR")
        printing = Card.objects.create(name="Counterspell", set_code="DMR", collector_number="45",
                                       ct_blueprint_id=1201)
        item = CollectionItem.objects.create(owner=self.user, card=printing, condition="NM", language="IT", quantity=1)
        Listing.objects.create(item=item, quantity=1, price=Decimal("1.00"))
        self.assertFalse(merge_cards(legacy, printing))
        self.assertTrue(Card.objects.filter(pk=legacy.pk).exists())
        self.assertEqual(self.resolve([legacy]), [legacy])

    def test_publish_after_merge_uses_surviving_printing(self):
        printing = Card.objects.create(name="Counterspell", set_code="DMR", collector_number="45",
                                       ct_blueprint_id=1201)
        self.client.post(reverse("ledger:api_publish"))
        listing = Listing.objects.get(item__card=printing)
        self.assertEqual(listing.status, Listing.Status.PUBLISHED)

    def test_resolve_does_not_merge_into_other_blueprint(self):  # I4
        Card.objects.create(name="Counterspell", set_code="DMR", collector_number="45", ct_blueprint_id=7777)
        legacy = Card.objects.get(name="Counterspell", set_code="DMR", collector_number="")
        self.assertEqual(self.resolve([legacy]), [legacy])
        self.assertTrue(Card.objects.filter(pk=legacy.pk, ct_blueprint_id__isnull=True).exists())


    def test_resolve_by_cardmarket_product(self):
        card = Card.objects.create(name="Fable of the Mirror-Breaker", set_code="NEO", collector_number="999",
                                   cm_product_id=cm_id(1101))
        self.assertEqual(self.resolve([card]), [])
        card.refresh_from_db()
        self.assertEqual((card.ct_blueprint_id, card.collector_number), (1101, "999"))

    def test_resolve_finds_expansion_by_catalog_name(self):
        load_catalog()
        Expansion.objects.filter(code="dmu").update(code="xdmu")
        card = Card.objects.create(name="Sheoldred, the Apocalypse", set_code="XDMU", collector_number="107",
                                   cm_product_id=cm_id(1301))
        self.assertEqual(self.resolve([card]), [])
        card.refresh_from_db()
        self.assertEqual(card.ct_blueprint_id, 1301)

    def test_resolve_unknown_expansion_stays_unmatched(self):
        card = Card.objects.create(name="Mystery", set_code="ZZZ", collector_number="1", cm_product_id=1)
        self.assertEqual(self.resolve([card]), [card])
        self.assertNotIn(("GET", "/blueprints/export"), self.ct.calls)

    def test_resolve_reads_product_properties_of_matched_products(self):
        load_catalog()
        box = catalog.card_for_blueprint(Blueprint.objects.get(cm_product_id=cm_id(1390)))
        box.ct_blueprint_id = 1390
        box.save()
        resolution = services.resolve_blueprints(self.ct.client(), [box])
        self.assertEqual(resolution.properties, {1390: ["mtg_language"]})


class MergeClashLogTest(LedgerTestCase):
    def test_clash_of_another_user_is_logged_only_for_them(self):
        """The friend's publish hits a duplicate printing that only the admin's live listings keep apart."""
        legacy = Card.objects.get(name="Ragavan, Nimble Pilferer", set_code="MH2")  # admin: live listing
        printing = Card.objects.create(name="Ragavan, Nimble Pilferer", set_code="MH2", collector_number="138",
                                       ct_blueprint_id=1001)
        twin = CollectionItem.objects.create(owner=self.user, card=printing, condition="NM", language="EN")
        Listing.objects.create(item=twin, price=Decimal("50.00"), status=Listing.Status.PUBLISHED, ct_product_id=4242)
        theirs = CollectionItem.objects.create(owner=self.other, card=legacy, condition="LP", language="EN")
        Listing.objects.create(item=theirs, price=Decimal("40.00"), status=Listing.Status.TO_PUBLISH)
        service = services.get_cardtrader(self.other)
        service.api_token, service.ct_user_id = "friend-token", 78
        service.save()

        services.publish_pending(self.other)

        clash = "both have live listings"
        self.assertFalse(SyncLog.objects.visible_to(self.other).filter(message__contains=clash).exists())
        self.assertTrue(SyncLog.objects.filter(owner=self.user, message__contains=clash).exists())
