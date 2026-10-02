# 连续出版物登记应用（SerialReg）

管理连续出版物的「编号覆盖」与「实体位置」两层关系。典型场景：**一期合刊覆盖两个期号，
同时又是馆内不同的物理实体**——系统用四层模型同时表达，不允许用一个条码覆盖多个期号关系。

## 核心领域模型（PostgreSQL）

| 层 | 模型 | 含义 |
|---|---|---|
| 书目 | `Title` | 刊名、ISSN、出版状态；停刊需填**停刊月份** |
| 编号 | `IssueNumber` | 卷期编号槽位（卷+期唯一），**不含年月**，跨年卷只有一个编号 |
| 发行 | `Issue` + `IssueNumbering` | 一次出版行为。普通期关联 1 个编号，**两期合刊关联 ≥2 个独立编号记录** |
| 实体 | `Item` | 一条条码 = 一个实物，指向一个 `Issue`，多期号覆盖由关联表表达 |
| 装订 | `Binding` + `BindingEntry` | 多个实物订成一册，记录装订后位置并封存各实物原位置 |

关键业务规则：

- **发行年月与卷期编号分开录入**：跨年卷（如 v.60 no.3 印 2023-12～2024-01）
  编号仍是一条，发行覆盖区间记在 `Issue.issue_month / issue_month_end`。
- **缺号 ≠ 缺藏**：
  - `not_published`（在刊但无发行记录）/ `ceased_gap`（停刊月之后）= **缺号**，
    只表示没有发行，不自动判定缺藏；
  - `issued+missing` = **缺藏**：已发行但没有可用实物（未入藏或全部丢失）。
- **合刊**：一个实物 + 两条（或更多）`IssueNumbering`。从 no.3 或 no.4 都能定位到同一实物。
- **装订**：实物条码与多期号关系不变，实际位置改指向装订册；
  **拆订**后按封存的 `previous_location` 恢复各自位置，合刊编号关系依旧完整。
- 禁止跨刊混装、重复装订；`status=bound` 只能由装订/拆订流程设置。

## 盘点批次（年度盘点）

盘点时馆员用条码核对库位的**预期馆藏**，缺号（未发行/停刊后缺号）绝不能因没扫到而判遗失：

- **启动即冻结快照**：批次范围内的实体字段、装订关系、实际位置与**版本号**
  （`Item.version` / `Binding.version`）、以及编号槽位当时的缺号/缺藏视图一并固化。
- **幂等扫描**：扫实体条码确认该实体；扫**装订册索书号**按快照展开册内全部实体
  （合刊、装订都只映射到既有实体，不制造副本）。同批次重复扫同一条码只保留一条
  原始事件，后续记 `duplicate`，结论与首次扫码位置不被覆盖。
- **盘点结论**：快照对象标为 `seen / unseen / misplaced（错架） / review（待核查）`。
- **并发冲突**：盘点期间实体被移动、报失、改期、装订、拆订，或装订册移库，
  系统比较快照版本与装订关系生成待处理冲突（`item_moved / bound / unbound /
  binding_moved / scope_changed`），**绝不用旧扫描覆盖新位置**；馆员可
  「按现状确认已见」或「按未见重新核查」，处理后以现状为新基线。
- **关闭门槛**：必须先显式确认**范围完整性**且无待处理冲突；关闭前服务端还会
  强制再比较一次版本（忘刷新也不会误判）。关闭后只有**已发行未见**实体进入
  「遗失候选」，再经显式确认才真正转 `lost`；`not_published` 与 `ceased_gap`
  始终不参与遗失判定。批次可重新打开，快照、扫描事件、冲突与审计流水全部保留。


## 技术栈

- 后端：Django 5 + Django REST Framework（`backend/`）
- 数据库：PostgreSQL 16（本地无 PG 时可用 `SERIALREG_DB=sqlite` 跑开发/测试）
- 前端：Vue 3 + Vite（`frontend/`），馆员时间轴界面

## 快速启动

### Docker Compose（推荐，含 PostgreSQL 与样例数据）

```bash
cd serialreg
docker compose up --build
# 前端 http://localhost:5173   后端 http://localhost:8000/api/
```

后端容器启动时自动 `migrate` 并执行 `seed_sample`（跨年卷 / 停刊 / 两期合刊 + 装订样例）。

### 本地分别启动

```bash
# 后端
cd backend
pip install -r requirements.txt
SERIALREG_DB=postgres PGHOST=127.0.0.1 python manage.py migrate
SERIALREG_DB=postgres python manage.py seed_sample
SERIALREG_DB=postgres python manage.py runserver

# 前端
cd frontend
npm install && npm run dev     # http://localhost:5173 ，/api 代理到 8000
```

