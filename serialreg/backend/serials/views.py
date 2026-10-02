from django.db.models import Exists, OuterRef, Prefetch
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from .models import (
    Binding, Issue, IssueNumber, IssueNumbering, Item,
    StocktakeBatch, StocktakeSnapshotItem, Title,
    locate_number, number_holding_status,
)
from .serializers import (
    BindingSerializer, IssueSerializer, ItemSerializer,
    IssueNumberSerializer, TitleSerializer, UnbindSerializer,
)


def _active_stocktake(title_id):
    """该刊当前进行中的盘点批次（可能为 None）。"""
    return (
        StocktakeBatch.objects.filter(
            title_id=title_id,
            status__in=[StocktakeBatch.Status.OPEN,
                        StocktakeBatch.Status.REOPENED],
        )
        .prefetch_related("snapshot_items")
        .order_by("-created_at")
        .first()
    )


def _item_stocktake_marks(active_batch):
    """item_id -> 该实体在进行中批次里的盘点结论（含快照范围标记）。"""
    if active_batch is None:
        return {}
    return {
        row.item_id: {
            "in_scope": True,
            "result": row.result,
            "result_label": dict(
                StocktakeSnapshotItem.Result.choices).get(row.result,
                                                          row.result),
            "outcome": row.outcome,
            "observed_location": row.observed_location,
            "frozen_actual_location": row.frozen_actual_location(),
        }
        for row in active_batch.snapshot_items.all()
    }


class TitleViewSet(viewsets.ModelViewSet):
    queryset = Title.objects.all()
    serializer_class = TitleSerializer


class IssueNumberViewSet(viewsets.ModelViewSet):
    queryset = IssueNumber.objects.all()
    serializer_class = IssueNumberSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        title_id = self.request.query_params.get("title")
        if title_id:
            qs = qs.filter(title_id=title_id)
        return qs


class IssueViewSet(viewsets.ModelViewSet):
    queryset = Issue.objects.prefetch_related(
        "numberings__number", "items",
    ).select_related("title")
    serializer_class = IssueSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        title_id = self.request.query_params.get("title")
        if title_id:
            qs = qs.filter(title_id=title_id)
        return qs


