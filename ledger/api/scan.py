"""The camera scan flow (/api/scan/), used by the scan screens of the React UI (/scan/...).

Authentication is the Django session; CSRF comes from the csrftoken cookie, sent back as X-CSRFToken.
"""

import logging

from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import status
from rest_framework.decorators import api_view, parser_classes
from rest_framework.parsers import MultiPartParser
from rest_framework.response import Response

from .. import catalog, recognition, scan, services
from ..cardtrader import CardTraderError
from ..models import Blueprint, Condition, Expansion, Language, ScanLine, ScanSession
from ..openrouter import OpenRouterError
from . import pages
from . import serializers as s
from .common import msg
from .errors import Blocked, Closed, ServiceError, ServiceMissing

SEARCH_LIMIT = 20
SESSION_LIST_LIMIT = 10
TAGS = ["scan"]
logger = logging.getLogger(__name__)


def _openrouter_configured():
    """OpenRouter is a server setting: when it is missing, say so in the server console, not to the user."""
    missing = [name for name, value in (("OPENROUTER_API_KEY", services.openrouter_key()),
                                        ("OPENROUTER_MODEL", services.openrouter_model())) if not value]
    if missing:
        logger.warning("OpenRouter is not configured: card recognition needs the %s environment variable%s.",
                       " and ".join(missing), "s" if len(missing) > 1 else "")
    return not missing


def _missing_services(user):
    """Connections to set up before scanning: [(message, profile link or "")].

    The catalog and prices come with the administrator's CardTrader token: only an administrator can fix it.
    """
    missing = []
    if not services.service_token():
        if user.is_superuser:
            missing.append(("CardTrader is not connected: the card catalog and prices need its API token.",
                            pages.PROFILE_CARDTRADER))
        else:
            missing.append(("Card catalog and prices are unavailable: ask the administrator to connect CardTrader.",
                            ""))
    return missing


def _search_expansion(code, session, warnings):
    """The expansion to search in: set=<code> (loaded on demand), else the session's, else None (all loaded)."""
    code = code.strip().lower()
    if code == "all":
        return None
    if code:
        expansion = catalog.expansion_by_code(code)
        if expansion is not None:
            try:
                with services.service_client() as client:
                    catalog.ensure_blueprints(client, expansion)
            except CardTraderError as exc:
                warnings.append(f"Could not load {expansion}: {exc}")
            return expansion
    return session.expansion


def _search(expansion, q):
    qs = Blueprint.objects.select_related("expansion")
    if expansion is not None:
        qs = qs.filter(expansion=expansion)
    q = q.strip()
    return list(qs.filter(name__icontains=q).order_by("name", "ct_id")[:SEARCH_LIMIT]) if q else []


def _choices(choices):
    return [{"code": code, "label": label} for code, label in choices]


def _validated(serializer_class, data, **kwargs):
    serializer = serializer_class(data=data, **kwargs)
    serializer.is_valid(raise_exception=True)
    return serializer.validated_data


def _get_session(user, uid, *, open_only=True):
    """The user's session (another user's answers 404, like a missing one)."""
    session = get_object_or_404(ScanSession.objects.owned_by(user).select_related("expansion"), uuid=uid)
    if open_only and session.status != ScanSession.Status.OPEN:
        raise Closed()
    return session


def _get_line(user, uid):
    line = get_object_or_404(
        ScanLine.objects.owned_by(user).select_related("session__expansion", "blueprint__expansion"), uuid=uid,
    )
    if line.session.status != ScanSession.Status.OPEN:
        raise Closed()
    return line


def _line_result(line, session):
    if line is not None:
        line.session = session
    return Response(s.LineResultSerializer({"line": line, "count": s.copies(session)}).data)


@extend_schema(tags=TAGS, responses=s.SetupSerializer,
               summary="Choices for a new session: expansions, conditions, languages and the last session's defaults")
@api_view(["GET"])
def setup(request):
    warnings = []
    if not Expansion.objects.exists() and services.service_token():
        try:
            with services.service_client() as client:
                catalog.sync_expansions(client)
        except CardTraderError as exc:
            warnings.append(f"Could not load the expansions from CardTrader: {exc}")
    last = ScanSession.objects.owned_by(request.user).order_by("-created_at", "-id").first()
    return Response(s.SetupSerializer({
        "expansions": Expansion.objects.all(),
        "conditions": _choices(Condition.choices),
        "languages": _choices(Language.choices),
        "defaults": {
            "expansion_id": last.expansion_id if last else None,
            "condition": last.default_condition if last else Condition.NEAR_MINT,
        },
        "missing": [{"message": message, "url": url} for message, url in _missing_services(request.user)],
        "warnings": warnings,
    }).data)


