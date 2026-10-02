"""盘点批次验收测试。

覆盖：
  1) 正常扫描合刊 + 普通期，盘点结果与期号定位、实际位置一致；
  2) 重复扫描同一条码只计一次并保留原始事件；
  3) 扫描装订册正确确认册内实体（不制造副本），拆订并发时显示冲突而不误判丢失；
  4) 范围未完成关闭被阻止；确认后只有已发行未见进入遗失候选；
     缺号(not_published/ceased_gap)永不因未扫到变更；刷新/重开后状态可追溯；
  另：移动/改实体/装订产生版本冲突，旧扫描不覆盖新位置。

运行：SERIALREG_DB=sqlite pytest serials/test_stocktake.py -q
"""
import pytest
from rest_framework.test import APIClient

from serials.models import (
    Binding, BindingEntry, Issue, IssueNumber, Item,
    StocktakeScan, StocktakeSnapshotItem, Title,
)


@pytest.fixture
def api():
    return APIClient()


@pytest.fixture
def library(db):
    """一种刊：v.8 no.1 普通期、no.3-4 合刊、no.5 普通期、
    no.6 未发行(not_published)。三本实体都在「现刊区 B-02」。"""
    t = Title.objects.create(title="季评", issn="4444-5555")
    n1 = IssueNumber.objects.create(title=t, volume="8", number="1", sort_key=1)
    n3 = IssueNumber.objects.create(title=t, volume="8", number="3", sort_key=3)
    n4 = IssueNumber.objects.create(title=t, volume="8", number="4", sort_key=4)
    n5 = IssueNumber.objects.create(title=t, volume="8", number="5", sort_key=5)
    n6 = IssueNumber.objects.create(title=t, volume="8", number="6", sort_key=6)

    iss1 = Issue.objects.create(
        title=t, kind=Issue.IssueKind.REGULAR, issue_month="2024-01-01")
    iss1.numbers.add(n1)
    comb = Issue.objects.create(
        title=t, kind=Issue.IssueKind.COMBINED,
        issue_month="2024-03-01", issue_month_end="2024-04-01")
    comb.numbers.set([n3, n4])
    iss5 = Issue.objects.create(
        title=t, kind=Issue.IssueKind.REGULAR, issue_month="2024-05-01")
    iss5.numbers.add(n5)

    it1 = Item.objects.create(
        barcode="JP-1", title=t, issue=iss1, location="现刊区 B-02")
    itc = Item.objects.create(
        barcode="JP-34", title=t, issue=comb, location="现刊区 B-02")
    it5 = Item.objects.create(
        barcode="JP-5", title=t, issue=iss5, location="现刊区 B-02")
    return {
        "title": t, "n1": n1, "n3": n3, "n4": n4, "n5": n5, "n6": n6,
        "iss1": iss1, "comb": comb, "iss5": iss5,
        "it1": it1, "itc": itc, "it5": it5,
    }


def _start(api, lib, **extra):
    payload = {"title": lib["title"].id, "name": "2024年度盘点", **extra}
    resp = api.post("/api/stocktakes/", payload, format="json")
    assert resp.status_code == 201, resp.json()
    return resp.json()


def _snap_map(detail):
    return {row["barcode"]: row for row in detail["snapshot_items"]}


# --------------------------------------------------------------------------
# 验收 1：正常扫描合刊与普通期，结果与期号定位、实际位置一致
# --------------------------------------------------------------------------

