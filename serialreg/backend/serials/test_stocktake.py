"""盘点批次验收测试。

覆盖：
  1) 正常扫描合刊与普通期 → 盘点结果与期号定位、实际位置一致；
  2) 重复扫描同一条码只计一次并保留原始事件；
  3) 扫描装订册正确确认册内全部实体（不制造副本）；盘点期间拆订 →
     显示冲突而不误判丢失；
  4) 范围未完成时关闭被阻止；确认完成后只有已发行未见实体进入遗失候选；
     缺号（not_published / ceased_gap）永远不因未扫到而变更；
     刷新与重新打开批次后状态仍可追溯。
"""
import pytest
from rest_framework.test import APIClient

from serials.models import (
    Binding, BindingEntry, Issue, IssueNumber, Item, Stocktake,
    StocktakeConflict, StocktakeItemSnapshot, StocktakeScan, Title,
)
from serials import stocktake as svc


@pytest.fixture
def api():
    return APIClient()


@pytest.fixture
def shelf(db):
    """《双月志》v.8：no.3-4 合刊 + no.5 普通期 + no.6 缺号（在刊无发行）。

    另造同刊 v.9 no.1，用于验证范围只冻结指定卷。
    """
    t = Title.objects.create(title="双月志", issn="4444-5555")
    n3 = IssueNumber.objects.create(title=t, volume="8", number="3", sort_key=3)
    n4 = IssueNumber.objects.create(title=t, volume="8", number="4", sort_key=4)
    n5 = IssueNumber.objects.create(title=t, volume="8", number="5", sort_key=5)
    n6 = IssueNumber.objects.create(title=t, volume="8", number="6", sort_key=6)
    comb = Issue.objects.create(
        title=t, kind=Issue.IssueKind.COMBINED, issue_month="2024-03-01",
        issue_month_end="2024-04-01",
    )
    comb.numbers.set([n3, n4])
    iss5 = Issue.objects.create(
        title=t, kind=Issue.IssueKind.REGULAR, issue_month="2024-05-01",
    )
    iss5.numbers.add(n5)
    it34 = Item.objects.create(
        barcode="SZ-34", title=t, issue=comb, location="现刊区 B-02",
    )
    it5 = Item.objects.create(
        barcode="SZ-05", title=t, issue=iss5, location="现刊区 B-03",
    )
    # 另一卷，不应进入 v.8 的盘点范围
    n91 = IssueNumber.objects.create(title=t, volume="9", number="1", sort_key=1)
    iss91 = Issue.objects.create(
        title=t, kind=Issue.IssueKind.REGULAR, issue_month="2024-09-01",
    )
    iss91.numbers.add(n91)
    it91 = Item.objects.create(
        barcode="SZ-91", title=t, issue=iss91, location="现刊区 B-09",
    )
    return {
        "title": t, "n3": n3, "n4": n4, "n5": n5, "n6": n6,
        "comb": comb, "iss5": iss5,
        "it34": it34, "it5": it5, "it91": it91,
    }


@pytest.fixture
def ceased_shelf(db):
    """《终刊报》2024-06 停刊：no.5 已发行无实物（缺藏），no.6 停刊后缺号。"""
    t = Title.objects.create(
        title="终刊报",
        status=Title.PublicationStatus.CEASED, ceased_month="2024-06-01",
    )
    n5 = IssueNumber.objects.create(title=t, volume="3", number="5", sort_key=5)
    n6 = IssueNumber.objects.create(title=t, volume="3", number="6", sort_key=6)
    iss5 = Issue.objects.create(
        title=t, kind=Issue.IssueKind.REGULAR, issue_month="2024-05-01",
    )
    iss5.numbers.add(n5)
    it5 = Item.objects.create(
        barcode="ZZ-05", title=t, issue=iss5, location="库 X-1",
    )
    return {"title": t, "n5": n5, "n6": n6, "it5": it5}


def _start(api, shelf, volume="8", **extra):
    payload = {"title": shelf["title"].id, "scope_volume": volume}
    payload.update(extra)
    resp = api.post("/api/stocktakes/", payload, format="json")
    assert resp.status_code == 201, resp.json()
    return resp.json()


# ---------- 1) 正常扫描：合刊 + 普通期，结果与定位一致 ----------