@extend_schema(methods=["GET"], tags=TAGS, responses=s.SessionListSerializer, operation_id="scan_sessions_list",
               summary=f"The user's open sessions, most recent activity first (at most {SESSION_LIST_LIMIT})")
@extend_schema(methods=["POST"], tags=TAGS, request=s.SessionCreateSerializer,
               responses={201: s.SessionCreatedSerializer},
               summary="Start a session, preloading the expansion's cards from CardTrader")
@api_view(["GET", "POST"])
def sessions(request):
    if request.method == "GET":
        listed = scan.open_sessions(request.user)[:SESSION_LIST_LIMIT]
        return Response(s.SessionListSerializer({"results": listed}, context={"request": request}).data)
    data = _validated(s.SessionCreateSerializer, request.data)
    expansion = data["expansion_id"]
    session = scan.start_session(request.user, expansion, data["condition"])
    warnings = []
    if expansion is not None:
        try:
            with services.service_client() as client:
                catalog.ensure_blueprints(client, expansion)
        except CardTraderError as exc:
            warnings.append(f"Could not preload {expansion} from CardTrader: {exc}")
    body = s.SessionCreatedSerializer({"session": session, "warnings": warnings}, context={"request": request})
    return Response(body.data, status=status.HTTP_201_CREATED)


@extend_schema(methods=["GET"], tags=TAGS, responses=s.SessionDetailSerializer, summary="A session, open or saved")
@extend_schema(methods=["DELETE"], tags=TAGS, responses={204: None},
               summary="Discard an open session with its cards; a saved one can't be discarded")
@api_view(["GET", "DELETE"])
def session_detail(request, uid):
    if request.method == "DELETE":
        try:
            scan.discard_session(_get_session(request.user, uid))
        except ScanSession.DoesNotExist:  # discarded by another request in the meantime
            raise Http404
        except scan.ScanError:  # saved by another request in the meantime
            raise Closed()
        return Response(status=status.HTTP_204_NO_CONTENT)
    session = _get_session(request.user, uid, open_only=False)
    return Response(s.SessionDetailSerializer({"session": session}, context={"request": request}).data)


def _capture_result(result, message, session, line=None, status_code=status.HTTP_200_OK):
    if line is not None:
        line.session = session
    body = {"result": result, "message": message, "line": line, "count": s.copies(session)}
    return Response(s.CaptureResultSerializer(body).data, status=status_code)


@extend_schema(tags=TAGS, request={"multipart/form-data": s.CaptureSerializer},
               responses={200: s.CaptureResultSerializer, 202: s.CaptureResultSerializer},
               summary="Recognise the card in a photo and add it to the session")
@api_view(["POST"])
@parser_classes([MultiPartParser])
def captures(request, uid):
    session = _get_session(request.user, uid)
    data = _validated(s.CaptureSerializer, request.data)
    if not _openrouter_configured():
        raise ServiceMissing("Card recognition is not available right now — try again later")
    missing = _missing_services(request.user)
    if missing:
        message, link = missing[0]
        raise ServiceMissing(message, link)

    capture_id = data["capture_id"]
    capture, created = scan.reserve_capture(session, capture_id) if capture_id else (None, True)
    if not created:
        if not capture.done:  # the first upload of this photo is still being recognised
            return _capture_result("busy", "Still recognising the previous photo…", session,
                                   status_code=status.HTTP_202_ACCEPTED)
        if capture.line is None:  # recognised, then undone or removed
            return _capture_result("handled", "Already handled", session)
        done = capture.line
    else:
        image = data["frame"].read()
        try:
            with services.make_openrouter() as model:
                rec = model.recognize_card(image)
            if not rec.name and not rec.collector_number:  # an empty table, a hand, a blurred frame
                scan.release_capture(capture)
                return _capture_result("no_card", "No card in the picture — frame a card", session)
            with services.service_client() as client:
                found = recognition.match(client, rec, session.expansion)
            done, _ = scan.add_capture(session, rec, found, frame=image, capture=capture)
        except (OpenRouterError, CardTraderError) as exc:
            scan.release_capture(capture)
            reason = str(exc)[:160]
            raise ServiceError(f"Couldn't recognise the card ({reason}) — take the photo again")
        except Exception:
            scan.release_capture(capture)
            raise

    if done.needs_confirmation:
        return _capture_result("confirm", "Which one is it?", session, done)
    copies = "copy" if done.quantity == 1 else "copies"
    return _capture_result("added", f"Added · {done.quantity} {copies}", session, done)


