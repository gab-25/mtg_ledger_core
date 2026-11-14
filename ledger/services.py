"""Business operations: CSV import/export and CardTrader sync."""

import csv
import io
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .cardtrader import (
    CONDITION_FROM_CT,
    CONDITION_TO_CT,
    LANGUAGE_TO_CT,
    MTG_GAME_ID,
    CardTraderClient,
    CardTraderError,
    money,
)
from .openrouter import OpenRouterClient
from .catalog import blueprint_of, clashing_owners, merge_cards, normalize_number
from .forms import get_or_create_card
from .models import (
    Blueprint,
    Card,
    CollectionItem,
    Condition,
    Language,
    Listing,
    Order,
    OrderItem,
    SyncConflict,
    SyncLog,
    SyncService,
)

CSV_FIELDS = ["name", "set", "condition", "language", "foil", "quantity", "number"]
CARDTRADER = "CardTrader"


def get_cardtrader(user):
    """The user's CardTrader connection (their own token and sync times)."""
    service, _ = SyncService.objects.get_or_create(
        user=user, name=CARDTRADER,
        defaults={"description": "Sale listings and orders", "direction": "Ledger ⇄ CardTrader (API)"},
    )
    return service


def user_token(user):
    """The user's own CardTrader token: the only one used for their listings, orders and shipments."""
    service = SyncService.objects.filter(user=user, name=CARDTRADER).first()
    return service.api_token if service else ""


def make_client(token):
    """Build the API client (an empty token raises CardTraderError). Tests patch this to inject a mock transport."""
    return CardTraderClient(token)


NO_SELLING_TOKEN = "Connect your CardTrader token in Profile and settings to sell."


def user_client(user):
    """The client for the user's own CardTrader account: never falls back to another user's token."""
    token = user_token(user)
    if not token:
        raise CardTraderError(NO_SELLING_TOKEN)
    return make_client(token)


class TokenInUse(Exception):
    pass


def _ct_user_id(token):
    with make_client(token) as client:
        return (client.info() or {}).get("user_id")


def set_api_token(user, token):
    """Check the token against CardTrader (/info), then store it as the user's own. Returns the app info.

    A CardTrader account can be connected to one user only (TokenInUse otherwise).
    """
    token = token.strip()
    with make_client(token) as client:
        info = client.info() or {}
    ct_user_id = info.get("user_id")
    others = SyncService.objects.filter(name=CARDTRADER).exclude(user=user).exclude(api_token="")
    for other in others.filter(ct_user_id__isnull=True):  # tokens kept from before: learn whose they are
        try:
            other.ct_user_id = _ct_user_id(other.api_token)
        except CardTraderError:
            continue
        if other.ct_user_id is not None:
            other.save(update_fields=["ct_user_id"])
    if ct_user_id is not None and others.filter(ct_user_id=ct_user_id).exists():
        raise TokenInUse("This CardTrader account is already connected to another user.")
    service = get_cardtrader(user)
    service.api_token = token
    service.ct_user_id = ct_user_id
    service.save(update_fields=["api_token", "ct_user_id"])
    _log(f"{CARDTRADER}: API token connected" + (f" (app {info['name']})" if info.get("name") else ""), owner=user)
    return info


def remove_api_token(user):
    service = get_cardtrader(user)
    service.api_token = ""
    service.ct_user_id = None
    service.save(update_fields=["api_token", "ct_user_id"])
    _log(f"{CARDTRADER}: API token removed", SyncLog.Level.WARNING, owner=user)


def openrouter_key():
    """OpenRouter is configured by the server admin only, through OPENROUTER_API_KEY."""
    return getattr(settings, "OPENROUTER_API_KEY", "")


def openrouter_model():
    """The vision model, set by the server admin only, through OPENROUTER_MODEL (no default)."""
    return getattr(settings, "OPENROUTER_MODEL", "")


def make_openrouter():
    """Build the OpenRouter client. Tests patch this to inject a fake."""
    return OpenRouterClient(openrouter_key(), model=openrouter_model())


def _log(message, level=SyncLog.Level.OK, owner=None):
    SyncLog.objects.create(message=message[:300], level=level, owner=owner)


# --- CSV -----------------------------------------------------------------

