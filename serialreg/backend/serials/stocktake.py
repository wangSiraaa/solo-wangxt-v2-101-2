"""盘点领域服务：冻结快照、幂等扫描、版本比较与冲突、关闭与遗失确认。

所有判定都以批次启动时的快照为基准，绝不修改在扫实体本身：

  冻结   该范围内的实体字段、装订关系、实际位置、版本号，以及编号槽位视图
  扫描   实体条码 → 该实体；装订册条码 → 快照中册内 *全部* 实体（不复制实体）
  冲突   盘点期间实体被移动/修改/装订/拆订、装订册被移动、范围变化
         → 比较快照版本与关系，生成待处理冲突，旧扫描不覆盖新位置
  关闭   必须先确认范围完整、无待处理冲突；not_published / ceased_gap
         永远不进入遗失候选；只有“已发行未见”成为遗失候选，
         且再经显式确认才会转为丢失
"""
from django.db import transaction
from django.utils import timezone

from .models import (
    Binding, BindingEntry, IssueNumber, Item, StocktakeAuditLog,
    StocktakeBatch, StocktakeConflict, StocktakeScan, StocktakeSnapshotBinding,
    StocktakeSnapshotItem, StocktakeScopeNumber,
    number_holding_status,
)

# 永远不参与“缺藏 → 遗失”判定的缺号视图
GAP_STATUSES = ("not_published", "ceased_gap")


# --------------------------------------------------------------------------
# 审计
# --------------------------------------------------------------------------

def _audit(batch, action, detail="", payload=None, actor=""):
    return StocktakeAuditLog.objects.create(
        batch=batch, action=action, detail=detail,
        payload=payload or {}, actor=actor,
    )


# --------------------------------------------------------------------------
# 启动盘点：冻结快照
# --------------------------------------------------------------------------

@transaction.atomic
def create_batch(*, title, name="", scope_location="", number_ids=None,
                 note="", actor=""):
    """创建批次并冻结范围内的编号槽位、实体与装订册快照。"""
    batch = StocktakeBatch.objects.create(
        title=title, name=(name or "").strip(),
        scope_location=(scope_location or "").strip(), note=note,
        created_by=actor,
    )

    numbers = list(IssueNumber.objects.filter(title=title).order_by(
        "sort_key", "id"))
    if number_ids:
        wanted = {int(i) for i in number_ids}
        numbers = [n for n in numbers if n.id in wanted]
        if {n.id for n in numbers} != wanted:
            raise ValueError("部分编号不属于该刊或不存在。")

    # 1) 编号槽位视图快照（缺号 / 缺藏 / 已入藏）
    StocktakeScopeNumber.objects.bulk_create([
        StocktakeScopeNumber(
            batch=batch, number=n,
            frozen_holding_status=number_holding_status(title, n),
        )
        for n in numbers
    ])

    # 2) 实体快照：范围 = 选中槽位所覆盖发行期的全部实体，再按库位过滤
    item_qs = (
        Item.objects.filter(title=title)
        .select_related("binding_entry__binding", "issue")
        .prefetch_related("issue__numbers")
    )
    if numbers:
        item_qs = item_qs.filter(issue__numbers__in=numbers).distinct()

    snap_rows = []
    bound_ids = set()
    for it in item_qs.order_by("barcode"):
        entry = getattr(it, "binding_entry", None)
        binding = entry.binding if entry is not None else None
        actual = binding.location if binding is not None else it.location
        if batch.scope_location and actual != batch.scope_location:
            continue
        snap_rows.append(StocktakeSnapshotItem(
            batch=batch, item=it,
            frozen_status=it.status,
            frozen_location=it.location or "",
            frozen_item_version=it.version,
            frozen_bound=binding is not None,
            frozen_binding=binding,
            frozen_binding_call_number=binding.call_number if binding else "",
            frozen_binding_location=binding.location if binding else "",
            frozen_binding_version=binding.version if binding else None,
        ))
        if binding is not None:
            bound_ids.add(binding.id)
    StocktakeSnapshotItem.objects.bulk_create(snap_rows)

    # 3) 装订册快照：册内只要有一个实体在范围内，整册冻结（扫册条码即展开）
    snap_bindings = []
    for binding in Binding.objects.filter(
        id__in=bound_ids,
    ).prefetch_related("entries__item"):
        snap_bindings.append(StocktakeSnapshotBinding(
            batch=batch, binding=binding,
            call_number=binding.call_number,
            frozen_location=binding.location,
            frozen_version=binding.version,
            frozen_item_barcodes=list(
                binding.entries.order_by("item__barcode")
                .values_list("item__barcode", flat=True)
            ),
        ))
    StocktakeSnapshotBinding.objects.bulk_create(snap_bindings)

    _audit(
        batch, StocktakeAuditLog.Action.CREATED,
        f"冻结 {len(snap_rows)} 个实体、{len(snap_bindings)} 册装订、"
        f"{len(numbers)} 个编号槽位"
        + (f"，库位：{batch.scope_location}" if batch.scope_location else ""),
        {"item_count": len(snap_rows),
         "binding_count": len(snap_bindings),
         "number_count": len(numbers)},
        actor=actor,
    )
    return batch


