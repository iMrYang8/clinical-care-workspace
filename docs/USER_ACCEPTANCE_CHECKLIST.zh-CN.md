# Nightingale 用户验收清单（中文）

> 用途：由最终验收人亲自操作并勾选。网页文案为英文；本文只使用本地合成数据。不得录入真实患者资料。

## 0. 交付信息

- 验收版本/Commit：`____________________________`
- 验收人：`____________________________`
- 日期：`____________________________`
- 浏览器与版本：`____________________________`
- 结论：`[ ] 通过  [ ] 有条件通过  [ ] 失败`

## 1. 一条命令重建与健康检查

```bash
cd "/Users/shc/Desktop/72 hour bulid/nightingale"
./scripts/demo-up.sh
curl --insecure https://localhost/api/v1/utils/health-check/
```

预期：终端显示 `Nightingale local workspace is ready`，健康接口返回 `true`。

- [ ] 通过
- 实际结果：`__________________________________________________`
- 截图：`docs/evidence/01-startup.png`

入口：

- 医护工作台：<https://localhost/login>
- 患者入口：<https://localhost/patient/login>
- 平台管理员：<https://localhost/platform/login>
- 本地邮件：<http://localhost:8025>

首次访问 HTTPS 时，接受本地自签名证书提示。

## 2. 本地合成账号

| 角色 | Clinic Code | Email | Password | 入口 |
|---|---|---|---|---|
| Platform Administrator | 不适用 | `platform.admin@nightingale.example` | `local-platform-owner-only` | `/platform/login` |
| Clinic Admin | `NIGHTINGALE` | `admin@nightingale.example` | `synthetic-demo-only` | `/login` |
| Clinician | `NIGHTINGALE` | `clinician@nightingale.example` | `synthetic-demo-only` | `/login` |
| Care Staff | `NIGHTINGALE` | `staff@nightingale.example` | `synthetic-demo-only` | `/login` |
| Patient | `NIGHTINGALE` | `patient@nightingale.example` | `synthetic-demo-only` | `/patient/login` |
| 第二诊所 Staff | `OTHERCLINIC` | `staff@other-clinic.example` | `synthetic-demo-only` | `/login` |
| 第二诊所 Clinician | `OTHERCLINIC` | `clinician@other-clinic.example` | `synthetic-demo-only` | `/login` |

生产环境不会建立上述默认平台账号；生产平台账号通过：

```bash
export NIGHTINGALE_PLATFORM_ADMIN_EMAIL='owner@example.com'
export NIGHTINGALE_PLATFORM_ADMIN_PASSWORD='至少16字符的长口令'
export NIGHTINGALE_PLATFORM_ADMIN_NAME='Platform Owner'
cd backend
python -m app.provision_platform_admin
```

## 3. 角色权限矩阵

| 操作 | Staff | Clinician | Clinic Admin | Patient | Platform Admin |
|---|---:|---:|---:|---:|---:|
| 查看本诊所患者 | ✅ | ✅ | ✅只读 | 仅本人 | 跨诊所只读 |
| 创建患者档案 | ✅ | ✅ | ❌ | ❌ | ❌ |
| 编辑 Staff section | ✅ | ❌ | ❌ | ❌ | ❌ |
| 编辑 Clinician section | ❌ | ✅ | ❌ | ❌ | ❌ |
| 邀请患者开通门户 | ✅ | ✅ | ❌ | ❌ | ❌ |
| 最终批准患者共享 | ❌ | ✅ | ❌ | ❌ | ❌ |
| 管理诊所成员 | ❌ | ❌ | ✅ | ❌ | 管理诊所管理员 |
| 解决 High/Critical 冲突 | ❌ | ✅且必须写原因 | ❌ | ❌ | ❌ |
| 修改临床正文 | 按分区 | 按分区 | ❌ | 仅本人 Insight | ❌ |

## 4. 医护登录、Clinic Code 与主题

1. 打开 `/login`。
2. 输入 Clinic Code `nightingale`，预期自动显示 `NIGHTINGALE`。
3. 用 Staff 账号登录。
4. 分别切换 Light、Dark、System，刷新并重新登录。
5. 输入 `NTU-01`、数字、中文或不足 3 个字母的 Clinic Code。

预期：合法 Code 大写后登录；非法 Code 在前端和后端都被拒绝；主题无白色硬编码区域、无首屏闪烁，并在刷新后保持。