@pytest.mark.django_db
def test_scan_combined_and_regular_matches_locate(api, library):
    batch = _start(api, library)
    # 扫普通期实体、合刊实体（合刊只产生一条快照行，不复制）
    for code in ("JP-1", "JP-34"):
        resp = api.post(f"/api/stocktakes/{batch['id']}/scan/",
                        {"barcode": code,
                         "observed_location": "现刊区 B-02"}, format="json")
        assert resp.status_code == 200, resp.json()
        assert resp.json()["event_result"] == "recorded"

    detail = api.get(f"/api/stocktakes/{batch['id']}/").json()
    rows = _snap_map(detail)
    assert rows["JP-1"]["result"] == "seen"
    assert rows["JP-34"]["result"] == "seen"
    # 合刊实体只有一行，盘点结果不产生多个副本
    assert len([r for r in detail["snapshot_items"]
                if r["barcode"] == "JP-34"]) == 1
    # 未扫的普通期仍为未见
    assert rows["JP-5"]["result"] == "unseen"

    stats = detail["stats"]
    assert stats["total"] == 3
    assert stats["seen"] == 2
    assert stats["unseen"] == 1

    # 盘点标记与期号定位一致：从合刊任一期号 no.3 / no.4 都看到同一实体已见
    for num in ("3", "4"):
        loc = api.get(
            f"/api/items/locate/?title={library['title'].id}"
            f"&volume=8&number={num}").json()
        m = loc["matches"][0]
        assert m["barcode"] == "JP-34"
        assert m["location"] == "现刊区 B-02"
        assert m["stocktake"]["result"] == "seen"
        assert m["stocktake"]["frozen_actual_location"] == "现刊区 B-02"

    # 时间轴同样带盘点标记
    tl = api.get(f"/api/timeline/?title={library['title'].id}").json()
    assert tl["active_stocktake"]["id"] == batch["id"]
    item_marks = {
        it["barcode"]: it["stocktake"]
        for slot in tl["slots"] for iss in slot["issues"]
        for it in iss["items"]
    }
    assert item_marks["JP-34"]["result"] == "seen"
    assert item_marks["JP-5"] is None or True  # JP-5 在快照中也有标记
    assert item_marks["JP-5"]["result"] == "unseen"


# --------------------------------------------------------------------------
# 验收 2：重复扫描只计一次并保留原始事件
# --------------------------------------------------------------------------

@pytest.mark.django_db
def test_duplicate_scan_counts_once_keeps_original(api, library):
    batch = _start(api, library)
    url = f"/api/stocktakes/{batch['id']}/scan/"

    r1 = api.post(url, {"barcode": "JP-1",
                        "observed_location": "现刊区 B-02"}, format="json").json()
    r2 = api.post(url, {"barcode": "JP-1",
                        "observed_location": "错误位置 X"}, format="json").json()
    r3 = api.post(url, {"barcode": "JP-1"}, format="json").json()

    assert r1["duplicate"] is False
    assert r2["duplicate"] is True
    assert r3["duplicate"] is True
    # 重复事件指向同一原始事件
    assert r2["original_scan_id"] == r1["scan_id"]
    assert r3["original_scan_id"] == r1["scan_id"]

    # 原始事件只有一条；重复事件全部留档
    scans = StocktakeScan.objects.filter(
        batch_id=batch["id"], barcode="JP-1").order_by("id")
    originals = scans.filter(is_duplicate=False)
    assert originals.count() == 1
    assert scans.count() == 3
    # 重复扫描带新位置也不能改变盘点结论（旧/重复扫码不覆盖）
    row = StocktakeSnapshotItem.objects.get(
        batch_id=batch["id"], item__barcode="JP-1")
    assert row.result == StocktakeSnapshotItem.Result.SEEN
    assert row.observed_location == "现刊区 B-02"

    stats = api.get(f"/api/stocktakes/{batch['id']}/").json()["stats"]
    assert stats["seen"] == 1


# --------------------------------------------------------------------------
# 验收 3：扫装订册确认册内实体；拆订并发 → 冲突，不误判丢失
# --------------------------------------------------------------------------

