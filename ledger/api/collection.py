"""The collection API (/api/collection/): list with filters and stats, add from the catalog, edit/remove copies,
CSV import and export."""

from decimal import Decimal

from django.db.models import Case, DecimalField, ExpressionWrapper, F, OuterRef, Q, Subquery, Sum, Value, When
from django.db.models.functions import NullIf
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.decorators import api_view, parser_classes
from rest_framework.parsers import MultiPartParser
from rest_framework.response import Response

from .. import services
from ..forms import CatalogItemForm, CsvImportForm, ItemForm
from ..models import Blueprint, Card, Category, CollectionItem, Condition, Language, Listing, SyncLog
from ..presentation import item_badge
from .common import PAGE, doc, form_body, form_errors, messages_response, msg, page_payload, query

TAGS = ["collection"]
VALUE = ExpressionWrapper(F("trend") * F("quantity"), output_field=DecimalField())
CATALOG_ITEM = form_body("CatalogItem", CatalogItemForm)
ITEM = form_body("Item", ItemForm)


def _blueprint_value(field):
    """The value of a field of the item's catalog blueprint (None when the card has none)."""
    return Subquery(Blueprint.objects.filter(cm_product_id=OuterRef("card__cm_product_id")).values(field)[:1])


def trend_price():
    """The Cardmarket trend of the item's printing (trend_foil for a foil card), None when unknown or 0.00."""
    trend = Case(
        When(foil=True, card__is_product=False, then=_blueprint_value("cm_product__trend_foil")),
        default=_blueprint_value("cm_product__trend"),
        output_field=DecimalField(max_digits=10, decimal_places=2),
    )
    return NullIf(trend, Value(Decimal("0.00")))


def _price(item):
    return f"{item.trend:.2f}" if item.trend is not None else None


def _with_blueprint(items):
    return items.annotate(category_ct_id=_blueprint_value("category__cm_id"),
                          category_name=_blueprint_value("category__name"),
                          image_url=_blueprint_value("image_url"),
                          image_large_url=_blueprint_value("image_large_url"),
                          trend=trend_price())