# --------------------------------------------------------------------------
# 版本比较：盘点期间的并发变更
# --------------------------------------------------------------------------

def _open_conflict(snap, kind):
    return snap.conflicts.filter(
        kind=kind, status=StocktakeConflict.Status.OPEN,
    ).first()


def _raise_conflict(snap, kind, detail, snapshot_value, current_value):
    """登记一条未处理冲突（同对象同类型只保留一条），并把结论置为待核查。

    旧扫描结论保留在 result_before_review，绝不被新状态或新扫描覆盖。
    """
    conflict, created = StocktakeConflict.objects.get_or_create(
        snapshot_item=snap, kind=kind,
        status=StocktakeConflict.Status.OPEN,
        defaults={
            "batch": snap.batch, "detail": detail,
            "snapshot_value": snapshot_value,
            "current_value": current_value,
        },
    )
    if not created:
        conflict.detail = detail
        conflict.current_value = current_value
        conflict.save(update_fields=["detail", "current_value"])
    if snap.result != StocktakeSnapshotItem.Result.REVIEW:
        snap.result_before_review = snap.result
        snap.result = StocktakeSnapshotItem.Result.REVIEW
        snap.save(update_fields=["result_before_review", "result"])
    return conflict


def detect_item_drift(snap, *, observed_location="", raise_misplaced=False):
    """比较快照行与实体现状，返回发现的冲突列表。

    冲突被馆员处理过后，以处理时的版本/关系为新基线（baseline_*），
    此后的比较只针对基线之后的新变更，不会重复报警。

    raise_misplaced=True 时配合本次扫码位置判断“错架”；
    冲突（版本/关系不一致）优先级始终高于错架。
    """
    conflicts = []
    snap_item = snap.item
    entry = BindingEntry.objects.select_related("binding").filter(
        item=snap_item,
    ).first()
    current_binding = entry.binding if entry is not None else None

    # 选择比较基准：冲突已处理则用确认基线，否则用冻结快照
    baseline_item_version = (
        snap.baseline_item_version
        if snap.baseline_item_version is not None
        else snap.frozen_item_version
    )
    baseline_bound = (
        snap.baseline_bound if snap.baseline_bound is not None
        else snap.frozen_bound
    )
    baseline_binding_id = (
        snap.baseline_binding_id if snap.baseline_bound is not None
        else snap.frozen_binding_id
    )
    baseline_binding_version = (
        snap.baseline_binding_version
        if snap.baseline_binding_version is not None
        else snap.frozen_binding_version
    )
    baseline_location = (
        snap.baseline_location
        if snap.baseline_location
        else (
            current_binding.location if (baseline_bound and current_binding
                                         and current_binding.id
                                         == baseline_binding_id)
            else (snap.frozen_binding_location
                  if baseline_bound else snap.frozen_location)
        )
    )

    # 1) 装订关系变化（拆订 / 新装订 / 改装）
    if current_binding is None and baseline_bound:
        old_call = (snap.frozen_binding_call_number or "（装订册）")
        c = _raise_conflict(
            snap, StocktakeConflict.Kind.UNBOUND,
            f"盘点期间 {old_call} 被拆订，实体已恢复各自位置。",
            f"装订于 {old_call} @ {snap.frozen_binding_location}",
            f"已拆订，当前位置 {snap_item.location or '（未排架）'}",
        )
        conflicts.append(c)
    elif current_binding is not None and not baseline_bound:
        c = _raise_conflict(
            snap, StocktakeConflict.Kind.BOUND,
            f"盘点期间实体被装订进 {current_binding.call_number}。",
            f"未装订 @ {snap.frozen_location or '（未排架）'}",
            f"装订于 {current_binding.call_number} @ {current_binding.location}",
        )
        conflicts.append(c)
    elif (
        current_binding is not None and baseline_bound
        and current_binding.id != (baseline_binding_id or -1)
    ):
        c = _raise_conflict(
            snap, StocktakeConflict.Kind.BOUND,
            "盘点期间实体被改装订进另一装订册 "
            f"{current_binding.call_number}（原 {snap.frozen_binding_call_number}）。",
            f"装订于 {snap.frozen_binding_call_number}",
            f"装订于 {current_binding.call_number} @ {current_binding.location}",
        )
        conflicts.append(c)

    # 2) 实体自身版本变化（移动、报失、改期等），以已确认基线版本为准
    elif snap_item.version != baseline_item_version:
        cur_actual = (
            current_binding.location if current_binding is not None
            else snap_item.location
        )
        c = _raise_conflict(
            snap, StocktakeConflict.Kind.ITEM_MOVED,
            "实体被移动或修改（基线版本 "
            f"{baseline_item_version} → 当前 {snap_item.version}）。",
            baseline_location or "（未排架）",
            cur_actual or "（未排架）",
        )
        conflicts.append(c)

    # 3) 装订册位置变化（仍在同一册内）
    if (
        current_binding is not None and baseline_bound
        and current_binding.id == baseline_binding_id
        and current_binding.version != (baseline_binding_version or 0)
    ):
        c = _raise_conflict(
            snap, StocktakeConflict.Kind.BINDING_MOVED,
            f"装订册 {current_binding.call_number} 被移动。",
            snap.frozen_binding_location or "（未排架）",
            current_binding.location or "（未排架）",
        )
        conflicts.append(c)

    if conflicts:
        return conflicts

    # 无冲突：若该行此前因冲突待核查（关系/版本已恢复一致），恢复原结论
    if snap.result == StocktakeSnapshotItem.Result.REVIEW:
        restored = snap.result_before_review \
            or StocktakeSnapshotItem.Result.UNSEEN
        snap.result = restored
        snap.result_before_review = ""
        snap.save(update_fields=["result", "result_before_review"])

    # 本次扫码位置与应在位置不一致 → 错架（非并发冲突）。
    # 冲突已按现状处理则按基线位置，否则按冻结位置；仍装订则取装订位置。
    if raise_misplaced and observed_location:
        if current_binding is not None and current_binding.id \
                == baseline_binding_id:
            expected = current_binding.location
        elif snap.baseline_location:
            expected = snap.baseline_location
        else:
            expected = baseline_location
        if observed_location != expected:
            _mark_result(snap, StocktakeSnapshotItem.Result.MISPLACED,
                         observed_location)
    return conflicts