@pytest.mark.django_db
def test_scan_binding_confirms_members_and_unbind_conflict(api, library):
    # 先把 JP-34（合刊）+ JP-5 装订成一册
    resp = api.post("/api/bindings/", {
        "call_number": "Q/JP-2024", "title": library["title"].id,
        "location": "装订库 C-12",
        "item_ids": [library["itc"].id, library["it5"].id],
    }, format="json")
    assert resp.status_code == 201, resp.json()

    batch = _start(api, library)
    # 扫装订册条码（索书号）：一册映射两个实体
    resp = api.post(f"/api/stocktakes/{batch['id']}/scan/",
                    {"barcode": "Q/JP-2024"}, format="json")
    assert resp.status_code == 200, resp.json()
    data = resp.json()
    assert set(data["mapped_barcodes"]) == {"JP-34", "JP-5"}
    # 没有因为扫装订册制造实体副本：快照仍只有 3 行
    detail = api.get(f"/api/stocktakes/{batch['id']}/").json()
    assert len(detail["snapshot_items"]) == 3
    rows = _snap_map(detail)
    assert rows["JP-34"]["result"] == "seen"
    assert rows["JP-5"]["result"] == "seen"
    # 装订扫描事件本身只有一条原始事件
    assert StocktakeScan.objects.filter(
        batch_id=batch["id"], is_duplicate=False).count() == 1

    # 盘点期间另一馆员拆订（并发变更）
    binding = Binding.objects.get(call_number="Q/JP-2024")
    api.post("/api/bindings/unbind/",
             {"binding_id": binding.id}, format="json")

    # 刷新比较版本：JP-34 / JP-5 均应为“待核查”冲突，而不是未见/丢失
    resp = api.post(f"/api/stocktakes/{batch['id']}/refresh/", {},
                    format="json")
    assert resp.status_code == 200
    assert len(resp.json()["conflict_ids"]) == 2

    detail = api.get(f"/api/stocktakes/{batch['id']}/").json()
    rows = _snap_map(detail)
    for code in ("JP-34", "JP-5"):
        assert rows[code]["result"] == "review"
        kinds = [c["kind"] for c in rows[code]["conflicts"]]
        assert "unbound" in kinds
    assert detail["stats"]["review"] == 2
    assert detail["stats"]["open_conflicts"] == 2

    # 待核查 + 未确认完整时关闭必须被阻止，且不会把已扫实体误判丢失
    resp = api.post(f"/api/stocktakes/{batch['id']}/close/", {},
                    format="json")
    assert resp.status_code == 409

    # 馆员按现状（拆订后在现刊区）确认已见：以新位置为基线
    for code in ("JP-34", "JP-5"):
        cid = rows[code]["conflicts"][0]["id"]
        r = api.post(
            f"/api/stocktakes/{batch['id']}/resolve_conflict/",
            {"conflict_id": cid, "resolution": "seen",
             "note": "拆订后已在架"}, format="json")
        assert r.status_code == 200, r.json()

    detail = api.get(f"/api/stocktakes/{batch['id']}/").json()
    rows = _snap_map(detail)
    assert rows["JP-34"]["result"] == "seen"
    assert rows["JP-5"]["result"] == "seen"
    assert detail["stats"]["open_conflicts"] == 0


@pytest.mark.django_db
def test_scan_binding_after_concurrent_unbind_blocks_close(api, library):
    """先扫册后拆订（不在刷新时处理）：关闭同样被冲突阻止，遗失候选为空。"""
    binding = Binding.objects.create(
        call_number="Q/JP-X1", title=library["title"],
        location="装订库 C-12")
    for it in (library["itc"], library["it5"]):
        BindingEntry.objects.create(
            item=it, binding=binding, previous_location=it.location)
    Item.objects.filter(id__in=[it.id for it in (
        library["itc"], library["it5"])]).update(status="bound")

    batch = _start(api, library)
    api.post(f"/api/stocktakes/{batch['id']}/scan/",
             {"barcode": "JP-1"}, format="json")
    api.post(f"/api/stocktakes/{batch['id']}/scan/",
             {"barcode": "Q/JP-X1"}, format="json")
    # 并发拆订
    api.post("/api/bindings/unbind/",
             {"binding_id": binding.id}, format="json")
    # 即使馆员忘了手动刷新，关闭时也会强制比较版本 → 冲突阻止关闭，
    # 已扫的 JP-34/JP-5 不会被当成未见/丢失
    api.post(f"/api/stocktakes/{batch['id']}/confirm_complete/", {},
             format="json")
    resp = api.post(f"/api/stocktakes/{batch['id']}/close/", {},
                    format="json")
    assert resp.status_code == 409
    assert "冲突" in resp.json()["detail"]

    detail = api.get(f"/api/stocktakes/{batch['id']}/").json()
    rows = _snap_map(detail)
    assert rows["JP-34"]["result"] == "review"
    assert rows["JP-5"]["result"] == "review"
    assert detail["stats"]["lost_candidates"] == 0


# --------------------------------------------------------------------------
# 验收 4：范围完整性 + 只有已发行未见进遗失候选 + 缺号永不遗失 + 刷新/重开追溯
# --------------------------------------------------------------------------