def export_csv(items, stream):
    writer = csv.writer(stream)
    writer.writerow(CSV_FIELDS)
    for item in items.select_related("card"):
        writer.writerow([
            item.card.name,
            item.card.set_code,
            item.condition,
            item.language,
            "1" if item.foil else "0",
            item.quantity,
            item.card.collector_number,
        ])


class CsvImportError(ValueError):
    pass


def _choice(value, choices, default, field_name, line):
    value = (value or "").strip().upper()
    if not value:
        return default
    if value not in choices.values:
        raise CsvImportError(f"Line {line}: invalid {field_name} '{value}'")
    return value


def link_blueprint(card):
    """Tie a card to its catalog printing by set + collector number, when it has none and one matches."""
    if card.cm_product_id or not card.collector_number:
        return
    bp = Blueprint.objects.filter(
        expansion__code=card.set_code.lower(), collector_number=normalize_number(card.collector_number)
    ).order_by("cm_product_id").first()
    if bp is not None and not Card.objects.filter(cm_product_id=bp.cm_product_id).exists():
        card.cm_product_id = bp.cm_product_id
        card.save(update_fields=["cm_product_id"])


def import_csv(uploaded_file, owner):
    """Create or update owner's collection items from a CSV. Returns (created, updated)."""
    try:
        text = uploaded_file.read().decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CsvImportError("The file is not UTF-8 encoded") from exc

    reader = csv.DictReader(io.StringIO(text))
    header = {h.strip().lower() for h in (reader.fieldnames or [])}
    missing = {"name", "set"} - header
    if missing:
        raise CsvImportError(f"Missing required columns: {', '.join(sorted(missing))}")

    created = updated = 0
    with transaction.atomic():
        for line, raw in enumerate(reader, start=2):
            row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
            if not row.get("name"):
                continue
            try:
                quantity = int(row.get("quantity") or 1)
            except ValueError as exc:
                raise CsvImportError(f"Line {line}: invalid quantity") from exc
            if quantity < 0:
                raise CsvImportError(f"Line {line}: quantity must be positive")

            card = get_or_create_card(
                row["name"],
                row["set"].upper(),
                normalize_number(row.get("number", "")),
            )
            link_blueprint(card)
            item, item_created = CollectionItem.objects.get_or_create(
                owner=owner,
                card=card,
                condition=_choice(row.get("condition"), Condition, Condition.NEAR_MINT, "condition", line),
                language=_choice(row.get("language"), Language, Language.EN, "language", line),
                foil=row.get("foil", "").lower() in {"1", "true", "yes", "y", "foil"},
                defaults={"quantity": quantity},
            )
            if item_created:
                created += 1
            else:
                item.quantity = quantity
                item.save(update_fields=["quantity", "updated_at"])
                updated += 1

        _log(f"CSV import: {created} new cards, {updated} quantities updated", owner=owner)
    return created, updated


# --- Catalog matching ------------------------------------------------------

def _ct_number(bp):
    """Collector number of a CardTrader blueprint (export payload), normalised."""
    return normalize_number((bp.get("fixed_properties") or {}).get("collector_number", ""))


def _ct_properties(bp):
    """Names of the properties CardTrader takes when selling the blueprint; None when it does not say."""
    props = bp.get("editable_properties")
    if props is None:
        return None
    return [p["name"] for p in props if isinstance(p, dict) and p.get("name")]


@dataclass
class Resolution:
    """resolve_blueprints(): cards with no CardTrader blueprint, and what CardTrader takes for each blueprint seen."""

    unmatched: list = field(default_factory=list)
    properties: dict = field(default_factory=dict)  # CardTrader blueprint id -> property names, or None


def _ct_expansion(card, by_code, by_name):
    """The CardTrader expansion id of a card: by its set code, else by its catalog expansion's name."""
    found = by_code.get(card.set_code.lower())
    if found is None:
        bp = blueprint_of(card)
        if bp is not None:
            found = by_name.get(bp.expansion.name.casefold())
    return found


