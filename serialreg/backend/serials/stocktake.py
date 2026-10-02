"""盘点批次领域服务。

职责：
  启动盘点  → 冻结范围内实物 / 装订关系 / 位置 / 版本快照；
  扫描      → 幂等事件 + 已见/错架/待核查标记，装订册扫描映射全部成员；
  冲突检测  → 盘点期间移动 / 拆订 / 装订 / 修改实体，比对快照与当前版本，
              产生待处理冲突，绝不用旧扫描覆盖新位置；
  关闭批次  → 必须先确认范围完整性（无未处理冲突/待核查/未见），
              显式确认后仅把「已发行未见」实物判为遗失候选/遗失，
              not_published 与 ceased_gap 永远不因未扫到而变更。
"""
from django.db import transaction
from django.utils import timezone

from .models import (
    Binding, Item, IssueNumbering,
    Stocktake, StocktakeBindingSnapshot, StocktakeConflict,
    StocktakeItemSnapshot, StocktakeScan,
)


# ---------------------------------------------------------------- 启动盘点

def _scope_items(stocktake):
    """批次范围内的当前实物（按实物所属刊与覆盖期号的卷号过滤）。"""
    qs = Item.objects.filter(title_id=stocktake.title_id)
    if stocktake.scope_volume:
        qs = qs.filter(
            issue__numberings__number__volume=stocktake.scope_volume,
        ).distinct()
    return qs.select_related("issue", "binding_entry__binding")


@transaction.atomic
def create_stocktake(title, scope_volume="", name="", note=""):
    """启动盘点：冻结范围内实物与装订关系快照。"""
    if scope_volume:
        used = IssueNumbering.objects.filter(
            number__title=title, number__volume=scope_volume,
        ).exists()
        if not used:
            raise ValueError(f"该刊没有卷 {scope_volume} 的编号，盘点范围为空。")

    stocktake = Stocktake.objects.create(
        title=title, scope_volume=scope_volume or "",
        name=name or "", note=note or "",
    )
    items = list(_scope_items(stocktake))

    item_snapshots = []
    binding_seen = {}  # binding id -> 成员 item id 列表
    for it in items:
        entry = getattr(it, "binding_entry", None)
        binding = entry.binding if entry is not None else None
        if binding is not None:
            binding_seen.setdefault(binding.id, []).append(it.id)
        number_ids = list(
            it.issue.numberings.values_list("number_id", flat=True),
        )
        item_snapshots.append(StocktakeItemSnapshot(
            stocktake=stocktake,
            item=it,
            issue_id=it.issue_id,
            number_ids=number_ids,
            status_snapshot=it.status,
            location_snapshot=it.location or "",
            effective_location_snapshot=it.current_location() or "",
            binding_id_snapshot=binding.id if binding else None,
            binding_call_snapshot=binding.call_number if binding else "",
            item_version=it.version,
            binding_version=binding.version if binding else None,
            published=True,  # 有实物必有已发行 Issue；缺号槽位不产生快照
        ))
    StocktakeItemSnapshot.objects.bulk_create(item_snapshots)

    binding_snapshots = []
    for binding in Binding.objects.filter(
        id__in=binding_seen,
    ).prefetch_related("entries"):
        # 成员以快照时刻的完整 BindingEntry 为准（可能含范围外同册实物）
        member_ids = list(
            binding.entries.values_list("item_id", flat=True),
        )
        binding_snapshots.append(StocktakeBindingSnapshot(
            stocktake=stocktake,
            binding=binding,
            binding_call=binding.call_number,
            location_snapshot=binding.location,
            binding_version=binding.version,
            item_ids=member_ids,
        ))
        binding_seen[binding.id] = member_ids
    StocktakeBindingSnapshot.objects.bulk_create(binding_snapshots)

    return stocktake


# ---------------------------------------------------------------- 冲突检测

