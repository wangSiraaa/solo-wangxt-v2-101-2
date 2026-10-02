"""盘点批次 API。

典型流程：
  POST   /api/stocktakes/                 启动盘点（冻结快照）
  GET    /api/stocktakes/                 批次列表（?title= 过滤；?active=1）
  GET    /api/stocktakes/{id}/            批次详情：快照、结论、冲突、审计
  POST   /api/stocktakes/{id}/scan/       扫实体条码 / 装订册条码（幂等）
  POST   /api/stocktakes/{id}/refresh/    重新比较版本，发现并发变更冲突
  POST   /api/stocktakes/{id}/resolve_conflict/
  POST   /api/stocktakes/{id}/confirm_complete/   声明范围完整
  POST   /api/stocktakes/{id}/close/      关闭（未确认完整/有冲突会被阻止）
  POST   /api/stocktakes/{id}/confirm_losses/     显式确认遗失
  POST   /api/stocktakes/{id}/reopen/     重新打开（状态仍可追溯）
"""
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from .models import (
    StocktakeBatch, StocktakeConflict, StocktakeSnapshotItem,
)
from . import stocktake as svc
from .stocktake_serializers import (
    ConflictResolveSerializer, ConfirmLossesSerializer, ScanInputSerializer,
    SnapshotItemSerializer, StocktakeBatchSerializer,
    StocktakeCreateSerializer, StocktakeDetailSerializer,
)


def _batch_detail_qs():
    return StocktakeBatch.objects.prefetch_related(
        "scope_numbers__number",
        "snapshot_items__item__binding_entry__binding",
        "snapshot_items__item__issue__numbers",
        "snapshot_items__conflicts",
        "snapshot_bindings",
        "conflicts__snapshot_item__item",
        "audit_logs",
    )