def _ct_match(card, exported):
    """The CardTrader blueprint of a card: by its Cardmarket product, else by number, else by name."""
    if card.cm_product_id:
        for bp in exported:
            if card.cm_product_id in (bp.get("card_market_ids") or []):
                return bp
    number = normalize_number(card.collector_number)
    if number and not number.startswith("CM"):
        for bp in exported:
            if _ct_number(bp) == number:
                return bp
        return None
    # Several blueprints can share a name (alternate versions): keep the base one.
    by_name = None
    for bp in exported:
        if bp["name"].casefold() == card.name.casefold() and (by_name is None or (by_name.get("version") and not bp.get("version"))):
            by_name = bp
    return by_name


def resolve_blueprints(client, cards):
    """Fill Card.ct_blueprint_id from the seller's CardTrader catalog, and read what CardTrader takes for products.

    Only the CardTrader expansions of these cards are downloaded; a card whose expansion is not found stays unmatched.
    """
    resolution = Resolution()
    todo = [c for c in cards if not c.ct_blueprint_id or c.is_product]
    if not todo:
        return resolution
    by_code, by_name = {}, {}
    for e in client.expansions():
        if e.get("game_id") == MTG_GAME_ID and e.get("code"):
            by_code.setdefault(e["code"].lower(), e["id"])
            by_name.setdefault((e.get("name") or "").casefold(), e["id"])
    by_expansion = {}
    for card in todo:
        expansion_id = _ct_expansion(card, by_code, by_name)
        if expansion_id is None:
            if not card.ct_blueprint_id:
                resolution.unmatched.append(card)
            continue
        by_expansion.setdefault(expansion_id, []).append(card)
    for expansion_id, set_cards in by_expansion.items():
        exported = client.blueprints(expansion_id) or []
        for card in set_cards:
            if card.ct_blueprint_id:  # a product already matched: only what it takes is needed
                known = next((bp for bp in exported if bp["id"] == card.ct_blueprint_id), None)
                if known is not None:
                    resolution.properties[known["id"]] = _ct_properties(known)
                continue
            bp = _ct_match(card, exported)
            if bp is None:
                resolution.unmatched.append(card)
                continue
            resolution.properties[bp["id"]] = _ct_properties(bp)
            if not _link_ct_blueprint(card, bp):
                resolution.unmatched.append(card)
    return resolution


def _link_ct_blueprint(card, bp):
    """Store bp as the card's CardTrader blueprint, folding a duplicate of the same printing. False if it can't."""
    number = normalize_number(card.collector_number) or _ct_number(bp)
    existing = (
        Card.objects.exclude(pk=card.pk)
        .filter(Q(ct_blueprint_id=bp["id"]) | Q(set_code=card.set_code, collector_number=number) & ~Q(collector_number=""))
        .first()
    )
    if existing is not None and existing.ct_blueprint_id not in (None, bp["id"]):
        _log(f"{card} matches #{number}, already used by {existing} (another printing): fix it by hand",
             SyncLog.Level.WARNING)
        return False
    if existing is not None:
        clash = clashing_owners(card, existing)
        if clash or not merge_cards(card, existing):
            # The clash is about someone's listings: only they see it, never in the shared log.
            for owner_id in clash:
                SyncLog.objects.create(
                    owner_id=owner_id, level=SyncLog.Level.WARNING,
                    message=f"{card} duplicates {existing} but both have live listings: merge them by hand"[:300],
                )
            return False
        if not existing.ct_blueprint_id:
            existing.ct_blueprint_id = bp["id"]
            existing.save(update_fields=["ct_blueprint_id"])
        return True
    card.ct_blueprint_id = bp["id"]
    card.collector_number = number
    card.save(update_fields=["ct_blueprint_id", "collector_number"])
    return True


# --- Listings ------------------------------------------------------------

def product_payload(listing, accepted=None):
    """The CardTrader product for a listing; products (sealed, accessories) send only what CardTrader takes for them.

    accepted: the property names CardTrader takes for the blueprint, None when unknown (language only, then).
    """
    item = listing.item
    if item.card.is_product:
        properties = {}
        if accepted is None or "mtg_language" in accepted:
            properties["mtg_language"] = LANGUAGE_TO_CT[item.language]
        if accepted is not None and "condition" in accepted:
            properties["condition"] = CONDITION_TO_CT[item.condition]
    else:
        properties = {
            "condition": CONDITION_TO_CT[item.condition],
            "mtg_language": LANGUAGE_TO_CT[item.language],
        }
        if item.foil:
            properties["mtg_foil"] = True
    payload = {
        "price": float(listing.price),
        "quantity": listing.quantity,
        "properties": properties,
        "error_mode": "strict",
        "user_data_field": f"mtg-ledger:{listing.pk}",
    }
    if listing.ct_product_id:
        payload["id"] = listing.ct_product_id
    else:
        payload["blueprint_id"] = item.card.ct_blueprint_id
    return payload