@pytest.mark.django_db
def test_close_requires_completeness_and_only_issued_unseen_become_candidates(
        api, library):
    batch = _start(api, library)
    # 只扫 JP-1 与合刊 JP-34；JP-5 已发行未扫
    for code in ("JP-1", "JP-34"):
        api.post(f"/api/stocktakes/{batch['id']}/scan/",
                 {"barcode": code}, format="json")

    # 未确认范围完整 → 关闭阻止
    resp = api.post(f"/api/stocktakes/{batch['id']}/close/", {},
                    format="json")
    assert resp.status_code == 409
    assert "完整性" in resp.json()["detail"]

    # 确认完整
    resp = api.post(f"/api/stocktakes/{batch['id']}/confirm_complete/", {},
                    format="json")
    assert resp.status_code == 200

    # 关闭：只有 JP-5（已发行未见）进入遗失候选
    resp = api.post(f"/api/stocktakes/{batch['id']}/close/", {},
                    format="json")
    assert resp.status_code == 200, resp.json()
    candidates = resp.json()["lost_candidates"]
    assert [c["barcode"] for c in candidates] == ["JP-5"]

    detail = api.get(f"/api/stocktakes/{batch['id']}/").json()
    rows = _snap_map(detail)
    assert rows["JP-1"]["outcome"] == "kept"
    assert rows["JP-34"]["outcome"] == "kept"
    assert rows["JP-5"]["outcome"] == "lost_candidate"
    # 缺号编号只出现在 gap_numbers，且永不进入候选
    gap = {(g["volume"], g["number"]): g["frozen_holding_status"]
           for g in detail["stats"]["gap_numbers"]}
    assert gap[("8", "6")] == "not_published"
    assert "JP-34" not in [c["barcode"] for c in candidates]

    # 候选尚未真正丢失：实体状态仍是 available
    library["it5"].refresh_from_db()
    assert library["it5"].status != Item.ItemStatus.LOST

    # 显式确认遗失：只能确认候选
    jp5_row = rows["JP-5"]["id"]
    resp = api.post(
        f"/api/stocktakes/{batch['id']}/confirm_losses/",
        {"item_ids": [library["it1"].id]}, format="json")
    assert resp.status_code == 400  # JP-1 不是候选
    resp = api.post(
        f"/api/stocktakes/{batch['id']}/confirm_losses/",
        {"item_ids": [library["it5"].id]}, format="json")
    assert resp.status_code == 200, resp.json()
    library["it5"].refresh_from_db()
    assert library["it5"].status == Item.ItemStatus.LOST

    # 期号定位随之变为缺藏
    loc = api.get(
        f"/api/items/locate/?title={library['title'].id}"
        "&volume=8&number=5").json()
    assert loc["holding_status"] == "issued+missing"
    # no.6 永远是缺号，不被盘点改变
    loc6 = api.get(
        f"/api/items/locate/?title={library['title'].id}"
        "&volume=8&number=6").json()
    assert loc6["holding_status"] == "not_published"
    assert loc6["matches"] == []


@pytest.mark.django_db
def test_ceased_gap_never_becomes_loss_candidate(api, db):
    """停刊后的 ceased_gap：盘点关闭后绝不进遗失候选。"""
    t = Title.objects.create(
        title="停刊季评",
        status=Title.PublicationStatus.CEASED, ceased_month="2024-06-01")
    n5 = IssueNumber.objects.create(title=t, volume="12", number="5",
                                    sort_key=5)
    IssueNumber.objects.create(title=t, volume="12", number="6", sort_key=6)
    iss5 = Issue.objects.create(
        title=t, kind=Issue.IssueKind.REGULAR, issue_month="2024-05-01")
    iss5.numbers.add(n5)
    it = Item.objects.create(
        barcode="TJ-5", title=t, issue=iss5, location="现刊区 Z-1")

    b = _start(api, {"title": t, "n5": n5, "it1": it})
    # 一本实体也不扫
    api.post(f"/api/stocktakes/{b['id']}/confirm_complete/", {},
             format="json")
    resp = api.post(f"/api/stocktakes/{b['id']}/close/", {}, format="json")
    assert resp.status_code == 200
    # 已发行的 TJ-5 是候选；no.6(ceased_gap) 不在任何候选里
    assert [c["barcode"] for c in resp.json()["lost_candidates"]] == ["TJ-5"]
    detail = api.get(f"/api/stocktakes/{b['id']}/").json()
    gap = [g for g in detail["stats"]["gap_numbers"]
           if g["number"] == "6"]
    assert gap and gap[0]["frozen_holding_status"] == "ceased_gap"