@pytest.mark.django_db
def test_scan_combined_and_regular_matches_locate(shelf, api):
    batch = _start(api, shelf)
    # 扫合刊实物
    resp = api.post(f"/api/stocktakes/{batch['id']}/scan/", {
        "barcode": "SZ-34", "observed_location": "现刊区 B-02",
    }, format="json")
    assert resp.status_code == 200, resp.json()
    body = resp.json()
    assert body["duplicate"] is False
    # 合刊扫描映射到唯一实体，而不是两个副本
    assert body["resolved_item_ids"] == [shelf["it34"].id]

    # 扫普通期
    resp = api.post(f"/api/stocktakes/{batch['id']}/scan/", {
        "barcode": "SZ-05", "observed_location": "现刊区 B-03",
    }, format="json")
    assert resp.status_code == 200

    detail = api.get(f"/api/stocktakes/{batch['id']}/").json()
    results = {it["barcode"]: it["result"] for it in detail["items"]}
    assert results == {"SZ-34": "seen", "SZ-05": "seen"}
    # 范围只含 v.8，不含 v.9
    assert len(detail["items"]) == 2
    assert detail["completeness"]["complete"] is True

    # 盘点标记与期号定位一致：合刊 no.3 / no.4 均命中同一已见实体
    for num in ("3", "4", "5"):
        loc = api.get(
            f"/api/items/locate/?title={shelf['title'].id}"
            f"&volume=8&number={num}&stocktake={batch['id']}",
        ).json()
        assert loc["stocktake"] == batch["id"]
        m = loc["matches"][0]
        assert m["stocktake"]["result"] == "seen", num
        assert m["location"] == (
            "现刊区 B-02" if num in ("3", "4") else "现刊区 B-03"), num

    # 缺号 no.6 无发行：盘点标记为空，且永远不会出现该实物快照
    loc6 = api.get(
        f"/api/items/locate/?title={shelf['title'].id}&volume=8&number=6",
    ).json()
    assert loc6["holding_status"] == "not_published"
    assert loc6["matches"] == []


@pytest.mark.django_db
def test_misplaced_scan_marked_misplaced(shelf, api):
    batch = _start(api, shelf)
    resp = api.post(f"/api/stocktakes/{batch['id']}/scan/", {
        "barcode": "SZ-05", "observed_location": "现刊区 Z-99",
    }, format="json")
    assert resp.status_code == 200
    detail = api.get(f"/api/stocktakes/{batch['id']}/").json()
    row = next(it for it in detail["items"] if it["barcode"] == "SZ-05")
    assert row["result"] == "misplaced"
    assert detail["completeness"]["misplaced"] == 1
    # 错架未复核前范围不完整
    assert detail["completeness"]["complete"] is False
    # 复核确认已见（馆员已把它放回/认可位置）
    resp = api.post(f"/api/stocktakes/{batch['id']}/review/", {
        "item_id": shelf["it5"].id, "result": "seen",
        "observed_location": "现刊区 B-03",
    }, format="json")
    assert resp.status_code == 200
    body = resp.json()
    assert body["completeness"]["misplaced"] == 0
    # 另一件合刊尚未扫，仍是未见
    assert body["completeness"]["pending"] == 1
    assert body["completeness"]["complete"] is False
    # 扫完合刊后范围才完整
    api.post(f"/api/stocktakes/{batch['id']}/scan/", {
        "barcode": "SZ-34", "observed_location": "现刊区 B-02",
    }, format="json")
    detail = api.get(f"/api/stocktakes/{batch['id']}/").json()
    assert detail["completeness"]["complete"] is True


# ---------- 2) 重复扫描幂等 ----------

@pytest.mark.django_db
def test_duplicate_scan_counts_once_keeps_original_event(shelf, api):
    batch = _start(api, shelf)
    first = api.post(f"/api/stocktakes/{batch['id']}/scan/", {
        "barcode": "SZ-34", "observed_location": "现刊区 B-02",
    }, format="json").json()
    first_scan_id = first["scan_id"]

    resp = api.post(f"/api/stocktakes/{batch['id']}/scan/", {
        "barcode": "SZ-34", "observed_location": "别处",
    }, format="json")
    assert resp.status_code == 200
    second = resp.json()
    assert second["duplicate"] is True
    assert second["first_scan_at"]  # 回放原始事件时间
    # 第二次扫描的架位不会覆盖原始扫描
    detail = api.get(f"/api/stocktakes/{batch['id']}/").json()
    row = next(it for it in detail["items"] if it["barcode"] == "SZ-34")
    assert row["observed_location"] == "现刊区 B-02"
    assert row["result"] == "seen"
    # 审计事件只有一条
    scans = [s for s in detail["scans"] if s["barcode"] == "SZ-34"]
    assert len(scans) == 1
    assert scans[0]["id"] == first_scan_id


