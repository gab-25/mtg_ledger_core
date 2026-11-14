"""CardTrader tokens are personal: nothing in the Ledger shares an administrator's token."""

from django.contrib.auth import get_user_model
from django.test import TestCase

from . import services


class PersonalTokenTest(TestCase):
    def setUp(self):
        User = get_user_model()
        self.admin = User.objects.create_superuser("admin", password="pw")
        self.friend = User.objects.create_user("friend", password="pw")

    def give(self, user, token):
        service = services.get_cardtrader(user)
        service.api_token = token
        service.save()

    def test_there_is_no_shared_token(self):
        for name in ("service_token", "service_client", "service_account_user"):
            self.assertFalse(hasattr(services, name), name)

    def test_user_token_is_the_users_own(self):
        self.give(self.admin, "admin-token")
        self.assertEqual(services.user_token(self.friend), "")
        self.give(self.friend, "u")
        self.assertEqual(services.user_token(self.friend), "u")