- [ ] 通过
- 实际结果：`__________________________________________________`
- 截图：`docs/evidence/02-login-theme.png`

## 5. 患者建档与两阶段去重

1. Staff 登录，进入 `Patients`，点击 `Add patient`。
2. 输入：
   - Full name：`Morgan Lim`
   - DOB：`1990-02-20`
   - MRN：`MRN-ACCEPT-001`
   - NRIC/FIN：`S9999999Z`
3. 点击 `Check for duplicate`，预期 `No matching patient found`。
4. 创建后预期跳转到患者详情。
5. 再用相同 MRN 或证件号建档，预期硬阻止并显示已有患者。
6. 保持姓名+DOB相同，改为 `MRN-ACCEPT-002` / `S8888888A`。
7. 预期出现疑似重复、掩码证件和实体证件核验复选框；未勾选时不能继续，勾选后才可创建。
8. 用 Clinic Admin、Patient 或 Platform Admin 尝试创建，预期被拒绝。

- [ ] 通过
- 实际结果：`__________________________________________________`
- 截图：`docs/evidence/03-patient-registration.png`

## 6. 患者邀请、激活与门户隔离

1. 在 Morgan Lim 详情的 `Portal access` 输入一个未使用的合成邮箱并发送邀请。
2. 打开 <http://localhost:8025>，读取邮件并打开 fragment-only 邀请链接。
3. 新账号设置 16–200 字符密码；两次密码必须一致。
4. 再次打开同一链接，预期已使用 Token 被拒绝。
5. 患者登录 `/patient/login`，预期只能看到自己的 Patient-facing 内容。
6. 直接访问 `/patients`、`/admin` 或其他患者 URL，预期跳回患者入口或得到拒绝。

- [ ] 通过
- 实际结果：`__________________________________________________`
- 截图：`docs/evidence/04-patient-invitation.png`

## 7. 平台最高管理员与跨诊所审计

1. 使用平台账号登录 `/platform/login`。
2. 预期看到 `NIGHTINGALE`、`OTHERCLINIC` 及成员/患者数量。
3. 依次进入两个诊所，查看患者和 Care timeline。
4. 页面应显示 `Read only`，不出现新增、编辑、删除或发布按钮。
5. 尝试把平台 Cookie 用于普通 Clinic 写接口，预期 `401/403`。
6. 执行第 15 节 SQL，预期每次跨诊所列表和患者 Timeline 访问都有 Request ID 审计。

- [ ] 通过
- 实际结果：`__________________________________________________`
- 截图：`docs/evidence/05-platform-audit.png`

### 7.1 第二诊所独立病例

1. 使用第二诊所 Clinician 登录：Clinic Code `OTHERCLINIC`。
2. Patients 列表预期仅显示 `Taylor Lee`、`Priya Nair`、`Daniel Koh`。
3. 打开 Taylor Lee，预期看到 2021–2026 哮喘随访时间线和一条可追溯 Current priority。
4. 打开 Priya Nair，预期看到 Staff 与 Clinician 两份降压药记录、1 条 High 未解决药物冲突、1 条正常优先事项和 1 条待复核事项。
5. 打开 Daniel Koh，预期看到 2024–2026 膝关节置换术后康复时间线和来源链接。
6. 切换到 `NIGHTINGALE` 诊所账号，预期看不到上述三位患者。
7. Platform Administrator 仍可跨诊所只读查看，且访问写入平台审计。

- [ ] 通过
- 实际结果：`__________________________________________________`
- 截图：`docs/evidence/05b-other-clinic.png`

## 8. Current priorities 与 Source details

1. Clinician 登录并打开 Alex Tan。
2. 检查 Current priorities 最多 5 项。
3. 每项显示 `Why this matters` 和组成分数；Critical 项在前。
4. 点击来源，预期显示来源标题、作者、日期、精确引用和历史状态。
5. 网页不得显示 UUID、SHA-256、offset、provider/model 或 If-Match。
6. 修改来源 Entry 后再次打开来源，预期仍解析到不可变历史版本。

- [ ] 通过
- 实际结果：`__________________________________________________`
- 截图：`docs/evidence/06-provenance.png`

## 9. Team discussion 与 Change history

1. 划词新增 Team discussion。
2. 按姓名选择 `@clinician` 和任务负责人，不输入 User ID/Membership ID。
3. 回复、Resolve，再打开 Change history。
4. 两个浏览器同时编辑同一 Entry，第二个旧版本保存预期得到明确冲突提示。
5. Restore 历史版本后应生成新版本，旧历史不删除。

