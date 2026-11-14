import json
import tempfile
import tomllib
from datetime import timedelta
from decimal import Decimal
from io import StringIO
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import Client
from django.urls import NoReverseMatch, reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from . import catalog, services
from .api.scan import SESSION_LIST_LIMIT
from .api.serializers import MAX_FRAME_BYTES
from .forms import CatalogItemForm, ItemForm, ListingForm
from .cardtrader import CardTraderError
from .models import Blueprint, CollectionItem, Expansion, ScanCapture, ScanLine, ScanSession
from .openrouter import OpenRouterError
from .recognition import Recognition
from .tests import LedgerTestCase

IPHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148 Safari/604.1"
DESKTOP = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36"


class FakeRecognizer:
    def __init__(self):
        self.result = Recognition(name="Counterspell", set_code="dmr", collector_number="045", language="IT")
        self.error = None
        self.calls = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def recognize_card(self, image):
        self.calls += 1
        if self.error:
            raise self.error
        return self.result


class ScanApiBase(LedgerTestCase):
    def setUp(self):
        super().setUp()
        api = self.ct.client()
        catalog.sync_expansions(api)
        self.dmr = Expansion.objects.get(code="dmr")
        catalog.ensure_blueprints(api, self.dmr)
        self.recognizer = FakeRecognizer()
        patcher = mock.patch.object(services, "make_openrouter", mock.Mock(return_value=self.recognizer))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.set_keys(openrouter="or-key", cardtrader="ct-token")
        self.client.defaults["HTTP_USER_AGENT"] = IPHONE

    def set_keys(self, *, openrouter, cardtrader, model="test/model"):
        override = self.settings(OPENROUTER_API_KEY=openrouter, OPENROUTER_MODEL=model)
        override.enable()
        self.addCleanup(override.disable)
        service = services.get_cardtrader(self.user)
        service.api_token = cardtrader
        service.save()

    def call(self, method, name, *args, data=None, **extra):
        body = json.dumps(data) if data is not None else ""
        return getattr(self.client, method)(reverse(f"ledger:{name}", args=args), body,
                                            content_type="application/json", **extra)

    def start(self, expansion_id=None, condition="NM"):
        resp = self.call("post", "scan_api_sessions",
                         data={"expansion_id": expansion_id or self.dmr.pk, "condition": condition})
        self.assertEqual(resp.status_code, 201, resp.content)
        return ScanSession.objects.get(uuid=resp.json()["session"]["uuid"])

    def capture(self, session, capture_id="c1"):
        frame = SimpleUploadedFile("f.jpg", b"\xff\xd8jpeg", content_type="image/jpeg")
        return self.client.post(reverse("ledger:scan_api_captures", args=[session.uuid]),
                                {"frame": frame, "capture_id": capture_id})

    def assertError(self, resp, status, code):
        """A non-validation error: {"detail", "code", "help_url"?}."""
        self.assertEqual(resp.status_code, status, resp.content)
        body = resp.json()
        self.assertEqual(body["code"], code)
        self.assertIsInstance(body["detail"], str)
        return body

    def assertInvalid(self, resp, *fields):
        """A validation error: 400 with the messages of exactly these fields."""
        self.assertEqual(resp.status_code, 400, resp.content)
        body = resp.json()
        self.assertEqual(sorted(body), sorted(fields))
        for messages in body.values():
            self.assertTrue(messages and all(isinstance(m, str) for m in messages), body)
        return body


