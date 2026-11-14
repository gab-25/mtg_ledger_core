from django.contrib import admin

from .models import Blueprint, Card, CardmarketPrice, Category, Expansion, CollectionItem, Listing, Order, OrderItem, SyncConflict, SyncLog, SyncService


@admin.register(Card)
class CardAdmin(admin.ModelAdmin):
    list_display = ["name", "set_code", "collector_number", "ct_blueprint_id"]
    list_filter = ["set_code"]
    search_fields = ["name"]


@admin.register(CollectionItem)
class CollectionItemAdmin(admin.ModelAdmin):
    list_display = ["card", "owner", "condition", "language", "foil", "quantity"]
    list_filter = ["owner", "condition", "language", "foil"]
    search_fields = ["card__name"]
    autocomplete_fields = ["card"]
    raw_id_fields = ["owner"]


@admin.register(Listing)
class ListingAdmin(admin.ModelAdmin):
    list_display = ["item", "quantity", "price", "status", "ct_product_id"]
    list_filter = ["status"]


@admin.register(SyncService)
class SyncServiceAdmin(admin.ModelAdmin):
    list_display = ["name", "user", "ct_user_id", "last_sync"]
    list_filter = ["user"]
    raw_id_fields = ["user"]


@admin.register(SyncConflict)
class SyncConflictAdmin(admin.ModelAdmin):
    list_display = ["item", "service", "field", "local_value", "remote_value", "resolution"]
    list_filter = ["resolution", "service"]


@admin.register(SyncLog)
class SyncLogAdmin(admin.ModelAdmin):
    list_display = ["created_at", "owner", "level", "message"]
    list_filter = ["level", "owner"]
    raw_id_fields = ["owner"]


class OrderItemInline(admin.TabularInline):
    model = OrderItem
    extra = 0
    raw_id_fields = ["listing"]


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = ["code", "owner", "state", "buyer_username", "ship_country_code", "size", "total", "paid_at", "sent_at"]
    list_filter = ["owner", "state", "via_cardtrader_zero"]
    raw_id_fields = ["owner"]
    search_fields = ["code", "buyer_username", "ship_name"]
    inlines = [OrderItemInline]
    readonly_fields = ["raw"]


@admin.register(Expansion)
class ExpansionAdmin(admin.ModelAdmin):
    list_display = ["code", "name", "released_at"]
    search_fields = ["code", "name"]


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ["name", "cm_id"]
    search_fields = ["name"]


@admin.register(Blueprint)
class BlueprintAdmin(admin.ModelAdmin):
    list_display = ["name", "expansion", "category", "collector_number", "rarity", "cm_product_id"]
    list_filter = ["category"]
    search_fields = ["name", "expansion__code"]
    list_select_related = ["expansion", "category"]


@admin.register(CardmarketPrice)
class CardmarketPriceAdmin(admin.ModelAdmin):
    list_display = ["id_product", "trend", "trend_foil", "low", "avg", "prices_date"]
    search_fields = ["id_product"]
