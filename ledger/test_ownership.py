"""Who owns what: the owner data migration and the owned_by() querysets."""

import os
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase
from django.urls import reverse

from .models import (
    Card, CollectionItem, Listing, Order, OrderItem, ScanLine, ScanSession, SyncConflict, SyncLog, SyncService,
)
from .tests import LedgerTestCase

AUTH = ("auth", "0012_alter_user_first_name_max_length")
BEFORE = [("ledger", "0013_scanline_read_name"), AUTH]
AFTER = [("ledger", "0015_assign_owner"), AUTH]


class AssignOwnerMigrationTest(TransactionTestCase):
    def setUp(self):
        self.executor = MigrationExecutor(connection)
        self.executor.migrate(BEFORE)
        self.apps = self.executor.loader.project_state(BEFORE).apps

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(executor.loader.graph.leaf_nodes())

    def migrate(self, env_token="env-tok"):
        executor = MigrationExecutor(connection)
        env = {"CARDTRADER_API_TOKEN": env_token} if env_token is not None else {}
        with mock.patch.dict(os.environ, env):
            if env_token is None:
                os.environ.pop("CARDTRADER_API_TOKEN", None)
            executor.migrate(AFTER)
        return executor.loader.project_state(AFTER).apps

    def make_users(self):
        User = self.apps.get_model("auth", "User")
        User.objects.create(pk=1, username="friend")
        User.objects.create(pk=2, username="admin", is_superuser=True, is_staff=True)
        User.objects.create(pk=3, username="admin2", is_superuser=True, is_staff=True)

    def make_data(self, api_token=""):
        a = self.apps
        card = a.get_model("ledger", "Card").objects.create(name="Counterspell", set_code="DMR")
        a.get_model("ledger", "CollectionItem").objects.create(card=card)
        a.get_model("ledger", "Order").objects.create(ct_id=1, code="c1")
        a.get_model("ledger", "ScanSession").objects.create()
        a.get_model("ledger", "SyncLog").objects.create(message="hello")
        a.get_model("ledger", "SyncService").objects.create(name="CardTrader", api_token=api_token)

    def test_existing_data_goes_to_the_first_superuser(self):
        self.make_users()
        self.make_data()
        apps = self.migrate()
        for model in ("CollectionItem", "Order", "ScanSession", "SyncLog"):
            with self.subTest(model=model):
                owners = set(apps.get_model("ledger", model).objects.values_list("owner_id", flat=True))
                self.assertEqual(owners, {2})
        service = apps.get_model("ledger", "SyncService").objects.get()
        self.assertEqual(service.user_id, 2)
        self.assertEqual(service.api_token, "env-tok")

    def test_stored_token_wins_over_env(self):
        self.make_users()
        self.make_data(api_token="db-tok")
        apps = self.migrate()
        self.assertEqual(apps.get_model("ledger", "SyncService").objects.get().api_token, "db-tok")

    def test_fails_without_superuser_when_data_exists(self):
        self.apps.get_model("auth", "User").objects.create(pk=1, username="friend")
        self.make_data()
        with self.assertRaisesMessage(Exception, "createsuperuser"):
            self.migrate()
        # leave the database migratable for tearDown
        self.apps.get_model("auth", "User").objects.filter(pk=1).update(is_superuser=True)

    def test_empty_database_migrates(self):
        apps = self.migrate(env_token=None)
        self.assertFalse(apps.get_model("ledger", "SyncService").objects.exists())


