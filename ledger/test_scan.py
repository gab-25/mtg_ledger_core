from decimal import Decimal

from . import catalog, scan
from .models import Blueprint, Card, CollectionItem, Expansion, Listing, ScanLine, SyncLog
from .recognition import Match, Recognition
from .tests import LedgerTestCase


class ScanTestCase(LedgerTestCase):
    def setUp(self):
        super().setUp()
        self.api = self.ct.client()
        catalog.sync_expansions(self.api)
        self.dmr = Expansion.objects.get(code="dmr")
        catalog.ensure_blueprints(self.api, self.dmr)
        self.bolt = Blueprint.objects.get(ct_id=1201)  # Counterspell DMR #45
        self.session = scan.start_session(self.user, self.dmr, "NM")

    def capture(self, capture_id, *, language="IT", foil=False, certain=True):
        rec = Recognition(name="Counterspell", language=language, foil=foil)
        m = Match(self.bolt, [self.bolt], "ok") if certain else Match(None, [self.bolt], "ambiguous")
        return scan.add_capture(self.session, rec, m, frame=b"jpeg", capture_id=capture_id)


class CaptureTest(ScanTestCase):
    def test_duplicate_capture_id_adds_once(self):
        self.capture("a")
        line, merged = self.capture("a")
        self.assertEqual(self.session.lines.count(), 1)
        self.assertEqual(line.quantity, 1)

    def test_same_card_merges_quantity(self):
        self.capture("a")
        line, merged = self.capture("b")
        self.assertTrue(merged)
        self.assertEqual((self.session.lines.count(), line.quantity), (1, 2))

    def test_different_language_is_a_new_line(self):
        self.capture("a")
        self.capture("b", language="EN")
        self.assertEqual(self.session.lines.count(), 2)

    def test_uncertain_capture_kept_with_frame(self):
        line, _ = self.capture("a", certain=False)
        self.assertTrue(line.needs_confirmation)
        self.assertEqual((bytes(line.frame), line.candidates, line.blueprint), (b"jpeg", [1201], None))

    def test_undo_and_confirm(self):
        line, _ = self.capture("a")
        self.capture("b")
        line.refresh_from_db()
        scan.undo(line)
        line.refresh_from_db()
        self.assertEqual(line.quantity, 1)
        scan.undo(line)
        self.assertFalse(ScanLine.objects.filter(pk=line.pk).exists())
        pending, _ = self.capture("c", certain=False)
        confirmed = scan.confirm_line(pending, self.bolt, "IT")
        self.assertFalse(confirmed.needs_confirmation)
        self.assertIsNone(confirmed.frame)

    def test_update_line_remerges(self):
        a, _ = self.capture("a")
        b, _ = self.capture("b", foil=True)
        merged = scan.update_line(b, foil=False)
        self.assertEqual((merged.pk, merged.quantity), (a.pk, 2))
        self.assertFalse(ScanLine.objects.filter(pk=b.pk).exists())

    def test_add_manual(self):
        line = scan.add_manual(self.session, self.bolt, "IT", False)
        self.assertEqual((line.condition, line.quantity, line.needs_confirmation), ("NM", 1, False))


class NoPriceTest(ScanTestCase):
    def test_line_has_no_price_fields(self):
        line, _ = self.capture("a")
        self.assertFalse(hasattr(line, "price") or hasattr(line, "for_sale") or hasattr(line, "listed"))

    def test_session_has_no_sale_threshold(self):
        self.assertFalse(hasattr(self.session, "sale_threshold"))


