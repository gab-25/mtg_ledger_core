from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("ledger", "0024_scan_without_prices")]

    operations = [
        migrations.AddField(
            model_name="card",
            name="cm_product_id",
            field=models.PositiveIntegerField(
                blank=True, db_index=True, null=True, verbose_name="Cardmarket product",
                help_text="Its printing in the catalog (Blueprint); the catalog sync keeps one card per product",
            ),
        ),
        migrations.AddField(
            model_name="expansion",
            name="released_at",
            field=models.DateField(blank=True, null=True),
        ),
    ]
