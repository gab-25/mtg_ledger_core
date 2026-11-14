from unittest import mock

import httpx
from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from ledger import services
from ledger.cardtrader import CardTraderClient
from ledger.models import SyncLog, SyncService


class ProfileTest(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user("gab", email="gab@example.com", password="old-pass-123")
        User.objects.create_user("other", email="other@example.com", password="x")
        self.client.force_login(self.user)

    def put(self, name, data):
        return self.client.put(reverse(f"accounts:{name}"), data, content_type="application/json")

    def test_requires_login(self):
        self.client.logout()
        resp = self.client.get(reverse("accounts:api_me"))
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["code"], "not_authenticated")

    def test_me_returns_user_and_counts(self):
        data = self.client.get(reverse("accounts:api_me")).json()
        self.assertEqual(data["user"]["username"], "gab")
        self.assertEqual(data["user"]["full_name"], "")
        self.assertFalse(data["user"]["is_staff"])
        self.assertEqual(data["counts"], {"pending_changes": 0, "orders_to_ship": 0})

    def test_account_reports_cardtrader_not_connected(self):
        data = self.client.get(reverse("accounts:api_account")).json()
        self.assertEqual(data["cardtrader"]["name"], "CardTrader")
        self.assertFalse(data["cardtrader"]["token_saved"])
        self.assertNotIn("shares_token", data)
        self.assertIn("Your password must contain at least 8 characters.", data["password_help"])

    def test_update_details(self):
        resp = self.put("api_profile", {"first_name": "Gabriele", "last_name": "S", "email": "new@example.com"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["messages"], [{"level": "success", "text": "Profile updated."}])
        self.user.refresh_from_db()
        self.assertEqual(self.user.get_full_name(), "Gabriele S")
        self.assertEqual(self.user.email, "new@example.com")

    def test_email_must_be_unique(self):
        resp = self.put("api_profile", {"email": "OTHER@example.com"})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json(), {"email": ["This email is already used by another account."]})

    def test_change_password_keeps_session(self):
        resp = self.client.post(reverse("accounts:api_password"), {
            "old_password": "old-pass-123", "new_password1": "N3w-secret-pass!", "new_password2": "N3w-secret-pass!",
        }, content_type="application/json")
        self.assertEqual(resp.json()["messages"][0]["text"], "Password changed.")
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("N3w-secret-pass!"))
        self.assertEqual(self.client.get(reverse("accounts:api_me")).status_code, 200)  # still logged in

    def test_wrong_old_password(self):
        resp = self.client.post(reverse("accounts:api_password"), {
            "old_password": "nope", "new_password1": "N3w-secret-pass!", "new_password2": "N3w-secret-pass!",
        }, content_type="application/json")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("old_password", resp.json())
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("old-pass-123"))


class LoginTest(TestCase):
    def setUp(self):
        get_user_model().objects.create_user("gab", password="pw-123456")
        self.client = Client(enforce_csrf_checks=True)

    def csrf(self):
        token = self.client.cookies.get("csrftoken")
        return token.value if token else ""

    def login(self, password, csrf=True):
        headers = {"HTTP_X_CSRFTOKEN": self.csrf()} if csrf else {}
        return self.client.post(reverse("accounts:api_login"), {"username": "gab", "password": password},
                                content_type="application/json", **headers)

    def test_login_ok_sets_session(self):
        self.client.get(reverse("accounts:api_csrf"))
        resp = self.login("pw-123456")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.client.get(reverse("accounts:api_me")).json()["user"]["username"], "gab")

    def test_login_wrong_password(self):
        self.client.get(reverse("accounts:api_csrf"))
        resp = self.login("nope")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Please enter a correct username and password", resp.json()["non_field_errors"][0])

    def test_login_needs_csrf(self):
        self.client.get(reverse("accounts:api_csrf"))
        resp = self.login("pw-123456", csrf=False)
        self.assertEqual(resp.status_code, 403)

    def test_logout_ends_session(self):
        self.client.get(reverse("accounts:api_csrf"))
        self.login("pw-123456")
        resp = self.client.post(reverse("accounts:api_logout"), HTTP_X_CSRFTOKEN=self.csrf())
        self.assertEqual(resp.status_code, 204)
        self.assertEqual(self.client.get(reverse("accounts:api_me")).status_code, 403)

    def test_schema_documents_the_login_body(self):
        resp = self.client.get(reverse("api_schema"), HTTP_ACCEPT="application/vnd.oai.openapi+json")
        schema = resp.json()
        body = schema["paths"]["/api/auth/login/"]["post"]["requestBody"]["content"]["application/json"]["schema"]
        login = schema["components"]["schemas"][body["$ref"].rsplit("/", 1)[1]]
        self.assertEqual(set(login["properties"]), {"username", "password"})
        self.assertEqual(set(login["required"]), {"username", "password"})


CT_ACCOUNTS = {"good-token": 7, "other-good": 8}


def fake_client(token):
    def handler(request):
        token = request.headers["Authorization"].removeprefix("Bearer ")
        for prefix, user_id in CT_ACCOUNTS.items():
            if token.startswith(prefix):
                return httpx.Response(200, json={"id": 1, "name": "My shop", "user_id": user_id})
        return httpx.Response(401, json={"error_code": "unauthorized", "extra": {"message": "bad token"}})
    return CardTraderClient(token, transport=httpx.MockTransport(handler))


