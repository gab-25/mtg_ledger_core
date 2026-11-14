"""Local card catalog (Cardmarket products described by Scryfall) and printing lookup."""

import re

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


def expansion_by_code(code):
    from .models import Expansion

    code = (code or "").strip().lower()
    return Expansion.objects.filter(code=code).first() if code else None