def _mark_result(snap, result, observed_location=""):
    """写入扫描结论（不触碰待核查行——待核查必须先解决冲突）。"""
    if snap.result == StocktakeSnapshotItem.Result.REVIEW:
        return False
    update = {"result": result}
    if observed_location:
        update["observed_location"] = observed_location
    if result in (
        StocktakeSnapshotItem.Result.SEEN,
        StocktakeSnapshotItem.Result.MISPLACED,
    ) and not snap.first_seen_at:
        update["first_seen_at"] = timezone.now()
    if snap.result != result or (
        observed_location and snap.observed_location != observed_location
    ):
        snap.__class__.objects.filter(pk=snap.pk).update(**update)
        snap.refresh_from_db()
    return True


def detect_scope_drift(batch):
    """编号槽位在冻结后被登记了发行期（缺号 → 已发行）：范围变化。"""
    conflicts = []
    for scope in batch.scope_numbers.select_related("number"):
        if scope.frozen_holding_status in GAP_STATUSES:
            current = number_holding_status(batch.title, scope.number)
            if current not in GAP_STATUSES:
                conflict, _ = StocktakeConflict.objects.get_or_create(
                    batch=batch, snapshot_item=None,
                    kind=StocktakeConflict.Kind.SCOPE_CHANGED,
                    status=StocktakeConflict.Status.OPEN,
                    defaults={
                        "detail": "编号 v."
                                  f"{scope.number.volume or '—'} "
                                  f"no.{scope.number.number} 在盘点期间登记了"
                                  "发行期，范围发生变化。",
                        "snapshot_value": f"number:{scope.number_id}",
                        "current_value": current,
                    },
                )
                conflicts.append(conflict)
    return conflicts


def _scope_number_from_conflict(conflict):
    raw = conflict.snapshot_value or ""
    return int(raw.split(":", 1)[1]) if raw.startswith("number:") else None