@pytest.mark.django_db
def test_unknown_barcode_is_404(shelf, api):
    batch = _start(api, shelf)
    resp = api.post(f"/api/stocktakes/{batch['id']}/scan/", {
        "barcode": "NO-SUCH",
    }, format="json")
    assert resp.status_code == 404
    detail = api.get(f"/api/stocktakes/{batch['id']}/").json()
    assert detail["scans"] == []


# ---------- 3) 装订册扫描 + 并发拆订冲突 ----------

def _bind(shelf):
    binding = Binding.objects.create(
        call_number="Q/SZ-2024", title=shelf["title"], location="装订库 C-12",
    )
    for it, prev in (
        (shelf["it34"], "现刊区 B-02"), (shelf["it5"], "现刊区 B-03"),
    ):
        BindingEntry.objects.create(
            item=it, binding=binding, previous_location=prev)
    Item.objects.filter(id__in=[shelf["it34"].id, shelf["it5"].id]).update(
        status=Item.ItemStatus.BOUND,
    )
    return binding


@pytest.mark.django_db
def test_scan_binding_confirms_all_members_no_duplicates(shelf, api):
    binding = _bind(shelf)
    batch = _start(api, shelf)
    resp = api.post(f"/api/stocktakes/{batch['id']}/scan/", {
        "barcode": "Q/SZ-2024",
    }, format="json")
    assert resp.status_code == 200, resp.json()
    body = resp.json()
    assert body["target_type"] == "binding"
    # 装订册扫描映射全部成员，且不制造副本
    assert sorted(body["resolved_item_ids"]) == sorted(
        [shelf["it34"].id, shelf["it5"].id])
    detail = api.get(f"/api/stocktakes/{batch['id']}/").json()
    assert {it["barcode"]: it["result"] for it in detail["items"]} == {
        "SZ-34": "seen", "SZ-05": "seen"}
    # 从合刊任一期号定位，实际位置是装订库且盘点已见
    for num in ("3", "4", "5"):
        m = api.get(
            f"/api/items/locate/?title={shelf['title'].id}"
            f"&volume=8&number={num}&stocktake={batch['id']}",
        ).json()["matches"][0]
        assert m["bound"] is True
        assert m["binding"] == "Q/SZ-2024"
        assert m["location"] == "装订库 C-12"
        assert m["stocktake"]["result"] == "seen"
    # 再扫册内任一条码：仍是重复扫描，不会新增事件
    again = api.post(f"/api/stocktakes/{batch['id']}/scan/", {
        "barcode": "SZ-34",
    }, format="json").json()
    assert again["duplicate"] is True
    detail = api.get(f"/api/stocktakes/{batch['id']}/").json()
    assert len(detail["scans"]) == 1  # 仅装订册那一条首次扫描


