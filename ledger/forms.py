from django import forms
from django.db.models import F
from django.db.models.functions import Now

from . import catalog
from .models import Blueprint, Card, CollectionItem, Condition, Language, Listing


def get_or_create_card(name, set_code, collector_number="", **defaults):
    """The printing (set + collector number) when the number is known, else the legacy name + set row."""
    if collector_number:
        card, _ = Card.objects.get_or_create(
            set_code=set_code, collector_number=collector_number, defaults={"name": name, **defaults}
        )
    else:
        card, _ = Card.objects.get_or_create(name=name, set_code=set_code, collector_number="", defaults=defaults)
    return card


PRODUCT_CONDITION = Condition.NEAR_MINT  # stored on products, which have no condition on CardTrader


class CatalogItemForm(forms.Form):
    """Adds copies of a catalog product (card, sealed or accessory) to the owner's collection."""

    blueprint = forms.IntegerField(label="Cardmarket product", min_value=1)
    condition = forms.ChoiceField(choices=Condition.choices, initial=Condition.NEAR_MINT, required=False)
    language = forms.ChoiceField(choices=Language.choices, initial=Language.EN, required=False)
    foil = forms.BooleanField(required=False)
    quantity = forms.IntegerField(min_value=1, initial=1)

    def __init__(self, *args, owner, **kwargs):
        super().__init__(*args, **kwargs)
        self.owner = owner

    def clean_blueprint(self):
        bp = Blueprint.objects.select_related("expansion", "category").filter(cm_product_id=self.cleaned_data["blueprint"]).first()
        if bp is None:
            raise forms.ValidationError("Unknown catalog product.")
        return bp

    def save(self):
        """(item, merged): an identical stack of the owner's gets the copies."""
        data = self.cleaned_data
        bp = data["blueprint"]
        condition, language, foil = data["condition"] or Condition.NEAR_MINT, data["language"] or Language.EN, data["foil"]
        if bp.is_product:
            condition, foil = PRODUCT_CONDITION, False
        card = catalog.card_for_blueprint(bp)
        stack = {"owner": self.owner, "card": card, "condition": condition, "language": language, "foil": foil}
        # Older rows may hold duplicate stacks (the free-text form never merged): the oldest gets the copies.
        item = CollectionItem.objects.filter(**stack).order_by("pk").first()
        if item is None:
            return CollectionItem.objects.create(**stack, quantity=data["quantity"]), False
        CollectionItem.objects.filter(pk=item.pk).update(quantity=F("quantity") + data["quantity"], updated_at=Now())
        item.refresh_from_db()
        return item, True


class ItemForm(forms.ModelForm):
    """Edits the copies of a collection item; the product they are copies of never changes."""

    class Meta:
        model = CollectionItem
        fields = ["condition", "language", "foil", "quantity"]

    def save(self, commit=True):
        if self.instance.card.is_product:
            self.instance.condition, self.instance.foil = PRODUCT_CONDITION, False
        return super().save(commit=commit)


class ListingForm(forms.ModelForm):
    class Meta:
        model = Listing
        fields = ["quantity", "price", "status"]

    def clean_quantity(self):
        qty = self.cleaned_data["quantity"]
        item = self.instance.item  # views always build the form with Listing(item=...)
        if qty > item.quantity:
            copies = "copy" if item.quantity == 1 else "copies"
            raise forms.ValidationError(f"You only own {item.quantity} {copies}.")
        if qty < 1:
            raise forms.ValidationError("List at least one copy.")
        return qty


class CsvImportForm(forms.Form):
    file = forms.FileField(label="CSV file")



class ShipForm(forms.Form):
    tracking_code = forms.CharField(max_length=120, required=False)
