"""盘点批次 API。

路由（挂在 /api/stocktakes/ 下）：
  GET  /stocktakes/?title=            批次列表（可按刊过滤）
  POST /stocktakes/                   启动盘点（冻结快照）
  GET  /stocktakes/{id}/              批次详情：快照结果 / 冲突 / 范围完整性
  POST /stocktakes/{id}/scan/         扫描实物条码或装订册（幂等）
  POST /stocktakes/{id}/review/       人工复核待核查/错架实物
  POST /stocktakes/{id}/conflicts/{cid}/resolve/  解决冲突
  POST /stocktakes/{id}/close/        关闭：先报告完整性，confirm=true 才判失
  POST /stocktakes/{id}/reopen/       重新打开，撤销关闭时判失，记录可追溯
"""
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from . import stocktake as svc
from .models import (
    Stocktake, StocktakeConflict, StocktakeItemSnapshot,
)
from .serializers import (
    StocktakeCloseSerializer, StocktakeCreateSerializer,
    StocktakeReviewSerializer, StocktakeScanSerializer,
)


RESULT_LABELS = {
    "pending": "未见",
    "seen": "已见",
    "misplaced": "错架",
    "review": "待核查",
    "lost": "盘点遗失",
}


def _snapshot_rows(stocktake):
    return [
        {
            "item_id": s.item_id,
            "barcode": s.item.barcode,
            "issue_id": s.issue_id,
            "number_ids": s.number_ids,
            "published": s.published,
            "result": s.result,
            "result_label": RESULT_LABELS.get(s.result, s.result),
            "status_snapshot": s.status_snapshot,
            "snapshot_location": s.effective_location_snapshot,
            "snapshot_binding": s.binding_call_snapshot or None,
            "observed_location": s.observed_location,
            "seen_at": s.seen_at,
            "current_location": s.item.current_location(),
            "current_status": s.item.status,
        }
        for s in stocktake.item_snapshots.select_related(
            "item", "item__binding_entry__binding",
        ).order_by("item__barcode")
    ]


def _binding_rows(stocktake):
    return [
        {
            "binding_id": b.binding_id,
            "call_number": b.binding_call,
            "snapshot_location": b.location_snapshot,
            "snapshot_item_ids": b.item_ids,
            "exists": b.binding_id is not None,
        }
        for b in stocktake.binding_snapshots.order_by("binding_call")
    ]


def _conflict_rows(stocktake):
    return [
        {
            "id": c.id,
            "kind": c.kind,
            "detail": c.detail,
            "status": c.status,
            "item_id": c.item_id,
            "item_barcode": c.item.barcode if c.item_id else None,
            "binding_id": c.binding_id,
            "snapshot_value": c.snapshot_value,
            "current_value": c.current_value,
            "created_at": c.created_at,
            "resolved_at": c.resolved_at,
        }
        for c in stocktake.conflicts.select_related("item", "binding")
    ]


def _scan_rows(stocktake):
    return [
        {
            "id": e.id,
            "target_type": e.target_type,
            "target_id": e.target_id,
            "barcode": e.barcode,
            "observed_location": e.observed_location,
            "resolved_item_ids": e.resolved_item_ids,
            "first_seen_item_ids": e.first_seen_item_ids,
            "duplicate": not e.first_seen_item_ids,
            "created_at": e.created_at,
        }
        for e in stocktake.scans.all()
    ]


def serialize_batch(stocktake, *, detail=False):
    """批次序列化。detail=True 时附带快照/冲突/扫描审计。"""
    report = svc.completeness(stocktake)
    data = {
        "id": stocktake.id,
        "name": str(stocktake),
        "title": stocktake.title_id,
        "scope_volume": stocktake.scope_volume,
        "scope_label": stocktake.scope_label,
        "state": stocktake.state,
        "note": stocktake.note,
        "created_at": stocktake.created_at,
        "closed_at": stocktake.closed_at,
        "completeness": report,
    }
    if detail:
        data.update({
            "items": _snapshot_rows(stocktake),
            "bindings": _binding_rows(stocktake),
            "conflicts": _conflict_rows(stocktake),
            "scans": _scan_rows(stocktake),
        })
    return data