class SetupAndSessionTest(ScanApiBase):
    def test_setup_lists_expansions_and_choices(self):
        body = self.call("get", "scan_api_setup").json()
        self.assertIn({"id": self.dmr.pk, "code": "dmr", "name": "Dominaria Remastered"}, body["expansions"])
        self.assertIn({"code": "NM", "label": "Near Mint"}, body["conditions"])
        self.assertIn({"code": "IT", "label": "Italian"}, body["languages"])
        self.assertEqual(body["defaults"], {"expansion_id": None, "condition": "NM"})
        self.assertEqual((body["missing"], body["warnings"]), ([], []))

    def test_setup_defaults_follow_the_last_session(self):
        self.start(condition="EX")
        self.assertEqual(self.call("get", "scan_api_setup").json()["defaults"],
                         {"expansion_id": self.dmr.pk, "condition": "EX"})

    def test_setup_syncs_expansions_when_empty(self):
        Expansion.objects.all().delete()
        body = self.call("get", "scan_api_setup").json()
        self.assertTrue(any(e["code"] == "dmr" for e in body["expansions"]))

    def test_start_preloads_catalog(self):
        resp = self.call("post", "scan_api_sessions", data={"expansion_id": self.dmr.pk, "condition": "EX"})
        self.assertEqual(resp.status_code, 201)
        body = resp.json()
        session = ScanSession.objects.get(uuid=body["session"]["uuid"])
        self.assertEqual((session.expansion, session.default_condition), (self.dmr, "EX"))
        self.assertEqual(body["session"]["expansion"]["code"], "dmr")
        self.assertEqual((body["session"]["status"], body["session"]["count"], body["warnings"]), ("open", 0, []))
        self.assertNotIn("sale_threshold", body["session"])

    def test_start_all_expansions(self):
        resp = self.call("post", "scan_api_sessions", data={"expansion_id": None, "condition": "NM"})
        self.assertEqual(resp.status_code, 201)
        self.assertIsNone(resp.json()["session"]["expansion"])

    def test_start_unknown_expansion_is_invalid(self):
        self.assertInvalid(self.call("post", "scan_api_sessions", data={"expansion_id": 999999}), "expansion_id")

    def test_start_unknown_condition_is_invalid(self):
        self.assertInvalid(self.call("post", "scan_api_sessions", data={"condition": "XX"}), "condition")

    def test_start_defaults_to_all_expansions_near_mint(self):
        resp = self.call("post", "scan_api_sessions", data={})
        self.assertEqual(resp.status_code, 201)
        session = ScanSession.objects.get(uuid=resp.json()["session"]["uuid"])
        self.assertEqual((session.expansion, session.default_condition), (None, "NM"))

    def test_session_detail_shows_last_added_line(self):
        session = self.start()
        self.assertIsNone(self.call("get", "scan_api_session", session.uuid).json()["session"]["last_line"])
        self.capture(session)
        body = self.call("get", "scan_api_session", session.uuid).json()["session"]
        self.assertEqual(body["count"], 1)
        line = body["last_line"]
        self.assertEqual(line["blueprint"]["id"], 1201)
        self.assertEqual(line["blueprint"]["expansion"]["code"], "dmr")
        self.assertEqual((line["language"], line["condition"]), ("IT", "NM"))
        for gone in ("price", "for_sale", "listed"):
            self.assertNotIn(gone, line)

    def test_saved_session_is_still_readable(self):
        session = self.start()
        session.status = ScanSession.Status.SAVED
        session.save()
        resp = self.call("get", "scan_api_session", session.uuid)
        self.assertEqual(resp.json()["session"]["status"], "saved")

    def test_unknown_session_404(self):
        self.assertError(self.call("get", "scan_api_session", "00000000-0000-0000-0000-000000000000"),
                         404, "not_found")


class SessionListTest(ScanApiBase):
    def listed(self):
        resp = self.call("get", "scan_api_sessions")
        self.assertEqual(resp.status_code, 200, resp.content)
        return resp.json()["results"]

    def touch(self, session, when):
        ScanSession.objects.filter(pk=session.pk).update(updated_at=when)

    def test_empty(self):
        self.assertEqual(self.listed(), [])

    def test_lists_open_sessions_with_their_details(self):
        session = self.start()
        self.capture(session)
        [item] = self.listed()
        self.assertEqual(item["uuid"], str(session.uuid))
        self.assertEqual(item["status"], "open")
        self.assertEqual(item["expansion"]["code"], "dmr")
        self.assertEqual((item["default_condition"], item["count"]), ("NM", 1))
        self.assertEqual(item["last_line"]["blueprint"]["id"], 1201)
        self.assertIn("phone_url", item)
        self.assertTrue(item["created_at"] and item["updated_at"])

    def test_saved_sessions_are_excluded(self):
        kept, saved = self.start(), self.start()
        saved.status = ScanSession.Status.SAVED
        saved.save()
        self.assertEqual([i["uuid"] for i in self.listed()], [str(kept.uuid)])

    def test_other_users_sessions_are_excluded(self):
        mine = self.start()
        ScanSession.objects.create(owner=self.other, expansion=self.dmr)
        self.assertEqual([i["uuid"] for i in self.listed()], [str(mine.uuid)])

    def test_newest_activity_first(self):
        older, newer = self.start(), self.start()
        self.touch(older, timezone.now() - timedelta(hours=2))
        self.touch(newer, timezone.now() - timedelta(hours=1))
        self.assertEqual([i["uuid"] for i in self.listed()], [str(newer.uuid), str(older.uuid)])

    def test_scanning_a_card_counts_as_activity(self):
        scanned, idle = self.start(), self.start()
        self.touch(scanned, timezone.now() - timedelta(hours=2))
        self.touch(idle, timezone.now() - timedelta(hours=1))
        self.capture(scanned)
        listed = self.listed()
        self.assertEqual([i["uuid"] for i in listed], [str(scanned.uuid), str(idle.uuid)])
        line = ScanLine.objects.get(session=scanned)
        self.assertEqual(parse_datetime(listed[0]["updated_at"]), line.updated_at)

    def test_capped(self):
        for _ in range(SESSION_LIST_LIMIT + 2):
            ScanSession.objects.create(owner=self.user, expansion=self.dmr)
        self.assertEqual(len(self.listed()), SESSION_LIST_LIMIT)


