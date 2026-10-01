# 公共采购密封投标与评审系统

标准库实现的招标发布、密封投标、开标校验收、规则评分、利益冲突、澄清、废标、投诉重评和授标快照服务。

## 评审—投诉—授标统一版本链

开标、评分、投诉、重新确认、授标、授标失败恢复共享同一条 `tender.version` 版本链，
每轮评审结果记录在 `evaluation_rounds`（`in_progress` / `confirmed` / `invalidated`）。

- **投诉受理即失效**：投诉一受理，当前轮评分与排名立即置为 `invalidated`，旧版本号同时过期；
  重新确认前授标一律拒绝。
- **改判与驳回**：改判（accepted）开启新一轮重评（旧轮及评分保留可追溯）；驳回（rejected）
  不会让旧排名复活，仍须调用确认接口重新确认。
- **可追溯**：重评期间各历史轮次的评分、排名快照、关联投诉均可通过
  `GET /api/tenders/{id}`（procurement/supervisor/auditor）查看。
- **评分没补齐不能授标**：确认与授标都会重算排名，任一有效投标缺评分项即 409 拒绝。
- **并发评分先到先得**：`POST /api/evaluations` 接受可选 `expected_version`；先到的提交落库并
  推进版本，后到者收到 409“请重新确认”，用新版本重新提交即可。
- **授标失败恢复**：授标过程中任何校验失败都随事务回滚（不产生半成品授标），失败留痕写入
  `award_attempts`；显式 `POST /api/tenders/award/fail` 可在授标后撤下授标、恢复到最近一轮
  已确认评审结果，投诉处理记录原样保留。

## 运行

要求 Python 3.11+（当前 Python 3.9 环境亦可）。

```bash
python3 app.py --init --seed
python3 app.py
```

默认地址 `http://127.0.0.1:8209`，数据库默认 `public_procurement.db`。

## 主要接口

使用 `X-User`、`X-Role` 请求头。角色有 `procurement`、`vendor`、`evaluator`、`supervisor`、`auditor`、`public`。

- `GET /health`、`GET /api/state`、`GET /api/tenders/{id}`
- `POST /api/vendors`、`POST /api/tenders`、`POST /api/tenders/publish`
- `POST /api/bids`、`POST /api/bids/withdraw`、`POST /api/bids/disqualify`
- `POST /api/tenders/open`：截止后开标、核验承诺哈希并建立第 1 轮评审
- `POST /api/conflicts`、`POST /api/evaluations`（可选 `expected_version` 乐观锁）
- `POST /api/evaluations/confirm`：确认/重新确认当前轮评审结果并锁定排名快照
- `POST /api/clarifications`、`POST /api/clarifications/answer`
- `POST /api/complaints`（受理即失效当前轮）、`POST /api/complaints/resolve`
- `POST /api/tenders/award`：仅接受已确认（或补齐评分后可自动确认）的最新轮结果
- `POST /api/tenders/award/fail`：授标失败后撤下授标并恢复最近评审结果

## 测试

```bash
python3 -m unittest discover -s tests -v
```

覆盖完整开标授标、截止前正文隐藏、利益冲突、重复评分覆盖、投诉受理即失效/改判重评/驳回重新确认、
评分不全拒绝授标、并发评分先到先得、授标失败恢复与投诉记录保留、角色权限。

## 局限

供应商与请求用户没有绑定校验，身份仍依赖请求头；投标正文虽然按接口阶段隐藏，但数据库本身未加密；评分规则适合演示，不覆盖复杂资格预审、保证金、电子签名和采购法规差异。
