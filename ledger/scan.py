"""Camera scan sessions: the draft list of scanned cards and saving it into the collection."""

from django.db import IntegrityError, transaction
from django.db.models import Max, Q
from django.db.models.functions import Greatest

from . import catalog
from .models import CollectionItem, ScanCapture, ScanLine, ScanSession, SyncLog

class ScanError(Exception):
    pass


def start_session(owner, expansion, condition):
    """New session for owner."""
    return ScanSession.objects.create(owner=owner, expansion=expansion, default_condition=condition)


def open_sessions(owner):
    """owner's open sessions, most recent activity first, annotated with last_activity.

    Adding or changing a card touches only its line, so the activity is the latest of the session's and its lines'.
    """
    return (ScanSession.objects.owned_by(owner).filter(status=ScanSession.Status.OPEN)
            .annotate(last_activity=Greatest("updated_at", Max("lines__updated_at")))
            .select_related("expansion").order_by("-last_activity", "-id"))


def _twin(line):
    """Another confirmed line of the session with the same printing/language/foil/condition."""
    return (
        line.session.lines.filter(
            needs_confirmation=False, blueprint=line.blueprint, language=line.language,
            foil=line.foil, condition=line.condition,
        )
        .exclude(pk=line.pk)
        .first()
    )


def _merge_or_save(line):
    """Fold a confirmed line into its twin if there is one. Returns the surviving line."""
    twin = _twin(line) if not line.needs_confirmation else None
    if twin is None:
        line.save()
        return line
    twin.quantity += line.quantity
    twin.save(update_fields=["quantity", "updated_at"])
    if line.pk:
        ScanCapture.objects.filter(line=line).update(line=twin)
        line.delete()
    return twin


def _add_certain(session, blueprint, language, foil):
    line = ScanLine(
        session=session, blueprint=blueprint, language=language, foil=foil, condition=session.default_condition,
    )
    twin = _twin(line)
    if twin is not None:
        twin.quantity += 1
        twin.save(update_fields=["quantity", "updated_at"])
        return twin, True
    line.save()
    return line, False


def reserve_capture(session, capture_id):
    """Claim a capture id. Returns (capture, created); created=False means it was seen before."""
    try:
        with transaction.atomic():
            return ScanCapture.objects.create(session=session, capture_id=capture_id), True
    except IntegrityError:
        return ScanCapture.objects.select_related("line").get(session=session, capture_id=capture_id), False


def release_capture(capture):
    """Forget a capture that failed, so the phone can retry it with the same id."""
    if capture is not None and not capture.done:
        capture.delete()


def add_capture(session, rec, m, *, frame, capture_id="", capture=None):
    """Add what one camera capture recognised. Returns (line, merged); a repeated capture id changes nothing."""
    if capture is None and capture_id:
        capture, created = reserve_capture(session, capture_id)
        if not created:
            return capture.line, False
    if m.blueprint is not None:
        line, merged = _add_certain(session, m.blueprint, rec.language, rec.foil)
    else:
        line = ScanLine.objects.create(
            session=session, language=rec.language, foil=rec.foil, condition=session.default_condition,
            needs_confirmation=True, candidates=[b.ct_id for b in m.candidates], frame=frame,
            read_name=rec.name.strip()[:200],
        )
        merged = False
    if capture is not None:
        capture.line, capture.done = line, True
        capture.save(update_fields=["line", "done"])
    return line, merged


def add_manual(session, blueprint, language, foil):
    """The Search button: add one copy of a printing picked by hand."""
    line, _ = _add_certain(session, blueprint, language, foil)
    return line


def undo(line):
    if line.quantity > 1:
        line.quantity -= 1
        line.save(update_fields=["quantity", "updated_at"])
    else:
        line.delete()


def update_line(line, *, foil=None, condition=None, language=None, quantity=None):
    """Edit a draft line from the toast or the review screen. Returns the surviving line (None if deleted)."""
    if quantity is not None and quantity <= 0:
        line.delete()
        return None
    key = (line.language, line.foil, line.condition)
    if foil is not None:
        line.foil = foil
    if condition is not None:
        line.condition = condition
    if language is not None:
        line.language = language
    if quantity is not None:
        line.quantity = quantity
    if (line.language, line.foil, line.condition) != key and not line.needs_confirmation:
        return _merge_or_save(line)
    line.save()
    return line


def confirm_line(line, blueprint, language):
    """Resolve an uncertain line with the printing and language the owner picked."""
    line.blueprint = blueprint
    line.language = language
    line.needs_confirmation = False
    line.frame = None
    line.candidates = []
    return _merge_or_save(line)


def _card_for(line):
    return catalog.card_for_blueprint(line.blueprint)


def discard_session(session):
    """Delete an open session with its lines and captures; a saved one raises ScanError."""
    with transaction.atomic():
        session = ScanSession.objects.select_for_update().get(pk=session.pk)
        if session.status == ScanSession.Status.SAVED:
            raise ScanError("This scan session was already saved.")
        session.delete()


def save_session(session):
    """Write the draft into the collection. Returns the copies saved."""
    original = session
    with transaction.atomic():
        session = ScanSession.objects.select_for_update().get(pk=session.pk)
        if session.status == ScanSession.Status.SAVED:
            raise ScanError("This scan session was already saved.")
        if session.lines.filter(needs_confirmation=True).exists():
            raise ScanError("Some cards still need confirmation.")
        saved = 0
        for line in session.lines.select_related("blueprint__expansion"):
            card = _card_for(line)
            item, created = CollectionItem.objects.get_or_create(
                owner=session.owner, card=card, condition=line.condition, language=line.language, foil=line.foil,
                defaults={"quantity": line.quantity},
            )
            if not created:
                item.quantity += line.quantity
                item.save(update_fields=["quantity", "updated_at"])
            saved += line.quantity
        session.status = ScanSession.Status.SAVED
        session.save(update_fields=["status", "updated_at"])
        SyncLog.objects.create(owner=session.owner, message=f"Camera scan: {saved} cards saved")
    original.status = session.status
    return saved