@transaction.atomic
def refresh_drift(batch, actor=""):
    """重新比较全部快照行与范围，发现新冲突；已不存在的冲突自动恢复。"""
    found = []
    for snap in batch.snapshot_items.select_related(
        "item", "frozen_binding",
    ):
        found.extend(detect_item_drift(snap))
    found.extend(detect_scope_drift(batch))

    # 关系/版本已恢复一致的冲突：解除待核查，恢复扫描结论
    for snap in batch.snapshot_items.all():
        open_kinds = set(
            snap.conflicts.filter(
                status=StocktakeConflict.Status.OPEN,
            ).values_list("kind", flat=True)
        )
        if not open_kinds and snap.result == \
                StocktakeSnapshotItem.Result.REVIEW:
            restored = snap.result_before_review \
                or StocktakeSnapshotItem.Result.UNSEEN
            snap.result = restored
            snap.result_before_review = ""
            snap.save(update_fields=["result", "result_before_review"])

    if found:
        _audit(
            batch, StocktakeAuditLog.Action.CONFLICT,
            f"刷新发现 {len({c.id for c in found})} 条待处理冲突。",
            actor=actor,
        )
    return found


# --------------------------------------------------------------------------
# 扫描（幂等）
# --------------------------------------------------------------------------

@transaction.atomic
def scan_barcode(batch, barcode, *, observed_location="", actor=""):
    """扫描一个条码（实体或装订册），返回事件与本次映射的快照行。

    重复扫描同一条码：只保留第一条原始事件，后续记 duplicate 并返回原事件，
    盘点结论不会被重复扫码改变。
    """
    if not batch.is_open:
        raise ValueError("批次已关闭，不能继续扫描；如需补扫请先重新打开。")
    barcode = (barcode or "").strip()
    if not barcode:
        raise ValueError("条码不能为空。")
    observed_location = (observed_location or "").strip()

    original = StocktakeScan.objects.filter(
        batch=batch, barcode=barcode, is_duplicate=False,
    ).select_related("item", "binding").first()
    if original is not None:
        dup = StocktakeScan.objects.create(
            batch=batch, barcode=barcode,
            scan_kind=original.scan_kind,
            event_result=StocktakeScan.EventResult.DUPLICATE,
            item=original.item, binding=original.binding,
            observed_location=observed_location,
            resolved_barcodes=original.resolved_barcodes,
            is_duplicate=True, duplicate_of=original,
            scanned_by=actor,
        )
        _audit(
            batch, StocktakeAuditLog.Action.SCAN,
            f"重复扫描 {barcode}，沿用原始事件 #{original.id}，只计一次。",
            {"scan_id": dup.id, "original_scan_id": original.id,
             "barcode": barcode, "duplicate": True},
            actor=actor,
        )
        return {
            "scan": dup, "duplicate": True, "original": original,
            "event_result": dup.event_result,
            "snapshot_items": _rows_by_barcodes(
                batch, original.resolved_barcodes),
            "conflicts": [],
        }

    # 先按实体条码、再按装订索书号解析
    item = Item.objects.select_related("issue").filter(
        barcode=barcode,
    ).first()
    binding = None if item is not None else Binding.objects.filter(
        call_number=barcode,
    ).prefetch_related("entries__item").first()

    if item is not None:
        return _scan_item(batch, barcode, item, observed_location, actor)
    if binding is not None:
        return _scan_binding(batch, barcode, binding, observed_location, actor)

    scan = StocktakeScan.objects.create(
        batch=batch, barcode=barcode,
        scan_kind=StocktakeScan.ScanKind.ITEM,
        event_result=StocktakeScan.EventResult.UNKNOWN,
        observed_location=observed_location, scanned_by=actor,
    )
    _audit(
        batch, StocktakeAuditLog.Action.SCAN,
        f"扫描未知条码 {barcode}，未匹配到任何实体或装订册。",
        {"scan_id": scan.id, "barcode": barcode}, actor=actor,
    )
    return {"scan": scan, "duplicate": False, "original": scan,
            "event_result": scan.event_result, "snapshot_items": [],
            "conflicts": []}


def _rows_by_barcodes(batch, barcodes):
    return list(
        batch.snapshot_items.filter(item__barcode__in=barcodes)
        .select_related("item")
    )