@pytest.mark.django_db
def test_concurrent_unbind_creates_conflict_not_false_loss(shelf, api):
    """盘点期间有人拆订：旧扫描不得覆盖新关系，必须出待处理冲突。"""
    binding = _bind(shelf)
    # 启动盘点（快照冻结为装订态）
    batch_obj = svc.create_stocktake(
        shelf["title"], scope_volume="8", name="v8盘点",
    )
    # 馆员先扫了装订册，两件实体都已见
    svc.scan(batch_obj, "Q/SZ-2024")
    assert set(StocktakeItemSnapshot.objects.filter(
        stocktake=batch_obj).values_list("result", flat=True)) == {"seen"}

    # 盘点期间另一流程拆订（版本推进）
    api.post("/api/bindings/unbind/",
             {"binding_id": binding.id}, format="json")

    # 刷新批次详情 → 出现未解决冲突，实物标待核查，不会被当成丢失
    resp = api.get(f"/api/stocktakes/{batch_obj.id}/").json()
    open_conflicts = [c for c in resp["conflicts"] if c["status"] == "open"]
    kinds = {c["kind"] for c in open_conflicts}
    assert "unbound" in kinds
    assert resp["completeness"]["open_conflicts"] >= 1
    assert resp["completeness"]["complete"] is False
    for it in resp["items"]:
        assert it["result"] == "review"
        assert it["result"] != "lost"

    # 存在未解决冲突时关闭必须被阻止
    blocked = api.post(f"/api/stocktakes/{batch_obj.id}/close/", {
        "confirm": True,
    }, format="json")
    assert blocked.status_code == 409
    assert blocked.json()["blocked"] is True

    # 解决冲突并复核后才能关闭
    for c in open_conflicts:
        api.post(
            f"/api/stocktakes/{batch_obj.id}/conflicts/{c['id']}/resolve/",
            {}, format="json",
        )
    for it in resp["items"]:
        api.post(f"/api/stocktakes/{batch_obj.id}/review/", {
            "item_id": it["item_id"], "result": "seen",
        }, format="json")
    closed = api.post(f"/api/stocktakes/{batch_obj.id}/close/", {
        "confirm": True,
    }, format="json")
    assert closed.status_code == 200, closed.json()
    assert closed.json()["state"] == "closed"


@pytest.mark.django_db
def test_move_during_stocktake_conflicts_and_keeps_new_location(shelf, api):
    batch_obj = svc.create_stocktake(shelf["title"], scope_volume="8")
    svc.scan(batch_obj, "SZ-34", observed_location="现刊区 B-02")
    # 盘点期间实物被移动到新位置（版本推进）
    it34 = shelf["it34"]
    it34.location = "现刊区 B-77"
    it34.save()
    # Item.save 不会自动 bump 版本（只有 API/装订流程推进），模拟 API 修改：
    from serials.serializers import ItemSerializer
    ser = ItemSerializer(
        it34, data={"location": "现刊区 B-77"}, partial=True,
    )
    assert ser.is_valid(), ser.errors
    ser.save()
    it34.refresh_from_db()
    assert it34.version == 2

    # 再扫同一条码：幂等事件不覆盖；刷新检测到移动冲突
    result = svc.scan(batch_obj, "SZ-34")
    assert result["duplicate"] is True
    _, open_n = svc.detect_conflicts(batch_obj)
    assert open_n >= 1
    conflict = StocktakeConflict.objects.get(kind="item_changed")
    assert "B-77" in conflict.current_value
    assert "B-02" in conflict.snapshot_value


@pytest.mark.django_db
def test_new_item_enters_scope_creates_moved_in_conflict(shelf, api):
    """盘点期间新入藏/移入范围的实物不在冻结快照里，需人工核查。"""
    batch_obj = svc.create_stocktake(shelf["title"], scope_volume="8")
    # 盘点期间为已登记但未发行的 no.6 补登记发行并入藏（已发行新实物）
    n6 = shelf["n6"]
    iss6 = Issue.objects.create(
        title=shelf["title"], kind=Issue.IssueKind.REGULAR,
        issue_month="2024-06-01",
    )
    iss6.numbers.add(n6)
    new_item = Item.objects.create(
        barcode="SZ-06", title=shelf["title"], issue=iss6,
        location="现刊区 B-04",
    )
    _, open_n = svc.detect_conflicts(batch_obj)
    assert open_n >= 1
    conflict = StocktakeConflict.objects.get(kind="moved_in")
    assert conflict.item_id == new_item.id
    # 新实物不在快照内，不会成为遗失候选也不凭空被标已见
    detail = api.get(f"/api/stocktakes/{batch_obj.id}/").json()
    barcodes = {it["barcode"] for it in detail["items"]}
    assert "SZ-06" not in barcodes
    assert detail["completeness"]["complete"] is False


# ---------- 4) 范围完整性 + 关闭/确认/重开 + 缺号不变 ----------

