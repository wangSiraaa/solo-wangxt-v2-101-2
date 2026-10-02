"""连续出版物登记领域模型。

四层结构：
  Title        连续出版物（书目层，可标记停刊）
  IssueNumber  卷期编号（一个编号槽位，跨发行月的跨年卷按卷+期唯一）
  Issue        发行实体（一次出版行为；普通期挂一个编号，合刊挂多个编号）
  Item         馆内实物（一条条码=一个实物，不允许一条条码代表多个合刊期号关系）
  Binding      装订册（多个 Item 装订在一起，拆订后 Item 恢复各自位置）

两条易混的业务规则分开表达：
  缺号 = IssueNumber 没有对应 Issue（没有发行记录），不自动等于缺藏；
  缺藏 = 该编号已发行（存在 Issue），但没有入库 Item 或 Item 丢失。
"""
from django.db import models
from django.db.models import Q
from django.core.exceptions import ValidationError


class Title(models.Model):
    """连续出版物刊名。"""

    class PublicationStatus(models.TextChoices):
        ACTIVE = "active", "在刊"
        CEASED = "ceased", "停刊"

    title = models.CharField("刊名", max_length=255)
    issn = models.CharField("ISSN", max_length=9, blank=True)
    publisher = models.CharField("出版者", max_length=255, blank=True)
    status = models.CharField(
        "出版状态", max_length=10,
        choices=PublicationStatus.choices, default=PublicationStatus.ACTIVE,
    )
    # 停刊月份：与卷期编号分开记录，只表示出版停止，不改变任何馆藏状态
    ceased_month = models.DateField("停刊月份", null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["title"]

    def __str__(self):
        return self.title

    def clean(self):
        if self.status == self.PublicationStatus.CEASED and not self.ceased_month:
            raise ValidationError({"ceased_month": "停刊刊名必须填写停刊月份。"})


class IssueNumber(models.Model):
    """卷期编号（编号槽位），与发行年月解耦。

    跨年卷：同一卷可以跨自然年，例如 v.60 no.3 印的是 2023-12、2024-01，
    编号仍只有一条 (volume=60, number=3)，发行时间记录在 Issue 上。
    """

    title = models.ForeignKey(
        Title, on_delete=models.CASCADE, related_name="numbers",
    )
    volume = models.CharField("卷", max_length=20, blank=True)
    number = models.CharField("期", max_length=20)
    sort_key = models.PositiveIntegerField(
        "排序键", default=0,
        help_text="馆员录入的编号顺序，跨年卷按编号顺序而非月份排列",
    )

    class Meta:
        verbose_name = "期号"
        unique_together = ("title", "volume", "number")
        ordering = ["sort_key", "id"]

    def __str__(self):
        return f"{self.volume}({self.number})" if self.volume else self.number


class Issue(models.Model):
    """一次发行。普通期关联一个 IssueNumber；两期合刊关联两个（或更多）。"""

    class IssueKind(models.TextChoices):
        REGULAR = "regular", "普通期"
        COMBINED = "combined", "合刊"

    title = models.ForeignKey(
        Title, on_delete=models.CASCADE, related_name="issues",
    )
    kind = models.CharField(
        "类型", max_length=10,
        choices=IssueKind.choices, default=IssueKind.REGULAR,
    )
    # 发行年月与卷期编号分开录入
    issue_month = models.DateField("发行年月", help_text="只取年月；合刊可只填起始月")
    issue_month_end = models.DateField(
        "发行截止年月", null=True, blank=True, help_text="合刊/跨年卷的覆盖结束月",
    )
    numbers = models.ManyToManyField(
        IssueNumber, through="IssueNumbering", related_name="issues",
    )
    note = models.CharField("备注", max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "发行期"
        ordering = ["issue_month", "id"]

    def __str__(self):
        nums = "·".join(str(n) for n in self.numbers.all())
        return f"{self.title.title} {nums}"

    def clean(self):
        if self.issue_month_end and self.issue_month_end < self.issue_month:
            raise ValidationError({"issue_month_end": "截止年月不能早于起始年月。"})


class IssueNumbering(models.Model):
    """Issue ↔ IssueNumber 关联表。

    合刊的两个期号必须是两条独立关联记录，而不是把 3-4 塞进一个条码字段。
    """

    issue = models.ForeignKey(
        Issue, on_delete=models.CASCADE, related_name="numberings",
    )
    number = models.ForeignKey(
        IssueNumber, on_delete=models.CASCADE, related_name="numberings",
    )
    label = models.CharField("封面标识", max_length=40, blank=True,
                             help_text="如 no.3-4，仅作展示")

    class Meta:
        unique_together = ("issue", "number")


class Item(models.Model):
    """馆内实物（册）。一个条码 = 一个实物。"""

    class ItemStatus(models.TextChoices):
        AVAILABLE = "available", "在馆"
        CHECKED_OUT = "checked_out", "借出"
        LOST = "lost", "丢失"
        BOUND = "bound", "已装订"

    barcode = models.CharField("条码", max_length=40, unique=True)
    title = models.ForeignKey(
        Title, on_delete=models.CASCADE, related_name="items",
    )
    issue = models.ForeignKey(
        Issue, on_delete=models.PROTECT, related_name="items",
        help_text="实物对应的发行期；合刊实物只指向这一个 Issue，"
                  "对多个期号的覆盖由 IssueNumbering 表达",
    )
    # 未装订时的实际位置；装订后以 binding 的 location 为准
    location = models.CharField("馆藏位置", max_length=100, blank=True)
    status = models.CharField(
        "馆藏状态", max_length=12,
        choices=ItemStatus.choices, default=ItemStatus.AVAILABLE,
    )
    # 版本号：任何被追踪的修改都会自增，盘点时与快照版本比较以发现并发变更
    version = models.PositiveIntegerField("版本号", default=1)
    accessioned_at = models.DateTimeField(auto_now_add=True)

    # 盘点需要纳入版本比较的字段（装订关系的变化经由 status/装订流程体现，
    # 另外由 Binding.version 与 BindingEntry 存在性直接比较）
    VERSION_TRACKED_FIELDS = ("location", "status", "issue_id")

    class Meta:
        ordering = ["barcode"]

    @property
    def is_bound(self):
        return hasattr(self, "binding_entry")

    def save(self, *args, **kwargs):
        # 已存在实体且被追踪字段参与保存时版本自增；bulk_update/.update() 需自行 +1
        if self.pk is not None:
            update_fields = kwargs.get("update_fields")
            tracked = set(self.VERSION_TRACKED_FIELDS)
            if update_fields is None or (set(update_fields) & tracked):
                self.version = (self.version or 1) + 1
                if update_fields is not None:
                    kwargs["update_fields"] = list(
                        dict.fromkeys(list(update_fields) + ["version"]),
                    )
        super().save(*args, **kwargs)

    def current_location(self):
        """装订后返回装订册位置，否则返回自身位置。"""
        entry = getattr(self, "binding_entry", None)
        if entry is not None:
            return entry.binding.location
        return self.location

    def __str__(self):
        return self.barcode


class Binding(models.Model):
    """装订册：把若干已入藏实物装订在一起，实物身份与条码不变。"""

    call_number = models.CharField("装订索书号", max_length=60, unique=True)
    title = models.ForeignKey(
        Title, on_delete=models.CASCADE, related_name="bindings",
    )
    location = models.CharField("装订后位置", max_length=100)
    bound_month = models.DateField("装订月份", null=True, blank=True)
    # 版本号：位置等被追踪字段修改时自增；拆订会删除装订册，成员实体的变更
    # 另外由 Item.version / BindingEntry 存在性比较发现
    version = models.PositiveIntegerField("版本号", default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    items = models.ManyToManyField(Item, through="BindingEntry", related_name="bindings")

    VERSION_TRACKED_FIELDS = ("location", "call_number", "bound_month")

    class Meta:
        verbose_name = "装订册"
        ordering = ["call_number"]

    def save(self, *args, **kwargs):
        if self.pk is not None:
            update_fields = kwargs.get("update_fields")
            if update_fields is None or (
                set(update_fields) & set(self.VERSION_TRACKED_FIELDS)
            ):
                self.version = (self.version or 1) + 1
                if update_fields is not None:
                    kwargs["update_fields"] = list(
                        dict.fromkeys(list(update_fields) + ["version"]),
                    )
        super().save(*args, **kwargs)

    def __str__(self):
        return self.call_number


class BindingEntry(models.Model):
    item = models.OneToOneField(
        Item, on_delete=models.CASCADE, related_name="binding_entry",
    )
    binding = models.ForeignKey(
        Binding, on_delete=models.CASCADE, related_name="entries",
    )
    # 装订时封存该实物原位置，拆订后恢复
    previous_location = models.CharField("装订前位置", max_length=100, blank=True)
    bound_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("item", "binding")

    def clean(self):
        if self.item_id and self.item.title_id != self.binding.title_id:
            raise ValidationError("装订册内的实物必须属于同一种刊。")


def number_holding_status(title, number):
    """计算某个期号的馆藏视图状态。

    issued+held       已发行且有在馆实物（含装订）
    issued+missing    已发行但缺藏（无实物或全部丢失/借出按调用方再细分）
    not_published     缺号：没有任何发行记录，不自动等同缺藏
    ceased_gap        停刊后出现的编号（永远不会有发行）
    """
    issues = list(number.issues.prefetch_related("items"))
    if not issues:
        ceased = title.ceased_month
        if title.status == Title.PublicationStatus.CEASED and ceased:
            return "ceased_gap"
        return "not_published"
    items = [it for iss in issues for it in iss.items.all()]
    held = any(it.status != Item.ItemStatus.LOST for it in items)
    return "issued+held" if held else "issued+missing"


def locate_number(number):
    """从任一期号找到其所在实物与实际位置（合刊、装订都可命中）。"""
    rows = []
    for issue in number.issues.all():
        for item in issue.items.select_related("title"):
            rows.append({
                "issue_id": issue.id,
                "barcode": item.barcode,
                "status": item.status,
                "location": item.current_location(),
                "bound": item.is_bound,
                "binding": item.binding_entry.binding.call_number if item.is_bound else None,
            })
    return rows


# =========================================================================
# 盘点批次
#
# 盘点期间一切判定都基于“启动盘点时冻结的快照”：
#   实体 / 装订关系 / 实际位置 / 版本号 + 编号槽位的缺号·缺藏视图。
# 扫描只改变快照对象的盘点结论（已见 / 错架 / 待核查），不直接改实体。
# 盘点期间实体被移动、改状态、装订、拆订 → 比较版本与关系快照产生冲突，
# 绝不能用旧扫描覆盖新位置。not_published / ceased_gap 永远不参与缺藏转遗失。
# =========================================================================


class StocktakeBatch(models.Model):
    """一次盘点：冻结一个刊种（可限定编号/库位）范围内的全部对象。"""

    class Status(models.TextChoices):
        OPEN = "open", "进行中"
        CLOSED = "closed", "已关闭"
        REOPENED = "reopened", "已重新打开"

    name = models.CharField("批次名称", max_length=100, blank=True)
    title = models.ForeignKey(
        Title, on_delete=models.PROTECT, related_name="stocktakes",
    )
    status = models.CharField(
        "批次状态", max_length=10,
        choices=Status.choices, default=Status.OPEN,
    )
    # 可选库位过滤：为空表示该刊全部库位；以冻结时实体“实际位置”为准
    scope_location = models.CharField("盘点库位范围", max_length=100, blank=True)
    # 馆员显式声明“范围完整”后才允许关闭
    completeness_confirmed = models.BooleanField("范围完整性已确认", default=False)
    completed_confirmed_at = models.DateTimeField(
        "确认完成时间", null=True, blank=True,
    )
    note = models.CharField("备注", max_length=255, blank=True)
    closed_at = models.DateTimeField("关闭时间", null=True, blank=True)
    created_by = models.CharField("创建人", max_length=60, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "盘点批次"
        ordering = ["-created_at"]
        constraints = [
            # 同一刊种同时只能有一个进行中（open/reopened）的盘点
            models.UniqueConstraint(
                fields=["title"],
                condition=Q(status__in=["open", "reopened"]),
                name="uniq_active_stocktake_per_title",
            ),
        ]

    def __str__(self):
        return self.name or f"盘点批次 #{self.pk}"

    @property
    def is_open(self):
        return self.status in (self.Status.OPEN, self.Status.REOPENED)


class StocktakeScopeNumber(models.Model):
    """批次冻结的编号槽位及其当时的缺号/缺藏视图。

    缺号（not_published / ceased_gap）只做记录，永远不会产生遗失候选。
    盘点期间该槽位被登记了发行期 → 范围发生变化，必须处理后才能关闭。
    """

    batch = models.ForeignKey(
        StocktakeBatch, on_delete=models.CASCADE, related_name="scope_numbers",
    )
    number = models.ForeignKey(
        IssueNumber, on_delete=models.PROTECT, related_name="stocktake_scopes",
    )
    frozen_holding_status = models.CharField("冻结时馆藏视图", max_length=20)

    class Meta:
        unique_together = ("batch", "number")


class StocktakeSnapshotItem(models.Model):
    """快照中的一个实体：冻结其装订关系、实际位置、状态、版本。"""

    class Result(models.TextChoices):
        UNSEEN = "unseen", "未见"
        SEEN = "seen", "已见"
        MISPLACED = "misplaced", "错架"
        REVIEW = "review", "待核查"

    # 关闭批次后的终态去向（只有显式确认才会落到 lost）
    class Outcome(models.TextChoices):
        PENDING = "pending", "待处理"
        LOST_CANDIDATE = "lost_candidate", "遗失候选"
        LOST_CONFIRMED = "lost_confirmed", "已转遗失"
        KEPT = "kept", "保留"

    batch = models.ForeignKey(
        StocktakeBatch, on_delete=models.CASCADE, related_name="snapshot_items",
    )
    item = models.ForeignKey(
        Item, on_delete=models.PROTECT, related_name="snapshot_rows",
    )
    # 冻结时实体字段
    frozen_status = models.CharField("冻结时状态", max_length=12)
    frozen_location = models.CharField("冻结时自身位置", max_length=100, blank=True)
    frozen_item_version = models.PositiveIntegerField("冻结时实体版本")
    # 冻结时装订关系
    frozen_bound = models.BooleanField("冻结时是否已装订", default=False)
    frozen_binding = models.ForeignKey(
        Binding, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="snapshot_rows",
    )
    frozen_binding_call_number = models.CharField(
        "冻结时装订索书号", max_length=60, blank=True,
    )
    frozen_binding_location = models.CharField(
        "冻结时实际位置", max_length=100, blank=True,
    )
    frozen_binding_version = models.PositiveIntegerField(
        "冻结时装订版本", null=True, blank=True,
    )

    result = models.CharField(
        "盘点结论", max_length=12,
        choices=Result.choices, default=Result.UNSEEN,
    )
    # 冲突待核查前，最近一次“干净”的扫描结论，冲突解除后自动恢复
    result_before_review = models.CharField(
        "核查前结论", max_length=12, blank=True, default="",
    )
    observed_location = models.CharField(
        "扫到位置", max_length=100, blank=True,
    )
    baseline = models.CharField(
        "当前对照基线", max_length=12, blank=True, default="",
        help_text="冲突按新状态解决后，基线更新为解决时的结论",
    )
    # 冲突处理后确认的新版本/关系基线：此后比较以它为准，
    # 不再对“馆员已经知晓并接受”的旧变更重复报警
    baseline_item_version = models.PositiveIntegerField(
        "基线实体版本", null=True, blank=True,
    )
    baseline_bound = models.BooleanField(
        "基线是否装订", null=True, blank=True,
    )
    baseline_binding = models.ForeignKey(
        Binding, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="baseline_snapshot_rows",
    )
    baseline_binding_version = models.PositiveIntegerField(
        "基线装订版本", null=True, blank=True,
    )
    baseline_location = models.CharField(
        "基线实际位置", max_length=100, blank=True, default="",
        help_text="冲突按现状处理时确认的位置，错架判断以此为准",
    )
    outcome = models.CharField(
        "关闭后去向", max_length=16,
        choices=Outcome.choices, default=Outcome.PENDING,
    )
    first_seen_at = models.DateTimeField("首次扫到时间", null=True, blank=True)

    class Meta:
        unique_together = ("batch", "item")

    def frozen_actual_location(self):
        return self.frozen_binding_location if self.frozen_bound \
            else self.frozen_location


class StocktakeSnapshotBinding(models.Model):
    """快照中的装订册：扫描装订条码时据此展开为册内全部实体。"""

    batch = models.ForeignKey(
        StocktakeBatch, on_delete=models.CASCADE, related_name="snapshot_bindings",
    )
    binding = models.ForeignKey(
        Binding, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="snapshot_binding_rows",
    )
    call_number = models.CharField("索书号", max_length=60)
    frozen_location = models.CharField("冻结时位置", max_length=100)
    frozen_version = models.PositiveIntegerField("冻结时版本")
    # 成员按条码冻结；拆订后即使 Binding 被删除也能逐实体比较
    frozen_item_barcodes = models.JSONField("冻结时成员条码", default=list)

    class Meta:
        unique_together = ("batch", "call_number")


class StocktakeScan(models.Model):
    """幂等扫描事件。同一批次重复扫同一条码：保留原始事件，后续记为重复。"""

    class ScanKind(models.TextChoices):
        ITEM = "item", "实体条码"
        BINDING = "binding", "装订册条码"

    class EventResult(models.TextChoices):
        RECORDED = "recorded", "已记录"
        DUPLICATE = "duplicate", "重复扫描"
        OUT_OF_SCOPE = "out_of_scope", "范围外"
        UNKNOWN = "unknown", "未知条码"

    batch = models.ForeignKey(
        StocktakeBatch, on_delete=models.CASCADE, related_name="scans",
    )
    barcode = models.CharField("扫描条码", max_length=60, db_index=True)
    scan_kind = models.CharField(
        "条码类型", max_length=8, choices=ScanKind.choices, default=ScanKind.ITEM,
    )
    event_result = models.CharField(
        "事件结果", max_length=14, choices=EventResult.choices,
        default=EventResult.RECORDED,
    )
    item = models.ForeignKey(
        Item, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="scan_events",
    )
    binding = models.ForeignKey(
        Binding, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="scan_events",
    )
    observed_location = models.CharField(
        "扫码时位置", max_length=100, blank=True,
    )
    # 装订册扫描展开出的成员（重复扫描时仍可审计），不代表产生多个实体副本
    resolved_barcodes = models.JSONField("映射到的实体条码", default=list)
    is_duplicate = models.BooleanField("是否重复", default=False)
    duplicate_of = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="repeat_scans",
    )
    scanned_by = models.CharField("扫描人", max_length=60, blank=True)
    scanned_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]
        indexes = [
            models.Index(fields=["batch", "barcode"]),
        ]
        constraints = [
            # 每个批次每个条码只允许一条“原始”事件
            models.UniqueConstraint(
                fields=["batch", "barcode"],
                condition=Q(is_duplicate=False),
                name="uniq_original_scan_per_batch_barcode",
            ),
        ]


class StocktakeConflict(models.Model):
    """快照与现状不一致：盘点期间对象被移动 / 修改 / 装订 / 拆订 / 范围变化。"""

    class Kind(models.TextChoices):
        ITEM_MOVED = "item_moved", "实体被移动或修改"
        BOUND = "bound", "盘点期间被装订"
        UNBOUND = "unbound", "盘点期间被拆订"
        BINDING_MOVED = "binding_moved", "装订册被移动"
        SCOPE_CHANGED = "scope_changed", "范围发生变化"

    class Status(models.TextChoices):
        OPEN = "open", "待处理"
        RESOLVED_SEEN = "resolved_seen", "按现状确认已见"
        RESOLVED_UNSEEN = "resolved_unseen", "按未见重新核查"
        RESOLVED_SCOPE = "resolved_scope", "范围变化已确认"

    batch = models.ForeignKey(
        StocktakeBatch, on_delete=models.CASCADE, related_name="conflicts",
    )
    snapshot_item = models.ForeignKey(
        StocktakeSnapshotItem, on_delete=models.CASCADE,
        null=True, blank=True, related_name="conflicts",
    )
    kind = models.CharField("冲突类型", max_length=16, choices=Kind.choices)
    detail = models.CharField("说明", max_length=255, blank=True)
    snapshot_value = models.CharField("快照值", max_length=255, blank=True)
    current_value = models.CharField("当前值", max_length=255, blank=True)
    status = models.CharField(
        "处理状态", max_length=18,
        choices=Status.choices, default=Status.OPEN,
    )
    detected_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField("处理时间", null=True, blank=True)
    resolved_by = models.CharField("处理人", max_length=60, blank=True)
    resolution_note = models.CharField("处理备注", max_length=255, blank=True)

    class Meta:
        ordering = ["id"]
        constraints = [
            # 同一快照对象同一冲突类型只保留一条未处理冲突，刷新不重复制造
            models.UniqueConstraint(
                fields=["snapshot_item", "kind"],
                condition=Q(status="open"),
                name="uniq_open_conflict_per_row_kind",
            ),
        ]


class StocktakeAuditLog(models.Model):
    """盘点审计流水：批次生命周期、扫描、冲突、关闭与遗失确认全部可追溯。"""

    class Action(models.TextChoices):
        CREATED = "created", "创建批次"
        SCAN = "scan", "扫描"
        CONFLICT = "conflict", "发现冲突"
        CONFLICT_RESOLVED = "conflict_resolved", "处理冲突"
        CONFIRMED_COMPLETE = "confirmed_complete", "确认范围完成"
        CLOSED = "closed", "关闭批次"
        LOSS_CONFIRMED = "loss_confirmed", "确认遗失"
        REOPENED = "reopened", "重新打开"

    batch = models.ForeignKey(
        StocktakeBatch, on_delete=models.CASCADE, related_name="audit_logs",
    )
    action = models.CharField("动作", max_length=20, choices=Action.choices)
    detail = models.CharField("说明", max_length=500, blank=True)
    payload = models.JSONField("附加数据", default=dict, blank=True)
    actor = models.CharField("操作人", max_length=60, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]