class SessionDeleteTest(ScanApiBase):
    def test_discard_open_session_with_its_cards(self):
        session = self.start()
        self.capture(session)
        resp = self.call("delete", "scan_api_session", session.uuid)
        self.assertEqual(resp.status_code, 204, resp.content)
        self.assertFalse(ScanSession.objects.filter(pk=session.pk).exists())
        self.assertFalse(ScanLine.objects.filter(session_id=session.pk).exists())
        self.assertFalse(ScanCapture.objects.filter(session_id=session.pk).exists())
        self.assertEqual(self.call("get", "scan_api_sessions").json()["results"], [])

    def test_saved_session_cannot_be_discarded(self):
        session = self.start()
        session.status = ScanSession.Status.SAVED
        session.save()
        self.assertError(self.call("delete", "scan_api_session", session.uuid), 409, "closed")
        self.assertTrue(ScanSession.objects.filter(pk=session.pk).exists())

    def test_saved_while_discarding_is_409(self):
        session = self.start()
        ScanSession.objects.filter(pk=session.pk).update(status=ScanSession.Status.SAVED)
        with mock.patch("ledger.api.scan._get_session", return_value=session):
            self.assertError(self.call("delete", "scan_api_session", session.uuid), 409, "closed")
        self.assertTrue(ScanSession.objects.filter(pk=session.pk).exists())

    def test_saving_a_session_discarded_meanwhile_is_404(self):
        session = self.start()
        ScanSession.objects.filter(pk=session.pk).delete()
        with mock.patch("ledger.api.scan._get_session", return_value=session):
            self.assertError(self.call("post", "scan_api_save", session.uuid), 404, "not_found")

    def test_other_users_session_is_404(self):
        theirs = ScanSession.objects.create(owner=self.other, expansion=self.dmr)
        self.assertError(self.call("delete", "scan_api_session", theirs.uuid), 404, "not_found")
        self.assertTrue(ScanSession.objects.filter(pk=theirs.pk).exists())


