# 服装社布料流转裁决 API

纯后端服务：服装社的布料经过**转交、撤回与改版**，系统按既有规则裁决每一个动作的
**补料责任成员**与**需取得许可的下一动作**。面向 13–16 岁女生成员场景建模，
但裁决本身完全由提交的规则与事件驱动。

* 技术栈：Python 3.12 · FastAPI · SQLite（WAL）· pytest
* 数据模型：**事件仅追加（append-only）**，状态、授权、裁决全部由确定性重放派生
* 交付：Docker Compose，常驻服务只有 `api`；`verify` 跑完 pytest 与 HTTP 探测即退出

---

## 1. 领域语义

### 1.1 布料与成员

* 布料有两个角色：**主人 `owner_id`** 与当前**持有人 `holder_id`**；登记布料时
  持有人即主人。
* 转交更换持有人；返还把持有人恢复为主人；主人身份不随转交变化。
* 成员需先登记，才能成为主人、持有人或被授权人。

### 1.2 事件类型

| 类型 | 载荷要点 | 是否裁决 |
|---|---|---|
| `member_define` | `member_id`、`age`、`gender` | 否（setup） |
| `fabric_define` | `fabric_id`、`owner_id` | 否（setup） |
| `rule_define` | `rule_id`、`priority`、`when`、`effects` | 否（setup） |
| `grant` | `fabric_id`、`grantee_id`、`action_id` | 是 |
| `cut` | `fabric_id` | 是 |
| `transfer` | `fabric_id`、`to_member_id` | 是 |
| `return` | `fabric_id` | 是 |
| `withdraw` | `fabric_id`、`grant_seq` | 是 |
| `correct` | `target_seq`、`new_type`、`new_payload` | 否（重放替代） |
| `appeal` | `target_seq`、`fabric_id` | 是 |

每个动作类事件都先按**此前投影**裁决，裁决通过后再更新状态。

### 1.3 授权、转交、返还、撤回

* **授权（grant）**：仅布料主人可授予；授权三元组为
  `(布料, 被授权成员, 动作 id)`。动作 id 是自由字符串，与事件类型同名
  （如 `cut` / `transfer` / `return` / `withdraw` / `grant` / `appeal`）。
* **转交（transfer）**：更换持有人。
* **返还（return）**：持有人恢复为主人。
* **撤回（withdraw）**：必须引用一条此前存在、属于同一布料且尚未撤回的授权；
  该授权**自撤回事件序号起失效**（失效点即撤回序号，历史时点仍可见其有效）。

规则条件中的 `authorized` 在裁决时取值：操作者当前对“本事件动作”是否持有有效授权。

### 1.4 规则与裁决

一条规则包含：

* `rule_id`：同一 id 的多次 `rule_define` 形成**版本**，唯一生效版本为
  **不晚于当前事件序号的最近版本**；
* `priority`：**数值小者优先**；
* `when`：条件**合取**，空集合 / `null` 表示该项恒真——
  * `event_types`：命中事件类型，空表示任意类型；
  * `operator_identities`：操作者相对布料的身份，可多重命中
    （`owner`、`holder`、`other`；主人转交前同时是 owner 与 holder），空表示任意身份；
  * `authorized`：`true` / `false` 要求有效授权有无，`null` 不检查；
* `effects`：两列——`responsible_member_ids`（补料责任成员）与
  `next_action_ids`（下一动作/许可）。

裁决算法（`app/adjudication.py`）：

1. fold 到事件之前的投影里，规则表天然只含更早的版本——
   **事件只取不晚于自身的最近规则，后版不得重算旧事件**；
2. 若裁决时刻不存在任何规则版本，事件被拒绝（`no_applicable_rule`，422）；
3. 逐条规则给出命中与否及三个子句结果，构成**逐条依据 `basis`**；
4. `priority` 小者优先；**同级时命中规则的两列 effects 各求并集并排序**；
5. 无命中：事件照常成立，责任与下一动作为空（`winning_rule_ids` 为空）。

### 1.5 更正（correct）

* 仅限**目标事件的原提交者**发起（否则 403）；
* 携带**完整替代载荷** `new_payload` 与替代类型 `new_type`（不能更正成另一条更正）；
* **自更正事件所在序号起**，用替代事件替换目标重放——更早日号的时点查询
  仍是原事件（目标行标 `correction_pending`）；
* 更早日号发生的中间旧事件**裁决保持冻结**（替代/后版都不重算它们）；自更正序号
  起，投影按“目标当初就是替代载荷”重建，其后事件继续折叠；
* 若替代会令后续引用无效（后续 `withdraw` 引用的授权序号不再是 grant、
  后续 `appeal` 引用不到裁决等），**整次更正拒绝（409）**，事件流不受污染。

查询形态：被更正的目标事件保留原序号、原载荷与原裁决，当前视图标记
`superseded: true`；替代后的形态与其重放裁决挂在**更正事件自己的序号**上
（`effective_event`、`verdict`）。

### 1.6 申诉（appeal）

* 引用一条此前存在的裁决序号（`target_seq`）；
* 依**申诉本序号生效的规则**裁决，产出的责任/动作作为**补救（remedy）追加**到
  目标裁决的 `appeal_remedies`；
* **旧裁决内容永不改变**；申诉之前的时点查询看不到该补救。

### 1.7 追加的原子性与幂等

入口要求请求体给出 `expected_next_seq`，并支持 `Idempotency-Key` 头：

