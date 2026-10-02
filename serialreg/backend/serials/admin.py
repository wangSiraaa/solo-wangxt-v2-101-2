from django.contrib import admin

from .models import (
    Binding, BindingEntry, Issue, IssueNumber, IssueNumbering, Item,
    Stocktake, StocktakeBindingSnapshot, StocktakeConflict,
    StocktakeItemSnapshot, StocktakeScan, Title,
)

admin.site.register(Title)
admin.site.register(IssueNumber)
admin.site.register(Issue)
admin.site.register(IssueNumbering)
admin.site.register(Item)
admin.site.register(Binding)
admin.site.register(BindingEntry)


class StocktakeItemSnapshotInline(admin.TabularInline):
    model = StocktakeItemSnapshot
    extra = 0
    readonly_fields = (
        "item", "issue_id", "number_ids", "status_snapshot",
        "effective_location_snapshot", "binding_id_snapshot",
        "binding_call_snapshot", "item_version", "binding_version",
        "published", "result", "observed_location", "seen_at",
    )


class StocktakeScanInline(admin.TabularInline):
    model = StocktakeScan
    extra = 0
    readonly_fields = (
        "target_type", "target_id", "barcode", "observed_location",
        "resolved_item_ids", "first_seen_item_ids", "duplicate", "created_at",
    )


class StocktakeConflictInline(admin.TabularInline):
    model = StocktakeConflict
    extra = 0
    readonly_fields = (
        "item", "binding", "kind", "detail",
        "snapshot_value", "current_value", "status",
        "created_at", "resolved_at",
    )


@admin.register(Stocktake)
class StocktakeAdmin(admin.ModelAdmin):
    list_display = ("id", "title", "scope_volume", "state", "created_at")
    list_filter = ("state",)
    inlines = [
        StocktakeItemSnapshotInline,
        StocktakeConflictInline,
        StocktakeScanInline,
    ]


admin.site.register(StocktakeBindingSnapshot)
admin.site.register(StocktakeItemSnapshot)
admin.site.register(StocktakeScan)
admin.site.register(StocktakeConflict)