class CaptureTest(ScanApiBase):
    def test_capture_adds_line(self):
        resp = self.capture(self.start())
        body = resp.json()
        self.assertEqual((body["result"], body["message"], body["count"]), ("added", "Added · 1 copy", 1))
        self.assertEqual(body["line"]["blueprint"]["name"], "Counterspell")

    def test_capture_retry_same_id(self):
        session = self.start()
        self.capture(session, "same")
        body = self.capture(session, "same").json()
        self.assertEqual((body["result"], body["count"]), ("added", 1))
        self.assertEqual(self.recognizer.calls, 1)

    def test_retry_after_merge_adds_once(self):
        session = self.start()
        self.capture(session, "a")
        self.capture(session, "b")
        body = self.capture(session, "b").json()
        self.assertEqual(body["count"], 2)
        self.assertEqual(body["message"], "Added · 2 copies")
        self.assertEqual(ScanLine.objects.get(session=session).quantity, 2)
        self.assertEqual(self.recognizer.calls, 2)

    def test_capture_in_progress_is_busy(self):
        session = self.start()
        ScanCapture.objects.create(session=session, capture_id="busy")
        resp = self.capture(session, "busy")
        self.assertEqual(resp.status_code, 202)
        self.assertEqual(resp.json()["result"], "busy")
        self.assertIn("Still recognising", resp.json()["message"])
        self.assertEqual(self.recognizer.calls, 0)

    def test_undone_capture_is_handled(self):
        session = self.start()
        line_uuid = self.capture(session, "u").json()["line"]["uuid"]
        self.call("post", "scan_api_undo", line_uuid)
        body = self.capture(session, "u").json()
        self.assertEqual((body["result"], body["line"], body["count"]), ("handled", None, 0))

    def test_failed_capture_can_be_retried_with_same_id(self):
        session = self.start()
        self.recognizer.error = OpenRouterError("down")
        self.assertError(self.capture(session, "x"), 502, "service_error")
        self.recognizer.error = None
        self.assertEqual(self.capture(session, "x").json()["message"], "Added · 1 copy")

    def test_capture_error_shows_the_reason(self):
        session = self.start()
        self.recognizer.error = OpenRouterError("OpenRouter failed (429): rate limited")
        body = self.assertError(self.capture(session), 502, "service_error")
        self.assertIn("rate limited", body["detail"])
        self.assertIn("take the photo again", body["detail"])
        self.assertEqual(ScanLine.objects.count(), 0)

    def test_photo_without_a_card_adds_nothing(self):
        session = self.start()
        self.recognizer.result = Recognition()
        body = self.capture(session, "empty").json()
        self.assertEqual((body["result"], body["line"], body["count"]), ("no_card", None, 0))
        self.assertIn("No card in the picture", body["message"])
        self.assertEqual(ScanLine.objects.count(), 0)
        self.assertFalse(ScanCapture.objects.filter(capture_id="empty").exists())

    def test_missing_photo(self):
        session = self.start()
        resp = self.client.post(reverse("ledger:scan_api_captures", args=[session.uuid]), {"capture_id": "x"})
        self.assertInvalid(resp, "frame")

    def test_oversized_photo(self):
        session = self.start()
        frame = SimpleUploadedFile("f.jpg", b"\xff" * (MAX_FRAME_BYTES + 1), content_type="image/jpeg")
        resp = self.client.post(reverse("ledger:scan_api_captures", args=[session.uuid]), {"frame": frame})
        self.assertInvalid(resp, "frame")
        self.assertEqual(self.recognizer.calls, 0)

    def test_capture_needs_multipart(self):
        session = self.start()
        self.assertError(self.call("post", "scan_api_captures", session.uuid, data={}), 415, "unsupported_media_type")

    def test_capture_on_closed_session_409(self):
        session = self.start()
        session.status = ScanSession.Status.SAVED
        session.save()
        self.assertError(self.capture(session), 409, "closed")
        self.assertEqual(self.recognizer.calls, 0)


class MissingServicesTest(ScanApiBase):
    def test_capture_without_openrouter_key_logs_it_on_the_server(self):
        session = self.start()
        self.set_keys(openrouter="", cardtrader="ct-token")
        with self.assertLogs("ledger.api.scan", "WARNING") as logs:
            resp = self.capture(session)
        self.assertIn("OPENROUTER_API_KEY", logs.output[0])
        body = self.assertError(resp, 503, "service_missing")
        self.assertEqual(body["detail"], "Card recognition is not available right now — try again later")
        self.assertNotIn("help_url", body)
        self.assertEqual((self.recognizer.calls, ScanLine.objects.count()), (0, 0))

    def test_capture_without_openrouter_model_logs_it_on_the_server(self):
        session = self.start()
        self.set_keys(openrouter="or-key", cardtrader="ct-token", model="")
        with self.assertLogs("ledger.api.scan", "WARNING") as logs:
            resp = self.capture(session)
        self.assertIn("OPENROUTER_MODEL", logs.output[0])
        body = self.assertError(resp, 503, "service_missing")
        self.assertEqual(body["detail"], "Card recognition is not available right now — try again later")
        self.assertEqual((self.recognizer.calls, ScanLine.objects.count()), (0, 0))

    def test_capture_without_cardtrader_token_says_so(self):
        session = self.start()
        self.set_keys(openrouter="or-key", cardtrader="")
        body = self.assertError(self.capture(session), 503, "service_missing")
        self.assertIn("CardTrader is not connected", body["detail"])
        self.assertEqual(body["help_url"], "/account/#cardtrader")
        self.assertEqual(self.recognizer.calls, 0)

    def test_friend_scans_with_admin_token(self):
        self.client.force_login(self.other)  # no CardTrader token of their own
        self.assertEqual(self.call("get", "scan_api_setup").json()["missing"], [])
        session = self.start()
        self.assertEqual(self.capture(session).json()["result"], "added")

    def test_capture_without_service_token_tells_friend_to_ask_admin(self):
        self.client.force_login(self.other)
        session = self.start()
        self.set_keys(openrouter="or-key", cardtrader="")
        body = self.assertError(self.capture(session), 503, "service_missing")
        self.assertIn("ask the administrator to connect CardTrader", body["detail"])
        self.assertNotIn("help_url", body)
        self.assertEqual(self.recognizer.calls, 0)

    def test_session_start_does_not_call_info(self):
        self.start()
        self.assertNotIn(("GET", "/info"), self.ct.calls)

    def test_setup_lists_missing_services(self):
        self.set_keys(openrouter="", cardtrader="")
        Expansion.objects.all().delete()
        no_token = CardTraderError("CardTrader API token is not configured.")
        with mock.patch.object(services, "make_client", side_effect=no_token), \
                self.assertNoLogs("ledger.api.scan", "WARNING"):
            body = self.call("get", "scan_api_setup").json()
        self.assertEqual(body["warnings"], [])
        self.assertEqual(len(body["missing"]), 1)
        self.assertIn("CardTrader is not connected", body["missing"][0]["message"])
        self.assertEqual(body["missing"][0]["url"], "/account/#cardtrader")
        self.assertNotIn("OpenRouter", json.dumps(body))


