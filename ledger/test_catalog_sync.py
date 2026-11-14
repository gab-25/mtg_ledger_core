import copy
from datetime import date
from decimal import Decimal
from io import StringIO
from unittest import mock

import httpx
from django.core.management import call_command
from django.test import TestCase

from . import cardmarket, catalog, catalog_sync, scryfall
from .models import Blueprint, Card, CardmarketPrice, Category, CollectionItem, Expansion, Listing, SyncLog
from .test_scryfall import FakeScryfall, image
from .tests import LedgerTestCase

TOKEN, UNMATCHED, ORPHAN = 501250, 501203, 777001


def product(id_product, name, category, expansion):
    names = {1: "Magic Single", 5: "Magic Display", 13: "Magic Miscellaneous", 9: "Magic Lot"}
    return {"idProduct": id_product, "name": name, "idCategory": category, "categoryName": names[category],
            "idExpansion": expansion, "idMetacard": 0, "dateAdded": "2022-01-01 00:00:00"}


SINGLES = [
    product(501201, "Counterspell", 1, 77),
    product(501204, "Ponder", 1, 77),
    product(TOKEN, "Soldier Token", 1, 77),
    product(UNMATCHED, "Brainstorm", 1, 77),  # no Scryfall card
    product(501301, "Sheoldred, the Apocalypse", 1, 78),
]
NONSINGLES = [
    product(501390, "Dominaria United Draft Booster Box", 5, 78),
    product(501391, "Dominaria United Playmat", 13, 78),
    product(ORPHAN, "100 random Commons", 9, 99),  # an expansion without singles
]
CARDS = [
    {"cardmarket_id": 501201, "set": "dmr", "collector_number": "45", "rarity": "uncommon",
     "image_uris": image("counterspell")},
    {"cardmarket_id": 501204, "set": "dmr", "collector_number": "46", "rarity": "common", "image_uris": image("ponder")},
    {"cardmarket_id": TOKEN, "set": "tdmr", "collector_number": "1", "rarity": "common", "image_uris": image("token")},
    {"cardmarket_id": 501301, "set": "dmu", "collector_number": "107", "rarity": "mythic",
     "image_uris": image("sheoldred")},
]
SETS = [
    {"code": "dmr", "name": "Dominaria Remastered", "released_at": "2023-01-13"},
    {"code": "tdmr", "name": "Dominaria Remastered Tokens", "released_at": "2023-01-13"},
    {"code": "dmu", "name": "Dominaria United", "released_at": "2022-09-09"},
]
GUIDE = {"version": 1, "createdAt": "2026-11-14T02:45:31+0100", "priceGuides": [
    {"idProduct": 501201, "trend": 1.2, "trend-foil": 3.5},
    {"idProduct": 501390, "trend": 150.0},
]}


class FakeCardmarket:
    """Cardmarket's product lists and price guide, served through httpx.MockTransport."""

    def __init__(self):
        self.singles, self.nonsingles = copy.deepcopy(SINGLES), copy.deepcopy(NONSINGLES)
        self.guide = copy.deepcopy(GUIDE)
        self.raw_singles = None

    def handler(self, request):
        url = str(request.url)
        if url == cardmarket.PRICE_GUIDE_URL:
            return httpx.Response(200, json=self.guide)
        if url == cardmarket.PRODUCT_LIST_URLS[0]:
            if self.raw_singles is not None:
                return httpx.Response(200, content=self.raw_singles)
            return httpx.Response(200, json={"version": 1, "products": self.singles})
        if url == cardmarket.PRODUCT_LIST_URLS[1]:
            return httpx.Response(200, json={"version": 1, "products": self.nonsingles})
        return httpx.Response(404)

    def client(self):
        return cardmarket.make_client(transport=httpx.MockTransport(self.handler))


class CatalogSyncTestCase(LedgerTestCase):
    def setUp(self):
        super().setUp()
        self.cm = FakeCardmarket()
        self.sf = FakeScryfall(cards=copy.deepcopy(CARDS), sets=copy.deepcopy(SETS))

    def sync(self):
        return catalog_sync.sync(self.cm.client(), self.sf.client())

    def run_sync(self):
        return catalog_sync.run_sync(self.cm.client(), self.sf.client())


