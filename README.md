# 青年驿站运营

衔接青年住宿资格、酒店房态、服务诉求与财政补贴。

## 运行

```bash
python3 service.py --check     # 基础检查（28 处酒店、床位、政策版本）
python3 service.py --port 8000 # 启动 HTTP 服务
npm test                       # 运行 59 项领域与 HTTP 测试，再执行健康契约
python3 -m compileall -q .     # 编译检查全部 Python 模块
```

## 领域规则

**资格快照**：按"申请当日有效"的政策版本判断户籍、毕业年限、学历、来城目的，
通过后固化快照（`Application.eligibility`）。政策跨生效日切换不影响已确认申请；
毕业年限超出但落在临界窗口内的申请自动转人工复核（`eligibility_appeal` 工单）。

**一份住宿权益**：确认占房、入住核验、临时离店、跨站调剂共用同一份 30 夜额度，
跨店合计（`GET /api/entitlements/{applicant}`）。撤回未使用日期立即释放房间与权益。

**唯一房态**：`(酒店, 床位, 夜)` 与 `(申请人, 夜)` 两个唯一索引，所有写操作在同一把
锁内提交——两家酒店争抢最后床位时恰好一个成功，另一个得到 `ROOM_UNAVAILABLE`；
跨店重复占房得到 `BOOKING_CONFLICT`。跨站调剂在同一事务内"截断旧单 + 新店承接"，
失败全量回滚，不会出现两边都没房或两边都占房。

**断网补传**：门锁/前台事件按 `event_id` 精确去重，再按（酒店, 床位/申请人, 类型,
5 分钟桶）语义去重——同一入住被门锁和前台各报一次只落地一条。找不到占房或与
冻结夜冲突的事件生成工单，绝不静默放行。

**人工例外**：爽约、超期、紧急延住、离店未归还、事件冲突全部生成挂到值班长名下
的复核工单（`GET /api/tickets`），值班长裁决留名、每单只裁一次。争议夜冻结期间
床位不可二次售出，也不参与清算。

**财政清算**：只按实际合规入住夜清算，每晚的房价与政策补贴单价在确认占房时锁定，
之后房价/政策变化不影响已确认补贴（`GET /api/hotels/{code}/subsidy-basis?date=`）。
存在未裁决工单或争议夜的占房不能清算；清算幂等，重复调用返回同一单据。

**材料脱敏**：求职材料按字段分级（identity / eligibility / service / job_search）。
团干部处理服务诉求时只能看到身份与参访意向等服务必需字段，期望薪资、简历、
作品集等非必要信息一律 `***无权查看***`；前台与财政仅见身份最小集。

## HTTP 接口

所有业务接口需要 `X-Actor-Id` 头（种子用户：`u_duty` 值班长、`u_verifier` 运营核验、
`u_officer` 团干部、`u_finance` 财政、`u_front_h01`…`u_front_h28` 各店前台）。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| GET | `/api/policies` | 政策版本 |
| GET | `/api/hotels?date=` | 28 站点与空房数 |
| GET | `/api/hotels/{code}/board?date=` | 唯一房态（运营/前台/财政） |
| GET | `/api/hotels/{code}/subsidy-basis?date=` | 逐日补贴依据 |
| POST | `/api/applications` | 提交申请（自动资格快照） |
| GET | `/api/applications/{id}/material` | 按角色脱敏的材料视图 |
| POST | `/api/applications/{id}/bookings` | 确认占房 |
| POST | `/api/allocations/{id}/withdraw` | 撤回未使用日期 |
| POST | `/api/allocations/{id}/transfer` | 跨站调剂 |
| POST | `/api/allocations/{id}/emergency-extension` | 紧急延住申请（转人工） |
| POST | `/api/allocations/{id}/settle` | 财政清算 |
| POST | `/api/events` `/api/events/batch` | 门锁/前台事件（含离线补传） |
| POST | `/api/review-scans` | 日终巡检（爽约/超期/离店未归还） |
| GET | `/api/tickets?status=open` | 人工复核工单 |
| POST | `/api/tickets/{id}/decisions` | 值班长裁决 |
| GET | `/api/entitlements/{applicant}` | 剩余权益与逐日消耗 |
| POST | `/api/applications/{id}/requests` | 服务诉求（企业参访等） |

错误体稳定为 `{"error": {"code", "message", ...}}`；资格临界、紧急延住等转人工的
场景返回 `202 + REVIEW_REQUIRED + ticket_id`。

`fixtures/domain.json` 保存领域名词与状态样例，便于接口联调时保持一致语义。
