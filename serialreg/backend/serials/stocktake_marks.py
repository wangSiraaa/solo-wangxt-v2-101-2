"""盘点标记辅助：时间轴 / 定位页复用，把进行中盘点的结果挂到实物上。"""
from .models import Stocktake, StocktakeItemSnapshot


def resolve_stocktake(title_id, stocktake_id=None):
    """按显式 id 找到批次；否则取该刊最近一个「盘点中」批次；都没有返回 None。"""
    if stocktake_id:
        return Stocktake.objects.filter(
            id=stocktake_id, title_id=title_id,
        ).first()
    return Stocktake.objects.filter(
        title_id=title_id, state=Stocktake.State.OPEN,
    ).order_by("-created_at").first()


def item_marks(stocktake):
    """返回 {item_id: 盘点标记}，供时间轴/定位行内联展示。"""
    if stocktake is None:
        return {}
    qs = StocktakeItemSnapshot.objects.filter(stocktake=stocktake)
    marks = {}
    for s in qs:
        marks[s.item_id] = {
            "stocktake_id": stocktake.id,
            "stocktake_name": str(stocktake),
            "result": s.result,
            "observed_location": s.observed_location,
        }
    return marks


def binding_conflict_items(stocktake):
    """进行中盘点里存在未解决冲突的实物 id 集合。"""
    if stocktake is None:
        return set()
    return set(
        stocktake.conflicts.filter(status="open")
        .exclude(item=None)
        .values_list("item_id", flat=True),
    )