@pytest.mark.django_db
def test_refresh_and_reopen_keep_history_traceable(api, library):
    batch = _start(api, library)
    api.post(f"/api/stocktakes/{batch['id']}/scan/",
             {"barcode": "JP-1"}, format="json")
    # 盘点期间移动 JP-5
    it5 = Item.objects.get(barcode="JP-5")
    it5.location = "现刊区 B-09"
    it5.save(update_fields=["location"])  # 版本自增

    found = api.post(f"/api/stocktakes/{batch['id']}/refresh/", {},
                     format="json").json()
    assert len(found["conflict_ids"]) == 1
    rows = _snap_map(api.get(f"/api/stocktakes/{batch['id']}/").json())
    assert rows["JP-5"]["result"] == "review"
    cid = rows["JP-5"]["conflicts"][0]["id"]
    assert rows["JP-5"]["conflicts"][0]["kind"] == "item_moved"
    assert "B-02" in rows["JP-5"]["conflicts"][0]["snapshot_value"]
    assert "B-09" in rows["JP-5"]["conflicts"][0]["current_value"]

    # 按未见处理（馆员要去新位置核查）
    api.post(f"/api/stocktakes/{batch['id']}/resolve_conflict/",
             {"conflict_id": cid, "resolution": "unseen"}, format="json")

    # 扫齐 JP-5、关闭
    api.post(f"/api/stocktakes/{batch['id']}/scan/",
             {"barcode": "JP-5", "observed_location": "现刊区 B-09"},
             format="json")
    api.post(f"/api/stocktakes/{batch['id']}/confirm_complete/", {},
             format="json")
    closed = api.post(f"/api/stocktakes/{batch['id']}/close/", {},
                      format="json")
    assert closed.status_code == 200

    # 刷新页面（重新 GET）历史仍在
    detail = api.get(f"/api/stocktakes/{batch['id']}/").json()
    assert detail["status"] == "closed"
    audit_actions = [a["action"] for a in detail["audit_logs"]]
    assert "created" in audit_actions
    assert "scan" in audit_actions
    assert "conflict" in audit_actions
    assert "conflict_resolved" in audit_actions
    assert "closed" in audit_actions

    # 重新打开：结论/事件/审计保留，可继续
    r = api.post(f"/api/stocktakes/{batch['id']}/reopen/", {},
                 format="json")
    assert r.status_code == 200
    detail = api.get(f"/api/stocktakes/{batch['id']}/").json()
    assert detail["status"] == "reopened"
    rows = _snap_map(detail)
    assert rows["JP-1"]["result"] == "seen"
    assert rows["JP-5"]["result"] == "seen"
    assert all(r["outcome"] == "pending" for r in detail["snapshot_items"])
    # 原始扫描事件数量不变（重开不清历史）
    assert StocktakeScan.objects.filter(
        batch_id=batch["id"], is_duplicate=False).count() == 2
    assert StocktakeScan.objects.filter(
        batch_id=batch["id"]).count() == 2


# --------------------------------------------------------------------------
# 版本冲突：盘点期间移动 / 修改实体，旧扫描不能覆盖新位置
# --------------------------------------------------------------------------

@pytest.mark.django_db
def test_scan_after_move_flags_conflict_not_overwrite(api, library):
    batch = _start(api, library)
    # 先在旧位置扫到 JP-5
    r = api.post(f"/api/stocktakes/{batch['id']}/scan/",
                 {"barcode": "JP-5",
                  "observed_location": "现刊区 B-02"}, format="json").json()
    assert r["snapshot_items"][0]["result"] == "seen"

    # 盘点中有人把实体移走并报失
    it5 = Item.objects.get(barcode="JP-5")
    it5.location = "修复区 R-2"
    it5.status = Item.ItemStatus.CHECKED_OUT
    it5.save()

    # 再扫同一条码（重复事件），结论不被重复扫描改变
    r = api.post(f"/api/stocktakes/{batch['id']}/scan/",
                 {"barcode": "JP-5"}, format="json").json()
    assert r["duplicate"] is True

    # 刷新发现并发变更 → 待核查，快照值仍是 B-02（不被覆盖）
    api.post(f"/api/stocktakes/{batch['id']}/refresh/", {}, format="json")
    detail = api.get(f"/api/stocktakes/{batch['id']}/").json()
    row = _snap_map(detail)["JP-5"]
    assert row["result"] == "review"
    assert row["frozen_actual_location"] == "现刊区 B-02"
    assert row["current_location"] == "修复区 R-2"
    conflict = row["conflicts"][0]
    assert conflict["kind"] == "item_moved"
    assert "B-02" in conflict["snapshot_value"]
    assert "R-2" in conflict["current_value"]