def _scan_item(batch, barcode, item, observed_location, actor):
    snap = batch.snapshot_items.filter(item=item).first()
    if snap is None:
        scan = StocktakeScan.objects.create(
            batch=batch, barcode=barcode,
            scan_kind=StocktakeScan.ScanKind.ITEM,
            event_result=StocktakeScan.EventResult.OUT_OF_SCOPE,
            item=item, resolved_barcodes=[],
            observed_location=observed_location, scanned_by=actor,
        )
        _audit(
            batch, StocktakeAuditLog.Action.SCAN,
            f"实体 {barcode} 不在本批次盘点范围内，已记录不改任何结论。",
            {"scan_id": scan.id, "barcode": barcode}, actor=actor,
        )
        return {"scan": scan, "duplicate": False, "original": scan,
                "event_result": scan.event_result, "snapshot_items": [],
                "conflicts": []}

    conflicts = detect_item_drift(
        snap, observed_location=observed_location,
        raise_misplaced=True,
    )
    if not conflicts:
        # 错架（扫到但位置与应在位置不符）优先于普通“已见”，不能被覆盖
        snap.refresh_from_db()
        if snap.result != StocktakeSnapshotItem.Result.MISPLACED:
            _mark_result(snap, StocktakeSnapshotItem.Result.SEEN,
                         observed_location)
        snap.refresh_from_db()

    scan = StocktakeScan.objects.create(
        batch=batch, barcode=barcode,
        scan_kind=StocktakeScan.ScanKind.ITEM,
        event_result=StocktakeScan.EventResult.RECORDED,
        item=item, resolved_barcodes=[barcode],
        observed_location=observed_location, scanned_by=actor,
    )
    _audit(
        batch, StocktakeAuditLog.Action.SCAN,
        f"扫描实体 {barcode}："
        + ("发现并发冲突，置为待核查。" if conflicts
           else f"结论={snap.get_result_display()}。"),
        {"scan_id": scan.id, "barcode": barcode,
         "snapshot_item_id": snap.id,
         "result": snap.result,
         "conflict_ids": [c.id for c in conflicts]},
        actor=actor,
    )
    for c in conflicts:
        _audit(
            batch, StocktakeAuditLog.Action.CONFLICT,
            f"扫描 {barcode} 时发现冲突：{c.detail}",
            {"conflict_id": c.id, "kind": c.kind,
             "snapshot_item_id": snap.id}, actor=actor,
        )
    return {"scan": scan, "duplicate": False, "original": scan,
            "event_result": scan.event_result,
            "snapshot_items": [snap], "conflicts": conflicts}


def _scan_binding(batch, barcode, binding, observed_location, actor):
    """扫装订册：展开为快照中该册的全部实体（同册实体只映射一次）。"""
    snap_binding = batch.snapshot_bindings.filter(
        call_number=binding.call_number,
    ).first()
    frozen_barcodes = (
        snap_binding.frozen_item_barcodes if snap_binding is not None
        else list(binding.entries.values_list("item__barcode", flat=True))
    )
    rows = _rows_by_barcodes(batch, frozen_barcodes)

    if snap_binding is None or not rows:
        scan = StocktakeScan.objects.create(
            batch=batch, barcode=barcode,
            scan_kind=StocktakeScan.ScanKind.BINDING,
            event_result=StocktakeScan.EventResult.OUT_OF_SCOPE,
            binding=binding, resolved_barcodes=frozen_barcodes,
            observed_location=observed_location, scanned_by=actor,
        )
        _audit(
            batch, StocktakeAuditLog.Action.SCAN,
            f"装订册 {barcode} 不在本批次范围内，已记录，不生成实体副本。",
            {"scan_id": scan.id, "barcode": barcode,
             "resolved_barcodes": frozen_barcodes},
            actor=actor,
        )
        return {"scan": scan, "duplicate": False, "original": scan,
                "event_result": scan.event_result,
                "snapshot_items": [], "conflicts": []}

    all_conflicts = []
    for snap in rows:
        conflicts = detect_item_drift(snap)
        if not conflicts:
            _mark_result(snap, StocktakeSnapshotItem.Result.SEEN,
                         observed_location or snap_binding.frozen_location)
            snap.refresh_from_db()
        all_conflicts.extend(conflicts)

    scan = StocktakeScan.objects.create(
        batch=batch, barcode=barcode,
        scan_kind=StocktakeScan.ScanKind.BINDING,
        event_result=StocktakeScan.EventResult.RECORDED,
        binding=binding, resolved_barcodes=frozen_barcodes,
        observed_location=observed_location or snap_binding.frozen_location,
        scanned_by=actor,
    )
    _audit(
        batch, StocktakeAuditLog.Action.SCAN,
        f"扫描装订册 {barcode}，映射册内 {len(rows)} 个实体（不制造副本）："
        f"{', '.join(frozen_barcodes)}。"
        + (" 发现并发冲突，相关实体置为待核查。" if all_conflicts else ""),
        {"scan_id": scan.id, "barcode": barcode,
         "snapshot_binding_id": snap_binding.id,
         "resolved_barcodes": frozen_barcodes,
         "conflict_ids": [c.id for c in all_conflicts]},
        actor=actor,
    )
    for c in all_conflicts:
        _audit(
            batch, StocktakeAuditLog.Action.CONFLICT,
            f"扫装订册 {barcode} 时发现冲突：{c.detail}",
            {"conflict_id": c.id, "kind": c.kind}, actor=actor,
        )
    return {"scan": scan, "duplicate": False, "original": scan,
            "event_result": scan.event_result,
            "snapshot_items": rows, "conflicts": all_conflicts}