def _category(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _choices(choices):
    return [[value, label] for value, label in choices]


@doc(TAGS, "Choices of the card and listing forms: conditions, languages, listing statuses")
@api_view(["GET"])
def choices(request):
    return Response({
        "conditions": _choices(Condition.choices),
        "languages": _choices(Language.choices),
        "listing_statuses": _choices(Listing.Status.choices),
    })


def _row(item):
    card = item.card
    return {
        "id": item.pk,
        "name": card.name,
        "set_code": card.set_code,
        "is_product": card.is_product,
        "category_id": item.category_ct_id,
        "category_name": item.category_name,
        "image_url": item.image_url or "",
        "image_large_url": item.image_large_url or "",
        "foil": item.foil,
        "condition": item.condition,
        "condition_label": item.get_condition_display(),
        "language": item.language,
        "quantity": item.quantity,
        "price": _price(item),
        "badge": item_badge(item.status),
    }


def _collection(request):
    user = request.user
    items = _with_blueprint(CollectionItem.objects.owned_by(user).select_related("card", "listing"))
    stats = items.aggregate(total=Sum("quantity"), value=Sum(VALUE))
    stats["unique"] = Card.objects.filter(items__owner=user).distinct().count()
    stats["for_sale"] = (
        Listing.objects.owned_by(user).exclude(status=Listing.Status.SOLD).aggregate(n=Sum("quantity"))["n"] or 0
    )
    if stats["value"] is not None:
        stats["value"] = f"{stats['value']:.2f}"

    params = request.query_params
    q = params.get("q", "").strip()
    foil = params.get("foil") == "1"
    set_code = params.get("set", "")
    cond = params.get("cond", "")
    category = _category(params.get("category"))
    if q:
        items = items.filter(Q(card__name__icontains=q) | Q(card__set_code__iexact=q))
    if foil:
        items = items.filter(foil=True)
    if set_code:
        items = items.filter(card__set_code=set_code)
    if cond in Condition.values:
        items = items.filter(condition=cond)
    if category is not None:
        items = items.filter(category_ct_id=category)
    owned_products = Card.objects.filter(items__owner=user, cm_product_id__isnull=False).values("cm_product_id")
    categories = (Category.objects.filter(blueprints__cm_product_id__in=owned_products).distinct()
                  .order_by("name", "cm_id"))

    return Response({
        **page_payload(request, items, _row),
        "stats": stats,
        "sets": list(Card.objects.filter(items__owner=user).values_list("set_code", flat=True)
                     .distinct().order_by("set_code")),
        "categories": [{"id": c.cm_id, "name": c.name} for c in categories],
        "filtered": bool(q or foil or set_code or cond or category is not None),
    })


@doc(TAGS, "The collection (GET: ?q, foil, set, cond, category, page) or add a catalog product to it (POST)",
     request=CATALOG_ITEM)
@extend_schema(methods=["GET"], parameters=[
    query("q", "Name or exact set code"),
    query("foil", "1: foils only", enum=["1"]),
    query("set", "Exact set code"),
    query("cond", "Condition", enum=Condition.values),
    query("category", "CardTrader category id of the item's catalog product"),
    PAGE,
])
@api_view(["GET", "POST"])
def collection(request):
    if request.method == "GET":
        return _collection(request)
    form = CatalogItemForm(request.data, owner=request.user)
    if not form.is_valid():
        raise form_errors(form)
    item, merged = form.save()
    added = form.cleaned_data["quantity"]
    if merged:
        return messages_response(msg("success", f"Added {added}× {item.card.name} to the copies you already had."),
                                 id=item.pk)
    return messages_response(msg("success", f"Added {added}× {item.card.name}."),
                             status=status.HTTP_201_CREATED, id=item.pk)


@doc(TAGS, "One collection item: read (GET), change its copies (PUT) or remove it (DELETE)", request=ITEM)
@extend_schema(methods=["GET"], operation_id="collection_item_retrieve")
@api_view(["GET", "PUT", "DELETE"])
def item(request, pk):
    obj = get_object_or_404(_with_blueprint(CollectionItem.objects.owned_by(request.user).select_related("card")), pk=pk)
    if request.method == "DELETE":
        name = obj.card.name
        obj.delete()
        return messages_response(msg("success", f"Removed {name} from the collection."))
    if request.method == "PUT":
        form = ItemForm(request.data, instance=obj)
        if not form.is_valid():
            raise form_errors(form)
        form.save()
        return messages_response(msg("success", f"Saved {obj.card.name}."))
    card = obj.card
    return Response({
        "id": obj.pk,
        "name": card.name,
        "set_code": card.set_code,
        "collector_number": card.collector_number,
        "is_product": card.is_product,
        "category_id": obj.category_ct_id,
        "category_name": obj.category_name,
        "condition": obj.condition,
        "language": obj.language,
        "foil": obj.foil,
        "quantity": obj.quantity,
        "price": _price(obj),
        "has_listing": hasattr(obj, "listing"),
    })


@doc(TAGS, "CSV import: the columns (GET) or import a file (POST multipart 'file')",
     request={"multipart/form-data": form_body("CsvImport", CsvImportForm)})
@api_view(["GET", "POST"])
@parser_classes([MultiPartParser])
def import_csv(request):
    if request.method == "GET":
        return Response({"columns": list(services.CSV_FIELDS)})
    form = CsvImportForm(data={}, files=request.FILES)
    if form.is_valid():
        try:
            created, updated = services.import_csv(form.cleaned_data["file"], request.user)
        except services.CsvImportError as exc:
            form.add_error("file", str(exc))
        else:
            return messages_response(msg("success", f"Import complete: {created} new, {updated} updated."))
    raise form_errors(form)


@doc(TAGS, "Download the collection as CSV")
@api_view(["GET"])
def export_csv(request):
    items = CollectionItem.objects.owned_by(request.user)
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    stamp = timezone.localdate().isoformat()
    response["Content-Disposition"] = f'attachment; filename="mtg-ledger-{stamp}.csv"'
    services.export_csv(items, response)
    SyncLog.objects.create(owner=request.user, message=f"CSV export: collection exported ({items.count():,} rows)")
    return response