@pytest.mark.django_db
def test_scan_unknown_and_out_of_scope(api, library):
    batch = _start(api, library)
    # 未知条码：记录事件但不生成快照行
    r = api.post(f"/api/stocktakes/{batch['id']}/scan/",
                 {"barcode": "NO-SUCH"}, format="json").json()
    assert r["event_result"] == "unknown"
    assert r["snapshot_items"] == []

    # 另一刊实体：范围外
    t2 = Title.objects.create(title="别刊")
    n = IssueNumber.objects.create(title=t2, volume="1", number="1")
    iss = Issue.objects.create(title=t2, issue_month="2024-01-01")
    iss.numbers.add(n)
    other = Item.objects.create(
        barcode="BK-1", title=t2, issue=iss, location="X")
    r = api.post(f"/api/stocktakes/{batch['id']}/scan/",
                 {"barcode": "BK-1"}, format="json").json()
    assert r["event_result"] == "out_of_scope"
    assert r["snapshot_items"] == []
    # 范围外扫描不改变任何盘点统计
    stats = api.get(f"/api/stocktakes/{batch['id']}/").json()["stats"]
    assert stats["seen"] == 0


@pytest.mark.django_db
def test_misplaced_shelf_does_not_become_loss_candidate(api, library):
    """扫到但位置不对 → 错架；错架是已见的一种，不进遗失候选。"""
    batch = _start(api, library)
    for code in ("JP-1", "JP-34", "JP-5"):
        api.post(f"/api/stocktakes/{batch['id']}/scan/",
                 {"barcode": code,
                  "observed_location": "错误书架 E-9"}, format="json")
    detail = api.get(f"/api/stocktakes/{batch['id']}/").json()
    assert detail["stats"]["misplaced"] == 3
    api.post(f"/api/stocktakes/{batch['id']}/confirm_complete/", {},
             format="json")
    resp = api.post(f"/api/stocktakes/{batch['id']}/close/", {},
                    format="json")
    assert resp.status_code == 200
    assert resp.json()["lost_candidates"] == []


@pytest.mark.django_db
def test_scope_location_filter_and_binding_location(api, library):
    """按库位范围启动盘点：装订册实体按装订后实际位置归属。"""
    binding = Binding.objects.create(
        call_number="Q/L", title=library["title"], location="装订库 C-12")
    BindingEntry.objects.create(
        item=library["it5"], binding=binding,
        previous_location="现刊区 B-02")
    Item.objects.filter(id=library["it5"].id).update(status="bound")

    b1 = _start(api, library, scope_location="装订库 C-12")
    detail = api.get(f"/api/stocktakes/{b1['id']}/").json()
    barcodes = {r["barcode"] for r in detail["snapshot_items"]}
    assert barcodes == {"JP-5"}

    # 同一刊不能并发开第二个进行中批次
    resp = api.post("/api/stocktakes/",
                    {"title": library["title"].id}, format="json")
    assert resp.status_code == 400