def _open_conflict(stocktake, *, kind, item=None, binding=None,
                   detail="", snapshot_value="", current_value=""):
    """登记一条未解决冲突；同物同类型未解决冲突只保留一条。"""
    return StocktakeConflict.objects.get_or_create(
        stocktake=stocktake, item=item, binding=binding, kind=kind,
        status=StocktakeConflict.Status.OPEN,
        defaults={
            "detail": detail, "snapshot_value": snapshot_value,
            "current_value": current_value,
        },
    )


def _check_item(stocktake, snap, item, entry):
    """比较单个快照实物与当前状态，返回发现的冲突列表。

    发现冲突时，把扫描结果降级为「待核查」——盘点期间数据变了，
    旧扫描不能继续当作「已见在正确位置」。
    """
    conflicts = []
    current_binding = entry.binding if entry is not None else None
    snap_binding_id = snap.binding_id_snapshot
    cur_binding_id = current_binding.id if current_binding else None

    def _downgrade():
        if snap.result == StocktakeItemSnapshot.Result.SEEN or \
                snap.result == StocktakeItemSnapshot.Result.MISPLACED:
            snap.result = StocktakeItemSnapshot.Result.REVIEW
            snap.save(update_fields=["result"])

    # 1) 装订关系变化（拆订 / 新装 / 换册）
    if snap_binding_id != cur_binding_id:
        if snap_binding_id and not cur_binding_id:
            kind = StocktakeConflict.Kind.UNBOUND
            detail = (
                f"实物 {item.barcode} 盘点开始时装订于 "
                f"{snap.binding_call_snapshot}，现已拆订。"
            )
        elif not snap_binding_id and cur_binding_id:
            kind = StocktakeConflict.Kind.BOUND
            detail = (
                f"实物 {item.barcode} 盘点开始时未装订，"
                f"现装订于 {current_binding.call_number}。"
            )
        else:
            kind = StocktakeConflict.Kind.REBOUND
            detail = (
                f"实物 {item.barcode} 装订册由 {snap.binding_call_snapshot} "
                f"变为 {current_binding.call_number}。"
            )
        _, created = _open_conflict(
            stocktake, kind=kind, item=item, binding=current_binding,
            detail=detail,
            snapshot_value=snap.binding_call_snapshot or "未装订",
            current_value=current_binding.call_number if current_binding else "未装订",
        )
        if created:
            conflicts.append(kind)
            _downgrade()

    # 2) 版本推进：位置/状态被改过
    if item.version != snap.item_version:
        _, created = _open_conflict(
            stocktake, kind=StocktakeConflict.Kind.ITEM_CHANGED, item=item,
            detail=(
                f"实物 {item.barcode} 在盘点期间被修改"
                f"（快照版本 {snap.item_version} → 当前 {item.version}），"
                "扫描结果不能覆盖新位置。"
            ),
            snapshot_value=(
                f"{snap.status_snapshot}@{snap.effective_location_snapshot}"
                f"#v{snap.item_version}"
            ),
            current_value=(
                f"{item.status}@{item.current_location()}#v{item.version}"
            ),
        )
        if created:
            conflicts.append(StocktakeConflict.Kind.ITEM_CHANGED)
            _downgrade()
    return conflicts