def _job_error(result):
    errors = result.get("errors") or {}
    if isinstance(errors, dict):
        return "; ".join(f"{k}: {', '.join(map(str, v)) if isinstance(v, list) else v}" for k, v in errors.items())
    return str(errors) or "unknown error"


@dataclass
class PublishResult:
    published: int = 0
    failed: list = field(default_factory=list)  # [(listing, reason)]


def publish_pending(user):
    """Push every listing of the user waiting to be published to their CardTrader account."""
    service = get_cardtrader(user)
    listings = list(
        Listing.objects.owned_by(user).filter(status=Listing.Status.TO_PUBLISH).select_related("item__card")
    )
    result = PublishResult()
    if not listings:
        return result

    with user_client(user) as client:
        resolution = resolve_blueprints(client, {l.item.card for l in listings})
        unmatched = {c.pk for c in resolution.unmatched}
        # Resolution may merge duplicate printings: reload so listings point at the surviving cards.
        listings = list(
            Listing.objects.filter(pk__in=[l.pk for l in listings]).select_related("item__card")
        )
        to_create, to_update = [], []
        for listing in listings:
            if listing.ct_product_id:
                to_update.append(listing)
            elif listing.item.card_id in unmatched or not listing.item.card.ct_blueprint_id:
                result.failed.append((listing, "card not found in the CardTrader catalog"))
            else:
                to_create.append(listing)

        for batch, call in ((to_create, client.bulk_create), (to_update, client.bulk_update)):
            if not batch:
                continue
            outcomes = call([product_payload(l, resolution.properties.get(l.item.card.ct_blueprint_id))
                             for l in batch])
            for listing, outcome in zip(batch, outcomes):
                if outcome.get("result") in ("ok", "warning") and outcome.get("product_id"):
                    listing.ct_product_id = outcome["product_id"]
                    listing.status = Listing.Status.PUBLISHED
                    listing.save(update_fields=["ct_product_id", "status", "updated_at"])
                    result.published += 1
                else:
                    result.failed.append((listing, _job_error(outcome)))

    service.last_sync = timezone.now()
    service.save(update_fields=["last_sync"])
    _log(f"{CARDTRADER}: {result.published} listings published", owner=user)
    for listing, reason in result.failed:
        _log(f"{CARDTRADER}: {listing.item.card.name} not published ({reason})", SyncLog.Level.WARNING, owner=user)
    return result


def remove_listing(listing):
    """Delete a listing locally and, if it was published, on CardTrader."""
    owner = listing.item.owner
    if listing.ct_product_id and listing.status != Listing.Status.SOLD:
        with user_client(owner) as client:
            try:
                client.delete_product(listing.ct_product_id)
            except CardTraderError as exc:
                if exc.status != 404:  # already gone remotely: fine
                    raise
        _log(f"{CARDTRADER}: listing for {listing.item.card.name} removed", owner=owner)
    listing.delete()


# --- Orders / shipments ----------------------------------------------------

def _dt(value):
    return parse_datetime(value) if value else None


def _decrements_stock(order):
    """CardTrader's rule: CT Zero orders count while hub_pending (later merged/closed);
    the weekly merged CT Zero order and regular orders follow the normal paid flow."""
    if order.via_cardtrader_zero:
        return order.state in (Order.State.HUB_PENDING, Order.State.CLOSED)
    return order.state in (Order.State.PAID, Order.State.SENT, Order.State.ARRIVED, Order.State.DONE)


def _apply_stock(order):
    for oi in order.items.select_related("listing__item"):
        listing = oi.listing
        if listing is None:
            continue
        item = listing.item
        listing.quantity = max(listing.quantity - oi.quantity, 0)
        item.quantity = max(item.quantity - oi.quantity, 0)
        if listing.quantity == 0:
            listing.status = Listing.Status.SOLD
            listing.sold_at = order.paid_at or timezone.now()
        listing.save(update_fields=["quantity", "status", "sold_at", "updated_at"])
        item.save(update_fields=["quantity", "updated_at"])
    order.stock_applied = True
    order.save(update_fields=["stock_applied"])


