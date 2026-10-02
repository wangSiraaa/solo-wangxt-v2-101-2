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
    # 乐观锁版本：任何位置/状态/装订关系变更都 +1。
    # 盘点用它比对快照：扫描时若当前版本高于快照版本，说明盘点期间数据被改动。
    version = models.PositiveIntegerField("版本", default=1)
    accessioned_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["barcode"]

    @property
    def is_bound(self):
        return hasattr(self, "binding_entry")

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
    created_at = models.DateTimeField(auto_now_add=True)
    # 装订册自身的版本：位置修改/装订/拆订都 +1，供盘点快照比对
    version = models.PositiveIntegerField("版本", default=1)
    items = models.ManyToManyField(Item, through="BindingEntry", related_name="bindings")

    class Meta:
        verbose_name = "装订册"
        ordering = ["call_number"]

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
                "item_id": item.id,
                "issue_id": issue.id,
                "barcode": item.barcode,
                "status": item.status,
                "location": item.current_location(),
                "bound": item.is_bound,
                "binding": item.binding_entry.binding.call_number if item.is_bound else None,
            })
    return rows


# ==========================================================================
# 盘点批次
#
# 盘点只回答「快照范围内、已发行的实物，在不在它应该在的位置」：
#   * 启动时冻结范围内的实物、装订关系、位置与版本快照；
#   * 扫描产生幂等事件（同一实物/装订册只保留首次扫描），结果分为
#     seen / misplaced / review 三类；
#   * 盘点期间实物被移动、拆订或修改 → 版本/关系比对出 conflict，
#     旧扫描不会覆盖新位置，必须先解决冲突；
#   * 关闭批次需要显式确认范围完整性，只有「已发行未见」才进入遗失候选；
#     not_published / ceased_gap（缺号）永远不会因未扫到而变更。
# ==========================================================================


