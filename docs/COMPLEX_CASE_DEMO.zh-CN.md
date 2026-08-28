# Nightingale 复杂纵向病例演示与数据库核验

## 1. 后端数据库如何维护

Nightingale 使用 PostgreSQL 16 保存正式业务状态，应用层使用 SQLModel，结构变更使用 Alembic migration。数据库不是前端本地存储，也不是临时 JSON。

核心维护边界：

- `clinics / clinic_memberships / patients / patient_identifiers`：诊所、角色、患者档案和加密标识。
- `entries / entry_versions / entry_relations`：类似块式协作数据库的临床条目、不可变版本和条目关系。
- `comments / comment_mentions / care_tasks`：划词讨论、@提及、分配和任务状态。
- `highlights / clinical_fact_assertions / provenance_pointers`：重点、结构化事实和精确来源。
- `conflict_cases / decision_assessments`：冲突、风险下限、置信资格和拒答状态。
- `ai_runs / redaction_runs / jobs / job_attempts`：AI 处理、脱敏和可恢复后台任务。
- `archive_blobs / decay_runs / retention_locks`：历史保留、加密归档和恢复。
- `audit_events`：谁在何时进行了什么操作。

每个租户业务表都绑定 `clinic_id`。PostgreSQL RLS 执行诊所隔离；患者正文、评论、身份字段和 AI 结果使用 AES-256-GCM 加密；病历号去重使用诊所作用域 HMAC。

## 2. 已写入数据库的复杂病例

登录后选择 **Jordan Wong**。这是合成病例，不包含真实患者信息。

```text
URL: https://localhost/login
Clinic Code: NIGHTINGALE
Clinician: clinician@nightingale.example
Care staff: staff@nightingale.example
Password: synthetic-demo-only
```

数据库中已存在：

| 年份 | 来源角色 | 可见条目 |
|---|---|---|
| 2004 | Care staff | Early metabolic risk review：肥胖和代谢风险 |
| 2012 | Clinician | Type 2 diabetes diagnosis |
| 2018 | Clinician | First acute pancreatitis admission |
| 2021 | Care staff | Weight and diabetes follow-up |
| 2025 | Clinician | Diabetes sick-day plan |
| 2026 | Clinician | Current pancreatitis admission plan，包含 3 个不可变版本 |
| 2026 | Care staff | Nursing escalation，包含 2 个版本 |
| 2026 | AI doctor scribe | AI-assisted multidisciplinary review |
| 2026 | AI nurse scribe | AI-assisted nursing handover |
| 2026 | AI patient session | AI-assisted patient account |

这个病例没有让 AI 自行决定哪条医学建议正确。系统同时保留：

1. 2025 年糖尿病 sick-day 口服补液计划及其适用条件；
2. 2026 年急性照护团队临时限制口服摄入的当前计划；
3. 护士发现两条指令可能被同时执行后发起的升级；
4. AI 对冲突的来源绑定提取；
5. 未解决的 High conflict；
6. 指派给 Clinician 的评论和任务。

临床医生最终通过新的 Correction entry 解决冲突，系统建立 `supersedes` 关系，而不是删除旧记录。

## 3. 10 秒阅读路径

打开 Jordan Wong 后，不需要先通读时间线：

1. 页首直接显示年龄、DOB、MRN、记录起始年份、记录跨度、条目数、AI-assisted note 数量和未解决冲突数。
2. 右侧第一张卡是 **Current priorities**，不是 Portal access。
3. 前五项优先显示当前急性计划、糖尿病监测、经临床医生确认的 AI handover、相关既往史。
4. 未通过安全门的冲突单独留在 **Needs clinical review**，不会混入正常重点，也不会消失。
5. 每一项都有 **View source**，点击后滚动到 Longitudinal timeline 的精确条目并高亮原句。

建议口头总结：

> Jordan 有长期肥胖和 2012 年以来的 2 型糖尿病史，2018 年有胰腺炎住院史。当前因复发性急性胰腺炎临时限制口服摄入，同时继续床旁血糖监测。系统发现该当前计划与 2025 年 sick-day 补液计划可能冲突，护士已升级，AI 只做来源绑定提取，冲突仍等待临床医生用 Correction entry 解决。

## 4. 推荐演示场景

### 场景 A：Glance + AI Scribe + 精确来源

1. 以 Care staff 登录。
2. 打开 Jordan Wong。
3. 在 10 秒内阅读 Current priorities。
4. 点击 `AI-scribed handover: hydration-plan discrepancy escalated for clinician review` 的 `View source`。
5. 页面滚动到 `AI-assisted nursing handover`，并高亮原句。