@transaction.atomic
def detect_conflicts(stocktake):
    """全量比对快照与当前数据，返回 (新增冲突数, 未解决冲突数)。

    在扫描、查看批次、关闭前都会调用，保证「刷新后状态仍可追溯」。
    """
    stocktake = Stocktake.objects.get(pk=stocktake.pk)
    created_count = 0

    # 快照实物：装订关系 / 版本比对 + 是否仍在范围
    current_ids = set(_scope_items(stocktake).values_list("id", flat=True))
    snaps = {
        s.item_id: s for s in
        stocktake.item_snapshots.select_related(
            "item", "item__binding_entry__binding",
        )
    }
    for item_id, snap in snaps.items():
        item = snap.item
        entry = getattr(item, "binding_entry", None)
        if item_id not in current_ids:
            _, created = _open_conflict(
                stocktake, kind=StocktakeConflict.Kind.MOVED_OUT, item=item,
                detail=(
                    f"实物 {item.barcode} 已不在盘点范围"
                    f"（{stocktake.scope_label}）内。"
                ),
            )
            if created:
                created_count += 1
                if snap.result in (
                    StocktakeItemSnapshot.Result.SEEN,
                    StocktakeItemSnapshot.Result.MISPLACED,
                ):
                    snap.result = StocktakeItemSnapshot.Result.REVIEW
                    snap.save(update_fields=["result"])
            continue
        created_count += len(_check_item(stocktake, snap, item, entry))

    # 范围新增实物（盘点期间新入藏/移入）
    snapshot_ids = set(snaps)
    for item in Item.objects.filter(id__in=current_ids - snapshot_ids):
        _, created = _open_conflict(
            stocktake, kind=StocktakeConflict.Kind.MOVED_IN, item=item,
            detail=(
                f"实物 {item.barcode} 在盘点开始后进入范围，"
                "不在冻结快照内，需人工核查。"
            ),
        )
        created_count += int(created)

    # 装订册成员关系比对：册内成员集合变化即为拆订/重组
    for bsnap in stocktake.binding_snapshots.select_related("binding"):
        if bsnap.binding_id is None:
            continue
        current_members = set(
            bsnap.binding.entries.values_list("item_id", flat=True),
        )
        if current_members != set(bsnap.item_ids):
            _, created = _open_conflict(
                stocktake, kind=StocktakeConflict.Kind.REBOUND,
                binding=bsnap.binding,
                detail=(
                    f"装订册 {bsnap.binding_call} 成员在盘点期间变化："
                    f"移出 {sorted(set(bsnap.item_ids) - current_members)}，"
                    f"移入 {sorted(current_members - set(bsnap.item_ids))}。"
                ),
                snapshot_value=str(sorted(bsnap.item_ids)),
                current_value=str(sorted(current_members)),
            )
            created_count += int(created)

    open_count = stocktake.conflicts.filter(
        status=StocktakeConflict.Status.OPEN,
    ).count()
    return created_count, open_count


# ---------------------------------------------------------------- 扫描

def _resolve_scan_target(barcode):
    """条码先按实物匹配，匹配不到再按装订册索书号匹配。

    装订册没有独立条码字段，馆内常直接扫装订索书号。
    """
    item = Item.objects.filter(barcode=barcode).first()
    if item is not None:
        return StocktakeScan.Target.ITEM, item
    binding = Binding.objects.filter(call_number=barcode).first()
    if binding is not None:
        return StocktakeScan.Target.BINDING, binding
    return None, None