class ConfirmTest(ScanApiBase):
    def uncertain(self, session):
        self.recognizer.result = Recognition(name="Counterspell", language="")
        return self.capture(session).json()

    def test_uncertain_capture_then_confirm(self):
        session = self.start()
        body = self.uncertain(session)
        self.assertEqual(body["result"], "confirm")
        line = body["line"]
        self.assertTrue(line["needs_confirmation"])
        self.assertEqual([c["id"] for c in line["candidates"]], [1201])
        frame = self.client.get(line["frame_url"])
        self.assertEqual(frame["Content-Type"], "image/jpeg")
        resp = self.call("post", "scan_api_confirm", line["uuid"],
                         data={"blueprint_id": 1201, "language": "IT", "condition": "EX", "foil": True})
        confirmed = resp.json()["line"]
        self.assertFalse(confirmed["needs_confirmation"])
        self.assertEqual((confirmed["language"], confirmed["condition"], confirmed["foil"]), ("IT", "EX", True))
        self.assertIsNone(confirmed["frame_url"])
        self.assertEqual(resp.json()["count"], 1)

    def test_candidates_say_which_match_the_name_read(self):
        session = self.start()
        dmr = Expansion.objects.get(code="dmr")
        Blueprint.objects.create(ct_id=1299, expansion=dmr, name="Ponder", collector_number="45")
        self.recognizer.result = Recognition(name="Lotus Petal", set_code="dmr", collector_number="045", language="EN")
        line = self.capture(session).json()["line"]
        self.assertEqual(line["read_name"], "Lotus Petal")
        self.assertEqual([(c["id"], c["matches_name"]) for c in line["candidates"]], [(1201, False), (1299, False)])
        self.recognizer.result = Recognition(name="Counterspell", set_code="dmr", collector_number="45", language="")
        line = self.capture(session, "c2").json()["line"]
        self.assertEqual([(c["id"], c["matches_name"]) for c in line["candidates"]], [(1201, True)])

    def test_confirm_needs_card_and_language(self):
        session = self.start()
        line = self.uncertain(session)["line"]
        self.assertInvalid(self.call("post", "scan_api_confirm", line["uuid"], data={"blueprint_id": 1201}),
                           "language")
        self.assertInvalid(self.call("post", "scan_api_confirm", line["uuid"], data={"blueprint_id": 999999,
                                                                                     "language": "IT"}),
                           "blueprint_id")

    def test_confirmed_line_is_returned_unchanged(self):
        session = self.start()
        line = self.capture(session).json()["line"]
        resp = self.call("post", "scan_api_confirm", line["uuid"], data={"blueprint_id": 1301, "language": "EN"})
        self.assertEqual(resp.json()["line"], line)

    def test_save_blocked_while_cards_need_confirmation(self):
        session = self.start()
        self.uncertain(session)
        body = self.assertError(self.call("post", "scan_api_save", session.uuid), 409, "blocked")
        self.assertEqual(body["detail"], "Some cards still need confirmation.")
        review = self.call("get", "scan_api_review", session.uuid).json()
        self.assertEqual((len(review["pending"]), len(review["lines"])), (1, 0))