通过标准：重点、AI-scribed note、immutable version、exact quote 和时间线位置一致。

### 场景 B：多角色协作 + 审计 + 回退

1. 查看最晚的 AI-assisted nursing handover。
2. Team discussion 中已有 Care staff 的 `@clinician` 评论和 Clinician assignment。
3. Clinician 打开 `Current pancreatitis admission plan` 的 Change history。
4. 比较 v1、v2、v3；演示 `Restore this version` 会创建新版本，不删除历史。
5. 在 Admin 的活动记录中查看评论、版本和冲突事件。

通过标准：评论、mention、assignment、任务、版本、diff、revert 和 audit 都来自数据库。

### 场景 C：Longitudinal context + 冲突解决

1. 在 Longitudinal timeline 查看 2004、2012、2018、2021、2025、2026 年分组。
2. 查看 Structured clinical context 中的 obesity、type 2 diabetes、acute pancreatitis、oral intake 和 glucose monitoring。
3. 对每个结构化事实点击 `View exact source`。
4. 在 Clinical conflicts 中依次查看 first source 和 conflicting source。
5. 以 Clinician 选择 `Current pancreatitis admission plan` 作为 Correction entry，填写 resolution reason 后解决。

通过标准：旧计划仍保留；临床医生 correction 成为当前权威记录；冲突状态和审计被更新。

### Bonus：Self-learning + 历史保留

1. Current priorities 当前采用完全确定性的排序；系统不执行随机探索。
2. 页面只有在卡片可见面积 ≥50% 且持续 ≥2 秒后记录 impression；该记录当前仅用于审计和曝光偏差分析，不直接参与评分。
3. Dismiss 必须提供原因；Critical、Unresolved、Clinician-confirmed 项不受负学习影响。
4. Clinician 可在 Historical retention 查看当前患者的 Archived、Protected、Eligible 数量。
5. Jordan 的未解决冲突和开放任务会保护相关历史，防止过早归档。
6. 条件解除后，低风险旧正文可压缩并 AES-GCM 加密到 `archive_blobs`；版本、来源哈希和审计不删除，可 rehydrate。

## 5. 数据库只读核验

先取得当前数据库容器名：

```bash
docker ps --format '{{.Names}}' | grep 'nightingale-demo.*-db-1'
```

进入数据库：

```bash
docker exec -it nightingale-demo-531ae7752655-db-1 psql -U postgres -d app
```

核验结构和数量：

```sql
SELECT tablename
FROM pg_tables
WHERE schemaname = 'public'
  AND tablename IN (
    'entries', 'entry_versions', 'comments', 'comment_mentions',
    'care_tasks', 'clinical_fact_assertions', 'provenance_pointers',
    'conflict_cases', 'decision_assessments', 'audit_events',
    'archive_blobs'
  )
ORDER BY tablename;

SELECT entry_type, origin, COUNT(*)
FROM entries
GROUP BY entry_type, origin
ORDER BY origin, entry_type;

SELECT fact_type, origin, COUNT(*)
FROM clinical_fact_assertions
GROUP BY fact_type, origin
ORDER BY fact_type, origin;

SELECT fact_type, normalized_key, severity, status,
       left_pointer_id IS NOT NULL AS has_left_source,
       right_pointer_id IS NOT NULL AS has_right_source
FROM conflict_cases
ORDER BY created_at DESC;

SELECT version_no, storage_tier, content_sha256
FROM entry_versions
WHERE entry_id = (
  SELECT id FROM entries
  WHERE entry_type = 'manual_clinician_note'
  ORDER BY occurred_at DESC
  LIMIT 1
)
ORDER BY version_no;
```

注意：正文、评论、身份号码和结构化事实的敏感值在数据库中是 ciphertext，不应通过 SQL 直接看到明文；网页通过授权服务层解密。

## 6. 自动化测试证据

后端测试：

```text
backend/tests/test_demo_fixtures.py
结果：4 passed（隔离 PostgreSQL，不操作正在演示的数据库）
```

覆盖：多年记录、三类 AI-scribed notes、三版本历史、High conflict、Human/AI assertions、exact provenance、Glance 前五项、abstention、comment mention、assignment 和 care task。

浏览器测试：

```text
frontend/tests/longitudinal.spec.ts
结果：1 passed（Chromium，https://proxy，8.9s）

Frontend Vitest
结果：18 files passed，75 tests passed
```

覆盖：10 秒页首信息、Longitudinal timeline 年份、AI 高亮跳转、Structured clinical context、冲突、Change history、Team discussion 和 Historical retention。