class SyncTest(CatalogSyncTestCase):
    def test_sync_builds_catalog_from_public_files(self):
        self.sync()
        bp = Blueprint.objects.get(cm_product_id=501201)
        self.assertEqual((bp.name, bp.expansion.code, bp.collector_number, bp.rarity, bp.category.cm_id),
                         ("Counterspell", "dmr", "45", "uncommon", 1))
        self.assertEqual((bp.image_url, bp.image_large_url),
                         ("https://img/counterspell-small.jpg", "https://img/counterspell-normal.jpg"))
        dmr = Expansion.objects.get(code="dmr")
        self.assertEqual((dmr.name, dmr.released_at), ("Dominaria Remastered", date(2023, 1, 13)))
        self.assertEqual(Category.objects.get(cm_id=5).name, "Magic Display")
        self.assertEqual(self.ct.calls, [])  # CardTrader is never contacted

    def test_token_gets_its_own_scryfall_set(self):
        self.sync()
        token = Blueprint.objects.get(cm_product_id=TOKEN)
        self.assertEqual((token.expansion.code, token.collector_number), ("tdmr", "1"))

    def test_product_goes_to_the_main_set_of_its_expansion(self):
        self.sync()
        box = Blueprint.objects.get(cm_product_id=501390)
        self.assertEqual((box.expansion.code, box.collector_number, box.is_product), ("dmu", "", True))

    def test_single_without_scryfall_card_goes_to_the_main_set(self):
        self.sync()
        bp = Blueprint.objects.get(cm_product_id=UNMATCHED)
        self.assertEqual((bp.expansion.code, bp.collector_number, bp.image_url), ("dmr", "", ""))

    def test_orphan_product_is_skipped_and_counted(self):
        result = self.sync()
        self.assertEqual(result.skipped, 1)
        self.assertFalse(Blueprint.objects.filter(cm_product_id=ORPHAN).exists())
        self.assertEqual((result.expansions, result.blueprints, result.priced), (3, 7, 2))

    def test_expansions_and_categories_without_printings_are_removed(self):
        Expansion.objects.create(code="old", name="Old CardTrader code")
        Category.objects.create(cm_id=99, name="Old category")
        self.sync()
        self.assertFalse(Expansion.objects.filter(code="old").exists())
        self.assertFalse(Category.objects.filter(cm_id=99).exists())

    def test_sync_twice_no_duplicates_prices_overwritten(self):
        self.sync()
        self.cm.guide["priceGuides"][0]["trend"] = 2.5
        self.sync()
        self.assertEqual(Blueprint.objects.count(), 7)
        self.assertEqual(CardmarketPrice.objects.get(pk=501201).trend, Decimal("2.50"))

    def test_sync_written_in_batches(self):
        with mock.patch.object(catalog_sync, "BATCH", 2):
            self.sync()
        self.assertEqual(Blueprint.objects.count(), 7)

    def test_scryfall_failure_changes_nothing_and_logs_warning(self):
        self.sf.fail_file = True
        self.assertIsNone(self.run_sync())
        self.assertFalse(Blueprint.objects.exists())
        log = SyncLog.objects.latest("pk")
        self.assertEqual(log.level, SyncLog.Level.WARNING)
        self.assertIn("Catalog sync failed: Scryfall cards", log.message)

    def test_malformed_product_list_changes_nothing(self):
        self.cm.raw_singles = b"{not json"
        self.assertIsNone(self.run_sync())
        self.assertFalse(Blueprint.objects.exists())
        self.assertIn("Catalog sync failed: Cardmarket products", SyncLog.objects.latest("pk").message)

    def test_unexpected_failure_is_logged_and_raised(self):
        with mock.patch.object(catalog_sync, "_write", side_effect=RuntimeError("db down")):
            with self.assertRaises(RuntimeError):
                self.run_sync()
        self.assertIn("RuntimeError: db down", SyncLog.objects.latest("pk").message)

    def test_run_sync_logs_summary(self):
        Card.objects.create(name="Nowhere", set_code="ZZZ", collector_number="1")
        self.run_sync()
        log = SyncLog.objects.latest("pk")
        self.assertEqual(log.level, SyncLog.Level.OK)
        self.assertTrue(log.message.startswith("Catalog sync: 3 expansions, 7 printings, 2 priced (guide 2026-11-14)"),
                        log.message)
        self.assertIn("1 products without set", log.message)
        self.assertIn("cards linked", log.message)


