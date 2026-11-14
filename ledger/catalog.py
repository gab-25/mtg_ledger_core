"""Local card catalog (Cardmarket products described by Scryfall) and printing lookup."""

import re
from dataclasses import dataclass

_NUMBER = re.compile(r"^0*(\d+)(.*)$")


def normalize_number(value):
    """Canonical collector number: no spaces, no leading zeros, upper case ("012a" -> "12A")."""
    value = (value or "").strip().replace(" ", "").upper()
    m = _NUMBER.match(value)
    if m:
        return (m.group(1) or "0") + m.group(2)
    return value


def _live_listing(item):
    from .models import Listing

    listing = getattr(item, "listing", None)
    return listing if listing is not None and listing.status != Listing.Status.SOLD else None


def _merge_pairs(source, target):
    """[(source item, same owner's twin on target or None)]: copies only ever fold into their owner's own."""
    pairs = []
    for item in source.items.select_related("listing"):
        twin = target.items.select_related("listing").filter(
            owner_id=item.owner_id, condition=item.condition, language=item.language, foil=item.foil
        ).first()
        pairs.append((item, twin))
    return pairs


def clashing_owners(source, target):
    """The users (ids) whose copies on both cards have live listings: merging would need two listings in one."""
    return {
        item.owner_id for item, twin in _merge_pairs(source, target)
        if twin is not None and _live_listing(item) and _live_listing(twin)
    }


def merge_cards(source, target):
    """Move source's copies onto target (same printing). Returns False, changing nothing, on a listing clash."""
    from django.db import transaction

    if clashing_owners(source, target):
        return False
    pairs = _merge_pairs(source, target)
    live_listing = _live_listing

    with transaction.atomic():
        for item, twin in pairs:
            if twin is None:
                item.card = target
                item.save(update_fields=["card", "updated_at"])
                continue
            twin.quantity += item.quantity
            twin.save(update_fields=["quantity", "updated_at"])
            listing = getattr(item, "listing", None)
            if listing is not None and live_listing(twin) is None:
                old = getattr(twin, "listing", None)  # a sold listing on the twin gives way
                if old is not None:
                    old.delete()
                listing.item = twin
                listing.save(update_fields=["item", "updated_at"])
            item.delete()
        source.delete()
    return True


def _mark_product(card, bp):
    if card.is_product != bp.is_product:
        card.is_product = bp.is_product
        card.save(update_fields=["is_product"])
    return card


def card_for_blueprint(bp):
    """The Ledger card of a catalog blueprint: the one tied to it, an adopted hand-entered row, or a new one."""
    from .forms import get_or_create_card
    from .models import Card, SyncLog

    card = Card.objects.filter(cm_product_id=bp.cm_product_id).order_by("pk").first()
    if card is not None:
        return _mark_product(card, bp)
    set_code = bp.expansion.code.upper()
    if bp.collector_number:
        # adopt a hand-entered (name + set) row instead of duplicating it
        legacy = Card.objects.filter(
            name__iexact=bp.name, set_code=set_code, collector_number="", cm_product_id__isnull=True
        ).first()
        if legacy is not None and not Card.objects.filter(
            set_code=set_code, collector_number=bp.collector_number
        ).exists():
            legacy.cm_product_id = bp.cm_product_id
            legacy.collector_number = bp.collector_number
            legacy.save(update_fields=["cm_product_id", "collector_number"])
            return _mark_product(legacy, bp)
    # One Card per printing: a missing number, or one already taken by another product, gets a unique stand-in.
    number = bp.collector_number or stand_in_number(bp.cm_product_id)
    taken = Card.objects.filter(set_code=set_code, collector_number=number).first()
    if taken is not None and taken.cm_product_id not in (None, bp.cm_product_id):
        SyncLog.objects.create(
            level=SyncLog.Level.WARNING,
            message=f"{bp} shares #{number} with {taken}: stored as #{stand_in_number(bp.cm_product_id)}"[:300],
        )
        number = stand_in_number(bp.cm_product_id)
    card = get_or_create_card(bp.name, set_code, number)
    if card.cm_product_id is None:
        card.cm_product_id = bp.cm_product_id
        card.save(update_fields=["cm_product_id"])
    return _mark_product(card, bp)


