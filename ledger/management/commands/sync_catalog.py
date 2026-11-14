from django.core.management.base import BaseCommand

from ledger import tasks


class Command(BaseCommand):
    help = (
        "Queue the full catalog sync (Cardmarket products and prices, Scryfall card data) on the Celery worker."
    )

    def handle(self, *args, **options):
        result = tasks.sync_catalog.delay()
        self.stdout.write(f"Catalog sync queued (task {result.id})")
