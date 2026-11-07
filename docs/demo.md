# 三分钟演示（离线）

> 只使用候选内合成数据与离线替身。真实模型、MySQL 和内部实测证据不在本候选。
> 开始前启动 Streamlit，保持默认 `mock`/`offline`，不设置任何 API key。

## 0:00–0:30 范围

- A07 是制造业务数据助手：受控问答 + 经人工确认的停机录入。
- 问答只走三个命名查询，不开放任意 SQL；录入只写独立 SQLite 沙箱。
- 数据标为 `synthetic`，不承诺企业准确率；首次真实批次 9/12、错误 ready 1/6，002严格11/12、错误ready0/6；v2真实字段/会话质量未认证。

## 0:30–1:10 受控问答

在“自然语言问答”入口输入：

```text
列出各产线当前累计实际产量快照（不按日期）。
```

- 展示 `ok / line_snapshot`、三行结果（含 L03=0）、口径与“离线模拟 SQL”标识；
- 再输入缺产线的问题（如“查一下工单进度”），展示澄清；
- 输入库存/写操作，展示拒绝；强调模型只产生三种动作。

## 1:10–2:40 停机录入

切到“停机录入（合成沙箱）”，保持 `offline`。

1. 加载预置单条已结束事件，点击“生成草稿”：
   展示五字段、原文片段、`[start,end)` 偏移、`execution_source=offline_template`。
2. 把 `reason` 改成“卡料”后直接点“确认”：
   服务端守卫阻止写入，页面明确提示需重新校验。
3. 点“重新校验”，再确认：
   展示 `record_id`、`replayed=False`；页面、回执、查回值均为“卡料”。
4. 再点一次确认：同 `record_id`、`replayed=True`。
5. 在时间窗内查回：一行记录；把窗口移到之外：空结果。
6. 粘贴两条完整模板或重复 `reason`（含空值顺序）：展示 `MULTIPLE_EVENTS`
   终止澄清、提交调用 0、隔离库零新增；说明需拆成单条后重新生成。
7. 切换 `real` 仅展示模式/来源与“生成草稿”显式动作；无本机配置时显示
   `CONFIG_MISSING`、零调用，不读取或打印 key。

## 2:40–3:00 边界与证据

- 真实模型质量：首次批次严格 9/12、错误 ready 1/6；002严格11/12、错误ready0/6；v2真实字段/会话质量未认证；
- 浏览器人工视觉、Python 3.12、真实数据库独立初始化：未在本候选验证；
- 公开证据：`candidate-manifest.json`、离线 CI、仓库 README 与 `docs/source-and-db.md`。

## 统一工作台离线补充演示

本段使用同一工作台UI和隔离测试SQLite，已做过基本浏览器演示；不发HTTP模型请求。
选择“统一工作台（v2）”，保持offline，使用单条模板：

```text
录入 machine_no=M02；start_time=2026-11-04T09:00:00+08:00；end_time=2026-11-04T09:12:00+08:00；reason=换型；order_id=WO03
```

生成草稿并明确确认后，输入“刚录入的停机记录”，应显示intake_sqlite及一行记录。
此流程只证明离线确认/查回，不能用来宣称真实模型准确率。

## 参考命令

```text
cd projects/A-mes-assistant
..\..\.venv\Scripts\python.exe -B -m streamlit run app.py --server.address 127.0.0.1 --server.port 8501 --server.headless true
```
