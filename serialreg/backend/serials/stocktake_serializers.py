"""盘点 API 序列化器。"""
from rest_framework import serializers

from .models import (
    StocktakeAuditLog, StocktakeBatch, StocktakeConflict, StocktakeScan,
    StocktakeSnapshotBinding, StocktakeSnapshotItem, StocktakeScopeNumber,
    Title,
)


class StocktakeCreateSerializer(serializers.Serializer):
    title = serializers.PrimaryKeyRelatedField(queryset=Title.objects.all())
    name = serializers.CharField(required=False, allow_blank=True, default="")
    scope_location = serializers.CharField(
        required=False, allow_blank=True, default="",
    )
    number_ids = serializers.ListField(
        child=serializers.IntegerField(), required=False, default=list,
    )
    note = serializers.CharField(required=False, allow_blank=True, default="")

    def validate_title(self, title):
        if StocktakeBatch.objects.filter(
            title=title,
            status__in=[StocktakeBatch.Status.OPEN,
                        StocktakeBatch.Status.REOPENED],
        ).exists():
            raise serializers.ValidationError(
                "该刊已有进行中的盘点批次，请先关闭再开新批次。",
            )
        return title


class ScopeNumberSerializer(serializers.ModelSerializer):
    volume = serializers.CharField(source="number.volume", read_only=True)
    number = serializers.CharField(source="number.number", read_only=True)

    class Meta:
        model = StocktakeScopeNumber
        fields = [
            "number_id", "volume", "number", "frozen_holding_status",
        ]


class SnapshotItemSerializer(serializers.ModelSerializer):
    barcode = serializers.CharField(source="item.barcode", read_only=True)
    issue_id = serializers.IntegerField(source="item.issue_id", read_only=True)
    frozen_actual_location = serializers.SerializerMethodField()
    current_location = serializers.SerializerMethodField()
    current_bound = serializers.SerializerMethodField()
    current_binding = serializers.SerializerMethodField()
    conflicts = serializers.SerializerMethodField()

    class Meta:
        model = StocktakeSnapshotItem
        fields = [
            "id", "item_id", "barcode", "issue_id",
            "frozen_status", "frozen_location", "frozen_item_version",
            "frozen_bound", "frozen_binding_call_number",
            "frozen_binding_location", "frozen_actual_location",
            "current_location", "current_bound", "current_binding",
            "result", "result_before_review", "observed_location",
            "baseline", "outcome", "first_seen_at", "conflicts",
        ]

    def get_frozen_actual_location(self, obj):
        return obj.frozen_actual_location()

    def get_current_location(self, obj):
        return obj.item.current_location()

    def get_current_bound(self, obj):
        return obj.item.is_bound

    def get_current_binding(self, obj):
        return obj.item.binding_entry.binding.call_number \
            if obj.item.is_bound else None

    def get_conflicts(self, obj):
        return ConflictSerializer(
            obj.conflicts.filter(status=StocktakeConflict.Status.OPEN),
            many=True,
        ).data


class SnapshotBindingSerializer(serializers.ModelSerializer):
    class Meta:
        model = StocktakeSnapshotBinding
        fields = [
            "id", "binding_id", "call_number", "frozen_location",
            "frozen_version", "frozen_item_barcodes",
        ]


class ScanSerializer(serializers.ModelSerializer):
    class Meta:
        model = StocktakeScan
        fields = [
            "id", "barcode", "scan_kind", "event_result", "is_duplicate",
            "duplicate_of", "observed_location", "resolved_barcodes",
            "scanned_by", "scanned_at",
        ]


class ConflictSerializer(serializers.ModelSerializer):
    kind_display = serializers.CharField(source="get_kind_display",
                                         read_only=True)
    status_display = serializers.CharField(source="get_status_display",
                                           read_only=True)
    barcode = serializers.SerializerMethodField()

    class Meta:
        model = StocktakeConflict
        fields = [
            "id", "snapshot_item_id", "kind", "kind_display", "detail",
            "snapshot_value", "current_value", "status", "status_display",
            "detected_at", "resolved_at", "resolved_by",
            "resolution_note", "barcode",
        ]

    def get_barcode(self, obj):
        return obj.snapshot_item.item.barcode \
            if obj.snapshot_item_id else None


class AuditLogSerializer(serializers.ModelSerializer):
    action_display = serializers.CharField(source="get_action_display",
                                           read_only=True)

    class Meta:
        model = StocktakeAuditLog
        fields = [
            "id", "action", "action_display", "detail", "payload",
            "actor", "created_at",
        ]


class StocktakeBatchSerializer(serializers.ModelSerializer):
    stats = serializers.SerializerMethodField()
    blockers = serializers.SerializerMethodField()

    class Meta:
        model = StocktakeBatch
        fields = [
            "id", "name", "title", "status", "scope_location",
            "completeness_confirmed", "completed_confirmed_at", "note",
            "closed_at", "created_by", "created_at", "stats", "blockers",
        ]

    def get_stats(self, obj):
        from .stocktake import batch_stats
        return batch_stats(obj)

    def get_blockers(self, obj):
        from .stocktake import closing_blockers
        return closing_blockers(obj)


class StocktakeDetailSerializer(StocktakeBatchSerializer):
    scope_numbers = ScopeNumberSerializer(many=True, read_only=True)
    snapshot_items = SnapshotItemSerializer(many=True, read_only=True)
    snapshot_bindings = SnapshotBindingSerializer(many=True, read_only=True)
    conflicts = ConflictSerializer(many=True, read_only=True)
    audit_logs = AuditLogSerializer(many=True, read_only=True)

    class Meta(StocktakeBatchSerializer.Meta):
        fields = StocktakeBatchSerializer.Meta.fields + [
            "scope_numbers", "snapshot_items", "snapshot_bindings",
            "conflicts", "audit_logs",
        ]


class ScanInputSerializer(serializers.Serializer):
    barcode = serializers.CharField()
    observed_location = serializers.CharField(
        required=False, allow_blank=True, default="",
    )
    scanned_by = serializers.CharField(
        required=False, allow_blank=True, default="",
    )


class ConflictResolveSerializer(serializers.Serializer):
    resolution = serializers.ChoiceField(
        choices=[("seen", "按现状确认已见"),
                 ("unseen", "按未见重新核查"),
                 ("scope", "确认范围变化")],
    )
    note = serializers.CharField(required=False, allow_blank=True, default="")
    actor = serializers.CharField(
        required=False, allow_blank=True, default="",
    )


class ConfirmLossesSerializer(serializers.Serializer):
    item_ids = serializers.ListField(
        child=serializers.IntegerField(), allow_empty=False,
    )
    actor = serializers.CharField(
        required=False, allow_blank=True, default="",
    )