# --------------------------------------------------------------------------
# 冲突处理
# --------------------------------------------------------------------------

@transaction.atomic
def resolve_conflict(conflict, *, resolution, note="", actor=""):
    """馆员处理冲突：按现状确认已见 / 按未见重新核查 / 确认范围变化。"""
    if conflict.status != StocktakeConflict.Status.OPEN:
        raise ValueError("该冲突已处理。")
    now = timezone.now()
    conflict.status = {
        "seen": StocktakeConflict.Status.RESOLVED_SEEN,
        "unseen": StocktakeConflict.Status.RESOLVED_UNSEEN,
        "scope": StocktakeConflict.Status.RESOLVED_SCOPE,
    }[resolution]
    conflict.resolved_at = now
    conflict.resolved_by = actor
    conflict.resolution_note = note
    conflict.save(update_fields=[
        "status", "resolved_at", "resolved_by", "resolution_note",
    ])

    if conflict.kind == StocktakeConflict.Kind.SCOPE_CHANGED:
        # 接受范围变化：更新槽位视图，新发行期的实体补进快照（未见）
        if resolution == "scope":
            scope = conflict.batch.scope_numbers.filter(
                number_id=_scope_number_from_conflict(conflict),
            ).first()
            if scope is not None:
                scope.frozen_holding_status = number_holding_status(
                    conflict.batch.title, scope.number)
                scope.save(update_fields=["frozen_holding_status"])
            _absorb_scope_change(conflict.batch)
    else:
        snap = conflict.snapshot_item
        snap_item = snap.item
        entry = BindingEntry.objects.select_related("binding").filter(
            item=snap_item).first()
        cur_binding = entry.binding if entry is not None else None
        # 以处理时的现状建立新基线：之后只比较基线之后的新变更
        snap.baseline_item_version = snap_item.version
        snap.baseline_bound = cur_binding is not None
        snap.baseline_binding = cur_binding
        snap.baseline_binding_version = (
            cur_binding.version if cur_binding is not None else None)
        snap.baseline_location = (
            cur_binding.location if cur_binding is not None
            else (snap_item.location or "")
        )
        if resolution == "seen":
            # 以新状态为基线确认已见（不是用旧扫描覆盖新位置）
            snap.baseline = StocktakeSnapshotItem.Result.SEEN
            snap.result = StocktakeSnapshotItem.Result.SEEN
            if not snap.first_seen_at:
                snap.first_seen_at = now
        else:
            snap.baseline = StocktakeSnapshotItem.Result.UNSEEN
            snap.result = StocktakeSnapshotItem.Result.UNSEEN
        snap.result_before_review = ""
        snap.save(update_fields=[
            "baseline", "result", "result_before_review", "first_seen_at",
            "baseline_item_version", "baseline_bound",
            "baseline_binding", "baseline_binding_version",
            "baseline_location",
        ])

    _audit(
        conflict.batch, StocktakeAuditLog.Action.CONFLICT_RESOLVED,
        f"冲突 #{conflict.id}（{conflict.get_kind_display()}）处理为"
        f"{conflict.get_status_display()}。{note}",
        {"conflict_id": conflict.id, "resolution": resolution}, actor=actor,
    )
    return conflict


def _absorb_scope_change(batch):
    """范围变化确认后：把新发行期带来的、尚未入快照的实体补进来。"""
    scope_number_ids = list(
        batch.scope_numbers.values_list("number_id", flat=True))
    existing_item_ids = set(
        batch.snapshot_items.values_list("item_id", flat=True))
    new_items = (
        Item.objects.filter(title=batch.title)
        .filter(issue__numbers__in=scope_number_ids)
        .exclude(id__in=existing_item_ids)
        .select_related("binding_entry__binding")
        .distinct()
    )
    rows = []
    for it in new_items:
        entry = getattr(it, "binding_entry", None)
        binding = entry.binding if entry is not None else None
        actual = binding.location if binding is not None else it.location
        if batch.scope_location and actual != batch.scope_location:
            continue
        rows.append(StocktakeSnapshotItem(
            batch=batch, item=it, frozen_status=it.status,
            frozen_location=it.location or "",
            frozen_item_version=it.version,
            frozen_bound=binding is not None, frozen_binding=binding,
            frozen_binding_call_number=binding.call_number if binding else "",
            frozen_binding_location=binding.location if binding else "",
            frozen_binding_version=binding.version if binding else None,
        ))
    StocktakeSnapshotItem.objects.bulk_create(rows)