- [ ] 通过
- 实际结果：`__________________________________________________`
- 截图：`docs/evidence/07-collaboration.png`

## 10. Human-Human 冲突与 Critical 保护

1. Staff 写入：`Patient is allergic to penicillin.`
2. Clinician 写入：`No allergy to penicillin was reported.`
3. 预期产生 `Critical` Allergy Conflict，并排显示两份精确来源。
4. Staff 尝试 Reject/Dismiss，预期被阻止，只能 Acknowledge 或 Request review。
5. Clinician 新建 Correction Entry，填写解决原因并 Resolve。
6. 预期冲突历史保留，记录解决人、时间、Correction 和原因。

- [ ] 通过
- 实际结果：`__________________________________________________`
- 截图：`docs/evidence/08-conflict.png`

## 11. Confidence、Risk、Importance 与 Abstention

对界面上的每项逐一回答：

| 项目 | 是什么 | 怎么知道错了 | 错了以后系统做什么 | 通过 |
|---|---|---|---|---|
| Risk | `max(deterministic floor, model risk)`；模型不能降低规则下限 | 与版本化规则和临床冲突测试对照 | 提升为 High/Critical，进入人工审核 | [ ] |
| Confidence | 仅来自匹配 Provider/Model/Task/Version 且样本足够的校准报告 | 查看 ECE、Brier、分桶 Precision、Selective Accuracy/Coverage、WER/CER、医学实体准确率 | Low/Unavailable 时 Abstain，不进入患者页面 | [ ] |
| Importance | 基础风险、固定保护项和诊所反馈权重组成 | 检查展示 impression 与显式 dismiss reason | Critical/Unresolved 永不因排名学习隐藏 | [ ] |

语音 Review 页面若没有有效校准报告，预期显示 `Confidence unavailable`，不得显示未经校准的百分比。

### 最新 Hint 七项逐条验证

1. **Extraction vs Generation**：打开 `Why this decision?`，确认每个 Fact 可跳到 immutable Entry Version 的精确原文；普通 Summary 没有发布按钮，也不能被当作来源。
2. **Risk floor**：输入严重过敏或相互矛盾的药物状态、剂量、途径、频次；确认规则下限为 High/Critical，模型结果不能把它降级。
3. **Confidence / Abstention**：删除或改动匹配报告的模型、参数或 Dataset Hash；刷新后应立即变为 `Confidence unavailable` 并进入 `Needs clinical review`。
4. **Redaction accuracy**：核对 `artifacts/evaluation/redaction-v2.json` 的 Recall、Residual PHI 和 Clinical Span Damage，而不是只验证“调用成功”。
5. **Self-learning**：卡片可见不足 50% 或不足 2 秒不得写 Impression；同一 `view_event_id` 只写一次；`Too busy to review` 不产生负权重。
6. **Conflict detection**：分别验证 Human↔Human、Human↔AI、Human↔Voice 的 Allergy、Medication、Dose、Route、Frequency 矛盾，左右来源都可打开。
7. **Patient-facing safety**：逐条 Publication Item 必须具有 exact source、有效 assessment 和 Clinician approval；任何 Low/Unavailable、Abstained 或未解决 High/Critical Conflict 都应阻止患者共享。

- [ ] 通过
- 实际结果：`__________________________________________________`
- 截图：`docs/evidence/09-trust-decision.png`

## 12. 患者共享审批与撤回

1. Staff 的 Patient-facing 操作只应表达 `Request patient sharing`，不能直接发布。
2. 制造未解决 High/Critical 冲突，Clinician 尝试发布，预期阻止。
3. 对 AI-assisted 内容制造 Low/Unavailable Confidence 或 unsupported fact，预期 Abstain 并阻止发布。
4. 解决冲突、确认精确 Provenance、通过 Redaction validation 后由 Clinician 批准。
5. 患者端应显示审核人、日期和来源。
6. Clinician 撤回后患者端不再显示，但 Change history、Publication 和 Audit 仍存在。
7. 患者自己提交 Insight 可立即出现在本人 Timeline，并标为 Patient-reported，而不是 clinically confirmed。

- [ ] 通过
- 实际结果：`__________________________________________________`
- 截图：`docs/evidence/10-patient-publication.png`

## 13. AI、Redaction 与故障回退