class OwnedByTest(TestCase):
    def setUp(self):
        User = get_user_model()
        self.a = User.objects.create_user("a")
        self.b = User.objects.create_user("b")
        self.rows = {user: self.make_rows(user, n) for n, user in enumerate((self.a, self.b), start=1)}

    def make_rows(self, user, n):
        card = Card.objects.create(name=f"Card {n}", set_code="DMR")
        item = CollectionItem.objects.create(card=card, owner=user)
        listing = Listing.objects.create(item=item, price=Decimal("1.00"))
        order = Order.objects.create(ct_id=n, code=f"c{n}", owner=user)
        order_item = OrderItem.objects.create(order=order, ct_id=n, name="x")
        session = ScanSession.objects.create(owner=user)
        line = ScanLine.objects.create(session=session, condition="NM")
        service = SyncService.objects.create(name="CardTrader", user=user)
        conflict = SyncConflict.objects.create(service=service, item=item, local_value="1", remote_value="2")
        log = SyncLog.objects.create(message=f"log {n}", owner=user)
        return {
            CollectionItem: item, Listing: listing, Order: order, OrderItem: order_item,
            ScanSession: session, ScanLine: line, SyncConflict: conflict, SyncLog: log,
        }

    def test_owned_by_returns_only_the_users_rows(self):
        for model in (CollectionItem, Listing, Order, OrderItem, ScanSession, ScanLine, SyncConflict):
            with self.subTest(model=model.__name__):
                self.assertEqual(list(model.objects.owned_by(self.a)), [self.rows[self.a][model]])

    def test_logs_visible_to_owner_and_global(self):
        shared = SyncLog.objects.create(message="global")
        visible = set(SyncLog.objects.visible_to(self.a))
        self.assertEqual(visible, {self.rows[self.a][SyncLog], shared})


class OwnerIsolationSweepTest(LedgerTestCase):
    """A friend reaching any of the admin's objects by id gets 404, on every URL that takes one."""

    # URL name → how to find one of self.user's objects for its argument
    TARGETS = {
        "api_item": lambda t: t.item.pk, "api_listing": lambda t: t.listing.item_id,
        "api_order": lambda t: t.order.pk, "api_order_ship": lambda t: t.order.pk,
        "api_resolve": lambda t: t.conflict.pk,
        "scan_api_session": lambda t: t.session.uuid, "scan_api_captures": lambda t: t.session.uuid,
        "scan_api_search": lambda t: t.session.uuid, "scan_api_manual": lambda t: t.session.uuid,
        "scan_api_review": lambda t: t.session.uuid,
        "scan_api_save": lambda t: t.session.uuid,
        "scan_api_line": lambda t: t.line.uuid, "scan_api_undo": lambda t: t.line.uuid,
        "scan_api_confirm": lambda t: t.line.uuid, "scan_api_frame": lambda t: t.line.uuid,
    }

    def setUp(self):
        super().setUp()
        self.item = CollectionItem.objects.owned_by(self.user).first()
        self.listing = Listing.objects.owned_by(self.user).first()
        self.order = Order.objects.owned_by(self.user).filter(state=Order.State.PAID).first()
        self.conflict = SyncConflict.objects.owned_by(self.user).first()
        self.session = ScanSession.objects.create(owner=self.user)
        self.line = ScanLine.objects.create(session=self.session, condition="NM", frame=b"jpeg")
        self.client.force_login(self.other)

    def test_every_object_url_is_covered(self):
        from .urls import urlpatterns

        with_args = {p.name for p in urlpatterns if p.pattern.converters}
        self.assertEqual(with_args - set(self.TARGETS), set(), "new URL with an object id: add it to TARGETS")

    def test_other_users_objects_are_404(self):
        for name, target in self.TARGETS.items():
            url = reverse(f"ledger:{name}", args=[target(self)])
            for method in ("get", "post", "delete"):
                with self.subTest(name=name, method=method):
                    self.assertIn(getattr(self.client, method)(url).status_code, (404, 405))
        # nothing changed
        self.assertTrue(CollectionItem.objects.filter(pk=self.item.pk).exists())
        self.assertEqual(self.session.lines.count(), 1)

    def test_some_method_answers_404_for_each_url(self):
        for name, target in self.TARGETS.items():
            url = reverse(f"ledger:{name}", args=[target(self)])
            statuses = {self.client.get(url).status_code, self.client.post(url).status_code,
                        self.client.put(url).status_code, self.client.patch(url).status_code}
            with self.subTest(name=name):
                self.assertIn(404, statuses)

    def test_list_pages_show_none_of_the_admins_cards(self):
        names = list(CollectionItem.objects.owned_by(self.user).values_list("card__name", flat=True))
        for name, params in (("api_collection", {}), ("api_listings", {}), ("api_catalog", {"owned": "1"}),
                             ("api_sync", {}), ("api_orders", {})):
            resp = self.client.get(reverse(f"ledger:{name}"), params)
            with self.subTest(page=name):
                self.assertEqual(resp.status_code, 200)
                for card in names:
                    self.assertNotContains(resp, card)
