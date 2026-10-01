# 公共采购密封投标与评审系统

标准库实现的招标发布、密封投标、开标校验收、规则评分、利益冲突、澄清、废标、投诉重评和授标快照服务。

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
- `POST /api/tenders/open`：截止后开标并核验承诺哈希
- `POST /api/conflicts`、`POST /api/evaluations`：评分须携带项目版本号，并发提交只接收先到版本，后到者 409 提示重新确认
- `POST /api/clarifications`、`POST /api/clarifications/answer`
- `POST /api/complaints`、`POST /api/complaints/resolve`：投诉受理后当前轮评分与排名立即失效并进入重评
- `POST /api/tenders/confirm-results`：确认当前轮评审结果；评分未补齐或存在未处理投诉时拒绝
- `POST /api/tenders/award`：仅接受当前轮已确认且未失效的评审结果，锁定评分并保存排名快照
- `POST /api/tenders/award/fail`：授标失败回退，恢复最近确认的评审结果，投诉与评分记录全部保留

## 版本链

评审、投诉和授标共用项目版本号：每次评分、废标、结果确认、投诉受理、授标及回退都会推进版本。已确认的评审结果在出现新评分或废标时自动失效（superseded），投诉受理时立即失效（invalidated）但保留可追溯；授标前必须重新确认当前轮结果。后台角色可通过 `GET /api/tenders/{id}` 查看全部轮次的结果链、评分明细和投诉记录。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整开标授标、截止前正文隐藏、利益冲突、重复评分覆盖、并发评分先到先收、投诉失效与重评确认、评分未补齐拦截、授标失败回退和角色权限。

## 局限

供应商与请求用户没有绑定校验，身份仍依赖请求头；投标正文虽然按接口阶段隐藏，但数据库本身未加密；评分规则适合演示，不覆盖复杂资格预审、保证金、电子签名和采购法规差异。