## 验证

```bash
# PostgreSQL
SERIALREG_DB=postgres pytest -q
# 无 PG 环境（SQLite，ORM 通用）
SERIALREG_DB=sqlite pytest -q
```

29 条测试覆盖：跨年卷单编号跨两年、停刊必须填月份、缺号(`ceased_gap`/`not_published`)
不等于缺藏(`issued+missing`)、合刊保留两条编号关联、任一期号可定位、条码反查得到两个期号、
装订后从 no.3/no.4/no.5 均指向装订册、禁止跨刊混装与重复装订、拆订恢复原位置且关系完好、
合刊实物在两个槽位下重复提交装订时自动去重；以及 14 条盘点验收：扫合刊/普通期后结果与期号
定位和实际位置一致、重复扫描只计一次且保留原始事件、扫装订册正确展开册内实体不造副本、
拆订/移库/新装订并发时产生待核查冲突且不误判丢失、范围未确认或有冲突时关闭被阻止、
确认后仅已发行未见进入遗失候选、缺号永不判遗失、刷新与重新打开后状态可追溯。

手工端到端（样例数据）：

```bash
# 装订态下从合刊任一期号定位 → 装订库
curl "/api/items/locate/?title=3&volume=8&number=4"
# 拆订 → 恢复「现刊区 B-02」
curl -X POST /api/bindings/unbind/ -H "Content-Type: application/json" -d '{"binding_id":1}'
```

## 主要 API

| 方法/路径 | 说明 |
|---|---|
| `GET/POST /api/titles/` | 刊名；停刊须带 `ceased_month` |
| `GET/POST /api/numbers/` | 卷期编号槽位 |
| `GET/POST /api/issues/` | 发行期；`kind=combined` 时 `number_ids` 至少 2 个 |
| `GET/POST /api/items/` | 入藏实物（条码+发行期+位置） |
| `GET /api/items/locate/?title=&volume=&number=` | 按期号定位实物/位置（缺号返回空匹配+状态） |
| `GET /api/items/locate/?barcode=` | 按条码反查（含合刊覆盖的全部期号） |
| `GET/POST /api/bindings/` | 装订（同刊、未装订实物） |
| `POST /api/bindings/unbind/` | 拆订，恢复各自位置 |
| `GET /api/timeline/?title=` | 时间轴：编号槽位×发行×实物×停刊标记×进行中盘点标记 |
| `POST /api/stocktakes/` | 启动盘点（冻结实体/装订/位置/版本快照，可带 `scope_location`、`number_ids`） |
| `GET /api/stocktakes/{id}/` | 批次详情：快照行、装订册、冲突、统计、审计流水 |
| `POST /api/stocktakes/{id}/scan/` | 扫实体条码或装订册条码（幂等，重复只计一次） |
| `POST /api/stocktakes/{id}/refresh/` | 重新比较快照版本，发现移动/拆订/装订冲突 |
| `POST /api/stocktakes/{id}/resolve_conflict/` | 处理冲突：按现状确认已见 / 按未见重新核查 / 确认范围变化 |
| `POST /api/stocktakes/{id}/confirm_complete/` | 显式确认范围完整性 |
| `POST /api/stocktakes/{id}/close/` | 关闭（不完整/有冲突返回 409；已发行未见进遗失候选） |
| `POST /api/stocktakes/{id}/confirm_losses/` | 仅对遗失候选显式确认转丢失 |
| `POST /api/stocktakes/{id}/reopen/` | 恢复盘点，历史状态保留可追溯 |

`/api/items/locate/` 与 `/api/timeline/` 在有进行中批次时返回每个实体的
`stocktake` 盘点标记（结论 + 冻结位置），前端时间轴/定位页同步显示。

## 界面

- 左侧刊种列表（含停刊月份徽标）与新增刊种；
- **时间轴**：每个卷期一个节点，区分「已入藏 / 缺藏 / 缺号 / 停刊后缺号」，
  展开显示发行年月区间、合刊徽标、实物条码与实际位置（装订后显示装订册）；
- 定位栏：按期号（合刊任一期号）或条码检索；进行中盘点时显示盘点结论徽标；
- 登记操作：编号槽位 → 发行期（普通/合刊，年月与编号分录）→ 入藏；
- 装订面板：勾选同刊未装订实物建装订册，一键拆订并显示各实物原位置；
- **年度盘点面板**：启动/恢复盘点、扫实体条码或装订册索书号（重复只计一次）、
  错架与待核查高亮、一键刷新比较版本、逐条处理冲突、确认范围完整后关闭、
  仅对遗失候选确认转丢失，并附完整审计流水；时间轴顶部显示进行中盘点横幅。