# --------------------------------------------------------------------------
# 关闭：范围完整性 + 只有已发行未见进遗失候选
# --------------------------------------------------------------------------

def _scope_gap_number_ids(batch):
    return set(
        batch.scope_numbers.filter(
            frozen_holding_status__in=GAP_STATUSES,
        ).values_list("number_id", flat=True)
    )


def closing_blockers(batch):
    """返回阻止关闭的原因列表（空列表表示可以关闭）。"""
    blockers = []
    if not batch.completeness_confirmed:
        blockers.append("范围完整性尚未确认：请馆员确认应盘对象全部核对完毕。")
    open_conflicts = batch.conflicts.filter(
        status=StocktakeConflict.Status.OPEN,
    )
    if open_conflicts.exists():
        kinds = dict(StocktakeConflict.Kind.choices)
        seen_kinds = [
            kinds[k] for k in
            open_conflicts.values_list("kind", flat=True).distinct()
        ]
        blockers.append(
            f"存在 {open_conflicts.count()} 条待处理冲突"
            f"（{'、'.join(seen_kinds)}），请处理后再关闭。",
        )
    if batch.snapshot_items.filter(
        result=StocktakeSnapshotItem.Result.REVIEW,
    ).exists():
        blockers.append("存在“待核查”实体，必须逐条处理冲突后才能关闭。")
    return blockers


def missing_candidates(batch):
    """遗失候选 = 已发行且未见的实体。

    缺号（not_published / ceased_gap）编号不在任何快照行上出现实体；
    冻结时已丢失的实体同样不是“本轮未见”，不算候选。
    """
    gap_number_ids = _scope_gap_number_ids(batch)
    candidates = []
    for snap in batch.snapshot_items.select_related(
        "item", "item__issue",
    ).prefetch_related("item__issue__numbers"):
        if snap.result != StocktakeSnapshotItem.Result.UNSEEN:
            continue
        # 待核查实体存在争议（盘点期间被移动/拆订等），不能判遗失
        if snap.conflicts.filter(
            status=StocktakeConflict.Status.OPEN,
        ).exists():
            continue
        if snap.frozen_status == Item.ItemStatus.LOST:
            continue  # 盘点前就已丢失，不是本轮新发现
        number_ids = set(
            snap.item.issue.numbers.values_list("id", flat=True))
        # 实体覆盖的编号全部是缺号才排除；合刊只要有已发行编号就在馆藏侧
        if number_ids and number_ids <= gap_number_ids:
            continue  # 双保险：缺号永不因未扫到进入遗失候选
        candidates.append(snap)
    return candidates


@transaction.atomic
def confirm_complete(batch, *, actor=""):
    """馆员显式声明范围完整（可反复确认/撤销由调用方保证状态开放）。"""
    if not batch.is_open:
        raise ValueError("批次未处于进行中状态。")
    batch.completeness_confirmed = True
    batch.completed_confirmed_at = timezone.now()
    batch.save(update_fields=["completeness_confirmed",
                              "completed_confirmed_at"])
    _audit(
        batch, StocktakeAuditLog.Action.CONFIRMED_COMPLETE,
        "馆员确认盘点范围完整，应盘对象已全部核对。", actor=actor,
    )
    return batch


@transaction.atomic
def prepare_close(batch, *, actor=""):
    """关闭前最后一次版本比较（独立提交）。

    返回阻止原因列表；即使随后关闭被阻止，本次发现的冲突也已持久化，
    不会因关闭事务回滚而丢失。
    """
    if not batch.is_open:
        raise ValueError("批次已经关闭。")
    refresh_drift(batch, actor=actor)
    return closing_blockers(batch)


@transaction.atomic
def close_batch(batch, *, actor="", _already_refreshed=False):
    """关闭批次：完整性已确认且无冲突时，把已发行未见标为遗失候选。

    版本比较需由调用方先以 prepare_close() 完成并提交：这样“关闭被阻止”
    时发现的冲突与待核查标记能保留下来，不随关闭事务回滚消失。
    """
    if not batch.is_open:
        raise ValueError("批次已经关闭。")
    if not _already_refreshed:
        refresh_drift(batch, actor=actor)
    blockers = closing_blockers(batch)
    if blockers:
        raise ValueError("  ".join(blockers))

    candidates = missing_candidates(batch)
    StocktakeSnapshotItem.objects.filter(
        id__in=[s.id for s in candidates],
    ).update(outcome=StocktakeSnapshotItem.Outcome.LOST_CANDIDATE)
    # 其余实体（已见/错架/冻结时已丢失）保留
    batch.snapshot_items.exclude(
        outcome=StocktakeSnapshotItem.Outcome.LOST_CANDIDATE,
    ).update(outcome=StocktakeSnapshotItem.Outcome.KEPT)

    batch.status = StocktakeBatch.Status.CLOSED
    batch.closed_at = timezone.now()
    batch.save(update_fields=["status", "closed_at"])
    _audit(
        batch, StocktakeAuditLog.Action.CLOSED,
        f"批次关闭：{len(candidates)} 个已发行未见实体进入遗失候选；"
        "缺号（未发行/停刊后缺号）不参与遗失判定。",
        {"lost_candidate_ids": [s.id for s in candidates],
         "lost_candidate_barcodes": [s.item.barcode for s in candidates]},
        actor=actor,
    )
    return batch, candidates


