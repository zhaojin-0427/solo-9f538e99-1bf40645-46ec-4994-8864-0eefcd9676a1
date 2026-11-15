# 点击事件合同登记 / 换版服务

埋点数据工程师在更换「点击事件」合同前，用本服务确认**数仓清洗器仍能接收全部
合法生产事件**。技术栈：FastAPI + SQLite，单容器即可运行。

## 1. 合同形式（JSON Schema 2020-12 平面对象子集）

合同只允许以下收窄形式，保证兼容性可以**精确判定**，而不是抽样验证：

- 顶层仅一个 `anyOf`，含 **1～4 支**；
- 每支只允许 `properties` / `required` / `additionalProperties`，且
  `additionalProperties` 必须显式为 `false`（**禁止额外字段**）；
- 字段 schema 只允许 `type` / `enum` / `minimum` / `maximum`；
- `type` 仅 `string` / `integer` / `null`（单值或数组，数组不重复）；
- `enum` 值类型必须与 `type` 相容；`minimum`/`maximum` 仅用于 `integer`；
- 所有分支 `properties` 键名的并集（参与判定的字段名）**至多 6 个**。

不符合子集的候选在结构校验阶段即被拒绝（HTTP 400，附全部原因）。

## 2. 兼容性判定（完整判定，非样例通过）

判定命题：**生产合同允许的每一个事件对象，都必须被所有清洗器合同接受。**

实现（`app/compat.py`）：

1. 对生产合同每个分支，枚举可选字段的全部出现组合（至多 2⁶=64 个键集合），
   每个键集合确定一个「生产者盒子」——各字段取值域的笛卡尔积；
2. 选出能接受该键集合的清洗器分支（其 `required` 被覆盖、且对象键都在其
   `properties` 中声明；清洗器额外声明的可选字段不影响接受）；
3. 字段取值域建模为 `null` / 整数闭区间 / 字符串集合（有限集、全集合、补集）
   的互不相交片段（`app/domains.py`），并实现完整的交集、差集、包含代数；
4. 盒子覆盖问题用**递归拆分**求解：在清洗器片段的整数边界点和字符串枚举点上
   拆分生产者域，使每个叶子片段对每个清洗器片段要么整体包含、要么完全不相交，
   叶子做整体包含判断。所有拆分点都来自清洗器域中有限的边界，递归必然终止；
5. 一旦发现未覆盖叶子，构造一条具体的**反例事件**返回，并在服务层自检该事件
   确实「生产者合法、清洗器拒收」，防止引擎静默出错。

不兼容时：返回一条合法生产事件 + 拒收它的清洗器 ID，**候选不启用，原发布保留**，
同时写入 `change_attempts` 审计表，可在 `/docs` 通过 `/api/change-attempts` 查看。

## 3. 修订与并发

- 每次成功换版：候选启用，该端 `revision` 递增（首版为 1）；
- 写操作在单事务内 `BEGIN IMMEDIATE` 串行化（SQLite WAL）；
- 提交时在写锁内重新读取当前修订：
  - 带 `expected_revision` 且与当前不符 → **409 冲突**，原发布保留；
  - 不带 → 按提交时的当前合同**重新判定**（满足「按当前合同重判或报冲突」）；
- 任何失败（结构非法 / 不兼容 / 冲突）都保留原发布，并有审计记录。
- 容量：生产端至多 1 个，清洗器至多 3 个。

## 4. 快速启动（Docker Compose）

```bash
docker compose up --build
# 服务监听 http://localhost:8000 ，Swagger 文档 http://localhost:8000/docs
```

数据持久化在命名卷 `contract-data`（容器内 `/data/contracts.db`）。
健康检查：`GET /api/health`。

首次启动自动播种：1 个生产端 `producer-main`、2 个清洗器 `cleaner-a` /
`cleaner-b`，并尝试一次不兼容的生产端换版，因此打开 `/docs` 即可看到带反例的
拒绝记录。

本地（无 Docker）运行：

```bash
pip install -r requirements.txt
CONTRACT_DB_PATH=./data/contracts.db uvicorn app.main:app --port 8000
```

## 5. 主要 API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/endpoints` | 登记端（producer/cleaner），可带首版合同 |
| GET | `/api/endpoints` | 列出全部端及当前修订 |
| GET | `/api/endpoints/{id}` | 端详情 + 当前合同 |
| GET | `/api/endpoints/{id}/revisions[/{rev}]` | 修订历史 / 指定修订 |
| PUT | `/api/endpoints/{id}/contract` | **换版**（候选 + 可选 expected_revision） |
| POST | `/api/endpoints/{id}/compatibility-check` | 无副作用试判定，返回反例 |
| POST | `/api/endpoints/{id}/validate-event` | 校验一条事件是否被当前合同接受 |
| POST | `/api/schema/validate` | 仅校验候选是否符合合同子集 |
| GET | `/api/change-attempts` | 换版审计（含每次拒绝的反例与拒收清洗器） |

换版响应：成功 `result=activated` 且 `new_revision` 递增；不兼容
`result=rejected_incompatible`，附 `counterexample` 与 `rejected_cleaner_id`；
冲突 HTTP 409；结构非法 HTTP 400。

## 6. 合同样例

`examples/` 下提供可直接粘进 `/docs` 的 JSON：

- `producer_v1.json` / `cleaner_a_v1.json` / `cleaner_b_v1.json`：初始合同；
- `producer_v2_compatible.json`：收紧坐标上限，仍被全部清洗器接受（可成功换版）；
- `producer_v2_incompatible.json`：`dwell_ms` 上限扩到 90000，超出 cleaner-a 的
  60000，换版被拒并给出 `dwell_ms>60000` 的反例；
- `cleaner_v2_incompatible.json`：清洗器把 `dwell_ms` 收到 10000，无法接受
  生产端 10001~60000 的事件，换版被拒。

## 7. 自测

```bash
pip install -r requirements.txt
pip install pytest httpx jsonschema
python -m pytest
```

测试构成：

- 取值域代数（含无界整数区间、字符串补集的交/差/包含）；
- 合同子集结构校验、事件实例校验；
- 兼容性引擎手工用例（枚举收紧、区间收紧、必填收紧、额外字段、多分支联合覆盖、
  null 类型、可选字段键集合枚举、不可满足分支检测）；
- **620 组随机合同对的差分测试**：引擎判定结果与有限值空间暴力枚举逐一比对，
  并用 `jsonschema` 库交叉确认合同与反例；
- 端到端 API（登记、换版、反例、乐观锁、容量上限、修订历史、审计）；
- 多线程并发换版（带/不带 expected_revision 的冲突与重判语义）。