@transaction.atomic
def scan(stocktake, barcode, observed_location=""):
    """处理一次扫描，返回结构化结果（幂等：同一实物只保留首次见到）。

    扫描装订册 → 映射册内全部实物（而非制造多个副本或只确认一册）；
    先扫装订册再扫册内条码（或反过来）→ 已被任一首次事件覆盖的实物
    一律按重复扫描处理，保留原始事件与原始架位；
    盘点期间实物被改动 → 登记冲突并标待核查，绝不用旧扫描覆盖新位置。
    """
    stocktake = Stocktake.objects.get(pk=stocktake.pk)
    if stocktake.state != Stocktake.State.OPEN:
        raise ValueError("批次已关闭，不能继续扫描；如需补扫请重新打开。")

    target_type, target = _resolve_scan_target(barcode)
    if target is None:
        return {"ok": False, "reason": "not_found",
                "detail": f"条码 {barcode} 既不匹配在馆实物，也不匹配装订册。"}

    if target_type == StocktakeScan.Target.ITEM:
        member_items = [target]
        target_id = target.id
    else:
        member_items = list(
            Item.objects.filter(binding_entry__binding=target)
            .select_related("binding_entry__binding"),
        )
        target_id = target.id

    member_ids = [it.id for it in member_items]

    # 幂等性以「实物」为准：查每个实物已被哪些首次扫描事件覆盖。
    # 本次解析出的实物若全部已被历史事件覆盖 → 整体重复扫描。
    prior_events = list(StocktakeScan.objects.filter(
        stocktake=stocktake,
    ).order_by("created_at", "id"))
    covered = {}  # item_id -> 首次覆盖它的事件
    for ev in prior_events:
        for iid in ev.resolved_item_ids:
            covered.setdefault(iid, ev)
    already = {iid: covered[iid] for iid in member_ids if iid in covered}

    duplicate = bool(member_ids) and len(already) == len(member_ids)

    if duplicate:
        detect_conflicts(stocktake)
        first_times = [ev.created_at for ev in already.values()]
        return {
            "ok": True, "duplicate": True,
            "target_type": target_type,
            "barcode": barcode,
            "first_scan_at": min(first_times) if first_times else None,
            "resolved_item_ids": member_ids,
            "detail": "重复扫描，沿用首次扫描事件，不重复计数。",
            "results": _snapshot_results(stocktake, member_ids),
        }

    # 只在「确有新实物被首次见到」时落一条审计事件；
    # 例如先扫条码再扫整本装订册，装订册事件只记录尚未见到的册内实物，
    # 同时保留对整本册的映射，方便审计回放。
    first_seen_ids = [iid for iid in member_ids if iid not in already]
    event = StocktakeScan.objects.create(
        stocktake=stocktake, target_type=target_type, target_id=target_id,
        barcode=barcode, observed_location=observed_location or "",
        resolved_item_ids=member_ids,
        first_seen_item_ids=first_seen_ids,
    )

    out_of_scope = []
    for it in member_items:
        if it.id in already:
            # 该实物此前已扫到（如先扫条码再扫装订册）：保留首次结果
            continue
        snap = StocktakeItemSnapshot.objects.filter(
            stocktake=stocktake, item=it,
        ).first()
        if snap is None:
            # 册内含范围外实物：记录但不纳入盘点统计
            out_of_scope.append(it.barcode)
            continue
        _mark_from_scan(stocktake, snap, it, observed_location)

    # 扫描后立刻做版本/关系比对（覆盖并发拆订等场景）
    _, open_conflicts = detect_conflicts(stocktake)

    return {
        "ok": True, "duplicate": False,
        "target_type": target_type,
        "barcode": barcode,
        "scan_id": event.id,
        "resolved_item_ids": member_ids,
        "already_item_ids": sorted(already),
        "out_of_scope": out_of_scope,
        "results": _snapshot_results(stocktake, member_ids),
        "open_conflicts": open_conflicts,
    }


def _mark_from_scan(stocktake, snap, item, observed_location):
    """根据当前（最新）数据给快照打标；检测到冲突一律待核查。"""
    entry = getattr(item, "binding_entry", None)
    current_binding = entry.binding if entry is not None else None

    # 扫描者给了架位：装订册扫描时以装订册当前位置为期望；
    # 关系/版本任何不一致先记冲突，再决定标记
    conflicts = _check_item(stocktake, snap, item, entry)

    if conflicts:
        # 盘点期间被移动/拆订/修改：不能按旧位置盖「已见」
        snap.result = StocktakeItemSnapshot.Result.REVIEW
    else:
        # 期望位置始终取「当前真实位置」，错架以快照实际位置比较
        expected = snap.effective_location_snapshot
        actual = observed_location or (
            current_binding.location if current_binding else item.location
        )
        observed = observed_location or actual
        snap.observed_location = observed
        if expected and observed and observed != expected:
            snap.result = StocktakeItemSnapshot.Result.MISPLACED
        else:
            snap.result = StocktakeItemSnapshot.Result.SEEN
    snap.seen_at = timezone.now()
    snap.save(update_fields=[
        "result", "observed_location", "seen_at",
    ])