class SearchTest(ScanApiBase):
    def test_manual_search_adds_line(self):
        session = self.start()
        body = self.client.get(reverse("ledger:scan_api_search", args=[session.uuid]), {"q": "counter"}).json()
        self.assertEqual([b["name"] for b in body["results"]], ["Counterspell"])
        self.assertEqual(body["where"], "dmr")
        resp = self.call("post", "scan_api_manual", session.uuid,
                         data={"blueprint_id": 1201, "language": "EN", "foil": False})
        self.assertEqual(resp.json()["count"], 1)
        self.assertEqual(ScanLine.objects.get(session=session).quantity, 1)

    def test_manual_needs_card_and_language(self):
        session = self.start()
        self.assertInvalid(self.call("post", "scan_api_manual", session.uuid, data={"blueprint_id": 1201}),
                           "language")

    def test_search_other_expansion_loads_it(self):
        session = self.start()  # DMR locked
        resp = self.client.get(reverse("ledger:scan_api_search", args=[session.uuid]), {"q": "sheoldred", "set": "dmu"})
        body = resp.json()
        self.assertEqual([b["name"] for b in body["results"]], ["Sheoldred, the Apocalypse"])
        self.assertEqual(body["where"], "dmu")

    def test_search_all_expansions(self):
        session = self.start()
        resp = self.client.get(reverse("ledger:scan_api_search", args=[session.uuid]), {"q": "counter", "set": "all"})
        self.assertIsNone(resp.json()["where"])

    def test_search_reports_load_failures(self):
        session = self.start()
        self.ct.fail_export.add(13)
        resp = self.client.get(reverse("ledger:scan_api_search", args=[session.uuid]), {"q": "x", "set": "dmu"})
        self.assertTrue(resp.json()["warnings"])


class PhoneTest(ScanApiBase):
    def test_desktop_gets_the_phone_url_of_this_session(self):
        session = self.start()
        body = self.call("get", "scan_api_session", session.uuid, HTTP_USER_AGENT=DESKTOP, secure=True).json()
        data = body["session"]
        self.assertFalse(data["is_phone"])
        self.assertEqual(data["phone_url"], f"https://testserver/scan/{session.uuid}/")
        self.assertNotIn("phone_qr_svg", data)
        self.assertTrue(data["phone_secure"])

    def test_desktop_qr_warns_when_not_https(self):
        session = self.start()
        data = self.call("get", "scan_api_session", session.uuid, HTTP_USER_AGENT=DESKTOP).json()["session"]
        self.assertFalse(data["phone_secure"])

    def test_android_is_a_phone(self):
        session = self.start()
        android = "Mozilla/5.0 (Linux; Android 15; Pixel 9) AppleWebKit/537.36 Chrome/140.0 Mobile Safari/537.36"
        data = self.call("get", "scan_api_session", session.uuid, HTTP_USER_AGENT=android).json()["session"]
        self.assertTrue(data["is_phone"])


class ReviewTest(ScanApiBase):
    def test_save(self):
        session = self.start()
        self.capture(session)
        body = self.call("get", "scan_api_review", session.uuid).json()
        self.assertEqual((body["copies"], body["unique"]), (1, 1))
        self.assertNotIn("listed", body)
        resp = self.call("post", "scan_api_save", session.uuid)
        self.assertEqual(resp.json(), {"saved": 1, "redirect": "/", "messages": [
            {"level": "success", "text": "Saved 1 cards."}]})
        self.assertTrue(CollectionItem.objects.filter(card__ct_blueprint_id=1201).exists())

    def test_threshold_endpoint_is_gone(self):
        with self.assertRaises(NoReverseMatch):
            reverse("ledger:scan_api_threshold", args=["00000000-0000-0000-0000-000000000000"])

    def test_closed_session_rejects_changes(self):
        session = self.start()
        line = self.capture(session).json()["line"]
        self.call("post", "scan_api_save", session.uuid)
        self.assertError(self.call("post", "scan_api_save", session.uuid), 409, "closed")
        self.assertError(self.call("post", "scan_api_manual", session.uuid,
                                   data={"blueprint_id": 1201, "language": "EN"}), 409, "closed")
        self.assertError(self.call("patch", "scan_api_line", line["uuid"], data={"foil": True}), 409, "closed")
        self.assertError(self.call("post", "scan_api_undo", line["uuid"]), 409, "closed")
        self.assertError(self.call("post", "scan_api_confirm", line["uuid"], data={}), 409, "closed")
        self.assertEqual(self.call("get", "scan_api_review", session.uuid).status_code, 200)