@transaction.atomic
def confirm_losses(batch, item_ids, *, actor=""):
    """显式确认遗失：只有“遗失候选”中的实体可转为丢失。"""
    if batch.status != StocktakeBatch.Status.CLOSED:
        raise ValueError("只有已关闭批次才能确认遗失。")
    rows = list(batch.snapshot_items.filter(item_id__in=item_ids))
    found_item_ids = {r.item_id for r in rows}
    missing = set(item_ids) - found_item_ids
    if missing:
        raise ValueError(f"实体不在本批次快照中：{sorted(missing)}")
    not_candidate = [
        r.item.barcode for r in rows
        if r.outcome != StocktakeSnapshotItem.Outcome.LOST_CANDIDATE
    ]
    if not_candidate:
        raise ValueError(
            "只有已发行未见的遗失候选可以确认遗失；缺号与已见实体不可："
            f"{not_candidate}",
        )
    confirmed = []
    for r in rows:
        r.item.status = Item.ItemStatus.LOST
        r.item.save(update_fields=["status"])  # 版本自增
        r.outcome = StocktakeSnapshotItem.Outcome.LOST_CONFIRMED
        r.save(update_fields=["outcome"])
        confirmed.append(r.item.barcode)
    _audit(
        batch, StocktakeAuditLog.Action.LOSS_CONFIRMED,
        f"确认 {len(confirmed)} 个实体遗失：{', '.join(confirmed)}。",
        {"barcodes": confirmed}, actor=actor,
    )
    return confirmed


@transaction.atomic
def reopen_batch(batch, *, actor=""):
    """重新打开已关闭批次：快照、事件、冲突与候选状态全部保留可追溯。"""
    if batch.status != StocktakeBatch.Status.CLOSED:
        raise ValueError("只有已关闭批次可以重新打开。")
    batch.status = StocktakeBatch.Status.REOPENED
    # 恢复后可能继续扫描，范围完整性需要重新确认
    batch.completeness_confirmed = False
    batch.completed_confirmed_at = None
    batch.closed_at = None
    batch.save(update_fields=[
        "status", "completeness_confirmed",
        "completed_confirmed_at", "closed_at",
    ])
    # 终态去向重置，扫描结论（seen/unseen/review）原样保留
    batch.snapshot_items.all().update(
        outcome=StocktakeSnapshotItem.Outcome.PENDING)
    _audit(
        batch, StocktakeAuditLog.Action.REOPENED,
        "批次重新打开，历史扫描与冲突保留，可继续扫描或刷新核查；"
        "范围完整性需重新确认。",
        actor=actor,
    )
    return batch


def batch_stats(batch):
    """批次盘点结果统计（缺号编号单列，永不遗失）。"""
    rows = batch.snapshot_items.all()
    counts = {
        "total": rows.count(),
        "seen": rows.filter(result=StocktakeSnapshotItem.Result.SEEN).count(),
        "misplaced": rows.filter(
            result=StocktakeSnapshotItem.Result.MISPLACED).count(),
        "unseen": rows.filter(
            result=StocktakeSnapshotItem.Result.UNSEEN).count(),
        "review": rows.filter(
            result=StocktakeSnapshotItem.Result.REVIEW).count(),
    }
    counts["lost_candidates"] = len(missing_candidates(batch)) if \
        batch.is_open else rows.filter(
            outcome=StocktakeSnapshotItem.Outcome.LOST_CANDIDATE).count()
    gap_scopes = batch.scope_numbers.filter(
        frozen_holding_status__in=GAP_STATUSES)
    gap_numbers = [
        {"number_id": s.number_id, "volume": s.number.volume,
         "number": s.number.number, "frozen_holding_status":
             s.frozen_holding_status}
        for s in gap_scopes.select_related("number")
    ]
    counts["gap_numbers"] = gap_numbers  # not_published / ceased_gap
    counts["open_conflicts"] = batch.conflicts.filter(
        status=StocktakeConflict.Status.OPEN).count()
    return counts