def stand_in_number(cm_product_id):
    """The collector number of a printing that has none (products): unique per Cardmarket product."""
    return f"CM{cm_product_id}"


def blueprint_of(card):
    """The catalog printing of a Ledger card: by its Cardmarket product, else by set code + collector number."""
    from .models import Blueprint

    if card.cm_product_id:
        bp = Blueprint.objects.select_related("expansion", "category").filter(cm_product_id=card.cm_product_id).first()
        if bp is not None:
            return bp
    number = normalize_number(card.collector_number)
    if not number:
        return None
    return (Blueprint.objects.select_related("expansion", "category")
            .filter(expansion__code=card.set_code.lower(), collector_number=number).order_by("cm_product_id").first())


@dataclass
class ReconcileResult:
    linked: int = 0
    unlinked: int = 0
    rewritten: int = 0
    merged: int = 0
    clashes: int = 0


def _fold(source, target, result):
    """Merge source into target (same printing). False, logging it to the owners, when live listings clash."""
    from .models import SyncLog

    clash = clashing_owners(source, target)
    if clash or not merge_cards(source, target):
        for owner_id in clash:  # about someone's listings: only they see it
            SyncLog.objects.create(
                owner_id=owner_id, level=SyncLog.Level.WARNING,
                message=f"{source} and {target} are the same printing but both have live listings: "
                        f"merge them by hand"[:300],
            )
        result.clashes += 1
        return False
    result.merged += 1
    return True


def reconcile_cards():
    """Align the Ledger's cards with the catalog: link, fold duplicates, take the catalog's set code and number.

    1. a card with no Cardmarket product is linked by set code + collector number;
    2. cards on the same product fold into the oldest (unless live listings clash: the newer one is unlinked);
    3. a linked card takes its printing's set code, collector number (or CM stand-in) and product flag; when another
       card already holds them, the two fold.
    """
    from django.db.models import Count

    from .models import Blueprint, Card
    from .services import link_blueprint

    result = ReconcileResult()
    for card in Card.objects.filter(cm_product_id__isnull=True).exclude(collector_number=""):
        link_blueprint(card)

    duplicated = (Card.objects.exclude(cm_product_id=None).values("cm_product_id").annotate(n=Count("pk"))
                  .filter(n__gt=1).values_list("cm_product_id", flat=True))
    for cm in list(duplicated):
        target, *sources = Card.objects.filter(cm_product_id=cm).order_by("pk")
        for source in sources:
            if not _fold(source, target, result):
                source.cm_product_id = None
                source.save(update_fields=["cm_product_id"])

    linked_ids = Card.objects.exclude(cm_product_id=None).values("cm_product_id")
    printings = {bp.cm_product_id: bp for bp in
                 Blueprint.objects.select_related("expansion", "category").filter(cm_product_id__in=linked_ids)}
    for card in Card.objects.filter(cm_product_id__in=list(printings)).order_by("pk"):
        bp = printings[card.cm_product_id]
        set_code, number = bp.expansion.code.upper(), bp.collector_number or stand_in_number(bp.cm_product_id)
        if (card.set_code, card.collector_number, card.is_product) == (set_code, number, bp.is_product):
            continue
        taken = Card.objects.filter(set_code=set_code, collector_number=number).exclude(pk=card.pk).first()
        if taken is not None and taken.cm_product_id not in (None, bp.cm_product_id):
            number = stand_in_number(bp.cm_product_id)  # the number belongs to another printing
            taken = Card.objects.filter(set_code=set_code, collector_number=number).exclude(pk=card.pk).first()
        if taken is not None:
            if _fold(card, taken, result):
                taken.cm_product_id, taken.is_product = bp.cm_product_id, bp.is_product
                taken.save(update_fields=["cm_product_id", "is_product"])
            continue
        card.set_code, card.collector_number, card.is_product = set_code, number, bp.is_product
        card.save(update_fields=["set_code", "collector_number", "is_product"])
        result.rewritten += 1

    result.linked = Card.objects.filter(cm_product_id__in=Blueprint.objects.values("cm_product_id")).count()
    result.unlinked = Card.objects.count() - result.linked
    return result


def expansion_by_code(code):
    from .models import Expansion

    code = (code or "").strip().lower()
    return Expansion.objects.filter(code=code).first() if code else None