class PruneTest(CatalogSyncTestCase):
    def old_blueprint(self, cm, **fields):
        old = Expansion.objects.get_or_create(code="ctold", defaults={"name": "Old CardTrader set"})[0]
        return Blueprint.objects.create(cm_product_id=cm, expansion=old, name="Old lot", **fields)

    def test_blueprints_no_longer_in_the_files_are_removed(self):
        self.old_blueprint(888001)
        self.sync()
        self.assertFalse(Blueprint.objects.filter(cm_product_id=888001).exists())
        self.assertFalse(Expansion.objects.filter(code="ctold").exists())

    def test_blueprint_of_an_open_scan_line_is_kept(self):
        from .models import ScanLine, ScanSession

        bp = self.old_blueprint(888002)
        session = ScanSession.objects.create(owner=self.user)
        ScanLine.objects.create(session=session, blueprint=bp, condition="NM", language="EN")
        self.sync()
        self.assertTrue(Blueprint.objects.filter(cm_product_id=888002).exists())

    def test_saved_scan_line_keeps_its_history_without_the_blueprint(self):
        from .models import ScanLine, ScanSession

        bp = self.old_blueprint(888003)
        session = ScanSession.objects.create(owner=self.user, status=ScanSession.Status.SAVED)
        line = ScanLine.objects.create(session=session, blueprint=bp, condition="NM", language="EN")
        self.sync()
        line.refresh_from_db()
        self.assertIsNone(line.blueprint)

    def test_suspiciously_small_download_prunes_nothing(self):
        for i in range(20):
            self.old_blueprint(889000 + i)
        result = self.sync()
        self.assertEqual(Blueprint.objects.filter(cm_product_id__gte=889000).count(), 20)
        self.assertFalse(result.pruned)