1. 不配置 `OPENAI_API_KEY`，提交记录并运行分析。
2. 预期原始记录保存；页面显示 Limited processing / Review required，而不是伪造成功。
3. Generated summary 不得成为 Highlight 来源；只有带精确不可变来源的 extracted fact 可进入 Highlight。
4. 检查日志不含姓名、电话、证件、MRN 或 redaction map。
5. 固定脱敏评测集 residual PHI 必须为 0；临床保护 Span 不被删除。

本地脱敏评测（无需模型 Key）：

```bash
cd "/Users/shc/Desktop/72 hour bulid/nightingale"
docker compose run --rm --no-deps \
  -e MIGRATION_DATABASE_URL=postgresql://postgres:changethis@db:5432/app \
  -v "$PWD:/workspace" backend sh -lc \
  'cd /workspace/backend && python -m app.evaluate_trust redaction \
   --output-dir /workspace/artifacts/evaluation'
```

真实 Mock/Synthetic 模型评测（Key 只放本机环境，不写入命令、Git、日志或报告）：

```bash
export OPENAI_API_KEY='在本机终端填写'
cd "/Users/shc/Desktop/72 hour bulid/nightingale"
docker compose run --rm --no-deps \
  -e OPENAI_API_KEY \
  -e MIGRATION_DATABASE_URL=postgresql://postgres:changethis@db:5432/app \
  -v "$PWD:/workspace" backend sh -lc \
  'cd /workspace/backend && python -m app.evaluate_trust voice \
   --transcribe-model gpt-4o-transcribe-diarize \
   --output-dir /workspace/artifacts/evaluation'
docker compose run --rm --no-deps \
  -e OPENAI_API_KEY \
  -e MIGRATION_DATABASE_URL=postgresql://postgres:changethis@db:5432/app \
  -v "$PWD:/workspace" backend sh -lc \
  'cd /workspace/backend && python -m app.evaluate_trust facts \
   --extract-model gpt-5.1 \
   --output-dir /workspace/artifacts/evaluation'
```

预期聚合报告：

```text
artifacts/evaluation/redaction-v2.json
artifacts/evaluation/voice-calibration.json
artifacts/evaluation/fact-calibration.json
```

真实结果不满足资格时，正确验收结果是 `Low/Unavailable + Abstention`，不得手工改成 High/Medium。

- [ ] 通过
- 实际结果：`__________________________________________________`
- 截图：`docs/evidence/11-ai-redaction.png`

## 14. Voice、Self-learning 与 Data Decay

### Voice

- [ ] 双设备加入同一 session。
- [ ] 断网恢复后 chunk 不重复、不缺失。
- [ ] Final transcript 有 speaker/timestamp；overlap 与 code-switch 明确标记。
- [ ] 点击 Clinical finding 跳到 transcript/audio 时间。
- [ ] 修改 transcript 后 summary/findings 标记 Update required。

### Self-learning

- [ ] Pin/Accept/Manual highlight 产生正反馈。
- [ ] 只有明确 `Not relevant/Outdated` 的 dismiss 影响非 Critical 排名。
- [ ] 未点击 impression 不被当作负反馈。
- [ ] Critical、Unresolved、Clinician-confirmed 不会被学习权重隐藏。

### Data Decay

- [ ] Cold payload 为 zstd + AES-GCM archive。
- [ ] archive 前后 checksum 一致。
- [ ] rehydrate 后历史版本和 Provenance 仍可解析。
- [ ] Retention lock、任务、置顶、冲突和临床确认内容不会错误归档。

证据截图：`docs/evidence/12-voice-learning-decay.png`

## 15. 数据库只读核验

```bash
cd "/Users/shc/Desktop/72 hour bulid/nightingale"
PROJECT="$(./scripts/demo-project-name.sh)"
docker compose --project-name "$PROJECT" \
  -f compose.yml -f compose.override.yml exec -T db \
  psql -U postgres -d app
```

进入 `psql` 后：

