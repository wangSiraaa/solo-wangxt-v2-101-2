from django.contrib import admin

from .models import (
    Binding, BindingEntry, Issue, IssueNumber, IssueNumbering, Item,
    StocktakeAuditLog, StocktakeBatch, StocktakeConflict, StocktakeScan,
    StocktakeSnapshotBinding, StocktakeSnapshotItem, StocktakeScopeNumber,
    Title,
)

admin.site.register(Title)
admin.site.register(IssueNumber)
admin.site.register(Issue)
admin.site.register(IssueNumbering)
admin.site.register(Item)
admin.site.register(Binding)
admin.site.register(BindingEntry)


@admin.register(StocktakeBatch)
class StocktakeBatchAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "title", "status", "scope_location",
                    "completeness_confirmed", "created_at")
    list_filter = ("status",)
    raw_id_fields = ("title",)


admin.site.register(StocktakeScopeNumber)
admin.site.register(StocktakeSnapshotItem)
admin.site.register(StocktakeSnapshotBinding)
admin.site.register(StocktakeScan)
admin.site.register(StocktakeConflict)
admin.site.register(StocktakeAuditLog)