class ReconcileTest(CatalogSyncTestCase):
    def setUp(self):
        super().setUp()
        self.sync()

    def stack(self, card, quantity=1, listing=None, owner=None):
        item = CollectionItem.objects.create(owner=owner or self.user, card=card, condition="NM", language="EN",
                                             quantity=quantity)
        if listing:
            Listing.objects.create(item=item, quantity=1, price=Decimal("1.00"), status=listing)
        return item

    def test_existing_card_rewritten_to_catalog_code(self):
        mat = Card.objects.create(name="Dominaria United Playmat", set_code="DMU", collector_number="CT1391",
                                  cm_product_id=501391)
        token = Card.objects.create(name="Soldier Token", set_code="DMR", collector_number="CT9999",
                                    cm_product_id=TOKEN)
        catalog.reconcile_cards()
        mat.refresh_from_db()
        token.refresh_from_db()
        self.assertEqual((mat.set_code, mat.collector_number, mat.is_product), ("DMU", "CM501391", True))
        self.assertEqual((token.set_code, token.collector_number), ("TDMR", "1"))

    def test_rewrite_collision_merges_cards(self):
        old = Card.objects.create(name="Counterspell", set_code="DMR", collector_number="CT1201", cm_product_id=501201)
        csv = Card.objects.create(name="Counterspell", set_code="DMR", collector_number="45")
        self.stack(old, 2)
        self.stack(csv, 3)
        result = catalog.reconcile_cards()
        self.assertEqual(result.merged, 1)
        [card] = Card.objects.filter(name="Counterspell", collector_number="45")
        self.assertEqual(card.cm_product_id, 501201)
        self.assertEqual(card.items.get(owner=self.user).quantity, 5)
        self.assertFalse(Card.objects.filter(collector_number="CT1201").exists())

    def test_duplicate_product_with_live_listings_warns_owner(self):
        first = Card.objects.create(name="Counterspell", set_code="DMR", collector_number="45", cm_product_id=501201)
        second = Card.objects.create(name="Counterspell", set_code="DMR", collector_number="CT1299",
                                     cm_product_id=501201)
        self.stack(first, listing=Listing.Status.PUBLISHED)
        self.stack(second, listing=Listing.Status.TO_PUBLISH)
        result = catalog.reconcile_cards()
        self.assertEqual(result.clashes, 1)
        second.refresh_from_db()
        self.assertIsNone(second.cm_product_id)
        warning = SyncLog.objects.filter(owner=self.user, level=SyncLog.Level.WARNING).latest("pk")
        self.assertIn("both have live listings", warning.message)

    def test_duplicate_product_without_listings_is_merged(self):
        first = Card.objects.create(name="Counterspell", set_code="DMR", collector_number="45", cm_product_id=501201)
        second = Card.objects.create(name="Counterspell", set_code="DMR", collector_number="CT1299",
                                     cm_product_id=501201)
        self.stack(second, 2)
        catalog.reconcile_cards()
        self.assertFalse(Card.objects.filter(pk=second.pk).exists())
        self.assertEqual(first.items.get().quantity, 2)

    def test_unlinked_card_linked_by_set_and_number(self):
        card = Card.objects.create(name="Counterspell", set_code="DMR", collector_number="045")
        result = catalog.reconcile_cards()
        card.refresh_from_db()
        self.assertEqual((card.cm_product_id, card.collector_number), (501201, "45"))
        self.assertGreaterEqual(result.linked, 1)

    def test_single_without_scryfall_number_keeps_its_own(self):
        card = Card.objects.create(name="Brainstorm", set_code="2XM", collector_number="129", cm_product_id=UNMATCHED)
        catalog.reconcile_cards()
        card.refresh_from_db()
        self.assertEqual((card.set_code, card.collector_number), ("2XM", "129"))

    def test_duplicate_product_with_different_numbers_is_unlinked_not_merged(self):
        base = Card.objects.create(name="Counterspell", set_code="DMR", collector_number="45", cm_product_id=501201)
        showcase = Card.objects.create(name="Counterspell", set_code="DMR", collector_number="300",
                                       cm_product_id=501201)
        self.stack(showcase, 2)
        catalog.reconcile_cards()
        showcase.refresh_from_db()
        self.assertIsNone(showcase.cm_product_id)
        self.assertEqual(showcase.items.get().quantity, 2)
        self.assertTrue(Card.objects.filter(pk=base.pk, cm_product_id=501201).exists())

    def test_card_with_unknown_product_stays_as_is(self):
        card = Card.objects.create(name="Gone", set_code="OLD", collector_number="CT1", cm_product_id=424242)
        result = catalog.reconcile_cards()
        card.refresh_from_db()
        self.assertEqual((card.set_code, card.collector_number), ("OLD", "CT1"))
        self.assertGreaterEqual(result.unlinked, 1)


class SyncCommandTest(TestCase):
    def test_command_only_enqueues(self):
        from . import tasks

        with mock.patch.object(tasks.sync_catalog, "delay", return_value=mock.Mock(id="abc")) as delay, \
                mock.patch.object(catalog_sync, "run_sync") as run:
            out = StringIO()
            call_command("sync_catalog", stdout=out)
        delay.assert_called_once_with()
        run.assert_not_called()
        self.assertIn("Catalog sync queued (task abc)", out.getvalue())

    def test_task_runs_sync(self):
        from . import tasks

        with mock.patch.object(catalog_sync, "run_sync") as run:
            tasks.sync_catalog.apply()
        run.assert_called_once_with()

    def test_no_beat_schedule(self):
        from config.celery import app

        self.assertFalse(app.conf.beat_schedule)