```sql
-- 平台管理员独立于 ClinicMembership
SELECT pa.id, u.email, pa.is_active
FROM platform_administrators pa JOIN users u ON u.id = pa.user_id;

-- 患者标识只展示 ciphertext 长度、HMAC 和掩码，不读取明文
SELECT identifier_type,
       octet_length(value_ciphertext) AS ciphertext_bytes,
       length(value_hmac) AS hmac_chars,
       masked_suffix
FROM patient_identifiers;

-- 患者账号、诊所 patient membership 与档案链接
SELECT cm.role, cm.is_active, pul.patient_id, u.email
FROM patient_user_links pul
JOIN users u ON u.id = pul.user_id
JOIN clinic_memberships cm
  ON cm.clinic_id = pul.clinic_id AND cm.user_id = pul.user_id;

-- 冲突来源、级别和解决凭证
SELECT fact_type, normalized_key, severity, status,
       left_pointer_id, right_pointer_id,
       resolved_by_membership_id, resolution
FROM conflict_cases ORDER BY created_at DESC;

-- 患者共享审批与撤回历史
SELECT patient_id, entry_version_id, approved_by_membership_id,
       approval_policy_version, approved_at, withdrawn_at
FROM patient_publications ORDER BY approved_at DESC;

-- 平台跨诊所访问审计
SELECT action, target_clinic_id, target_patient_id, request_id, created_at
FROM platform_audit_events ORDER BY created_at DESC;

-- Confidence/Abstention 决策记录
SELECT support_state, deterministic_floor, model_risk, effective_risk,
       risk_rule_version, risk_rule_ids_json,
       confidence_band, confidence_lower_bound,
       calibration_report_id, abstained, abstention_reason
FROM decision_assessments ORDER BY created_at DESC;

-- 逐条事实来源与患者发布 Gate
SELECT cfa.fact_type, cfa.origin, cfa.source_entry_version_id,
       cfa.provenance_pointer_id, ppi.publication_id,
       ppi.support_state, ppi.confidence_band
FROM clinical_fact_assertions cfa
LEFT JOIN patient_publication_items ppi ON ppi.assertion_id = cfa.id
ORDER BY cfa.created_at DESC;

-- 校准报告必须精确绑定模型、任务、参数、数据和有效期
SELECT provider, exact_model_id, task, sample_count, consultation_count,
       confidence_band, accuracy_lower_bound, expires_at
FROM calibration_reports ORDER BY created_at DESC;

-- 脱敏报告必须是零残留、零临床 Span 损坏
SELECT redactor_version, sample_count, phi_recall,
       residual_phi_count, clinical_span_damage_count, passed
FROM redaction_evaluation_runs ORDER BY created_at DESC;

-- 展示 impression；没有点击不等于负反馈
SELECT patient_id, highlight_id, viewer_membership_id, rank, surface,
       view_event_id, exposure_probability, visible_ratio,
       visible_duration_ms, shown_at
FROM importance_impressions ORDER BY shown_at DESC;
```

预期：平台账号存在且没有 ClinicMembership；标识 ciphertext 不是输入明文，HMAC 长度为 64；邀请激活后 link/membership 存在；Conflict、Publication、Platform Audit 均有可核验记录。

- [ ] 通过
- 实际结果：`__________________________________________________`
- 截图：`docs/evidence/13-database.png`

## 16. 复杂纵向病例验收

完整步骤见 `docs/COMPLEX_CASE_DEMO.zh-CN.md`。

以 Clinician 登录并打开 Jordan Wong：

- [ ] 页首显示 DOB、年龄、MRN、22 年记录跨度、条目数、AI-assisted notes 数和未解决冲突数。
- [ ] Current priorities 位于右侧第一张卡，包含当前胰腺炎计划、糖尿病监测和 AI-scribed handover。
- [ ] 点击 AI-scribed 高亮后滚动到对应 AI-assisted nursing handover，并精确高亮原句。
- [ ] Longitudinal timeline 显示 2004、2012、2018、2021、2025、2026 年分组。
- [ ] Structured clinical context 显示 Human 与 AI 的 source-linked facts，每项可以打开精确来源。
- [ ] Clinical conflicts 同时打开既往 hydration plan 和当前 oral-intake restriction 两个来源。
- [ ] Staff 只能请求复核；Clinician 可用 Correction entry 和原因解决冲突。
- [ ] Current pancreatitis admission plan 显示 v3；Change history 可比较 v1/v2/v3，并以新版本方式恢复。
- [ ] Team discussion 显示 `@clinician`、指派人和开放任务。
- [ ] Historical retention 显示 Archived、Protected、Eligible 统计，未解决冲突不会被负学习或归档隐藏。

实际结果：`__________________________________________________`

## 17. 自动测试验收

```bash
cd "/Users/shc/Desktop/72 hour bulid/nightingale"

# 本地静态检查与单元测试
.venv/bin/ruff check backend/app backend/tests
.venv/bin/mypy backend/app --config-file backend/pyproject.toml
cd frontend
npm run typecheck
npm run lint
/Users/shc/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node \
  ../node_modules/vitest/vitest.mjs run
```