class LineTest(ScanApiBase):
    def setUp(self):
        super().setUp()
        self.session = self.start()
        self.line = self.capture(self.session).json()["line"]

    def patch(self, data):
        return self.call("patch", "scan_api_line", self.line["uuid"], data=data)

    def test_patch_fields(self):
        body = self.patch({"foil": True, "condition": "EX", "language": "en", "quantity": 3}).json()
        line = body["line"]
        self.assertEqual((line["foil"], line["condition"], line["language"], line["quantity"]),
                         (True, "EX", "EN", 3))
        self.assertEqual(body["count"], 3)

    def test_patch_quantity_zero_deletes(self):
        self.assertEqual(self.patch({"quantity": 0}).json(), {"line": None, "count": 0})

    def test_patch_validation(self):
        for data, fields in (({"foil": "maybe"}, ["foil"]), ({"condition": "XX"}, ["condition"]),
                             ({"language": "KL"}, ["language"]), ({"quantity": -1}, ["quantity"]),
                             ({"quantity": "two"}, ["quantity"]), ({"quantity": True}, ["quantity"]),
                             ({"for_sale": False}, ["for_sale"]), ({"price": "1"}, ["price"]),
                             ({"foil": True, "price": "1"}, ["price"]), ({}, ["non_field_errors"]),
                             (["foil"], ["non_field_errors"])):
            with self.subTest(data=data):
                self.assertInvalid(self.patch(data), *fields)
        self.assertError(self.client.patch(reverse("ledger:scan_api_line", args=[self.line["uuid"]]), "{",
                                           content_type="application/json"), 400, "parse_error")
        line = ScanLine.objects.get(uuid=self.line["uuid"])
        self.assertEqual((line.foil, line.condition, line.quantity), (False, "NM", 1))

    def test_undo(self):
        self.capture(self.session, "c2")
        body = self.call("post", "scan_api_undo", self.line["uuid"]).json()
        self.assertEqual((body["line"]["quantity"], body["count"]), (1, 1))
        self.assertEqual(self.call("post", "scan_api_undo", self.line["uuid"]).json(), {"line": None, "count": 0})
        self.assertError(self.call("post", "scan_api_undo", self.line["uuid"]), 404, "not_found")