class ItemViewSet(viewsets.ModelViewSet):
    queryset = Item.objects.select_related(
        "title", "issue", "binding_entry__binding",
    ).prefetch_related("issue__numbers")
    serializer_class = ItemSerializer

    @action(detail=False, methods=["get"])
    def locate(self, request):
        """按 (title, volume, number) 或 barcode 定位实物。

        合刊的任一期号都必须能找到同一实物；装订后返回装订册位置。
        """
        title_id = request.query_params.get("title")
        volume = request.query_params.get("volume", "")
        number = request.query_params.get("number")
        barcode = request.query_params.get("barcode")

        if barcode:
            items = self.get_queryset().filter(barcode=barcode)
            active = _active_stocktake(
                items.first().title_id) if items.exists() else None
            marks = _item_stocktake_marks(active)
            result = []
            for it in items:
                result.append({
                    "barcode": it.barcode,
                    "issue_id": it.issue_id,
                    "numbers": [
                        {"volume": n.volume, "number": n.number}
                        for n in it.issue.numbers.all()
                    ],
                    "location": it.current_location(),
                    "bound": it.is_bound,
                    "binding": it.binding_entry.binding.call_number
                    if it.is_bound else None,
                    "status": it.status,
                    "stocktake": (
                        {"batch_id": active.id, **marks[it.id]}
                        if active is not None and it.id in marks else None
                    ),
                })
            return Response({
                "query": {"barcode": barcode},
                "active_stocktake": (
                    {"id": active.id, "name": str(active),
                     "status": active.status,
                     "scope_location": active.scope_location}
                    if active is not None else None
                ),
                "matches": result,
            })

        if not (title_id and number):
            return Response(
                {"detail": "需要提供 barcode，或同时提供 title 与 number。"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        qs = IssueNumber.objects.filter(
            title_id=title_id, number=number,
        )
        if volume != "":
            qs = qs.filter(volume=volume)
        try:
            issue_number = qs.get()
        except IssueNumber.DoesNotExist:
            # 编号本身未登记：区别于「已登记但无发行」的缺号
            return Response({
                "detail": "该卷期编号未在馆藏系统登记。",
                "holding_status": "unregistered",
                "matches": [],
            }, status=status.HTTP_404_NOT_FOUND)
        except IssueNumber.MultipleObjectsReturned:
            return Response(
                {"detail": "卷/期定位到多条编号，请补全卷号。"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        matches = locate_number(issue_number)
        active = _active_stocktake(title_id)
        marks = _item_stocktake_marks(active)
        for row in matches:
            mark = None
            if active is not None:
                # locate_number 只带 barcode，反查 item id 取盘点标记
                item = Item.objects.filter(barcode=row["barcode"]).first()
                if item is not None and item.id in marks:
                    mark = {"batch_id": active.id, **marks[item.id]}
            row["stocktake"] = mark
        # 缺号（无发行记录）是正常业务状态，返回 200，不自动等同缺藏
        return Response({
            "query": {"title": title_id, "volume": volume, "number": number},
            "holding_status": number_holding_status(issue_number.title, issue_number),
            "matches": matches,
            "active_stocktake": (
                {"id": active.id, "name": str(active),
                 "status": active.status,
                 "scope_location": active.scope_location}
                if active is not None else None
            ),
        })


class BindingViewSet(viewsets.ModelViewSet):
    queryset = Binding.objects.prefetch_related(
        "entries__item",
    ).select_related("title")
    serializer_class = BindingSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        title_id = self.request.query_params.get("title")
        if title_id:
            qs = qs.filter(title_id=title_id)
        return qs

    @action(detail=False, methods=["post"])
    def unbind(self, request):
        serializer = UnbindSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        items = serializer.save()
        return Response({
            "detail": "拆订完成，各实物已恢复原位置。",
            "restored": [
                {"barcode": it.barcode, "location": it.location,
                 "status": it.status}
                for it in items
            ],
        })


class TimelineViewSet(viewsets.ViewSet):
    """前端时间轴数据源：编号 × 发行 × 实物三层，外加停刊标记。"""

    def list(self, request):
        title_id = request.query_params.get("title")
        if not title_id:
            return Response(
                {"detail": "需要提供 title 参数。"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        title = Title.objects.get(pk=title_id)
        active = _active_stocktake(title_id)
        marks = _item_stocktake_marks(active)
        numbers = (
            IssueNumber.objects.filter(title=title)
            .prefetch_related(
                Prefetch(
                    "issues",
                    queryset=Issue.objects.prefetch_related(
                        Prefetch(
                            "numberings",
                            queryset=IssueNumbering.objects.select_related("number"),
                        ),
                        Prefetch(
                            "items",
                            queryset=Item.objects.select_related(
                                "binding_entry__binding",
                            ),
                        ),
                    ),
                ),
            )
            .order_by("sort_key", "id")
        )
        slots = []
        for n in numbers:
            issues = list(n.issues.all())
            items = [it for iss in issues for it in iss.items.all()]
            slots.append({
                "number_id": n.id,
                "volume": n.volume,
                "number": n.number,
                "holding_status": number_holding_status(title, n),
                "issues": [
                    {
                        "issue_id": iss.id,
                        "kind": iss.kind,
                        "issue_month": iss.issue_month,
                        "issue_month_end": iss.issue_month_end,
                        "label": "·".join(
                            f"{nn.number.volume}({nn.number.number})"
                            for nn in iss.numberings.all()
                        ),
                        "combined_numbers": [
                            {"volume": nn.number.volume, "number": nn.number.number}
                            for nn in iss.numberings.all()
                        ],
                        "items": [
                            {
                                "item_id": it.id,
                                "barcode": it.barcode,
                                "status": it.status,
                                "location": it.current_location(),
                                "bound": it.is_bound,
                                "binding": it.binding_entry.binding.call_number
                                if it.is_bound else None,
                                "stocktake": (
                                    {"batch_id": active.id, **marks[it.id]}
                                    if active is not None and it.id in marks
                                    else None
                                ),
                            }
                            for it in iss.items.all()
                        ],
                    }
                    for iss in issues
                ],
            })
        return Response({
            "title": TitleSerializer(title).data,
            "slots": slots,
            "active_stocktake": (
                {
                    "id": active.id, "name": str(active),
                    "status": active.status,
                    "scope_location": active.scope_location,
                    "completeness_confirmed":
                        active.completeness_confirmed,
                }
                if active is not None else None
            ),
        })