发布前还应在干净 Git worktree 执行：

```bash
./scripts/verify-release.sh --e2e --benchmark --ffmpeg
```

- [ ] Backend Ruff
- [ ] Backend mypy
- [ ] Backend pytest + Alembic upgrade/downgrade/check
- [ ] Frontend lint/typecheck/Vitest/build
- [ ] Playwright 全量
- [ ] Glance warm P95 ≤ 300 ms
- [ ] FFmpeg inventory
- 实际结果：`__________________________________________________`

## 18. 产品文案与最终签字

默认网页逐页搜索并确认不出现以下任务/实现词：

```text
72h, 72-hour, candidate, brief, demo, fixture, Scenario, Bonus,
micro-test, synthetic, If-Match, SHA-256, offset, RLS, provider,
model, reason code, raw UUID
```

- [ ] 医护入口只有医护导航；患者入口独立。
- [ ] Platform 入口独立且始终 read-only。
- [ ] Patient DTO/网络响应不含 raw AI、内部评论、证件明文。
- [ ] 所有数据均为合成/Mock。
- [ ] 未修改演示视频。

最终意见：

```text
__________________________________________________________________
__________________________________________________________________
```

验收人签字：`____________________`  日期：`____________________`

开发负责人签字：`________________`  日期：`____________________`

## 19. 患者目录、SGT 与诊所 AI 配置增补验收

### 19.1 患者目录与重名处理

1. 以 `NIGHTINGALE` 的 Staff、Clinician 或 Admin 登录。
2. 打开 `Patients`，预期显示 300 条以上记录，并且每页最多 24 条。
3. 分别用姓名、MRN、DOB 搜索；预期后端返回过滤后的总数和当前页。
4. 搜索 `Wei Ming Tan`；预期出现多条同名档案，每张卡显示
   `verify DOB/MRN`，且 DOB、MRN 均不同。
5. 点击任意目录患者，新建 Care staff note 或 Clinical note；预期记录、
   不可变版本、审计事件以及可识别的结构化事实/冲突均由后端产生。

- [ ] 通过
- 实际结果：`__________________________________________________`

### 19.2 页面导航、角色语义和时间

1. 打开复杂患者页；左侧章节导航应随滚动变化选中项。
2. 点击 `Clinical review` 或 `Current priorities`；目标卡片应滚动到可见区并短暂高亮。
3. 页面顶部应有 `Back to patients`。
4. 冲突存在时，`Clinical conflicts` 在右栏首位。
5. Timeline 标题下应解释：Care staff note 是观察/交接/随访，Clinical note
   是评估/诊断/治疗计划。
6. Timeline、Team discussion、Change history、Source details、Patient portal、
   Platform read-only view 和 Activity log 均显示 `SGT`。

- [ ] 通过
- 实际结果：`__________________________________________________`

### 19.3 Clinic Admin AI processing

1. 以 Clinic Admin 登录，打开 `Administration → AI processing`。
2. 若 `.env.local` 已配置 Key，页面显示 server environment credential active；
   不显示 Key 内容或尾号。
3. 输入诊所自有 OpenAI Key，并设置 Fast、Careful、Transcription 三个模型后保存。
4. 刷新页面，预期只显示 `ending XXXX`，密码框为空，网络响应中无完整 Key。
5. Fast 路径用于常规提取；Careful 路径只在确定性规则发现高风险/冲突时复核；
   Transcription 路径用于最终语音转录。
6. 关闭远程出站或让脱敏资格失败；即使诊所 Key 存在，远程调用仍应停止。

数据库只读核验（不要查询或打印密钥明文）：

```sql
SELECT c.code, s.provider, s.api_key_last4,
       octet_length(s.api_key_ciphertext) AS encrypted_bytes,
       s.fast_model, s.careful_model, s.transcribe_model, s.updated_at
FROM clinic_ai_settings s
JOIN clinics c ON c.id = s.clinic_id;

SELECT action, resource_type, created_at
FROM audit_events
WHERE action = 'clinic.ai_settings.updated'
ORDER BY created_at DESC;
```

- [ ] 页面不返回完整 Key
- [ ] 数据库只保存 AES-GCM ciphertext 与 last4
- [ ] Staff/Clinician 请求 `/api/v1/admin/ai-settings` 返回 403
- [ ] 模型路由和隐私/校准/Abstention Gate 同时生效
- 实际结果：`__________________________________________________`
