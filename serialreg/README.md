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
- **盘点批次**：启动时冻结范围内实物 / 装订关系 / 位置 / 版本快照；
  扫描实体条码或装订册索书号产生**幂等扫描事件**（同一实物只记首次见到，
  扫装订册正确映射册内全部实体、不制造副本），结果分为已见 / 错架 / 待核查。
  盘点期间若有人移动、拆订或修改实体，系统比对快照版本与装订关系并产生
  **待处理冲突**，旧扫描不会覆盖新位置。关闭批次前必须确认范围完整性，
  只有经馆员**逐件确认遗失**的已发行实体才会转为丢失；
  `not_published` 与 `ceased_gap`（缺号）没有实物快照，永远不会因未扫到而变更。

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

28 条测试覆盖：跨年卷单编号跨两年、停刊必须填月份、缺号(`ceased_gap`/`not_published`)
不等于缺藏(`issued+missing`)、合刊保留两条编号关联、任一期号可定位、条码反查得到两个期号、
装订后从 no.3/no.4/no.5 均指向装订册、禁止跨刊混装与重复装订、拆订恢复原位置且关系完好、
合刊实物在两个槽位下重复提交装订时自动去重；
盘点：扫合刊+普通期与期号定位/实际位置一致、重复扫描只计一次并保留原始事件、
扫装订册确认册内全部实体、盘点期间拆订/移动产生待处理冲突而不误判丢失、
范围未完成阻止关闭、只有确认遗失的已发行实体转丢失、缺号永不因未扫到变更、
刷新与重新打开批次后状态仍可追溯。

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
| `GET/POST /api/stocktakes/` | 盘点批次列表 / 启动盘点（冻结快照） |
| `GET /api/stocktakes/{id}/` | 批次详情：快照结果、冲突、扫描审计、范围完整性 |
| `POST /api/stocktakes/{id}/scan/` | 扫描条码/装订册（幂等；装订册映射全部成员） |
| `POST /api/stocktakes/{id}/review/` | 复核：错架/待核查→已见，未见→确认遗失 |
| `POST /api/stocktakes/{id}/conflicts/{cid}/resolve/` | 按当前数据解决冲突 |
| `POST /api/stocktakes/{id}/close/` | 关闭：`confirm=false` 返回完整性报告，`true` 才落账 |
| `POST /api/stocktakes/{id}/reopen/` | 重新打开（撤销关闭时判失，记录保留可追溯） |
| `GET /api/timeline/?title=` | 时间轴：编号槽位×发行×实物×停刊标记（带 `stocktake=` 时附盘点标记） |

## 界面

- 左侧刊种列表（含停刊月份徽标）与新增刊种；
- **时间轴**：每个卷期一个节点，区分「已入藏 / 缺藏 / 缺号 / 停刊后缺号」，
  展开显示发行年月区间、合刊徽标、实物条码与实际位置（装订后显示装订册）；
- 定位栏：按期号（合刊任一期号）或条码检索；
- 登记操作：编号槽位 → 发行期（普通/合刊，年月与编号分录）→ 入藏；
- 装订面板：勾选同刊未装订实物建装订册，一键拆订并显示各实物原位置。
- **盘点面板**：按卷（或全刊）启动盘点并冻结快照；条码/装订册扫描框、
  已见/错架/待核查/未见统计、待处理冲突清单（移动/拆订/修改实体触发）、
  未见实体「确认遗失」按钮、范围完整性检查与确认关闭、关闭后一键恢复盘点；
  时间轴与定位页同步显示「盘点·已见/错架/待核查」徽标与冲突标记。
