import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q


def _owner(related_name):
    """The user a personal row belongs to."""
    return models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name=related_name)


class OwnedQuerySet(models.QuerySet):
    """Rows belonging to one user; owner_path is the lookup from the model to its owner."""

    owner_path = "owner"

    def owned_by(self, user):
        return self.filter(**{self.owner_path: user})


def owned_manager(path):
    return type(f"OwnedBy{path.title().replace('__', '')}QuerySet", (OwnedQuerySet,), {"owner_path": path}).as_manager()


class SyncLogQuerySet(models.QuerySet):
    def visible_to(self, user):
        """The user's own entries plus the global ones (no owner, e.g. the catalog sync)."""
        return self.filter(Q(owner=user) | Q(owner__isnull=True))


class Condition(models.TextChoices):
    MINT = "M", "Mint"
    NEAR_MINT = "NM", "Near Mint"
    EXCELLENT = "EX", "Excellent"
    GOOD = "GD", "Good"
    LIGHT_PLAYED = "LP", "Light Played"
    PLAYED = "PL", "Played"
    POOR = "PO", "Poor"


class Language(models.TextChoices):
    EN = "EN", "English"
    IT = "IT", "Italian"
    DE = "DE", "German"
    FR = "FR", "French"
    ES = "ES", "Spanish"
    PT = "PT", "Portuguese"
    JP = "JP", "Japanese"
    KO = "KO", "Korean"
    ZHS = "ZHS", "Chinese (S)"
    RU = "RU", "Russian"


class Card(models.Model):
    """A specific printing of a card (name + set)."""

    name = models.CharField(max_length=200)
    set_code = models.CharField(max_length=10)
    collector_number = models.CharField(max_length=40, blank=True, default="")
    ct_blueprint_id = models.PositiveIntegerField(
        "CardTrader blueprint", null=True, blank=True, help_text="Matched automatically on first publish"
    )
    is_product = models.BooleanField(
        default=False, help_text="A sealed product or accessory (no condition or foil), not a card"
    )

    class Meta:
        ordering = ["name", "set_code"]
        constraints = [
            models.UniqueConstraint(
                fields=["set_code", "collector_number"], condition=~Q(collector_number=""),
                name="unique_card_set_number",
            ),
            models.UniqueConstraint(
                fields=["name", "set_code"], condition=Q(collector_number=""), name="unique_card_legacy_name_set",
            ),
            models.UniqueConstraint(
                fields=["ct_blueprint_id"], condition=Q(ct_blueprint_id__isnull=False), name="unique_card_blueprint",
            ),
        ]

    def __str__(self):
        if self.collector_number:
            return f"{self.name} ({self.set_code} #{self.collector_number})"
        return f"{self.name} ({self.set_code})"


class CollectionItem(models.Model):
    """A stack of identical copies owned by the user."""

    class Status(models.TextChoices):
        COLLECTION = "collection", "In collection"
        FOR_SALE = "sale", "For sale"
        PENDING = "pending", "Sync pending"

    owner = _owner("collection_items")
    card = models.ForeignKey(Card, on_delete=models.CASCADE, related_name="items")
    condition = models.CharField(max_length=2, choices=Condition.choices, default=Condition.NEAR_MINT)
    language = models.CharField(max_length=3, choices=Language.choices, default=Language.EN)
    foil = models.BooleanField(default=False)
    quantity = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = owned_manager("owner")

    class Meta:
        ordering = ["card__name", "card__set_code"]

    def __str__(self):
        return f"{self.quantity}× {self.card} {self.condition} {self.language}"

    @property
    def status(self):
        listing = getattr(self, "listing", None)
        if listing is None or listing.status == Listing.Status.SOLD:
            return self.Status.COLLECTION
        if listing.status == Listing.Status.TO_PUBLISH:
            return self.Status.PENDING
        return self.Status.FOR_SALE


