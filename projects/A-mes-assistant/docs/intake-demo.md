# M5 停机录入（合成沙箱）演示

## 入口与模式

Streamlit 顶部“入口”选择 **停机录入（合成沙箱）**；原有自然语言问答和命名查询入口保留。
录入页默认 **offline**，可显式切到 **real**；只有点击“生成草稿”才调用模型。
页面显示 `execution_source`、provider/model、实际 complete 次数与 usage。
切换模式、原文或 reference_time 会使旧草稿失去确认资格。

## 三分钟演示

- 0:00–0:20：说明问答与录入使用不同数据源；录入写入独立 SQLite 沙箱。
- 0:20–0:40：加载预置 fixture，保持 offline，生成草稿；指出五字段、原文片段、
  `[start,end)` 偏移和状态。
- 0:40–1:10：把 reason 改为“卡料”，直接点确认——展示确认被服务端守卫阻止、
  零写入；再点“重新校验”，页面/提交/回执/查回统一为“卡料”。
- 1:10–1:40：确认并写入；展示 record_id、replayed=False、来源 offline_template；
  再点一次确认，展示同 ID、replayed=True。
- 1:40–2:10：按可编辑时间窗查回，展示一行记录；把窗外时间输入查回，展示空结果。
- 2:10–2:30：粘贴两条完整模板或空值重复 reason，展示 MULTIPLE_EVENTS 终态和
  零写入；说明需拆成单条后重新生成。
- 2:30–3:00：切到 real 仅展示模式/来源与“生成草稿”显式动作；无本机配置时显示
  CONFIG_MISSING、零调用，不读取或打印 key。

演示原文来自独立 fixture：

```text
machine_no=M02；start_time=2026-11-04T09:00:00+08:00；end_time=2026-11-04T09:12:00+08:00；reason=换型；order_id=WO03
```

该 fixture 是 A 自造的显式模板，不复制 C 的 12 条冻结题库答案。

## 流程

1. 点击“加载预置演示”（或粘贴单条已结束停机文本）。
   离线模板先统计每个字段赋值出现次数，再去空白/空值/完整性；任一段重复
   （即使其中一段值为空，如 `reason=；reason=换型`）或两条完整记录时，
   固定澄清为多事件，要求拆开，零写入；不再按首条匹配截取。
   单次出现的可选 `order_id=` 按业务契约存 null。
2. 显式点击“生成草稿”——离线替身只在此调用一次模型接口；
   其他自由文本给范围说明，不生成可确认字段。
3. 页面显示状态、缺项/固定 issue、五字段原文片段与 `[start,end)` 偏移。
   多事件、拒绝和解析错误草稿整条关闭提交；重新校验也不能解锁。
   引用不成立的字段清空值并保留待处理状态。
4. 可编辑五个字段；点击“重新校验”后，人工修改字段标记为 user，
   重新做字段/时间/规范校验。未修改的失效字段不会恢复模型值；
   选填工单可用“工单明确留空”显式清空。
   页面同时核对当前表单与最后已校验的规范化草稿；直接改字段后点确认会被
   服务端守卫阻止并提示重新校验，确认写入的只能是最后校验值。
5. 草稿绑定生成时的原文与 reference_time；改原文/参考时间后旧草稿不可确认，
   必须重新生成。新草稿使用新 draft ID，字段和窗口控件按 draft ID 隔离。
6. 仅 `state=ready` 且无 issue 时显示“确认并写入”；点击后调用 B 的
   `DowntimeStore.commit`，提交键使用草稿 ID。重复点击返回同一 record ID 和
   `replayed=True`；同键改载荷显示 `SUBMISSION_CONFLICT`；回执显示所属 draft。
7. 写入状态与查回状态分开展示；确认后自动查回，也可编辑
   `window_start/window_end` 后点击“查回”。查回使用 B 的
   `start_time < window_end AND end_time > window_start` 半开相交规则。
   查回失败后可用已有 ID 重试读操作。
8. 普通 rerun 不调用模型，不写库；只有显式按钮触发。

## 存储

- 页面引用 B 的工作区级固定默认路径：`runtime/a07-intake/downtime.sqlite3`；
  生产入口不读取 UI、模型或环境变量传入的任意 DB 路径。
- 测试通过注入 `DowntimeStore(path, test_root=受信测试根)` 或可信临时目录；
  不接触原 MySQL。
- 写入前由 B 独立复校；页面不提供任意 SQL。
- 合成标识：`intake-sandbox-v1`；不代表真实企业数据。

## 验证命令

工作目录 `projects/A-mes-assistant`：

```text
.venv-ui\Scripts\python.exe -B -m unittest discover -s tests -p test_intake*.py -v
```

当前 114 项：A 提取 13、页面 24、runtime 10、batch 13、B SQLite 54。
另跑 `test_question*.py` 42、`test_ui*.py` 16、`test_streamlit_app.py` 5。
覆盖提取严格 JSON/引用唯一性/原文单事件护栏/i07-i08 缺项码/缺项/反向窗/超时，页面无确认零写入、普通 rerun
无模型/DB 写、伪证据不清确认、多事件/空值重复字段关闭、改原文/参考时间失效、
切换草稿隔离、未校验修改被确认守卫阻止、重校验后落卡料、缺项补齐、选填留空、
重复确认、冲突、存储失败与查回重试；runtime offline/real 配置错误零调用；
batch dry-run/锁/账本/停止码；同时包含 B 的 SQLite 存储测试。

## 边界

- 首轮只收一条已结束的合成停机事件；自然语言日期/多事件拆分、真实模型效果另测。
- 真实模型、原 MySQL、GitHub 和数据迁移不在本卡；不复制 C 答案。
- 真实 12 题批测 pending_budget；本演示不执行真实调用。
- 浏览器视觉不是本卡自动测试项；AppTest 与 HTTP 健康分别记录。
