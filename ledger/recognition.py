"""Turn what the vision model read on a card into a catalog printing (Blueprint)."""

from dataclasses import dataclass, field

from django.db.models import Q

from . import catalog
from .models import Blueprint

MAX_CANDIDATES = 5


@dataclass
class Recognition:
    """What the model read. language: a Ledger code or "" (unreadable/unsupported)."""

    name: str = ""
    set_code: str = ""
    collector_number: str = ""
    language: str = ""
    foil: bool = False


@dataclass
class Match:
    """blueprint is set only for a unique, certain match. reason: ok | ambiguous | not_found | language."""

    blueprint: Blueprint | None
    candidates: list = field(default_factory=list)
    reason: str = "not_found"


def _scope(rec, expansion):
    """(blueprints to search, certain_allowed). Matches outside a known expansion are never certain."""
    code = rec.set_code.strip().lower()
    if expansion is not None and (not code or code == expansion.code):
        return Blueprint.objects.filter(expansion=expansion), True
    if code:
        # A printed code wins over the locked expansion: boosters hold bonus-sheet and List cards.
        found = catalog.expansion_by_code(code)
        if found is not None:
            return Blueprint.objects.filter(expansion=found), True
    if expansion is not None:
        return Blueprint.objects.filter(expansion=expansion), False
    # No usable set code: search the whole local catalog, and let the owner confirm.
    return Blueprint.objects.all(), False


def _by_name(qs, name):
    name = name.strip()
    if not name:
        return qs.none()
    return qs.filter(Q(name__iexact=name) | Q(name__istartswith=f"{name} // "))


def same_name(bp, name):
    """The printing carries the name read (for a split card, its front face)."""
    a, b = bp.name.casefold(), name.strip().casefold()
    return a == b or a.split(" // ")[0] == b


def _candidates(scope, name, hits=()):
    """Cards with the name read first (number hits, then the scope, then anywhere if the scope has none),
    then number hits with another name. Within each group: by Cardmarket product id."""
    named = list(_by_name(Blueprint.objects.all(), name).select_related("expansion"))
    in_scope = set(scope.filter(pk__in=[b.pk for b in named]).values_list("pk", flat=True))
    if in_scope:
        named = [b for b in named if b.pk in in_scope]
    hit_ids = {h.pk for h in hits}
    named_ids = {b.pk for b in named}

    def rank(bp):
        place = 0 if bp.pk in hit_ids else 1 if bp.pk in in_scope else 2
        return (place if bp.pk in named_ids else 3, bp.cm_product_id)

    others = [h for h in hits if h.pk not in named_ids]
    return sorted(named + others, key=rank)[:MAX_CANDIDATES]


def match(rec, expansion):
    scope, certain_allowed = _scope(rec, expansion)
    number = catalog.normalize_number(rec.collector_number)
    certain = None

    if number:
        hits = list(scope.filter(collector_number=number).select_related("expansion"))
        named_hits = [h for h in hits if rec.name and same_name(h, rec.name)]
        if len(named_hits) == 1 or (len(hits) == 1 and not rec.name.strip()):
            certain = (named_hits or hits)[0]
        elif hits:
            return Match(None, _candidates(scope, rec.name, hits), "ambiguous")

    if certain is None:
        named = list(_by_name(scope, rec.name).select_related("expansion")[:2])
        if len(named) != 1:
            candidates = _candidates(scope, rec.name)
            return Match(None, candidates, "ambiguous" if candidates else "not_found")
        certain = named[0]

    if not certain_allowed:
        return Match(None, [certain], "ambiguous")
    if not rec.language:
        return Match(None, [certain], "language")
    return Match(certain, [certain], "ok")