class Listing(models.Model):
    """A sale listing on CardTrader for a collection item."""

    class Status(models.TextChoices):
        PUBLISHED = "published", "Published"
        TO_PUBLISH = "to_publish", "To publish"
        SOLD = "sold", "Sold"

    item = models.OneToOneField(CollectionItem, on_delete=models.CASCADE, related_name="listing")
    quantity = models.PositiveIntegerField(default=1)
    price = models.DecimalField(max_digits=10, decimal_places=2)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.TO_PUBLISH)
    ct_product_id = models.PositiveBigIntegerField("CardTrader product", null=True, blank=True, unique=True)
    sold_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = owned_manager("item__owner")

    class Meta:
        ordering = ["item__card__name"]

    def __str__(self):
        return f"{self.item.card} @ {self.price}"


class SyncService(models.Model):
    """A user's connection to a service (CardTrader): their own API token and sync times."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="sync_services")
    name = models.CharField(max_length=80)
    description = models.CharField(max_length=200, blank=True)
    direction = models.CharField(max_length=120, blank=True)
    api_token = models.TextField(blank=True)  # CardTrader tokens are long JWTs
    ct_user_id = models.PositiveBigIntegerField(
        "CardTrader user", null=True, blank=True, unique=True,
        help_text="The CardTrader account behind the token: one app user per account",
    )
    last_sync = models.DateTimeField(null=True, blank=True)
    orders_synced_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "name"], name="unique_service_per_user")]

    def __str__(self):
        return self.name


class SyncConflict(models.Model):
    class Resolution(models.TextChoices):
        OPEN = "open", "Open"
        LEDGER = "ledger", "Kept Ledger"
        REMOTE = "remote", "Kept remote"

    service = models.ForeignKey(SyncService, on_delete=models.CASCADE, related_name="conflicts")
    item = models.ForeignKey(CollectionItem, on_delete=models.CASCADE, related_name="conflicts")
    field = models.CharField(max_length=40, blank=True, help_text="Listing field that differs, e.g. 'price'")
    local_value = models.CharField(max_length=120)
    remote_value = models.CharField(max_length=120)
    remote_amount = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
        help_text="Numeric remote value (price or quantity) applied on 'Keep remote'",
    )
    resolution = models.CharField(max_length=8, choices=Resolution.choices, default=Resolution.OPEN)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = owned_manager("item__owner")

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.item.card}: {self.local_value} ≠ {self.remote_value}"


class SyncLog(models.Model):
    class Level(models.TextChoices):
        OK = "ok", "OK"
        WARNING = "warn", "Warning"

    owner = models.ForeignKey(  # None: a global entry (the catalog sync)
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, null=True, blank=True, related_name="sync_logs",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    level = models.CharField(max_length=4, choices=Level.choices, default=Level.OK)
    message = models.CharField(max_length=300)

    objects = SyncLogQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return self.message


class Order(models.Model):
    """A CardTrader sale order (you are the seller)."""

    class State(models.TextChoices):
        HUB_PENDING = "hub_pending", "CT Zero pending"
        PAID = "paid", "To ship"
        SENT = "sent", "Shipped"
        ARRIVED = "arrived", "Arrived"
        DONE = "done", "Completed"
        REQUEST_FOR_CANCEL = "request_for_cancel", "Cancellation requested"
        CANCELED = "canceled", "Canceled"
        LOST = "lost", "Lost"
        CLOSED = "closed", "Merged (CT Zero)"

    owner = _owner("orders")
    ct_id = models.PositiveBigIntegerField(unique=True)
    code = models.CharField(max_length=40)
    state = models.CharField(max_length=24, default=State.PAID)
    via_cardtrader_zero = models.BooleanField(default=False)
    buyer_username = models.CharField(max_length=120, blank=True)
    buyer_email = models.CharField(max_length=200, blank=True)
    buyer_phone = models.CharField(max_length=40, blank=True)
    ship_name = models.CharField(max_length=200, blank=True)
    ship_street = models.CharField(max_length=300, blank=True)
    ship_zip = models.CharField(max_length=20, blank=True)
    ship_city = models.CharField(max_length=120, blank=True)
    ship_province = models.CharField(max_length=120, blank=True)
    ship_country = models.CharField(max_length=80, blank=True)
    ship_country_code = models.CharField(max_length=2, blank=True)
    ship_note = models.TextField(blank=True)
    shipping_method = models.CharField(max_length=200, blank=True)
    tracked = models.BooleanField(default=False)
    tracking_code = models.CharField(max_length=120, blank=True)
    max_shipping_days = models.PositiveIntegerField(null=True, blank=True)
    packing_number = models.PositiveIntegerField(null=True, blank=True)
    size = models.PositiveIntegerField(default=0)
    total = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    currency = models.CharField(max_length=3, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(null=True, blank=True)
    stock_applied = models.BooleanField(
        default=False, help_text="Sold quantities already removed from the Ledger"
    )
    raw = models.JSONField(default=dict, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = owned_manager("owner")

    class Meta:
        ordering = ["-paid_at", "-ct_id"]

    def __str__(self):
        return f"#{self.code}"

    @property
    def state_label(self):
        try:
            return self.State(self.state).label
        except ValueError:
            return self.state.replace("_", " ").capitalize()

    @property
    def can_ship(self):
        return self.state == self.State.PAID

    @property
    def address_lines(self):
        city = " ".join(p for p in (self.ship_zip, self.ship_city, self.ship_province) if p)
        return [l for l in (self.ship_name, self.ship_street, city, self.ship_country) if l]


class OrderItem(models.Model):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="items")
    ct_id = models.PositiveBigIntegerField()
    ct_product_id = models.PositiveBigIntegerField(null=True, blank=True)
    listing = models.ForeignKey(Listing, null=True, blank=True, on_delete=models.SET_NULL, related_name="order_items")
    name = models.CharField(max_length=200)
    expansion = models.CharField(max_length=200, blank=True)
    quantity = models.PositiveIntegerField(default=1)
    price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    condition = models.CharField(max_length=40, blank=True)
    language = models.CharField(max_length=10, blank=True)
    foil = models.BooleanField(default=False)

    objects = owned_manager("order__owner")

    class Meta:
        ordering = ["name"]
        constraints = [models.UniqueConstraint(fields=["order", "ct_id"], name="unique_order_item")]

    def __str__(self):
        return f"{self.quantity}× {self.name}"


class Expansion(models.Model):
    """A CardTrader MTG expansion (local catalog copy)."""

    ct_id = models.PositiveIntegerField("CardTrader expansion", unique=True)
    code = models.CharField(max_length=10, db_index=True)
    name = models.CharField(max_length=200)
    blueprints_synced_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.code.upper()} · {self.name}"


# CardTrader categories that are cards (collector number, condition, foil): Single Card, Token/Emblem, Oversized.
# Every other category (sealed, accessories, ...) is a product.
CARD_CATEGORY_IDS = frozenset({1, 2, 3})


class Category(models.Model):
    """A CardTrader MTG product category (local catalog copy): single cards, booster boxes, playmats, ..."""

    ct_id = models.PositiveIntegerField("CardTrader category", unique=True)
    name = models.CharField(max_length=100, blank=True)

    class Meta:
        ordering = ["name"]
        verbose_name_plural = "categories"

    def __str__(self):
        return self.name or f"Category {self.ct_id}"


class Blueprint(models.Model):
    """A CardTrader blueprint: one printing of a card (local catalog copy)."""

    ct_id = models.PositiveIntegerField("CardTrader blueprint", unique=True)
    expansion = models.ForeignKey(Expansion, on_delete=models.CASCADE, related_name="blueprints")
    name = models.CharField(max_length=200, db_index=True)
    collector_number = models.CharField(max_length=40, blank=True)
    version = models.CharField(max_length=80, blank=True)
    rarity = models.CharField(max_length=40, blank=True)
    image_url = models.URLField(max_length=500, blank=True)
    image_large_url = models.URLField(
        max_length=500, blank=True, help_text="CardTrader's large image (488×680); image_url is its thumbnail",
    )
    category = models.ForeignKey(Category, null=True, blank=True, on_delete=models.SET_NULL, related_name="blueprints")
    editable_properties = models.JSONField(
        null=True, blank=True, help_text="Names of the properties CardTrader accepts when selling it; empty if unknown",
    )
    cm_price = models.ForeignKey(
        "CardmarketPrice", null=True, blank=True, on_delete=models.DO_NOTHING, db_constraint=False,
        related_name="blueprints", verbose_name="Cardmarket product",
        help_text="First of the blueprint's Cardmarket ids; its price row may not exist",
    )

    class Meta:
        ordering = ["expansion__name", "name"]

    @property
    def is_product(self):
        """Sealed product or accessory rather than a card; a blueprint not yet categorised counts as a card."""
        return self.category_id is not None and self.category.ct_id not in CARD_CATEGORY_IDS

    def accepts(self, prop):
        """Whether CardTrader accepts this property for it; unknown properties are assumed accepted."""
        return self.editable_properties is None or prop in self.editable_properties

    def __str__(self):
        number = f" #{self.collector_number}" if self.collector_number else ""
        return f"{self.name} ({self.expansion.code.upper()}{number})"


class CardmarketPrice(models.Model):
    """Cardmarket price guide of one Cardmarket product (local copy of Cardmarket's public daily file)."""

    id_product = models.PositiveIntegerField("Cardmarket product", primary_key=True)
    low = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    trend = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    avg = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    avg1 = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    avg7 = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    avg30 = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    low_foil = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    trend_foil = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    avg_foil = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    avg1_foil = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    avg7_foil = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    avg30_foil = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    prices_date = models.DateTimeField(null=True, blank=True, help_text="createdAt of the price guide")

    def __str__(self):
        return f"Cardmarket {self.id_product}: {self.trend}"


class ScanSession(models.Model):
    """A camera scanning session: a draft of cards to add to the collection."""

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        SAVED = "saved", "Saved"

    owner = _owner("scan_sessions")
    uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    expansion = models.ForeignKey(Expansion, null=True, blank=True, on_delete=models.SET_NULL)
    default_condition = models.CharField(max_length=2, choices=Condition.choices, default=Condition.NEAR_MINT)
    status = models.CharField(max_length=5, choices=Status.choices, default=Status.OPEN)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = owned_manager("owner")

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Scan session {self.pk} ({self.expansion or 'all expansions'})"


class ScanLine(models.Model):
    """One draft row of a scan session: copies of a printing with the same language/foil/condition."""

    uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    session = models.ForeignKey(ScanSession, on_delete=models.CASCADE, related_name="lines")
    blueprint = models.ForeignKey(Blueprint, null=True, blank=True, on_delete=models.PROTECT)
    language = models.CharField(max_length=3, choices=Language.choices, blank=True)
    foil = models.BooleanField(default=False)
    condition = models.CharField(max_length=2, choices=Condition.choices)
    quantity = models.PositiveIntegerField(default=1)
    needs_confirmation = models.BooleanField(default=False)
    candidates = models.JSONField(default=list, blank=True, help_text="Blueprint ct_ids to choose from")
    read_name = models.CharField(max_length=200, blank=True, help_text="The card name the model read")
    frame = models.BinaryField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = owned_manager("session__owner")

    class Meta:
        ordering = ["-updated_at", "-id"]

    def __str__(self):
        return f"{self.quantity}× {self.blueprint or 'unconfirmed card'}"


class ScanCapture(models.Model):
    """One photo sent by the phone, so a retried upload is never processed (or paid) twice."""

    session = models.ForeignKey(ScanSession, on_delete=models.CASCADE, related_name="captures")
    capture_id = models.CharField(max_length=64)
    line = models.ForeignKey(ScanLine, null=True, blank=True, on_delete=models.SET_NULL, related_name="captures")
    done = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["session", "capture_id"], name="unique_scan_capture")]

    def __str__(self):
        return f"{self.capture_id} → {self.line_id or '-'}"
