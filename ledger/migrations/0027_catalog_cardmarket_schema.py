from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("ledger", "0026_catalog_cardmarket_data")]

    operations = [
        migrations.RemoveField(model_name="blueprint", name="ct_id"),
        migrations.RemoveField(model_name="blueprint", name="version"),
        migrations.RemoveField(model_name="blueprint", name="editable_properties"),
        migrations.RenameField(model_name="blueprint", old_name="cm_price", new_name="cm_product"),
        migrations.AlterField(
            model_name="blueprint",
            name="cm_product",
            field=models.OneToOneField(
                blank=True, null=True, db_constraint=False, on_delete=django.db.models.deletion.DO_NOTHING, related_name="blueprint",
                to="ledger.cardmarketprice", verbose_name="Cardmarket product",
                help_text="Cardmarket's idProduct; its price row may not exist",
            ),
        ),
        migrations.AlterField(
            model_name="blueprint",
            name="image_url",
            field=models.URLField(blank=True, max_length=500, help_text="Scryfall's small image"),
        ),
        migrations.AlterField(
            model_name="blueprint",
            name="image_large_url",
            field=models.URLField(blank=True, max_length=500, help_text="Scryfall's normal image (488×680)"),
        ),
        migrations.RemoveField(model_name="expansion", name="ct_id"),
        migrations.RemoveField(model_name="expansion", name="blueprints_synced_at"),
        migrations.AlterField(
            model_name="expansion",
            name="code",
            field=models.CharField(max_length=10, unique=True, help_text="Scryfall set code, lower case"),
        ),
        migrations.RenameField(model_name="category", old_name="ct_id", new_name="cm_id"),
        migrations.AlterField(
            model_name="category",
            name="cm_id",
            field=models.PositiveIntegerField(unique=True, verbose_name="Cardmarket category"),
        ),
        migrations.AlterField(
            model_name="scanline",
            name="candidates",
            field=models.JSONField(blank=True, default=list, help_text="Cardmarket product ids to choose from"),
        ),
    ]
