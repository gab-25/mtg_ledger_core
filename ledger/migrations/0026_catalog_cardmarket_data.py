"""Re-key the local catalog from CardTrader blueprints to Cardmarket products, keeping what points at it.

The catalog itself is reloaded by the next catalog sync; here only what users' data refers to is carried over.
"""

from django.db import migrations


def rekey(apps, schema_editor):
    Blueprint = apps.get_model("ledger", "Blueprint")
    Card = apps.get_model("ledger", "Card")
    Category = apps.get_model("ledger", "Category")
    Expansion = apps.get_model("ledger", "Expansion")
    ScanLine = apps.get_model("ledger", "ScanLine")
    ScanSession = apps.get_model("ledger", "ScanSession")

    cm_by_ct = dict(Blueprint.objects.exclude(cm_price_id=None).values_list("ct_id", "cm_price_id"))

    # a. Cards learn their Cardmarket product from the blueprint they were matched to.
    for card in Card.objects.exclude(ct_blueprint_id=None):
        cm_id = cm_by_ct.get(card.ct_blueprint_id)
        if cm_id is not None:
            card.cm_product_id = cm_id
            card.save(update_fields=["cm_product_id"])

    # b. One blueprint per Cardmarket product: the base version, else the lowest CardTrader id.
    kept = {}
    for bp in Blueprint.objects.exclude(cm_price_id=None).order_by("version", "ct_id"):
        if bp.cm_price_id not in kept:
            kept[bp.cm_price_id] = bp.pk
            continue
        ScanLine.objects.filter(blueprint_id=bp.pk).update(blueprint_id=kept[bp.cm_price_id])
        bp.delete()
    orphans = Blueprint.objects.filter(cm_price_id=None)
    ScanLine.objects.filter(blueprint__in=orphans, session__status="open").delete()
    ScanLine.objects.filter(blueprint__in=orphans).update(blueprint=None)
    orphans.delete()

    # c. Candidates to confirm become Cardmarket product ids.
    for line in ScanLine.objects.exclude(candidates=[]):
        ids = []
        for ct_id in line.candidates:
            cm_id = cm_by_ct.get(ct_id)
            if cm_id is not None and cm_id not in ids:
                ids.append(cm_id)
        line.candidates = ids
        line.save(update_fields=["candidates"])

    # d. One expansion per code.
    first = {}
    for expansion in Expansion.objects.order_by("pk"):
        if expansion.code not in first:
            first[expansion.code] = expansion.pk
            continue
        Blueprint.objects.filter(expansion_id=expansion.pk).update(expansion_id=first[expansion.code])
        ScanSession.objects.filter(expansion_id=expansion.pk).update(expansion_id=first[expansion.code])
        expansion.delete()

    # e. CardTrader categories go: the sync loads Cardmarket's.
    Blueprint.objects.update(category=None)
    Category.objects.all().delete()


class Migration(migrations.Migration):
    dependencies = [("ledger", "0025_catalog_cardmarket_fields")]

    operations = [migrations.RunPython(rekey, migrations.RunPython.noop)]