def upsert_order(data, owner):
    """Create/update owner's Order from the CardTrader payload. Returns (order, created)."""
    address = data.get("order_shipping_address") or {}
    method = data.get("order_shipping_method") or {}
    buyer = data.get("buyer") or {}
    total = data.get("seller_total") or {}
    order, created = Order.objects.update_or_create(
        ct_id=data["id"],
        defaults={
            "owner": owner,
            "code": data.get("code") or "",
            "state": data.get("state") or "",
            "via_cardtrader_zero": bool(data.get("via_cardtrader_zero")),
            "buyer_username": buyer.get("username") or "",
            "buyer_email": buyer.get("email") or "",
            "buyer_phone": buyer.get("phone") or "",
            "ship_name": address.get("name") or "",
            "ship_street": address.get("street") or "",
            "ship_zip": address.get("zip") or "",
            "ship_city": address.get("city") or "",
            "ship_province": address.get("state_or_province") or "",
            "ship_country": address.get("country") or "",
            "ship_country_code": address.get("country_code") or "",
            "ship_note": address.get("note") or "",
            "shipping_method": method.get("name") or "",
            "tracked": bool(method.get("tracked")),
            "tracking_code": method.get("tracking_code") or "",
            "max_shipping_days": method.get("max_estimate_shipping_days") or None,
            "packing_number": data.get("packing_number"),
            "size": data.get("size") or 0,
            "total": money(total),
            "currency": total.get("currency") or "",
            "paid_at": _dt(data.get("paid_at")),
            "sent_at": _dt(data.get("sent_at")),
            "created_at": _dt((data.get("order_items") or [{}])[0].get("created_at")),
            "raw": data,
        },
    )
    listings = {
        l.ct_product_id: l
        for l in Listing.objects.owned_by(owner).filter(
            ct_product_id__in=[i.get("product_id") for i in data.get("order_items") or []]
        )
    }
    for raw in data.get("order_items") or []:
        props = raw.get("properties") or {}
        OrderItem.objects.update_or_create(
            order=order,
            ct_id=raw["id"],
            defaults={
                "ct_product_id": raw.get("product_id"),
                "listing": listings.get(raw.get("product_id")),
                "name": raw.get("name") or "",
                "expansion": raw.get("expansion") or "",
                "quantity": raw.get("quantity") or 1,
                "price": money(raw.get("seller_price")),
                "condition": CONDITION_FROM_CT.get(props.get("condition"), props.get("condition") or ""),
                "language": (props.get("mtg_language") or "").upper(),
                "foil": bool(props.get("mtg_foil")),
            },
        )
    if not order.stock_applied and _decrements_stock(order):
        _apply_stock(order)
    return order, created


def import_orders(client, service):
    """Import the orders of service's user (client is their own)."""
    owner = service.user
    since = service.orders_synced_at - timedelta(days=14) if service.orders_synced_at else None
    created = updated = 0
    for data in client.all_orders(date_from=since.date() if since else None):
        with transaction.atomic():
            _, was_created = upsert_order(data, owner)
        created += was_created
        updated += not was_created
    service.orders_synced_at = timezone.now()
    service.save(update_fields=["orders_synced_at"])
    to_ship = Order.objects.owned_by(owner).filter(state=Order.State.PAID).count()
    _log(f"{CARDTRADER}: {created} new orders, {updated} updated, {to_ship} to ship", owner=owner)
    return created, updated