@mock.patch.object(services, "make_client", side_effect=fake_client)
class CardTraderTokenTest(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user("gab", password="pw")
        self.friend = User.objects.create_user("ami", password="pw")
        self.client.force_login(self.user)
        self.url = reverse("accounts:api_token")

    def connect(self, token, user=None):
        if user is not None:
            self.client.force_login(user)
        return self.client.put(self.url, {"token": token}, content_type="application/json")

    def account(self):
        return self.client.get(reverse("accounts:api_account")).json()

    def test_valid_token_is_saved_on_own_account(self, _):
        resp = self.connect("  good-token ")
        self.assertEqual(resp.json()["messages"], [{"level": "success", "text": "CardTrader connected (app “My shop”)."}])
        mine = services.get_cardtrader(self.user)
        self.assertEqual((mine.api_token, mine.ct_user_id), ("good-token", 7))
        self.assertEqual(services.get_cardtrader(self.friend).api_token, "")
        account = self.account()
        self.assertTrue(account["cardtrader"]["token_saved"])
        self.assertNotIn("good-token", str(account))  # never sent back
        self.assertTrue(SyncLog.objects.filter(owner=self.user, message__contains="API token connected").exists())

    def test_long_jwt_token_is_saved(self, _):
        token = "good-token." + "x" * 1200  # CardTrader tokens are JWTs, longer than 500 characters
        self.assertEqual(self.connect(token).status_code, 200)
        self.assertEqual(services.get_cardtrader(self.user).api_token, token)

    def test_rejected_token_is_not_saved(self, _):
        resp = self.connect("bad-token")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("CardTrader rejected this token", resp.json()["token"][0])
        self.assertEqual(services.get_cardtrader(self.user).api_token, "")

    def test_same_cardtrader_account_rejected_for_second_user(self, _):
        self.connect("good-token")
        resp = self.connect("good-token-2", user=self.friend)
        self.assertIn("already connected to another user", resp.json()["token"][0])
        self.assertEqual(services.get_cardtrader(self.friend).api_token, "")
        self.assertEqual(services.get_cardtrader(self.user).api_token, "good-token")

    def test_migrated_token_without_user_id_still_blocks_duplicates(self, _):
        mine = services.get_cardtrader(self.user)
        mine.api_token = "good-token"  # copied by the migration: its CardTrader user is not known yet
        mine.save()
        resp = self.connect("good-token-2", user=self.friend)
        self.assertIn("already connected to another user", resp.json()["token"][0])
        mine.refresh_from_db()
        self.assertEqual(mine.ct_user_id, 7)

    def test_reconnecting_own_account_is_allowed(self, _):
        self.connect("good-token")
        self.assertEqual(self.connect("good-token-new").status_code, 200)
        self.assertEqual(services.get_cardtrader(self.user).api_token, "good-token-new")

    def test_another_account_for_the_friend_is_fine(self, _):
        self.connect("good-token")
        self.connect("other-good", user=self.friend)
        self.assertEqual(services.get_cardtrader(self.friend).ct_user_id, 8)

    def test_remove_token(self, _):
        self.connect("good-token")
        self.connect("other-good", user=self.friend)
        resp = self.client.delete(self.url)
        self.assertEqual(resp.json()["messages"][0]["text"], "CardTrader token removed.")
        friend = services.get_cardtrader(self.friend)
        self.assertEqual((friend.api_token, friend.ct_user_id), ("", None))
        self.assertEqual(services.get_cardtrader(self.user).api_token, "good-token")
        self.assertFalse(self.account()["cardtrader"]["token_saved"])

    def test_get_returns_own_token_only(self, _):
        self.connect("good-token")
        self.connect("other-good", user=self.friend)
        self.assertEqual(self.client.get(self.url).json(), {"token": "other-good"})
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(self.url).json(), {"token": "good-token"})

    def test_get_without_token_is_404(self, _):
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 404)

    def test_get_requires_login(self, _):
        self.client.logout()
        self.assertIn(self.client.get(self.url).status_code, (401, 403))

    def test_admin_token_is_not_shared(self, _):
        admin = get_user_model().objects.create_superuser("boss", password="pw")
        self.connect("good-token", user=admin)
        self.assertNotIn("shares_token", self.account())

    def test_environment_token_is_not_used(self, _):
        with mock.patch.dict("os.environ", {"CARDTRADER_API_TOKEN": "env-token"}):
            self.assertFalse(self.account()["cardtrader"]["token_saved"])


class OpenRouterNotInProfileTest(TestCase):
    """OpenRouter is configured only through OPENROUTER_API_KEY, never from the profile."""

    def setUp(self):
        self.user = get_user_model().objects.create_user("gab", password="pw")
        self.client.force_login(self.user)

    def test_account_has_no_openrouter_section(self):
        self.assertNotIn("openrouter", str(self.client.get(reverse("accounts:api_account")).json()).lower())

    @override_settings(OPENROUTER_API_KEY="")
    def test_openrouter_key_cannot_be_saved_from_profile(self):
        self.client.put(reverse("accounts:api_profile"), {"openrouter_key": "sk-or-secret"},
                        content_type="application/json")
        self.assertEqual(services.openrouter_key(), "")
        self.assertFalse(SyncService.objects.filter(name="OpenRouter").exists())

    @override_settings(OPENROUTER_API_KEY="env-key")
    def test_openrouter_key_comes_from_environment(self):
        self.assertEqual(services.openrouter_key(), "env-key")