def _snapshot_results(stocktake, item_ids):
    return [
        {
            "item_id": s.item_id,
            "barcode": s.item.barcode,
            "result": s.result,
            "observed_location": s.observed_location,
            "snapshot_location": s.effective_location_snapshot,
        }
        for s in stocktake.item_snapshots.filter(item_id__in=item_ids)
        .select_related("item")
    ]


# ---------------------------------------------------------------- 复核 / 关闭

def review_snapshot(stocktake, item_id, result, observed_location=None):
    """人工复核：把待核查/错架/未见实物确认成明确结果。

    seen / misplaced / review 用于扫描后的复核；
    lost 只允许作用于「未见(pending)」的已发行实物——
    即馆员在关闭前明确承认「这本已发行实物确实找不到」。
    """
    snap = stocktake.item_snapshots.get(item_id=item_id)
    allowed = (
        StocktakeItemSnapshot.Result.SEEN,
        StocktakeItemSnapshot.Result.REVIEW,
        StocktakeItemSnapshot.Result.MISPLACED,
        StocktakeItemSnapshot.Result.LOST,
    )
    if result not in allowed:
        raise ValueError("复核结果只能是 seen / misplaced / review / lost。")
    if result == StocktakeItemSnapshot.Result.LOST:
        if snap.result != StocktakeItemSnapshot.Result.PENDING:
            raise ValueError("只有未见（未扫到）的实物才能确认为遗失。")
        if not snap.published:
            # 双保险：缺号实物根本不入库，这里再挡一次
            raise ValueError("缺号（未发行）不能判为遗失。")
    if result in (
        StocktakeItemSnapshot.Result.SEEN,
        StocktakeItemSnapshot.Result.MISPLACED,
    ) and snap.result == StocktakeItemSnapshot.Result.PENDING:
        raise ValueError("未见实物必须先扫描，不能手工标成已见/错架。")
    snap.result = result
    if observed_location is not None:
        snap.observed_location = observed_location
    snap.save(update_fields=["result", "observed_location"])
    return snap


def resolve_conflict(stocktake, conflict_id):
    conflict = stocktake.conflicts.get(pk=conflict_id)
    conflict.status = StocktakeConflict.Status.RESOLVED
    conflict.resolved_at = timezone.now()
    conflict.save(update_fields=["status", "resolved_at"])

    # 解决后把快照对齐到当前版本/关系，后续检测不再重复报同一冲突
    if conflict.item_id:
        snap = StocktakeItemSnapshot.objects.filter(
            stocktake=stocktake, item_id=conflict.item_id,
        ).first()
        if snap is not None:
            item = conflict.item
            entry = getattr(item, "binding_entry", None)
            binding = entry.binding if entry else None
            snap.item_version = item.version
            snap.binding_id_snapshot = binding.id if binding else None
            snap.binding_call_snapshot = binding.call_number if binding else ""
            snap.binding_version = binding.version if binding else None
            snap.effective_location_snapshot = item.current_location() or ""
            snap.save()

    # 装订册级冲突 / 拆订后：把所有装订关系快照对齐到当前成员集合，
    # 否则成员集合差异会在刷新时再次产生 open 冲突，阻止关闭。
    for bsnap in stocktake.binding_snapshots.select_related("binding"):
        if bsnap.binding_id is None:
            continue
        current_members = list(
            bsnap.binding.entries.values_list("item_id", flat=True),
        )
        if current_members != list(bsnap.item_ids):
            bsnap.item_ids = current_members
            bsnap.binding_version = bsnap.binding.version
            bsnap.location_snapshot = bsnap.binding.location
            bsnap.save(update_fields=[
                "item_ids", "binding_version", "location_snapshot",
            ])
    return conflict