class Stocktake(models.Model):
    """盘点批次：启动时冻结范围快照，扫描期间持续比对版本。"""

    class State(models.TextChoices):
        OPEN = "open", "盘点中"
        CLOSED = "closed", "已关闭"

    # 范围：一个刊种 + 可选卷号；volume 为空表示盘点该刊全部
    title = models.ForeignKey(
        Title, on_delete=models.PROTECT, related_name="stocktakes",
    )
    scope_volume = models.CharField("盘点卷", max_length=20, blank=True)
    name = models.CharField("批次名称", max_length=120, blank=True)
    state = models.CharField(
        "状态", max_length=10, choices=State.choices, default=State.OPEN,
    )
    note = models.CharField("备注", max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    closed_at = models.DateTimeField("关闭时间", null=True, blank=True)

    class Meta:
        verbose_name = "盘点批次"
        ordering = ["-created_at"]

    def __str__(self):
        return self.name or f"盘点#{self.id}"

    @property
    def scope_label(self):
        return f"{self.title.title} v.{self.scope_volume}" if self.scope_volume \
            else self.title.title

    def in_scope_item_ids(self):
        """该批次范围内当前应在的实物 id（始终以实物 title/volume 归属为准）。"""
        qs = Item.objects.filter(title_id=self.title_id)
        if self.scope_volume:
            qs = qs.filter(issue__numberings__number__volume=self.scope_volume)
        return qs.distinct()


class StocktakeItemSnapshot(models.Model):
    """实物快照：冻结盘点启动时的状态、位置、版本与装订关系。"""

    class Result(models.TextChoices):
        PENDING = "pending", "未见"
        SEEN = "seen", "已见"
        MISPLACED = "misplaced", "错架"
        REVIEW = "review", "待核查"
        LOST = "lost", "盘点遗失"

    stocktake = models.ForeignKey(
        Stocktake, on_delete=models.CASCADE, related_name="item_snapshots",
    )
    item = models.ForeignKey(
        Item, on_delete=models.PROTECT, related_name="stocktake_snapshots",
    )
    issue_id = models.PositiveBigIntegerField("发行期 id")
    number_ids = models.JSONField("覆盖期号 ids", default=list)
    status_snapshot = models.CharField("盘点开始时状态", max_length=12)
    # 实物自身位置 + 快照时刻的「实际位置」（装订后取装订册位置）
    location_snapshot = models.CharField("盘点开始时位置", max_length=100, blank=True)
    effective_location_snapshot = models.CharField(
        "盘点开始时实际位置", max_length=100, blank=True,
    )
    binding_id_snapshot = models.PositiveBigIntegerField(
        "盘点开始时装订册 id", null=True, blank=True,
    )
    binding_call_snapshot = models.CharField(
        "盘点开始时装订册索书号", max_length=60, blank=True,
    )
    item_version = models.PositiveIntegerField("实物版本快照")
    binding_version = models.PositiveBigIntegerField(
        "装订册版本快照", null=True, blank=True,
    )
    # 快照时刻该实物覆盖的编号是否已发行；缺号槽位本身不产生实物快照
    published = models.BooleanField("已发行", default=True)
    result = models.CharField(
        "盘点结果", max_length=12,
        choices=Result.choices, default=Result.PENDING,
    )
    # 扫描时馆员所在架位（可空）：与快照实际位置不同 → 错架
    observed_location = models.CharField("扫描架位", max_length=100, blank=True)
    seen_at = models.DateTimeField("首次见到时间", null=True, blank=True)

    class Meta:
        verbose_name = "盘点实物快照"
        unique_together = ("stocktake", "item")

    def __str__(self):
        return f"{self.stocktake_id}:{self.item_id}={self.result}"


class StocktakeBindingSnapshot(models.Model):
    """装订关系快照：冻结「装订册 ↔ 册内实物」成员关系，用于发现拆订/重组。"""

    stocktake = models.ForeignKey(
        Stocktake, on_delete=models.CASCADE, related_name="binding_snapshots",
    )
    binding = models.ForeignKey(
        Binding, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="stocktake_snapshots",
        help_text="装订册可能在盘点期间被拆订删除，故仅置空快照行",
    )
    # 冗余存储，装订册被删除后仍可追溯
    binding_call = models.CharField("装订册索书号", max_length=60)
    location_snapshot = models.CharField("装订位置快照", max_length=100)
    binding_version = models.PositiveIntegerField("装订册版本快照")
    item_ids = models.JSONField("册内实物 ids", default=list)

    class Meta:
        verbose_name = "盘点装订快照"
        unique_together = ("stocktake", "binding")


class StocktakeScan(models.Model):
    """扫描事件（审计流）。

    幂等性以「实物首次见到」为准并在服务层保证：同一实物无论从自身条码
    还是从装订册条码扫到，只记首次见到；重复扫描不改变任何快照结果。
    这里不设 (批次, 目标) 唯一约束——先扫册内条码再扫装订册是两次不同的
    扫描动作，装订册事件仍需落账（其 resolved_item_ids 含全册映射）。
    """

    class Target(models.TextChoices):
        ITEM = "item", "实体条码"
        BINDING = "binding", "装订册条码"

    stocktake = models.ForeignKey(
        Stocktake, on_delete=models.CASCADE, related_name="scans",
    )
    target_type = models.CharField("扫描目标类型", max_length=10, choices=Target.choices)
    # target 被删除（拆订删册）后仍可追溯
    target_id = models.PositiveBigIntegerField("目标 id", null=True, blank=True)
    barcode = models.CharField("扫到的条码", max_length=60)
    observed_location = models.CharField("扫描架位", max_length=100, blank=True)
    # 本次扫描映射到的全部实物（装订册扫描时为册内成员），用于审计
    resolved_item_ids = models.JSONField("映射实物 ids", default=list)
    # 本次首次见到的实物（已被早前事件覆盖的不计入），重复扫描时为空
    first_seen_item_ids = models.JSONField("首次见到实物 ids", default=list)
    duplicate = models.BooleanField("是否重复扫描", default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "盘点扫描事件"
        ordering = ["created_at", "id"]


class StocktakeConflict(models.Model):
    """盘点冲突：快照与当前数据不一致（移动/拆订/重组/修改），待人工处理。"""

    class Kind(models.TextChoices):
        ITEM_CHANGED = "item_changed", "实物被修改（位置/状态）"
        MOVED_IN = "moved_in", "实物新进入范围"
        MOVED_OUT = "moved_out", "实物离开范围"
        UNBOUND = "unbound", "盘点期间被拆订"
        BOUND = "bound", "盘点期间被装订"
        REBOUND = "rebound", "装订成员重组"

    class Status(models.TextChoices):
        OPEN = "open", "待处理"
        RESOLVED = "resolved", "已解决"

    stocktake = models.ForeignKey(
        Stocktake, on_delete=models.CASCADE, related_name="conflicts",
    )
    item = models.ForeignKey(
        Item, on_delete=models.CASCADE, null=True, blank=True,
        related_name="stocktake_conflicts",
    )
    binding = models.ForeignKey(
        Binding, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="stocktake_conflicts",
    )
    kind = models.CharField("冲突类型", max_length=14, choices=Kind.choices)
    detail = models.CharField("说明", max_length=255, blank=True)
    snapshot_value = models.CharField("快照值", max_length=255, blank=True)
    current_value = models.CharField("当前值", max_length=255, blank=True)
    status = models.CharField(
        "处理状态", max_length=10,
        choices=Status.choices, default=Status.OPEN,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField("解决时间", null=True, blank=True)

    class Meta:
        verbose_name = "盘点冲突"
        ordering = ["id"]
        constraints = [
            # 同一批次同一实物同一未解决类型只保留一条，重复检测不产生重复冲突
            models.UniqueConstraint(
                fields=["stocktake", "item", "kind"],
                condition=models.Q(status="open"),
                name="uq_open_conflict_item",
            ),
            # 装订册级冲突（item 为空）按册去重；item IS NULL 用本地外键列表达
            models.UniqueConstraint(
                fields=["stocktake", "binding", "kind"],
                condition=models.Q(status="open", item=None),
                name="uq_open_conflict_binding",
            ),
        ]