@pytest.mark.django_db
def test_scope_change_when_issue_registered_mid_stocktake(api, library):
    """盘点期间为缺号 no.6 登记发行期 → 范围变化冲突，确认后纳入。"""
    batch = _start(api, library)
    n6 = library["n6"]
    iss6 = Issue.objects.create(
        title=library["title"], issue_month="2024-06-01")
    iss6.numbers.add(n6)

    found = api.post(f"/api/stocktakes/{batch['id']}/refresh/", {},
                     format="json").json()
    assert len(found["conflict_ids"]) == 1
    # 未处理范围变化 → 关闭阻止
    api.post(f"/api/stocktakes/{batch['id']}/confirm_complete/", {},
             format="json")
    assert api.post(f"/api/stocktakes/{batch['id']}/close/", {},
                    format="json").status_code == 409

    # 确认范围变化：缺号变为已发行；新实体尚未入藏 → 仍是缺号侧无实体，
    # 此处登记一本实体验证其被吸收进快照（未见，需要扫）
    it6 = Item.objects.create(
        barcode="JP-6", title=library["title"], issue=iss6,
        location="现刊区 B-02")
    cid = found["conflict_ids"][0]
    r = api.post(f"/api/stocktakes/{batch['id']}/resolve_conflict/",
                 {"conflict_id": cid, "resolution": "scope"},
                 format="json")
    assert r.status_code == 200
    detail = api.get(f"/api/stocktakes/{batch['id']}/").json()
    rows = _snap_map(detail)
    assert "JP-6" in rows and rows["JP-6"]["result"] == "unseen"

    # 不扫 JP-6 直接关闭 → JP-6 成为遗失候选（它是已发行未见，不再是缺号）
    for code in ("JP-1", "JP-34", "JP-5"):
        api.post(f"/api/stocktakes/{batch['id']}/scan/",
                 {"barcode": code}, format="json")
    resp = api.post(f"/api/stocktakes/{batch['id']}/close/", {},
                    format="json")
    assert resp.status_code == 200
    assert [c["barcode"] for c in resp.json()["lost_candidates"]] == ["JP-6"]


@pytest.mark.django_db
def test_binding_moved_during_stocktake_conflict(api, library):
    """盘点期间装订册整体移库 → binding 版本变化冲突。"""
    binding = Binding.objects.create(
        call_number="Q/M", title=library["title"], location="装订库 C-12")
    for it in (library["itc"], library["it5"]):
        BindingEntry.objects.create(
            item=it, binding=binding, previous_location=it.location)
    Item.objects.filter(id__in=[library["itc"].id, library["it5"].id]) \
        .update(status="bound")

    batch = _start(api, library)
    api.post(f"/api/stocktakes/{batch['id']}/scan/",
             {"barcode": "Q/M"}, format="json")
    # 移库（走 Binding.save，版本自增）
    binding.location = "装订库 D-03"
    binding.save(update_fields=["location"])

    found = api.post(f"/api/stocktakes/{batch['id']}/refresh/", {},
                     format="json").json()
    assert len(found["conflict_ids"]) == 2
    detail = api.get(f"/api/stocktakes/{batch['id']}/").json()
    for code in ("JP-34", "JP-5"):
        kinds = [c["kind"] for c in _snap_map(detail)[code]["conflicts"]]
        assert "binding_moved" in kinds


@pytest.mark.django_db
def test_bound_during_stocktake_then_conflict_not_lost(api, library):
    """盘点期间把未装订实体装订 → BOUND 冲突，该实体不进遗失候选。"""
    batch = _start(api, library)
    # 扫 JP-1 与合刊 JP-34，只有 JP-5 未扫
    for code in ("JP-1", "JP-34"):
        api.post(f"/api/stocktakes/{batch['id']}/scan/",
                 {"barcode": code}, format="json")
    # 盘点期间把 JP-5 装订（另一册，只含 JP-5 也允许）
    resp = api.post("/api/bindings/", {
        "call_number": "Q/NEW", "title": library["title"].id,
        "location": "装订库 C-99", "item_ids": [library["it5"].id],
    }, format="json")
    assert resp.status_code == 201, resp.json()

    api.post(f"/api/stocktakes/{batch['id']}/confirm_complete/", {},
             format="json")
    # 关闭被阻止（JP-5 待核查），而不是把 JP-5 当遗失
    resp = api.post(f"/api/stocktakes/{batch['id']}/close/", {},
                    format="json")
    assert resp.status_code == 409
    detail = api.get(f"/api/stocktakes/{batch['id']}/").json()
    row = _snap_map(detail)["JP-5"]
    assert row["result"] == "review"
    assert row["conflicts"][0]["kind"] == "bound"
    # 候选里没有 JP-5
    assert detail["stats"]["lost_candidates"] == 0