@pytest.mark.django_db
def test_close_blocked_until_complete_then_only_issued_lost(shelf, api):
    batch = _start(api, shelf)
    # 只扫合刊，普通期 no.5 未见
    api.post(f"/api/stocktakes/{batch['id']}/scan/", {
        "barcode": "SZ-34", "observed_location": "现刊区 B-02",
    }, format="json")

    # 试关闭（不带 confirm）：返回完整性报告，列出遗失候选但阻止正式关闭
    resp = api.post(f"/api/stocktakes/{batch['id']}/close/", {}, format="json")
    assert resp.status_code == 200
    assert resp.json()["blocked"] is True
    candidates = {c["barcode"] for c in resp.json()["lost_candidates"]}
    assert candidates == {"SZ-05"}

    # 仍有待见实物时，即便 confirm 也阻止关闭（范围完整性未交代）
    resp = api.post(f"/api/stocktakes/{batch['id']}/close/", {
        "confirm": True,
    }, format="json")
    assert resp.status_code == 409
    assert resp.json()["pending"] == 1

    # 馆员扫到 no.5 → 范围完整
    api.post(f"/api/stocktakes/{batch['id']}/scan/", {
        "barcode": "SZ-05", "observed_location": "现刊区 B-03",
    }, format="json")
    resp = api.post(f"/api/stocktakes/{batch['id']}/close/", {
        "confirm": True,
    }, format="json")
    assert resp.status_code == 200, resp.json()
    assert resp.json()["state"] == "closed"


@pytest.mark.django_db
def test_acknowledge_unseen_issued_item_then_close_marks_lost(shelf, api):
    """关键验收：未见的已发行实物需馆员显式确认遗失才判失；
    手工不能把「未见」直接标成「已见」绕过扫描。"""
    batch = _start(api, shelf)
    # SZ-34 扫到
    api.post(f"/api/stocktakes/{batch['id']}/scan/", {
        "barcode": "SZ-34", "observed_location": "现刊区 B-02",
    }, format="json")
    # 不能把未扫到的 SZ-05 直接手工标成已见
    bad = api.post(f"/api/stocktakes/{batch['id']}/review/", {
        "item_id": shelf["it5"].id, "result": "seen",
    }, format="json")
    assert bad.status_code == 400

    # 馆员在现场找过，明确承认 SZ-05 遗失
    resp = api.post(f"/api/stocktakes/{batch['id']}/review/", {
        "item_id": shelf["it5"].id, "result": "lost",
    }, format="json")
    assert resp.status_code == 200, resp.json()
    assert resp.json()["completeness"]["pending"] == 0
    assert resp.json()["completeness"]["acknowledged_lost"] == 1

    # 第一次不带 confirm：要求确认
    pre = api.post(f"/api/stocktakes/{batch['id']}/close/", {}, format="json")
    assert pre.status_code == 200
    assert pre.json()["needs_confirmation"] is True
    # 确认关闭：只有已发行实物 SZ-05 落账为 lost；SZ-34 保持在馆
    closed = api.post(f"/api/stocktakes/{batch['id']}/close/", {
        "confirm": True,
    }, format="json")
    assert closed.status_code == 200
    shelf["it5"].refresh_from_db()
    shelf["it34"].refresh_from_db()
    assert shelf["it5"].status == Item.ItemStatus.LOST
    assert shelf["it34"].status == Item.ItemStatus.AVAILABLE

    # 期号定位随之变化：no.5 变缺藏；合刊 no.3/no.4 仍已入藏
    loc5 = api.get(
        f"/api/items/locate/?title={shelf['title'].id}&volume=8&number=5",
    ).json()
    assert loc5["holding_status"] == "issued+missing"
    loc3 = api.get(
        f"/api/items/locate/?title={shelf['title'].id}&volume=8&number=3",
    ).json()
    assert loc3["holding_status"] == "issued+held"

    # 刷新批次（重新 GET）结果仍可追溯
    detail = api.get(f"/api/stocktakes/{batch['id']}/").json()
    row = next(it for it in detail["items"] if it["barcode"] == "SZ-05")
    assert row["result"] == "lost"
    assert row["current_status"] == "lost"

    # 重新打开：撤销判失，SZ-05 回到盘点开始时状态、结果回到未见
    reopened = api.post(
        f"/api/stocktakes/{batch['id']}/reopen/", {}, format="json",
    ).json()
    assert reopened["state"] == "open"
    shelf["it5"].refresh_from_db()
    assert shelf["it5"].status == Item.ItemStatus.AVAILABLE
    detail = api.get(f"/api/stocktakes/{batch['id']}/").json()
    row = next(it for it in detail["items"] if it["barcode"] == "SZ-05")
    assert row["result"] == "pending"
    # 审计事件仍然保留
    assert len(detail["scans"]) == 1
    assert detail["scans"][0]["barcode"] == "SZ-34"