class StocktakeViewSet(viewsets.ViewSet):
    def list(self, request):
        qs = Stocktake.objects.select_related("title")
        title_id = request.query_params.get("title")
        if title_id:
            qs = qs.filter(title_id=title_id)
        state = request.query_params.get("state")
        if state:
            qs = qs.filter(state=state)
        return Response([
            serialize_batch(b) for b in qs.order_by("-created_at")
        ])

    def retrieve(self, request, pk=None):
        stocktake = self._get(pk)
        return Response(serialize_batch(stocktake, detail=True))

    def create(self, request):
        serializer = StocktakeCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        batch = svc.create_stocktake(
            title=data["title"],
            scope_volume=data.get("scope_volume", ""),
            name=data.get("name", ""),
            note=data.get("note", ""),
        )
        return Response(
            serialize_batch(batch, detail=True),
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["post"])
    def scan(self, request, pk=None):
        stocktake = self._get(pk)
        serializer = StocktakeScanSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            result = svc.scan(
                stocktake,
                serializer.validated_data["barcode"],
                serializer.validated_data.get("observed_location", ""),
            )
        except ValueError as exc:
            return Response({"detail": str(exc)},
                            status=status.HTTP_400_BAD_REQUEST)
        if not result.get("ok"):
            return Response(
                {"detail": result["detail"]},
                status=status.HTTP_404_NOT_FOUND,
            )
        result["batch"] = svc.completeness(stocktake)
        return Response(result)

    @action(detail=True, methods=["post"], url_path="review")
    def review(self, request, pk=None):
        stocktake = self._get(pk)
        serializer = StocktakeReviewSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            svc.review_snapshot(
                stocktake,
                item_id=serializer.validated_data["item_id"],
                result=serializer.validated_data["result"],
                observed_location=serializer.validated_data.get(
                    "observed_location"),
            )
        except StocktakeItemSnapshot.DoesNotExist:
            return Response(
                {"detail": "该实物不在本批次快照范围内。"},
                status=status.HTTP_404_NOT_FOUND,
            )
        except ValueError as exc:
            return Response({"detail": str(exc)},
                            status=status.HTTP_400_BAD_REQUEST)
        fresh = self._get(pk)
        return Response(serialize_batch(fresh, detail=True))

    @action(detail=True, methods=["post"],
            url_path=r"conflicts/(?P<conflict_id>[0-9]+)/resolve")
    def resolve_conflict(self, request, pk=None, conflict_id=None):
        stocktake = self._get(pk)
        try:
            svc.resolve_conflict(stocktake, conflict_id)
        except StocktakeConflict.DoesNotExist:
            return Response(
                {"detail": "冲突不存在。"},
                status=status.HTTP_404_NOT_FOUND,
            )
        fresh = self._get(pk)
        return Response(serialize_batch(fresh, detail=True))

    @action(detail=True, methods=["post"])
    def close(self, request, pk=None):
        stocktake = self._get(pk)
        serializer = StocktakeCloseSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        result = svc.close_stocktake(
            stocktake, confirm=serializer.validated_data["confirm"],
        )
        # 「试关闭」（未带 confirm）始终返回 200 + 完整性报告，
        # 前端据此展示遗失候选与阻止原因；真正 confirm 关闭时若范围
        # 不完整才返回 409。
        if result.get("blocked") and serializer.validated_data["confirm"]:
            return Response(result, status=status.HTTP_409_CONFLICT)
        if result.get("closed") or result.get("already_closed"):
            fresh = self._get(pk)
            return Response(serialize_batch(fresh, detail=True))
        return Response(result)

    @action(detail=True, methods=["post"])
    def reopen(self, request, pk=None):
        stocktake = self._get(pk)
        svc.reopen_stocktake(stocktake)
        fresh = Stocktake.objects.select_related("title").get(pk=pk)
        return Response(serialize_batch(fresh, detail=True))

    def _get(self, pk):
        return Stocktake.objects.select_related("title").get(pk=pk)