class SaveTest(ScanTestCase):
    def printing(self, *, quantity=1, listing_status=None):
        card = Card.objects.create(name="Counterspell", set_code="DMR", collector_number="45", ct_blueprint_id=1201)
        item = CollectionItem.objects.create(owner=self.user, card=card, condition="NM", language="IT", quantity=quantity)
        if listing_status:
            Listing.objects.create(item=item, quantity=1, price=Decimal("9.00"), status=listing_status)
        return item

    def test_save_only_adds_to_collection(self):
        Card.objects.filter(name="Counterspell").delete()
        listings = Listing.objects.count()
        self.capture("a")
        self.assertEqual(scan.save_session(self.session), 1)
        item = CollectionItem.objects.get(card__ct_blueprint_id=1201)
        self.assertEqual((item.card.collector_number, item.quantity), ("45", 1))
        self.assertEqual(Listing.objects.count(), listings)
        self.assertTrue(SyncLog.objects.filter(owner=self.user, message="Camera scan: 1 cards saved").exists())

    def test_save_adds_to_own_item_for_same_printing(self):
        mine = self.printing(quantity=2)
        friend_session = scan.start_session(self.other, self.dmr, "NM")
        rec = Recognition(name="Counterspell", language="IT")
        scan.add_capture(friend_session, rec, Match(self.bolt, [self.bolt], "ok"), frame=b"jpeg", capture_id="f")
        scan.save_session(friend_session)
        theirs = CollectionItem.objects.get(owner=self.other)
        self.assertEqual((theirs.card_id, theirs.quantity), (mine.card_id, 1))
        mine.refresh_from_db()
        self.assertEqual(mine.quantity, 2)
        self.assertTrue(SyncLog.objects.filter(owner=self.other, message__startswith="Camera scan").exists())

    def test_save_adds_to_existing_copies(self):
        item = self.printing()
        self.capture("a")
        self.capture("b")
        scan.save_session(self.session)
        item.refresh_from_db()
        self.assertEqual(item.quantity, 3)

    def test_save_upgrades_legacy_card_for_base_printing(self):
        legacy = Card.objects.get(name="Counterspell", set_code="DMR")  # demo: NM/IT x4 @0.80
        self.capture("a")
        scan.save_session(self.session)
        legacy.refresh_from_db()
        self.assertEqual((legacy.ct_blueprint_id, legacy.collector_number), (1201, "45"))
        self.assertEqual(legacy.items.get(condition="NM", language="IT").quantity, 5)

    def test_save_leaves_existing_listing_alone(self):
        item = self.printing(listing_status=Listing.Status.PUBLISHED)
        self.capture("a")
        scan.save_session(self.session)
        item.listing.refresh_from_db()
        item.refresh_from_db()
        self.assertEqual((item.quantity, item.listing.quantity, item.listing.status), (2, 1, Listing.Status.PUBLISHED))

    def test_save_blocked_by_unconfirmed(self):
        self.capture("a", certain=False)
        with self.assertRaises(scan.ScanError):
            scan.save_session(self.session)

    def test_save_twice_raises(self):
        self.capture("a")
        scan.save_session(self.session)
        with self.assertRaises(scan.ScanError):
            scan.save_session(self.session)
        self.assertEqual(self.session.status, "saved")


class CardForPrintingTest(ScanTestCase):  # I4
    def save_blueprints(self, *blueprints):
        Card.objects.filter(name="Counterspell").delete()
        for i, bp in enumerate(blueprints):
            self.bolt = bp
            self.capture(f"c{i}")
        scan.save_session(self.session)

    def test_alternate_printings_without_number_stay_separate(self):
        Blueprint.objects.filter(pk=self.bolt.pk).update(collector_number="")
        base = Blueprint.objects.get(pk=self.bolt.pk)
        alt = Blueprint.objects.create(ct_id=1299, expansion=self.dmr, name="Counterspell", version="borderless")
        self.save_blueprints(base, alt)
        self.assertEqual(Card.objects.filter(ct_blueprint_id__in=[1201, 1299]).count(), 2)

    def test_printings_sharing_a_number_stay_separate(self):
        alt = Blueprint.objects.create(ct_id=1299, expansion=self.dmr, name="Counterspell",
                                       collector_number="45", version="borderless")
        self.save_blueprints(self.bolt, alt)
        self.assertEqual(Card.objects.filter(ct_blueprint_id__in=[1201, 1299]).count(), 2)