def completeness(stocktake):
    """范围完整性统计：关闭前必须逐项交代清楚。"""
    _, open_conflicts = detect_conflicts(stocktake)
    snaps = stocktake.item_snapshots.all()
    pending = snaps.filter(result=StocktakeItemSnapshot.Result.PENDING).count()
    review = snaps.filter(result=StocktakeItemSnapshot.Result.REVIEW).count()
    misplaced = snaps.filter(
        result=StocktakeItemSnapshot.Result.MISPLACED,
    ).count()
    seen = snaps.filter(result=StocktakeItemSnapshot.Result.SEEN).count()
    lost = snaps.filter(result=StocktakeItemSnapshot.Result.LOST).count()

    # 遗失候选：已发行但未扫到的实物（尚未经馆员确认）。
    # 缺号（not_published / ceased_gap）根本不产生实物快照，天然永不入候选。
    lost_candidates = list(
        snaps.filter(result=StocktakeItemSnapshot.Result.PENDING)
        .select_related("item"),
    )
    complete = open_conflicts == 0 and pending == 0 and review == 0
    return {
        "total": snaps.count(),
        "seen": seen,
        "misplaced": misplaced,
        "review": review,
        "pending": pending,
        "acknowledged_lost": lost,
        "open_conflicts": open_conflicts,
        "complete": complete,
        "lost_candidates": [
            {
                "item_id": s.item_id,
                "barcode": s.item.barcode,
                "snapshot_location": s.effective_location_snapshot,
                "status_snapshot": s.status_snapshot,
            }
            for s in lost_candidates
        ],
    }


@transaction.atomic
def close_stocktake(stocktake, confirm=False):
    """关闭盘点。

    始终先返回范围完整性报告：
      * 范围未交代清楚（未见 / 待核查 / 未解决冲突）→ blocked=True，
        无论是否 confirm 都不能关闭；
      * 范围完整但 confirm=False → needs_confirmation=True，
        列出本次确认会落账的遗失结果，请馆员再确认一次；
      * complete 且 confirm=True → 关闭：把「盘点遗失」结果同步到
        实物 status。缺号（not_published / ceased_gap）没有实物快照，
        永远不可能在此被改动。
    """
    stocktake = Stocktake.objects.get(pk=stocktake.pk)
    if stocktake.state == Stocktake.State.CLOSED:
        return {"already_closed": True, **completeness(stocktake)}

    report = completeness(stocktake)
    if not report["complete"]:
        return {"blocked": True, **report}
    if not confirm:
        # 范围已完整，但缺最后一次显式确认
        return {
            "needs_confirmation": True,
            "confirm_detail": (
                f"确认关闭后，{report['acknowledged_lost']} 件已确认遗失的"
                "实物将正式标记为丢失。"
            ),
            **report,
        }

    # 关闭即落账：馆员已逐件确认的遗失结果同步到实物 status
    for s in stocktake.item_snapshots.filter(
        result=StocktakeItemSnapshot.Result.LOST,
    ).select_related("item"):
        Item.objects.filter(pk=s.item_id).update(status=Item.ItemStatus.LOST)

    stocktake.state = Stocktake.State.CLOSED
    stocktake.closed_at = timezone.now()
    stocktake.save(update_fields=["state", "closed_at"])
    return {"closed": True, **completeness(stocktake)}


@transaction.atomic
def reopen_stocktake(stocktake):
    """重新打开批次：撤销关闭时判失的实物，扫描事件/冲突/快照全部保留。"""
    stocktake = Stocktake.objects.get(pk=stocktake.pk)
    if stocktake.state == Stocktake.State.OPEN:
        return {"reopened": False, "state": stocktake.state}
    for s in stocktake.item_snapshots.filter(
        result=StocktakeItemSnapshot.Result.LOST,
    ).select_related("item"):
        # 恢复为盘点开始时的状态（通常 available），结果回到「未见」
        Item.objects.filter(pk=s.item_id).update(status=s.status_snapshot)
        s.result = StocktakeItemSnapshot.Result.PENDING
        s.save(update_fields=["result"])
    stocktake.state = Stocktake.State.OPEN
    stocktake.closed_at = None
    stocktake.save(update_fields=["state", "closed_at"])
    return {"reopened": True, "state": stocktake.state}
