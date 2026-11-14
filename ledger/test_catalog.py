from . import catalog
from .models import Blueprint, Card, CollectionItem, Expansion, SyncLog
from .tests import LedgerTestCase, cm_id, load_catalog


class CatalogTest(LedgerTestCase):
    def setUp(self):
        super().setUp()
        load_catalog()

    def test_expansion_by_code_case_insensitive(self):
        self.assertEqual(catalog.expansion_by_code("NEO").name, "Kamigawa: Neon Dynasty")
        self.assertIsNone(catalog.expansion_by_code("zzz"))

    def test_blueprint_without_category_is_a_card(self):
        self.assertFalse(Blueprint(name="x").is_product)

    def test_product_category_is_a_product(self):
        self.assertTrue(Blueprint.objects.get(cm_product_id=cm_id(1390)).is_product)
        self.assertFalse(Blueprint.objects.get(cm_product_id=cm_id(1301)).is_product)

    def test_card_for_blueprint_product_uses_cm_stand_in(self):
        card = catalog.card_for_blueprint(Blueprint.objects.get(cm_product_id=cm_id(1390)))
        self.assertEqual((card.set_code, card.collector_number, card.cm_product_id, card.is_product),
                         ("DMU", f"CM{cm_id(1390)}", cm_id(1390), True))

    def test_card_for_blueprint_reuses_the_linked_card(self):
        bp = Blueprint.objects.get(cm_product_id=cm_id(1301))
        self.assertEqual(catalog.card_for_blueprint(bp).pk, catalog.card_for_blueprint(bp).pk)

    def test_card_for_blueprint_number_taken_by_another_product(self):
        Card.objects.create(name="Other", set_code="DMU", collector_number="107", cm_product_id=1)
        card = catalog.card_for_blueprint(Blueprint.objects.get(cm_product_id=cm_id(1301)))
        self.assertEqual(card.collector_number, f"CM{cm_id(1301)}")
        self.assertTrue(SyncLog.objects.filter(message__contains="shares #107").exists())

    def test_blueprint_of_by_product_then_by_number(self):
        linked = Card(name="Sheoldred", set_code="XXX", cm_product_id=cm_id(1301))
        by_number = Card(name="Sheoldred", set_code="DMU", collector_number="0107")
        self.assertEqual(catalog.blueprint_of(linked).cm_product_id, cm_id(1301))
        self.assertEqual(catalog.blueprint_of(by_number).cm_product_id, cm_id(1301))
        self.assertIsNone(catalog.blueprint_of(Card(name="x", set_code="DMU")))


class MergeOwnersTest(LedgerTestCase):
    def test_merge_keeps_owners_apart(self):
        source = Card.objects.create(name="Bolt", set_code="M10")
        target = Card.objects.create(name="Lightning Bolt", set_code="M10", collector_number="146")
        mine = CollectionItem.objects.create(owner=self.user, card=source, condition="NM", language="EN", quantity=2)
        theirs = CollectionItem.objects.create(owner=self.other, card=target, condition="NM", language="EN", quantity=5)
        self.assertTrue(catalog.merge_cards(source, target))
        mine.refresh_from_db()
        theirs.refresh_from_db()
        self.assertEqual((mine.card_id, mine.owner, mine.quantity), (target.pk, self.user, 2))
        self.assertEqual((theirs.owner, theirs.quantity), (self.other, 5))
        self.assertEqual(CollectionItem.objects.filter(card=target).count(), 2)
