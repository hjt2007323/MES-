# 停机录入沙箱与幂等事务（M5 / B-010）

`mes_assistant/intake_store.py` 是 M5 的独立 SQLite 沙箱：校验五字段停机记录，按提交键
幂等写入，并按显式时间窗查回。它不接触原 MySQL、不联网、不调用模型，只使用标准库
SQLite 与固定参数化 SQL。

- 沙箱标识：`intake-sandbox-v1`
- 默认库：`runtime/a07-intake/downtime.sqlite3`（工程根相对；`DEFAULT_DB_PATH`）
- 测试库：本卡隔离的临时文件；不写原 MySQL 运行目录

## 公共 API

```python
from mes_assistant.intake_store import DowntimeStore

store = DowntimeStore()  # 固定生产路径：runtime/a07-intake/downtime.sqlite3

record = store.validate_record({
    "machine_no": "M01",
    "start_time": "2026-11-04T08:00:00+08:00",
    "end_time": "2026-11-04T09:00:00+08:00",
    "reason": "换型",
    "order_id": "WO01",
})

receipt = store.commit(record, submission_key="draft-uuid")
# {"record_id": 1, "replayed": False, "record": {...规范化记录...}}

table = store.list_window("2026-11-04T00:00:00+08:00",
                          "2026-11-05T00:00:00+08:00")
# {"columns": [...], "rows": [[...]], "truncated": False, "truncation_reason": None}
```

测试/联调只能显式注入可信测试根：

```python
store = DowntimeStore(test_db_path, test_root=trusted_test_root)
```

构造签名：`DowntimeStore(path=None, *, test_root=None)`。

## 路径隔离

- **生产入口**（A 页面/CLI）：`DowntimeStore()`，路径固定为工作区
  `runtime/a07-intake/downtime.sqlite3`。边界以工作区运行根的**原始规范路径**为准，
  不以 junction/符号链接解析后的目标作为允许根。
- **测试入口**：必须同时传入 `path` 与显式关键字 `test_root`；测试根必须是可信位置下
  **独立、已存在**的可信子目录（测试根或系统临时子目录），
  拒绝系统临时根目录本身、`execution` 根本身。UI、模型输出或环境变量字符串不得作为测试根来源。
- 生产/测试路径解析后必须严格位于边界/测试根内；检查现有各级路径组件，
  拒绝符号链接、Windows junction、其他重解析点，以及数据库文件的**硬链接别名**
  （`st_nlink > 1`）；每次 `commit`/`list_window` 打开 SQLite 或建目录前都重新校验，
  覆盖 store 构造后目录或文件被替换/置入别名的情况。
- 拒绝：无测试根的外部路径、生产路径前缀的兄弟目录、`..` 越界、链接/junction 逃逸、
  数据库文件硬链接别名、原 MySQL（`mysql-a07`、`mysql-8.0.28-winx64`）与 DSH 来源
  （`dsh/.dsh` 路径段）。
- 校验失败在创建任何目录/数据库之前抛出 `STORE_UNAVAILABLE`；不产生新文件或记录。
- 合法测试注入只在测试根内建库，不触碰生产库；生产默认库只做只读路径核对。

### 记录契约

`record` 只允许五个键：`machine_no,start_time,end_time,reason,order_id`。

| 字段 | 规则 |
|---|---|
| `machine_no` | 必填；白名单 `M01/M02/M03`；另接受 `1号机/一号机` → M01 等明确别名 |
| `start_time` | 必填；精确 `YYYY-MM-DDTHH:MM:SS+08:00`；时区固定 +08:00 |
| `end_time` | 必填；格式同上，必须晚于 `start_time`（不自动互换） |
| `reason` | 必填；去首尾空白后 1–32 字符，保留原词 |
| `order_id` | 可空；白名单 `WO01`–`WO06`；缺省或 null 均可 |

`validate_record` 返回规范化记录（别名展开、空白去除、时间统一为秒级 ISO），不改数据库。

### 错误码

| 码 | 含义 |
|---|---|
| `INVALID_RECORD` | 结构/字段/时间格式/原因长度/未知设备或工单/非法提交键 |
| `INVALID_TIME_ORDER` | 结束不晚于开始（写入或查询窗口） |
| `SUBMISSION_CONFLICT` | 同一提交键已存在且规范化载荷不同 |
| `STORE_UNAVAILABLE` | 路径不可用、目录冲突、SQLite 打开/提交失败 |

错误信息为固定文本，不回显本机路径、底层 SQL 或字段值。

## 幂等与并发

- 提交键由 A 按草稿生成 UUID；页面 rerun/重试复用同一键。
- 同键 + 同规范化载荷：返回原 `record_id` 且 `replayed=True`，不新增行。
- 同键 + 不同载荷：`SUBMISSION_CONFLICT`，不覆盖原记录。
- 写入在 `BEGIN IMMEDIATE` 事务中完成；只有 `COMMIT` 成功才返回回执。
- 两个连接并发提交同键：唯一约束保证最多一条；竞争方读取已提交行并判为 replay 或冲突。
- `list_window` 只读，不产生写入；普通重绘不得调用 `commit`。

## 查回

- 只返回**已结束**记录（本沙箱不收 `end_time=NULL`）。
- 相交规则：`start_time < window_end AND end_time > window_start`（半开窗）。
- 按 `id ASC` 稳定排序；最多 200 行；`limit` 默认 200，超过 200 按 200 处理。
- `truncated=True` 时 `truncation_reason="row_limit"`。
- `columns` 直接复用 `query_catalog.describe_query("downtime_details")["columns"]`：
  `downtime_id,machine_no,start_time,end_time,reason,order_id`；`rows` 为按列顺序的值列表，
  `order_id` 可为 null。
- 窗边界、空结果、按 ID 找到本次录入均有离线测试覆盖。

## 最小样例（合成）

```text
确认：M01 于 2026-11-04 08:00:00+08:00 至 09:00:00+08:00 停机，原因“换型”，工单 WO01。
提交键：demo-1
首次提交：record_id=1，replayed=False
重复确认（同键同内容）：record_id=1，replayed=True
查回 [08:00,09:00)：返回该行
```

## 边界

- 不做自然语言解析、相对日期解释或字段补全；这些由 A 的提取/草稿层完成。
- 不提供任意 SQL、批量导入、更新/删除接口；没有写入之外的副作用。
- 不迁移原 MySQL 历史异常记录；两套数据源保持独立。
- 首轮只收单条已结束事件；多事件、未结束、未知工单由 A 澄清或拒绝。