class ProtocolTest(ScanApiBase):
    def test_403_without_login(self):
        self.client.logout()
        session = ScanSession.objects.create(owner=self.user)
        for resp in (self.call("get", "scan_api_setup"),
                     self.call("post", "scan_api_sessions", data={}),
                     self.call("get", "scan_api_review", session.uuid),
                     self.client.get(reverse("ledger:scan_api_frame", args=[session.uuid]))):
            self.assertError(resp, 403, "not_authenticated")

    def test_403_without_csrf(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        resp = client.post(reverse("ledger:scan_api_sessions"), "{}", content_type="application/json")
        self.assertError(resp, 403, "permission_denied")
        client.get(reverse("accounts:api_csrf"))
        token = client.cookies["csrftoken"].value
        resp = client.post(reverse("ledger:scan_api_sessions"), "{}", content_type="application/json",
                           HTTP_X_CSRFTOKEN=token)
        self.assertEqual(resp.status_code, 201)

    def test_405_on_wrong_method(self):
        session = self.start()
        resp = self.call("delete", "scan_api_sessions")
        self.assertError(resp, 405, "method_not_allowed")
        self.assertEqual(set(resp["Allow"].split(", ")), {"GET", "POST", "OPTIONS"})
        self.assertError(self.call("post", "scan_api_review", session.uuid), 405, "method_not_allowed")


class SchemaTest(LedgerTestCase):
    def test_schema_describes_the_scan_api(self):
        resp = self.client.get(reverse("api_schema"), HTTP_ACCEPT="application/vnd.oai.openapi+json")
        self.assertEqual(resp.status_code, 200)
        schema = json.loads(resp.content)
        self.assertIn("/api/scan/sessions/{uid}/captures/", schema["paths"])
        self.assertIn("Line", schema["components"]["schemas"])

    def test_page_api_bodies_and_filters_are_documented(self):
        resp = self.client.get(reverse("api_schema"), HTTP_ACCEPT="application/vnd.oai.openapi+json")
        schema = json.loads(resp.content)
        components = schema["components"]["schemas"]

        def body(path, method):
            content = schema["paths"][path][method].get("requestBody", {}).get("content", {})
            return {kind: set(components[c["schema"]["$ref"].rsplit("/", 1)[1]]["properties"])
                    for kind, c in content.items()}

        listing = set(ListingForm.base_fields)
        self.assertEqual(body("/api/collection/", "post")["application/json"], set(CatalogItemForm.base_fields))
        self.assertEqual(body("/api/collection/{id}/", "put")["application/json"], set(ItemForm.base_fields))
        self.assertIn("blueprint", CatalogItemForm.base_fields)
        self.assertNotIn("name", ItemForm.base_fields)
        self.assertEqual(body("/api/collection/import/", "post"), {"multipart/form-data": {"file"}})
        self.assertEqual(body("/api/listings/{item_pk}/", "put")["application/json"], listing)
        self.assertEqual(body("/api/orders/{id}/ship/", "post")["application/json"], {"tracking_code", "save_tracking"})
        self.assertEqual(body("/api/sync/conflicts/{id}/resolve/", "post")["application/json"], {"keep"})
        self.assertEqual(body("/api/account/profile/", "put")["application/json"], {"first_name", "last_name", "email"})
        self.assertEqual(body("/api/account/password/", "post")["application/json"],
                         {"old_password", "new_password1", "new_password2"})
        self.assertEqual(body("/api/account/cardtrader-token/", "put")["application/json"], {"token"})
        for path in ("/api/auth/logout/", "/api/listings/publish/",
                     "/api/orders/import/", "/api/sync/run/"):
            self.assertEqual(body(path, "post"), {}, path)

        def query(path):
            return {p["name"] for p in schema["paths"][path]["get"].get("parameters", []) if p["in"] == "query"}

        self.assertEqual(query("/api/collection/"), {"q", "foil", "set", "cond", "category", "page"})
        self.assertEqual(query("/api/listings/"), {"status"})
        self.assertEqual(query("/api/orders/"), {"tab"})
        self.assertEqual(query("/api/catalog/"), {"q", "set", "number", "category", "owned", "page"})
        self.assertNotIn("parameters", schema["paths"]["/api/collection/"]["post"])

    def test_schema_version_is_the_project_version(self):
        resp = self.client.get(reverse("api_schema"), HTTP_ACCEPT="application/vnd.oai.openapi+json")
        pyproject = tomllib.loads((settings.BASE_DIR / "pyproject.toml").read_text())
        self.assertEqual(json.loads(resp.content)["info"]["version"], pyproject["project"]["version"])

    def test_schema_generates_without_warnings(self):
        out, err = StringIO(), StringIO()
        call_command("spectacular", "--validate", "--fail-on-warn", stdout=out, stderr=err)
        self.assertEqual(err.getvalue(), "")

    def test_docs_are_public(self):
        self.client.logout()
        self.assertEqual(self.client.get(reverse("api_schema")).status_code, 200)
        self.assertContains(self.client.get(reverse("api_docs")), "swagger")


class ScanIsolationTest(ScanApiBase):
    """Another user's sessions and lines answer 404, as if they did not exist."""

    def test_other_users_session_and_lines_are_404(self):
        session = self.start()
        self.recognizer.result = Recognition(name="Counterspell", language="")
        line = ScanLine.objects.get(uuid=self.capture(session).json()["line"]["uuid"])
        self.client.force_login(self.other)
        frame = SimpleUploadedFile("f.jpg", b"\xff\xd8jpeg", content_type="image/jpeg")
        session_calls = [
            ("get", "scan_api_session", None), ("get", "scan_api_review", None),
            ("get", "scan_api_search", None), ("post", "scan_api_manual", {}),
            ("post", "scan_api_save", None),
        ]
        for method, name, data in session_calls:
            with self.subTest(name=name):
                self.assertError(self.call(method, name, session.uuid, data=data), 404, "not_found")
        with self.subTest(name="scan_api_captures"):
            resp = self.client.post(reverse("ledger:scan_api_captures", args=[session.uuid]),
                                    {"frame": frame, "capture_id": "x"})
            self.assertError(resp, 404, "not_found")
        line_calls = [
            ("patch", "scan_api_line", {"quantity": 3}), ("post", "scan_api_undo", None),
            ("post", "scan_api_confirm", {}),
        ]
        for method, name, data in line_calls:
            with self.subTest(name=name):
                self.assertError(self.call(method, name, line.uuid, data=data), 404, "not_found")
        with self.subTest(name="scan_api_frame"):
            self.assertEqual(self.client.get(reverse("ledger:scan_api_frame", args=[line.uuid])).status_code, 404)
        line.refresh_from_db()
        self.assertEqual(line.quantity, 1)
        self.assertEqual(self.recognizer.calls, 1)

    def test_setup_defaults_come_from_own_last_session(self):
        self.start(condition="LP")
        self.client.force_login(self.other)
        self.assertEqual(self.call("get", "scan_api_setup").json()["defaults"]["condition"], "NM")

    def test_new_session_belongs_to_the_user(self):
        self.client.force_login(self.other)
        self.assertEqual(self.start().owner, self.other)