class StocktakeViewSet(viewsets.ViewSet):
    lookup_value_regex = r"\d+"

    def list(self, request):
        qs = StocktakeBatch.objects.select_related("title").order_by(
            "-created_at")
        title_id = request.query_params.get("title")
        if title_id:
            qs = qs.filter(title_id=title_id)
        state = request.query_params.get("status")
        if state in ("open", "closed", "reopened"):
            qs = qs.filter(status=state)
        if request.query_params.get("active"):
            qs = qs.filter(status__in=[
                StocktakeBatch.Status.OPEN,
                StocktakeBatch.Status.REOPENED,
            ])
        return Response(
            StocktakeBatchSerializer(qs, many=True).data)

    def retrieve(self, request, pk=None):
        batch = _batch_detail_qs().get(pk=pk)
        return Response(StocktakeDetailSerializer(batch).data)

    def create(self, request):
        serializer = StocktakeCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            batch = svc.create_batch(
                title=data["title"],
                name=data.get("name", ""),
                scope_location=data.get("scope_location", ""),
                number_ids=data.get("number_ids") or None,
                note=data.get("note", ""),
                actor=request.data.get("actor", ""),
            )
        except ValueError as exc:
            return Response({"detail": str(exc)},
                            status=status.HTTP_400_BAD_REQUEST)
        batch = _batch_detail_qs().get(pk=batch.id)
        return Response(
            StocktakeDetailSerializer(batch).data,
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["post"])
    def scan(self, request, pk=None):
        batch = StocktakeBatch.objects.get(pk=pk)
        serializer = ScanInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            result = svc.scan_barcode(
                batch,
                serializer.validated_data["barcode"],
                observed_location=serializer.validated_data.get(
                    "observed_location", ""),
                actor=serializer.validated_data.get("scanned_by", ""),
            )
        except ValueError as exc:
            return Response({"detail": str(exc)},
                            status=status.HTTP_400_BAD_REQUEST)
        return Response({
            "detail": _scan_message(result),
            "scan_id": result["scan"].id,
            "event_result": result["event_result"],
            "duplicate": result["duplicate"],
            "original_scan_id": result["original"].id,
            "mapped_barcodes": result["scan"].resolved_barcodes,
            "snapshot_items": SnapshotItemSerializer(
                result["snapshot_items"], many=True).data,
            "conflict_ids": [c.id for c in result["conflicts"]],
        })

    @action(detail=True, methods=["post"], url_path="refresh")
    def refresh_drift(self, request, pk=None):
        batch = StocktakeBatch.objects.get(pk=pk)
        if not batch.is_open:
            return Response(
                {"detail": "批次已关闭；重新打开后才能刷新比较版本。"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        found = svc.refresh_drift(batch, actor=request.data.get("actor", ""))
        unique = {c.id for c in found}
        return Response({
            "detail": (f"发现 {len(unique)} 条待处理冲突。"
                       if found else "快照与现状一致，无新增冲突。"),
            "conflict_ids": list(unique),
        })

    @action(detail=True, methods=["post"],
            url_path="resolve_conflict")
    def resolve_conflict(self, request, pk=None):
        batch = StocktakeBatch.objects.get(pk=pk)
        conflict = StocktakeConflict.objects.filter(
            id=request.data.get("conflict_id"), batch=batch,
        ).first()
        if conflict is None:
            return Response({"detail": "冲突不存在或不属于该批次。"},
                            status=status.HTTP_404_NOT_FOUND)
        serializer = ConflictResolveSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        if (conflict.kind == StocktakeConflict.Kind.SCOPE_CHANGED
                and serializer.validated_data["resolution"] != "scope"):
            return Response(
                {"detail": "范围变化冲突只能以“确认范围变化(scope)”处理。"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            svc.resolve_conflict(
                conflict,
                resolution=serializer.validated_data["resolution"],
                note=serializer.validated_data.get("note", ""),
                actor=serializer.validated_data.get("actor", ""),
            )
        except ValueError as exc:
            return Response({"detail": str(exc)},
                            status=status.HTTP_400_BAD_REQUEST)
        return Response({"detail": "冲突已处理。", "conflict_id": conflict.id})

    @action(detail=True, methods=["post"], url_path="confirm_complete")
    def confirm_complete(self, request, pk=None):
        batch = StocktakeBatch.objects.get(pk=pk)
        try:
            svc.confirm_complete(batch, actor=request.data.get("actor", ""))
        except ValueError as exc:
            return Response({"detail": str(exc)},
                            status=status.HTTP_400_BAD_REQUEST)
        return Response({"detail": "范围完整性已确认，可以关闭批次。"})

    @action(detail=True, methods=["post"])
    def close(self, request, pk=None):
        batch = StocktakeBatch.objects.get(pk=pk)
        actor = request.data.get("actor", "")
        # 先在独立事务里完成版本比较：冲突即使导致关闭被阻止也会保留
        blockers = svc.prepare_close(batch, actor=actor)
        if blockers:
            return Response({"detail": "  ".join(blockers)},
                            status=status.HTTP_409_CONFLICT)
        _, candidates = svc.close_batch(
            batch, actor=actor, _already_refreshed=True)
        return Response({
            "detail": f"批次已关闭，{len(candidates)} 个已发行未见实体"
                      "进入遗失候选（尚未转丢失）。",
            "lost_candidates": [
                {"snapshot_item_id": s.id, "barcode": s.item.barcode}
                for s in candidates
            ],
        })

    @action(detail=True, methods=["post"], url_path="confirm_losses")
    def confirm_losses(self, request, pk=None):
        batch = StocktakeBatch.objects.get(pk=pk)
        serializer = ConfirmLossesSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            confirmed = svc.confirm_losses(
                batch, serializer.validated_data["item_ids"],
                actor=serializer.validated_data.get("actor", ""),
            )
        except ValueError as exc:
            return Response({"detail": str(exc)},
                            status=status.HTTP_400_BAD_REQUEST)
        return Response({
            "detail": f"已确认 {len(confirmed)} 个实体遗失。",
            "confirmed_barcodes": confirmed,
        })

    @action(detail=True, methods=["post"])
    def reopen(self, request, pk=None):
        batch = StocktakeBatch.objects.get(pk=pk)
        try:
            svc.reopen_batch(batch, actor=request.data.get("actor", ""))
        except ValueError as exc:
            return Response({"detail": str(exc)},
                            status=status.HTTP_400_BAD_REQUEST)
        return Response({"detail": "批次已重新打开，历史状态保留可追溯。"})


def _scan_message(result):
    if result["duplicate"]:
        return "重复扫描：只计一次，保留原始事件，结论不变。"
    labels = {
        "recorded": "扫描已记录。",
        "out_of_scope": "条码不在本批次盘点范围内，已记录但不改结论。",
        "unknown": "未知条码，未匹配到实体或装订册。",
        "duplicate": "重复扫描。",
    }
    msg = labels.get(result["event_result"], "扫描已记录。")
    if result["conflicts"]:
        msg += " 发现盘点期间的并发变更，相关实体已置为待核查。"
    return msg
