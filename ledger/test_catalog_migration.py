"""The upgrade of the CardTrader-keyed catalog to the Cardmarket-keyed one (migrations 0025-0027)."""

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

AUTH = ("auth", "0012_alter_user_first_name_max_length")
BEFORE = [("ledger", "0024_scan_without_prices"), AUTH]
AFTER = [("ledger", "0027_catalog_cardmarket_schema"), AUTH]


class CatalogMigrationTest(TransactionTestCase):
    def setUp(self):
        self.executor = MigrationExecutor(connection)
        self.executor.migrate(BEFORE)
        self.apps = self.executor.loader.project_state(BEFORE).apps

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(executor.loader.graph.leaf_nodes())

    def migrate(self):
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(AFTER)
        return executor.loader.project_state(AFTER).apps

    def test_duplicates_and_scan_lines_survive(self):
        get = self.apps.get_model
        user = get("auth", "User").objects.create(username="gab")
        Expansion, Blueprint = get("ledger", "Expansion"), get("ledger", "Blueprint")
        dmu = Expansion.objects.create(ct_id=13, code="dmu", name="Dominaria United")
        twin = Expansion.objects.create(ct_id=14, code="dmu", name="Dominaria United (again)")
        get("ledger", "Category").objects.create(ct_id=1, name="Magic Single Card")
        Blueprint.objects.create(ct_id=1, expansion=dmu, name="Sheoldred", version="", cm_price_id=900)
        Blueprint.objects.create(ct_id=2, expansion=twin, name="Sheoldred", version="showcase", cm_price_id=900)
        Blueprint.objects.create(ct_id=3, expansion=dmu, name="Playmat", cm_price_id=None)
        Session, Line = get("ledger", "ScanSession"), get("ledger", "ScanLine")
        open_session = Session.objects.create(owner=user, expansion=twin)
        saved_session = Session.objects.create(owner=user, status="saved")
        line_on_2 = Line.objects.create(session=open_session, blueprint_id=Blueprint.objects.get(ct_id=2).pk,
                                        condition="NM", candidates=[1, 2, 3])
        line_on_3 = Line.objects.create(session=open_session, blueprint_id=Blueprint.objects.get(ct_id=3).pk,
                                        condition="NM")
        saved_line_on_3 = Line.objects.create(session=saved_session,
                                              blueprint_id=Blueprint.objects.get(ct_id=3).pk, condition="NM")
        card = get("ledger", "Card").objects.create(name="Sheoldred", set_code="DMU", collector_number="107",
                                                    ct_blueprint_id=2)
        unlinked = get("ledger", "Card").objects.create(name="Island", set_code="DMU")

        apps = self.migrate()
        get = apps.get_model
        Blueprint, Line = get("ledger", "Blueprint"), get("ledger", "ScanLine")
        bp = Blueprint.objects.get()
        self.assertEqual((bp.cm_product_id, bp.expansion.code), (900, "dmu"))
        line = Line.objects.get(pk=line_on_2.pk)
        self.assertEqual((line.blueprint_id, line.candidates), (bp.pk, [900]))
        self.assertFalse(Line.objects.filter(pk=line_on_3.pk).exists())  # open session, no Cardmarket id: dropped
        self.assertIsNone(Line.objects.get(pk=saved_line_on_3.pk).blueprint_id)  # saved session: kept, no printing
        card = get("ledger", "Card").objects.get(pk=card.pk)
        self.assertEqual((card.cm_product_id, card.ct_blueprint_id), (900, 2))
        self.assertIsNone(get("ledger", "Card").objects.get(pk=unlinked.pk).cm_product_id)
        self.assertEqual(get("ledger", "Expansion").objects.filter(code="dmu").count(), 1)
        self.assertEqual(get("ledger", "ScanSession").objects.get(pk=open_session.pk).expansion_id, bp.expansion_id)
        self.assertFalse(get("ledger", "Category").objects.exists())
