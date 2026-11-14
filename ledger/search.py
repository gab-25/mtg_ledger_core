"""Catalog search: the words of ?q matched against the start of the words of a blueprint's name, set codes and
collector numbers, accents, case and punctuation ignored, results ordered by relevance."""

import re
from collections import defaultdict
from functools import reduce
from operator import add

from django.contrib.postgres.search import TrigramSimilarity, TrigramWordSimilarity
from django.db import connection
from django.db.models import Case, CharField, F, Func, IntegerField, Q, Value, When

from .catalog import normalize_number
from .models import CARD_CATEGORY_ID, Expansion

_NUMBER = re.compile(r"\d+[a-z]?")
# Minimum pg_trgm word similarity of every query word to the name in the typo-tolerant fallback, run only when no
# blueprint matches the words.
FUZZY_THRESHOLD = 0.4


class SearchText(Func):
    """The name as the search sees it: SQL function ledger_search_text (migration 0020)."""

    function = "ledger_search_text"
    output_field = CharField()


def normalize_all(texts):
    """The texts as ledger_search_text sees them (lower case, no accents or apostrophes, other punctuation as one
    space), computed by the same SQL function so query and names never disagree."""
    if not texts:
        return []
    with connection.cursor() as cursor:
        cursor.execute("SELECT ledger_search_text(t) FROM unnest(%s::text[]) WITH ORDINALITY AS u(t, i) ORDER BY i",
                       [list(texts)])
        return [row[0] for row in cursor.fetchall()]


class _Query:
    """q split into terms: a raw token that is a set code (even "sld-cvd") is one term, every other token gives one
    term per normalised word. Each term is (ids of the set it names or None, words)."""

    def __init__(self, q):
        tokens = q.lower().split()
        normalized = normalize_all(tokens)
        candidates = set(tokens) | {w for n in normalized for w in n.split()}
        self.code_ids = defaultdict(list)
        for code, pk in Expansion.objects.filter(code__in=candidates).values_list("code", "id"):
            self.code_ids[code].append(pk)
        self.terms = []
        for token, norm in zip(tokens, normalized):
            if token in self.code_ids:
                self.terms.append((self.code_ids[token], norm.split()))
            else:
                self.terms += [(self.code_ids.get(w), [w]) for w in norm.split()]
        self.words = [w for _, words in self.terms for w in words]
        self.set_ids = [pk for ids in self.code_ids.values() for pk in ids]

    def number(self, words):
        """The collector number a term stands for: a lone numeric word, when q also names a set."""
        if self.set_ids and len(words) == 1 and _NUMBER.fullmatch(words[0]):
            return normalize_number(words[0])
        return None


def _starts_word(word):
    # words hold only [a-z0-9], so they are safe inside a regex
    return Q(st__regex=rf"(^| ){word}")


def _term_condition(query, ids, words):
    """A term matches the start of words of the name, its set or, when q names a set, a collector number there."""
    cond = reduce(lambda a, b: a & b, (_starts_word(w) for w in words)) if words else Q(pk__in=[])
    if ids:
        cond |= Q(expansion_id__in=ids)
    number = query.number(words)
    if number:
        cond |= Q(collector_number=number, expansion_id__in=query.set_ids)
    return cond


def _tie_breaks():
    """Cards (and blueprints not yet categorised) before sealed products, then newest expansion first."""
    is_product = Case(
        When(Q(category__isnull=True) | Q(category__cm_id=CARD_CATEGORY_ID), then=Value(0)),
        default=Value(1), output_field=IntegerField(),
    )
    return [is_product, F("expansion__released_at").desc(nulls_last=True), "name", "cm_product_id"]


def search_blueprints(queryset, q):
    """The blueprints of the queryset matching every term of q, most relevant first; when none does, the ones whose
    name is close to q (typos), most similar first."""
    query = _Query(q)
    phrase = " ".join(query.words)
    rank = Case(
        When(st=phrase, then=Value(0)), When(st__startswith=phrase, then=Value(1)),
        default=Value(2), output_field=IntegerField(),
    )
    matches = (queryset.alias(st=SearchText("name"))
               .filter(*(_term_condition(query, ids, words) for ids, words in query.terms))
               .order_by(rank, *_tie_breaks()))
    if not query.terms or matches.exists():
        return matches
    return _fuzzy(queryset, query)


def _fuzzy(queryset, query):
    """Set codes and collector numbers as filters, every other word close to a word of the name; the closest words
    first, then the name closest to those words (so "Lightning Bolt" before "Art Series: Lightning Bolt")."""
    words = []
    for ids, term_words in query.terms:
        number = query.number(term_words)
        if ids:
            queryset = queryset.filter(expansion_id__in=ids)
        elif number:
            queryset = queryset.filter(collector_number=number)
        else:
            words += term_words
    if not words:
        return queryset.none()
    sims = {f"sim{i}": TrigramWordSimilarity(Value(w), SearchText("name")) for i, w in enumerate(words)}
    return (queryset.alias(**sims)
            .filter(**{f"{k}__gte": FUZZY_THRESHOLD for k in sims})
            .alias(total=reduce(add, sims.values()),
                   whole=TrigramSimilarity(Value(" ".join(words)), SearchText("name")))
            .order_by("-total", "-whole", *_tie_breaks()))