@extend_schema(tags=TAGS, parameters=[s.SearchQuerySerializer], responses=s.SearchResultSerializer,
               summary="Search cards by name, to add or confirm one by hand")
@api_view(["GET"])
def search(request, uid):
    session = _get_session(request.user, uid, open_only=False)
    query = _validated(s.SearchQuerySerializer, request.query_params)
    warnings = []
    where = _search_expansion(query["set"], session, warnings)
    return Response(s.SearchResultSerializer({
        "results": _search(where, query["q"]),
        "where": where.code if where is not None else None,
        "warnings": warnings,
    }).data)


@extend_schema(tags=TAGS, request=s.PickSerializer, responses=s.LineResultSerializer,
               summary="Add a card picked by hand")
@api_view(["POST"])
def manual(request, uid):
    session = _get_session(request.user, uid)
    data = _validated(s.PickSerializer, request.data)
    blueprint, language, foil = data["blueprint_id"], data["language"], data["foil"]
    return _line_result(scan.add_manual(session, blueprint, language, foil), session)


def _review(request, session):
    lines = list(session.lines.select_related("blueprint__expansion").order_by("needs_confirmation", "-updated_at"))
    for line in lines:
        line.session = session
    confirmed = [l for l in lines if not l.needs_confirmation]
    return Response(s.ReviewSerializer({
        "session": session,
        "lines": confirmed,
        "pending": [l for l in lines if l.needs_confirmation],
        "copies": sum(l.quantity for l in lines),
        "unique": len(confirmed),
    }, context={"request": request}).data)


@extend_schema(tags=TAGS, responses=s.ReviewSerializer, summary="The session's cards, before saving")
@api_view(["GET"])
def review(request, uid):
    return _review(request, _get_session(request.user, uid, open_only=False))


@extend_schema(tags=TAGS, request=None, responses=s.SaveResultSerializer,
               summary="Save the session's cards into the collection")
@api_view(["POST"])
def save(request, uid):
    session = _get_session(request.user, uid)
    try:
        saved = scan.save_session(session)
    except ScanSession.DoesNotExist:  # discarded by another request in the meantime
        raise Http404
    except scan.ScanError as exc:
        if "already saved" in str(exc):  # saved by another request in the meantime
            raise Closed()
        raise Blocked(str(exc))
    return Response(s.SaveResultSerializer({
        "saved": saved, "redirect": pages.COLLECTION,
        "messages": [msg("success", f"Saved {saved} cards.")],
    }).data)


@extend_schema(tags=TAGS, request=s.LineUpdateSerializer, responses=s.LineResultSerializer,
               summary="Change a line; quantity 0 removes it")
@api_view(["PATCH"])
def line_detail(request, uid):
    line = _get_line(request.user, uid)
    session = line.session
    changes = _validated(s.LineUpdateSerializer, request.data)
    return _line_result(scan.update_line(line, **changes), session)


@extend_schema(tags=TAGS, request=None, responses=s.LineResultSerializer,
               summary="Undo the last copy added to a line")
@api_view(["POST"])
def line_undo(request, uid):
    line = _get_line(request.user, uid)
    session = line.session
    scan.undo(line)
    survivor = ScanLine.objects.select_related("blueprint__expansion").filter(pk=line.pk).first() if line.pk else None
    return _line_result(survivor, session)


@extend_schema(tags=TAGS, request=s.ConfirmSerializer, responses=s.LineResultSerializer,
               summary="Confirm which card a line is; a line already confirmed is returned unchanged")
@api_view(["POST"])
def line_confirm(request, uid):
    line = _get_line(request.user, uid)
    session = line.session
    if not line.needs_confirmation:
        return _line_result(line, session)
    data = _validated(s.ConfirmSerializer, request.data)
    blueprint, language = data["blueprint_id"], data["language"]
    line.foil = data["foil"]
    line.condition = data["condition"] or session.default_condition
    return _line_result(scan.confirm_line(line, blueprint, language), session)


@extend_schema(tags=TAGS, responses={(200, "image/jpeg"): OpenApiResponse(OpenApiTypes.BINARY)},
               summary="The photo of a line that needs confirmation")
@api_view(["GET"])
def line_frame(request, uid):
    line = get_object_or_404(ScanLine.objects.owned_by(request.user), uuid=uid)
    if not line.frame:
        raise Http404
    return HttpResponse(bytes(line.frame), content_type="image/jpeg")