@pytest.mark.django_db
def test_gap_numbers_never_turn_lost(ceased_shelf, api):
    """缺号（not_published / ceased_gap）没有实物，永远不因未扫到变更。"""
    t = ceased_shelf["title"]
    batch = _start(api, ceased_shelf, volume="3")
    # 唯一实物 ZZ-05 扫到
    api.post(f"/api/stocktakes/{batch['id']}/scan/", {
        "barcode": "ZZ-05", "observed_location": "库 X-1",
    }, format="json")
    resp = api.post(f"/api/stocktakes/{batch['id']}/close/", {
        "confirm": True,
    }, format="json")
    assert resp.status_code == 200, resp.json()
    # 遗失候选始终为空：缺号槽位不可能产生遗失候选
    assert resp.json()["completeness"]["lost_candidates"] == []

    # 时间轴：no.6 仍为 ceased_gap；未发行状态没有被盘点改动
    tl = api.get(f"/api/timeline/?title={t.id}").json()
    statuses = {s["number"]: s["holding_status"] for s in tl["slots"]}
    assert statuses["5"] == "issued+held"
    assert statuses["6"] == "ceased_gap"


@pytest.mark.django_db
def test_not_published_slot_has_no_snapshot_and_cannot_acknowledge(shelf, api):
    """在刊缺号 no.6（not_published）不进入快照，也没有确认遗失的入口。"""
    batch = _start(api, shelf)
    snapshot_count = StocktakeItemSnapshot.objects.filter(
        stocktake_id=batch["id"],
    ).count()
    assert snapshot_count == 2  # 只有 SZ-34、SZ-05 两件已发行实物
    # 尝试对不存在于快照的期号/实物做遗失确认 → 404
    resp = api.post(f"/api/stocktakes/{batch['id']}/review/", {
        "item_id": 999999, "result": "lost",
    }, format="json")
    assert resp.status_code == 404


@pytest.mark.django_db
def test_reopen_reverses_loss_and_keeps_audit(shelf, api):
    batch = _start(api, shelf)
    # 两件都扫到并关闭
    for code, loc in (("SZ-34", "现刊区 B-02"), ("SZ-05", "现刊区 B-03")):
        api.post(f"/api/stocktakes/{batch['id']}/scan/", {
            "barcode": code, "observed_location": loc,
        }, format="json")
    api.post(f"/api/stocktakes/{batch['id']}/close/",
             {"confirm": True}, format="json")

    # 重新打开
    resp = api.post(f"/api/stocktakes/{batch['id']}/reopen/", {}, format="json")
    assert resp.status_code == 200
    assert resp.json()["state"] == "open"
    # 扫描审计事件、快照仍然存在，可追溯
    detail = api.get(f"/api/stocktakes/{batch['id']}/").json()
    assert len(detail["scans"]) == 2
    assert {it["barcode"] for it in detail["items"]} == {"SZ-34", "SZ-05"}


# ---------- 时间轴盘点标记 ----------

@pytest.mark.django_db
def test_timeline_shows_stocktake_marks(shelf, api):
    batch = _start(api, shelf)
    api.post(f"/api/stocktakes/{batch['id']}/scan/", {
        "barcode": "SZ-34", "observed_location": "现刊区 B-02",
    }, format="json")
    tl = api.get(
        f"/api/timeline/?title={shelf['title'].id}"
        f"&stocktake={batch['id']}",
    ).json()
    assert tl["stocktake"]["id"] == batch["id"]
    comp = tl["stocktake"]["completeness"]
    assert comp["seen"] == 1 and comp["pending"] == 1
    by_number = {s["number"]: s for s in tl["slots"]}
    # 合刊槽位 no.3 / no.4 同一实体均显示已见
    for num in ("3", "4"):
        mark = by_number[num]["issues"][0]["items"][0]["stocktake"]
        assert mark["result"] == "seen"
    # 未扫的普通期显示未见
    mark5 = by_number["5"]["issues"][0]["items"][0]["stocktake"]
    assert mark5["result"] == "pending"
    # 缺号槽位无实物，自然无盘点标记
    assert by_number["6"]["issues"] == []