* `expected_next_seq` 与库内下一序号不符 → 409（乐观并发，输家重取序号再试）；
* 同键 + 同载荷 → 复用既有事件，返回同一序号，`reused: true`；
* 同键 + 异载荷 → 409（`idempotency_conflict`）；
* 幂等指纹为 `{type, submitted_by, payload}` 的规范化 JSON SHA-256
  （不含 `expected_next_seq`）。

追加在单个 `BEGIN IMMEDIATE` 事务内完成（WAL + busy_timeout），事件行与幂等键
同提交。

---

## 2. HTTP API

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/health` | 健康检查与当前下一序号 |
| `POST` | `/api/events` | 原子追加事件（`Idempotency-Key` 可选） |
| `GET` | `/api/events` | 历史：全部事件；`?as_of=N` 时点视图 |
| `GET` | `/api/events/{seq}` | 单序号：责任、下一动作、逐条依据、申诉补救 |
| `GET` | `/api/state` | 当前/时点投影：成员、布料主持有人、规则版本、授权、裁决 |

### 请求体

```json
{
  "type": "cut",
  "submitted_by": "alice",
  "payload": { "fabric_id": "cloth" },
  "expected_next_seq": 5
}
```

### 裁决书结构（节选）

```json
{
  "seq": 7,
  "event_type": "cut",
  "fabric_id": "cloth",
  "actor": "alice",
  "actor_identities": ["holder", "owner"],
  "authorized": false,
  "responsible_member_ids": ["club"],
  "next_action_ids": ["record"],
  "winning_rule_ids": ["ALL"],
  "priority": 10,
  "basis": [
    {
      "rule_id": "ALL",
      "version_seq": 4,
      "priority": 10,
      "matched": true,
      "clause": { "event_type": true, "operator_identity": true, "authorized": true }
    }
  ],
  "appeal_remedies": []
}
```

错误统一为 `4xx + {"detail": {"code": ..., "msg": ...}}`（载荷结构错误为
 FastAPI 原生 422 详情数组）。

---

## 3. 本地运行（不用 Docker）

需要 Python 3.12（Docker 内固定 3.12）：

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

APP_DB_PATH=data/events.db uvicorn app.main:app --reload --port 8000
pytest -q
```

一段最小体验（先登记成员、布料，再登记规则，之后动作才不会因“无适用版本”被拒绝）：

```bash
curl -s localhost:8000/health
curl -s -X POST localhost:8000/api/events -H 'Content-Type: application/json' -d '{
  "type":"rule_define","submitted_by":"club","expected_next_seq":1,
  "payload":{"rule_id":"R","priority":10,
    "when":{"event_types":["cut"],"operator_identities":[],"authorized":null},
    "effects":{"responsible_member_ids":["alice"],"next_action_ids":["pay"]}}}'
```

---

## 4. Docker Compose 交付

```bash
# 指定宿主端口（默认 8000）
API_PORT=8080 docker compose up --build
```

* **常驻的只有 `api`**：`uvicorn` 监听容器 8000，映射到 `${API_PORT:-8000}`；
  SQLite 数据保存在命名卷 `api-data`（`/data/events.db`）。
* `verify` 依赖 `api` 健康后启动一次：
  1. 等待并探测 HTTP `/health`；
  2. 对 api 容器做端到端冒烟（登记 → 布料 → 规则 → 剪裁裁决 → 幂等复用 → 查询）；
  3. 在镜像内运行完整 `pytest`；
  4. 打印结果后**退出**（`restart: "no"`），api 继续常驻。

只看验证结果并在 verify 退出后整体停止：

```bash
docker compose up --build --abort-on-container-exit verify --exit-code-from verify
```

清理（含数据卷）：

```bash
docker compose down -v
```

---

## 5. 测试覆盖

`pytest -q` 共 23 个用例：

| 文件 | 覆盖点 |
|---|---|
| `service_tests/test_versions.py` | 无规则拒绝；改版只影响未来事件、旧裁决稳定；无命中为空 |
| `service_tests/test_priority_tie.py` | 同级两列求并集排序；小 priority 遮蔽；身份/授权条件合取 |
| `service_tests/test_correction.py` | 原提交者限制、完整载荷、自更正序号重放、类型替换、后续撤回/申诉失效整次拒绝 |
| `service_tests/test_appeal.py` | 依本序号规则追加补救、旧裁决不变、时点视图、必须引用裁决 |
| `service_tests/test_concurrency.py` | 同键同载荷复用、同键异载荷冲突、过期序号冲突、多线程并行追加 |
| `service_tests/test_fabric_lifecycle.py` | 转交换持有人、返还恢复主人、授权/撤回生效时点、重复撤回拒绝 |
| `service_tests/test_edge.py` | 未知类型/坏载荷 422；更正规则版本的前向生效与不重算旧事件 |

---

## 6. 代码结构（四模块）

```
app/
  models.py        # 事件信封与 10 类载荷、规则 when/effects 模型
  adjudication.py  # 规则裁决：版本可见性、合取条件、priority 并集、逐条依据
  replay.py        # 确定性重放/状态投影；更正物化与试重放；申诉补救；查询视图
  database.py      # SQLite append-only 存储；IMMEDIATE 事务 + 幂等 + 序号竞争
  main.py          # FastAPI：追加、历史查询、状态、健康检查
scripts/verify.py  # compose verify：HTTP 探测 + 冒烟 + pytest，结束即退出
service_tests/    # pytest 套件
```

核心不变量：**给定同一条事件流，重放永远得到同一份状态与同一批裁决**；
所有读接口都不读缓存，只折叠事件流。
