from django.urls import path

from .api import catalog as catalog_api
from .api import collection as collection_api
from .api import orders as orders_api
from .api import sale as sale_api
from .api import scan as scan_api
from .api import sync as sync_api

app_name = "ledger"

urlpatterns = [
    path("api/choices/", collection_api.choices, name="api_choices"),
    path("api/collection/", collection_api.collection, name="api_collection"),
    path("api/collection/<int:pk>/", collection_api.item, name="api_item"),
    path("api/collection/import/", collection_api.import_csv, name="api_import"),
    path("api/collection/export/", collection_api.export_csv, name="api_export"),
    path("api/listings/", sale_api.listings, name="api_listings"),
    path("api/listings/publish/", sale_api.publish, name="api_publish"),
    path("api/listings/<int:item_pk>/", sale_api.listing, name="api_listing"),
    path("api/catalog/", catalog_api.catalog, name="api_catalog"),
    path("api/catalog/categories/", catalog_api.categories, name="api_catalog_categories"),
    path("api/orders/", orders_api.orders, name="api_orders"),
    path("api/orders/import/", orders_api.import_orders, name="api_orders_import"),
    path("api/orders/<int:pk>/", orders_api.order, name="api_order"),
    path("api/orders/<int:pk>/ship/", orders_api.ship, name="api_order_ship"),
    path("api/sync/", sync_api.sync, name="api_sync"),
    path("api/sync/run/", sync_api.run, name="api_sync_run"),
    path("api/sync/conflicts/<int:pk>/resolve/", sync_api.resolve, name="api_resolve"),
    path("api/scan/setup/", scan_api.setup, name="scan_api_setup"),
    path("api/scan/sessions/", scan_api.sessions, name="scan_api_sessions"),
    path("api/scan/sessions/<uuid:uid>/", scan_api.session_detail, name="scan_api_session"),
    path("api/scan/sessions/<uuid:uid>/captures/", scan_api.captures, name="scan_api_captures"),
    path("api/scan/sessions/<uuid:uid>/search/", scan_api.search, name="scan_api_search"),
    path("api/scan/sessions/<uuid:uid>/manual/", scan_api.manual, name="scan_api_manual"),
    path("api/scan/sessions/<uuid:uid>/review/", scan_api.review, name="scan_api_review"),
    path("api/scan/sessions/<uuid:uid>/save/", scan_api.save, name="scan_api_save"),
    path("api/scan/lines/<uuid:uid>/", scan_api.line_detail, name="scan_api_line"),
    path("api/scan/lines/<uuid:uid>/undo/", scan_api.line_undo, name="scan_api_undo"),
    path("api/scan/lines/<uuid:uid>/confirm/", scan_api.line_confirm, name="scan_api_confirm"),
    path("api/scan/lines/<uuid:uid>/frame.jpg", scan_api.line_frame, name="scan_api_frame"),
]