def _diff_listings(service, products):
    """Open/refresh conflicts between service's user's listings and their CardTrader products."""
    remote = {p["id"]: p for p in products}
    seen = set()
    listings = Listing.objects.owned_by(service.user).filter(
        status=Listing.Status.PUBLISHED, ct_product_id__isnull=False
    ).select_related("item__card")
    for listing in listings:
        product = remote.get(listing.ct_product_id)
        diffs = []
        if product is None:
            diffs.append(("status", "Published", "Not on CardTrader", None))
        else:
            remote_price = money(product.get("price_cents"))
            if remote_price is not None and remote_price != listing.price:
                diffs.append(("price", f"€{listing.price}", f"€{remote_price}", remote_price))
            remote_qty = product.get("quantity")
            if remote_qty is not None and remote_qty != listing.quantity:
                diffs.append(("quantity", f"{listing.quantity} for sale", f"{remote_qty} for sale", Decimal(remote_qty)))
        for field_name, local, remote_value, amount in diffs:
            conflict, _ = SyncConflict.objects.update_or_create(
                service=service, item=listing.item, field=field_name, resolution=SyncConflict.Resolution.OPEN,
                defaults={"local_value": local, "remote_value": remote_value, "remote_amount": amount},
            )
            seen.add(conflict.pk)
    stale = SyncConflict.objects.filter(service=service, resolution=SyncConflict.Resolution.OPEN).exclude(pk__in=seen)
    stale.delete()
    return len(seen)


def sync_all(user):
    """Import the user's orders (applying sold stock), then compare their listings with CardTrader."""
    service = get_cardtrader(user)
    with user_client(user) as client:
        import_orders(client, service)
        conflicts = _diff_listings(service, client.my_products() or [])
    service.last_sync = timezone.now()
    service.save(update_fields=["last_sync"])
    if conflicts:
        _log(f"{CARDTRADER}: {conflicts} listings differ from Ledger", SyncLog.Level.WARNING, owner=user)
    else:
        _log(f"{CARDTRADER}: listings in sync", owner=user)
    return conflicts


def resolve_conflict(conflict, keep):
    """keep is 'ledger' or 'remote'."""
    listing = getattr(conflict.item, "listing", None)
    with transaction.atomic():
        if listing is not None:
            if keep == SyncConflict.Resolution.REMOTE:
                if conflict.field == "price" and conflict.remote_amount is not None:
                    listing.price = conflict.remote_amount
                elif conflict.field == "quantity" and conflict.remote_amount is not None:
                    listing.quantity = int(conflict.remote_amount)
                elif conflict.field == "status":
                    # Gone from CardTrader: treat as no longer listed.
                    listing.ct_product_id = None
                    listing.status = Listing.Status.SOLD
                    listing.sold_at = listing.sold_at or timezone.now()
                listing.save()
            else:
                # Ledger wins: push local values again on next publish.
                if conflict.field == "status":
                    listing.ct_product_id = None  # recreate the product
                listing.status = Listing.Status.TO_PUBLISH
                listing.save()
        conflict.resolution = keep
        conflict.save(update_fields=["resolution"])
        _log(
            f"{conflict.service.name}: conflict on {conflict.item.card.name} resolved "
            f"({conflict.get_resolution_display().lower()})",
            owner=conflict.item.owner,
        )


def ship_order(order, tracking_code=""):
    """Set the tracking code (if any) and mark the order as shipped on CardTrader."""
    tracking_code = tracking_code.strip()
    if (order.via_cardtrader_zero or order.tracked) and not (tracking_code or order.tracking_code):
        kind = "CardTrader Zero orders" if order.via_cardtrader_zero else "Tracked shipments"
        raise CardTraderError(f"{kind} need a tracking code before shipping.")
    with user_client(order.owner) as client:
        if tracking_code and tracking_code != order.tracking_code:
            client.set_tracking_code(order.ct_id, tracking_code)
            order.tracking_code = tracking_code
            order.save(update_fields=["tracking_code", "updated_at"])
        data = client.ship(order.ct_id)
    if isinstance(data, dict) and data.get("id") == order.ct_id:
        order, _ = upsert_order(data, order.owner)
    else:
        order.state = Order.State.SENT
        order.sent_at = timezone.now()
        order.save(update_fields=["state", "sent_at", "updated_at"])
    _log(f"{CARDTRADER}: order #{order.code} shipped", owner=order.owner)
    return order


def save_tracking_code(order, tracking_code):
    with user_client(order.owner) as client:
        client.set_tracking_code(order.ct_id, tracking_code.strip())
    order.tracking_code = tracking_code.strip()
    order.save(update_fields=["tracking_code", "updated_at"])
    _log(f"{CARDTRADER}: tracking code set for order #{order.code}", owner=order.owner)
