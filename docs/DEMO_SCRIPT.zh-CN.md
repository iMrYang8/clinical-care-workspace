# Nightingale 最终演示脚本（核心功能 + Bonus）

> 建议成片时长：9–10 分钟；可选平台管理员附录：40 秒。  
> 病例、账号、录音和评测数据均为合成或 Mock 数据。

## 1. 演示目标

整段演示只回答一个产品问题：

> 医护人员能否在 10 秒内知道患者现在最重要的事情，并把每个判断追溯到精确、不可变的原始记录？

按评分权重依次证明：

1. **一眼可读与可行动**：Current priorities 不超过五项，未验证内容进入独立 Clinical review。
2. **协作与 AI 集成**：Staff、Clinician、Patient、Admin 各有清晰边界，人工与三类 AI-assisted note 在同一纵向记录协作。
3. **来源与信任**：重点、结构化事实和冲突均能打开精确来源；版本、修订和冲突历史不被覆盖。
4. **安全与隐私**：Patient 只见已批准内容；Admin 只读临床正文；服务端 RBAC、RLS、加密和脱敏共同执行边界。
5. **Bonus**：诊所级重要性学习、历史数据衰减/恢复、加密语音采集与人工 Review Mode。

## 2. 录制前准备

### 2.1 启动

在仓库根目录运行：

```bash
./scripts/demo-up.sh
```

打开 `https://localhost`，首次访问接受本地自签名证书。录制前确认 `/login`、`/patient/login` 和 `/admin` 所需页面可访问。

不要为了录制清空当前数据库。若确实需要恢复确定性 Seed，先备份并明确确认当前合成交互可以丢弃，再使用仓库已有的隔离重建流程。

### 2.2 四个隔离会话

使用四个浏览器 Profile 或四个隔离 Context 预先登录，避免同域 Cookie 相互覆盖：

| 窗口 | 入口 | Clinic Code | Email | Password |
| --- | --- | --- | --- | --- |
| Staff | `https://localhost/login` | `NIGHTINGALE` | `staff@nightingale.example` | `synthetic-demo-only` |
| Clinician | `https://localhost/login` | `NIGHTINGALE` | `clinician@nightingale.example` | `synthetic-demo-only` |
| Clinic Admin | `https://localhost/login` | `NIGHTINGALE` | `admin@nightingale.example` | `synthetic-demo-only` |
| Patient | `https://localhost/patient/login` | `NIGHTINGALE` | `patient@nightingale.example` | `synthetic-demo-only` |

主病例使用 **Jordan Wong**；Patient Portal 使用已连接患者账号的 **Alex Tan**。

### 2.3 录制布局

- 浏览器缩放建议 80%–90%，隐藏书签栏和无关标签页。
- Staff 预停在 Patients；Clinician 预停在 Jordan；Patient 预停在 My Care；Admin 预停在 Administration。
- 排练时，打开会改变数据库状态的对话框后选择 **Cancel**；最终成片才执行一次 Create、Restore、Resolve、Confirm 或 Keep at top。
- 终端测试结果和架构图作为最后的证据插页，不要让终端取代产品演示。

## 3. 9–10 分钟主脚本

### 0:00–0:25｜开场

**画面：** Staff 的 Patients 页面。  
**旁白：**

> Nightingale 是面向临床团队的共享纵向患者记录。它不是把 AI 摘要堆在时间线上，而是帮助团队在 10 秒内看懂当前重点，并把每一项直接追溯到保存下来的精确来源。

---

### 0:25–1:45｜Staff：患者检索、10 秒重点与精确来源

**操作：**

1. 展示 **Today's visits**、**Previous records** 和搜索框。
2. 搜索 `Jordan Wong`，打开患者档案。
3. 停留 5–8 秒，让观众读取页首摘要、Clinical review 和 Current priorities。
4. 在 `AI-scribed handover: hydration-plan discrepancy escalated for clinician review` 上点击 **View source**。
5. 展示 Source details 中被标记的精确原句，然后关闭。

**旁白：**

> Jordan 有 22 年纵向记录、三类 AI-assisted notes 和一个尚未解决的临床冲突。Current priorities 只展示已经通过来源与决策检查的内容；未验证或冲突内容不会消失，而是单独留在 Clinical review。现在点击 AI handover 的来源，系统不是跳到一篇相似摘要，而是打开产生该判断的不可变版本和精确文字。

**本段证明：** Top Card、10 秒可读性、AI Scribe、精确 provenance、未验证内容不被隐藏。

---

### 1:45–3:15｜Staff：新建记录与行内协作

**操作：**

1. 点击 **Add care note**，创建一条简短 Staff note：

   ```text
   Title: Post-round observation
   Body: Patient reports persistent nausea. Please reassess oral intake before changing the plan.
   ```

2. 在一条 Staff note 中点击 **Edit**，选中短语，点击 **Comment on selection**。
3. 评论内容填写：`Please reconcile this with the current pancreatitis plan.`
4. Mention 选择 **Clinician**，Assign to 选择 **Clinician**，提交到 Team discussion。
5. 打开 **Team discussion**，展示精确引用、提及、负责人和 resolve/reopen 状态。

**旁白：**

> Staff 记录观察、交接和随访，但不能修改 Clinician 的诊断与治疗计划。讨论线程绑定在创建时的不可变版本和原始选中文本上；提及、分配、解决和重新打开都会写入数据库与审计事件，而不是只存在浏览器里。

**本段证明：** Staff 写入边界、Tiptap 划词评论、mention、assignment、resolve/unresolve、数据库协作。

---

### 3:15–4:35｜Clinician：纵向上下文、三类 AI note 与版本历史

**画面切换：** Clinician 的 Jordan 页面。  
**操作：**

1. 用左侧导航依次点击 **Timeline** 和 **Source-linked facts**，展示当前区块的选中状态与滚动定位。
2. 快速扫过 2004、2012、2018、2021、2025、2026 年分组。
3. 指出三类独立条目：
   - AI-assisted multidisciplinary review；
   - AI-assisted nursing handover；
   - AI-assisted patient account。
4. 打开 `Current pancreatitis admission plan` 的 **Change history**。
5. 展示 v1、v2、v3 和 diff；最终成片可执行一次 **Restore this version**，强调它会生成新版本，不删除历史。

**旁白：**

> 这是同一位患者从年轻时期的代谢风险、糖尿病和既往胰腺炎，到当前急性照护的连续记录。人工 Staff note、Clinical note 和三类 AI-assisted note 混合按时间呈现，但标签、作者边界和版本始终保留。回退不是改写历史，而是从旧快照创建一个新的当前版本。

**本段证明：** Longitudinal Timeline、三类 AI note、角色区分、版本快照、diff、revert、不可变历史。

---

### 4:35–5:45｜Clinician：冲突、风险下限与人工纠正

**操作：**

1. 打开 **Clinical review**，展示 oral-intake High conflict。
2. 分别点击两个来源，说明 2025 年糖尿病 sick-day 补液计划与 2026 年急性胰腺炎临时限制口服摄入的适用条件不同。
3. 打开一张重点的 **Why this decision?**，停留在三段：
   - What is it?
   - How could it be wrong?
   - What happens when it is wrong?
4. 点击 **Resolve conflict**，选择 Clinician correction，填写原因。排练时 Cancel；最终成片再提交一次。

**建议理由：**

```text
The acute pancreatitis plan applies during active vomiting. Continue bedside glucose monitoring and reassess oral intake after acute-care review.
```

**旁白：**

> 系统不会让模型投票决定哪个医生正确。确定性规则设置风险下限，模型只能提高、不能降低。来源不唯一、Confidence 不合格或存在 High/Critical 冲突时，系统 abstain、阻止患者共享，并要求 Clinician 用新的 Correction entry 解决；旧记录仍然保留。

**本段证明：** Human/Human/AI 冲突、双来源、deterministic risk floor、abstention、Clinician correction、患者发布 Gate。

---

### 5:45–6:35｜Patient：独立、最小化的 My Care

**画面切换：** Patient 的 `My Care · Alex Tan`。  
**操作：**

1. 展示 Patient 只有 My Care 导航。
2. 展示已批准的 timeline 和 Current priorities。
3. 点击 **View approved source**。
4. 打开 **Add my insight**，展示患者可以提交自己的更新，然后 Cancel。

**旁白：**

> Patient 使用独立入口，只能看到经过批准、面向患者的内容和批准来源。原始 AI note、内部评论、风险分数、转录文本、Admin 页面都不会出现在患者响应中。患者自己的 insight 会明确标记为 patient-reported，不会被伪装成临床确认。

**本段证明：** Patient-safe projection、独立门户、最小披露、批准凭证、患者贡献。

---

### 6:35–7:15｜Clinic Admin：真实成员管理、AI 配置与审计

**画面切换：** Clinic Admin 的 Administration。  
**操作：**

1. 展示成员和邀请状态。
2. 展示 AI processing 设置；密钥只显示是否已配置或末四位，不回显完整 secret。
3. 展示以 Singapore time 呈现的 **Activity log**。
4. 打开 Jordan，指出 `read-only oversight`，且没有 Add/Edit/Resolve 临床控件。

**旁白：**

> Admin 管理成员、诊所级模型配置和活动审计，但不能编辑临床正文。Activity log 来自 PostgreSQL 的真实 AuditEvent，只记录谁、何时、对哪个区域做了什么，不把临床正文复制进日志。

**本段证明：** Admin read-only、邀请制注册、诊所级配置、真实审计、secret 最小暴露。

---

### 7:15–8:05｜Bonus 1：可审计的重要性学习

**画面切换：** Clinician 的 Current priorities。  
**操作：**

1. 在非 Critical 项展开 **Why this decision?**。
2. 指出 rule-based score、clinic feedback adjustment、final score 和 protected 状态。
3. 点击 **Dismiss… → Too busy to review**；刷新后说明该事件会被记录，但不会被误当成内容无关的负反馈，也不会压低保护项。
4. 插入一张预先准备的只读数据库或自动测试证据，展示一次 Confirm/Pin 后，同类 bounded feature 的权重和另一条相似 highlight 的 learned component 如何变化。不要把“同一张卡被 Pin 到顶部”本身当作相似内容已经学习的证明。
5. 证据画面显示：
   - `importance_feedback_events`；
   - `importance_feature_stats`；
   - `importance_impressions`。

**旁白：**

> 这不是 LLM，也不是对某位医生建立个人画像。系统使用诊所级、确定性的在线特征权重：近期性、风险、临床实体、未解决状态和 Clinician 确认形成基础分；Confirm、Pin、Comment、Edit 或带理由的 Dismiss 更新同类特征权重。Critical、Unresolved 和 Clinician-confirmed 内容不会被负反馈隐藏。Impression 目前只用于审计与曝光偏差分析，不参与评分，也没有随机探索。

**本段证明：** 自适应重要性、显式人类反馈、可解释分数、保护下限、学习日志。

---

### 8:05–8:40｜Bonus 2：数据衰减不是删除历史

**画面：** Clinician 的 **Historical retention** 卡片，随后插入 schema/测试证据。  
**旁白：**

> 对较旧、低风险且没有开放任务的正文，系统可以压缩并以 AES-GCM 加密进入冷存储。EntryVersion 元数据、checksum、provenance 和 audit 始终留在 PostgreSQL；读取旧来源时执行 rehydrate 并核验 checksum。Critical、未解决、Clinician-confirmed、Pinned 或被任务引用的内容会保持 Protected。当前浏览器只展示 Archived、Protected、Eligible，归档操作是受控后端流程。

**本段证明：** Hybrid storage、保护规则、可恢复历史、checksum、不是软删除。

---

### 8:40–9:25｜Bonus 3：语音录制、Review Mode 与诚实拒答

**画面：** Clinician 点击 **Record visit**；随后切换到已准备的合成录音 Review Mode 或自动化证据片段。  
**操作：**

1. 展示加密、可恢复的录音上传入口。
2. 在 Review Mode 展示 speaker、timestamp、overlap/language 状态和 Clinical findings。
3. 点击一个 fact，使页面跳到对应 transcript segment/audio range。
4. 指出 **Publish reviewed note** 只在 Clinician 审核后可用。

**旁白：**

> 浏览器先把录音分块加密保存到 IndexedDB，再进行可恢复上传；最终转录以不可变 revision 保存，结构化 fact 可以跳回 transcript 与 audio 时间段。真实 PriMock57 评测得到 Low，而不是包装成 High，所以运行时正确行为是 Confidence Low/Unavailable、进入人工 Review 或 abstain。这里展示的是安全的人工复核闭环，不宣称临床级 ASR 已经通过验证。

**本段证明：** Ambient capture、断网恢复、speaker/timestamp/overlap、fact-to-audio provenance、Clinician publication、真实负面评测。

---

### 9:25–9:50｜收束

**画面：** 回到 Jordan 的 Current priorities 与 Clinical review。  
**旁白：**

> Nightingale 的核心不是三个漂亮的分数，而是每个 Risk、Confidence 和 Importance 都能回答：它是什么，怎样知道它可能错了，以及错了以后系统做什么。可以证明的内容进入 Current priorities；不能证明的内容不会消失，也不会发给患者，而是保留来源并等待临床复核。

## 4. 官方场景对照

| 官方要求 | 本脚本对应段落 | 可见证据 |
| --- | --- | --- |
| A：Staff 10 秒 Glance | 0:25–1:45 | 最多五项、Clinical review、AI priority |
| A：高亮跳到 AI 原始条目/片段 | 0:25–1:45 | Source details 的精确 mark 与 immutable version |
| B：Staff note、评论、@Clinician、assignment | 1:45–3:15 | 新条目、选中文本锚点、Team discussion |
| B：Clinician edit、diff、revert、audit | 3:15–4:35 与 6:35–7:15 | v1/v2/v3、Changes、Restore、Activity log |
| C：跨日期混合人工/AI 时间线 | 3:15–4:35 | 2004–2026 年组、三类 AI note |
| C：重要性逻辑 | 7:15–8:05 | rule score、clinic adjustment、保护状态 |
| Bonus：Data decay | 8:05–8:40 | Historical retention + schema/测试证据 |
| Bonus：Ambient voice | 8:40–9:25 | Record visit、Review Mode、fact → transcript/audio |
| RBAC 与患者安全 | 5:45–7:15 | Patient-only projection、Admin read-only |

## 5. 可选 40 秒平台管理员附录

```text
URL: https://localhost/platform/login
Email: platform.admin@nightingale.example
Password: local-platform-owner-only
```

展示两个诊所、患者数量、跨诊所只读查看和 platform audit。旁白只需说明：Platform Administrator 使用独立身份、Cookie 和 scope；每次跨诊所读取都会审计，任何临床写请求都返回 403。不要把它与 Clinic Admin 混为一个角色。

## 6. 录制前新增闭环检查

原先标出的三项边界已经补入当前工作树。正式录制前用隔离角色会话各走一次，确认最终提交与浏览器镜像包含同一实现：

1. **AI 划词 Highlight**：以 Clinician 打开任意 AI-assisted note，选中一段文字，点击 **Add to priorities**，在弹窗确认名称；创建后应刷新 Current priorities，并可重新打开同一不可变版本的精确来源。Staff、Admin、Patient 不应看到该控件。
2. **Timeline 直接元数据**：检查 timeline 响应；AI/System Entry 应直接包含 `author_role=system` 和顶层 `provenance`。有来源时状态为 `resolved`，归档为 `archived`，找不到可信来源时必须是 `unavailable`。
3. **患者共享工作台**：以 Staff 对一条保存后的 Staff note 发起 **Request clinician review**；以 Clinician 在 **Patient sharing** 中打开请求的精确版本并 **Approve and publish**；患者确认可见后，Clinician 执行 **Withdraw**，患者时间线应移除该条，但保留撤回回执和审计历史。

仍然不得声称：

- Voice 已达到临床级准确率；真实评测为 Low。
- Self-learning 是个人画像、神经模型或随机探索；当前是诊所级确定性权重。
- Data decay 在浏览器中直接执行；当前浏览器展示状态，归档/恢复是受控后端流程。
- 远程音频在 ASR 之前已经完成文本级 PHI 脱敏；当前语音演示全部使用合成音频，真实 PHI 的严格路径需要 no-audio-egress/local ASR 配置。
- 已完成 revision-bound 全量 release gate，除非最终干净提交、镜像 digest、测试报告和 benchmark 已重新绑定。

## 7. 失败回退方案

| 现场问题 | 立即处理 | 仍可证明什么 |
| --- | --- | --- |
| Source details 未滚动或未聚焦 | 关闭后从 Current priorities 再点一次 View source | 精确 quote、version、source title 仍可见 |
| 最终冲突已被前一次排练解决 | 展示 resolved conflict 与 correction relation；或恢复隔离 Seed 后重新录制 | 历史不删除、Clinician correction、audit |
| Voice Provider 未配置或失败 | 展示明确的 unavailable/review 状态，再切入 Scenario F 自动化证据 | fail-closed、加密恢复、Review Mode 合约 |
| 学习排序变化不明显 | 展开 Why 面板并插入只读 feature stats 证据 | 事件、特征权重、保护规则、可审计公式 |
| Data decay 没有 Eligible 项 | 展示 Protected 原因和隔离 `Scenario C` 测试报告 | 风险/任务保护、archive/rehydrate/checksum 路径 |
| 登录 Cookie 相互覆盖 | 改用预登录的隔离浏览器 Profile | 各角色服务端权限仍保持独立 |

## 8. 最终录制 Gate

- [ ] Staff、Clinician、Admin、Patient 四个隔离会话均可登录。
- [ ] Jordan 的 Current priorities、Clinical review、2004–2026 timeline、三类 AI note 和三版本历史可见。
- [ ] 至少一个 AI priority 与一个 structured fact 能打开精确来源。
- [ ] Clinician 能在 AI note 中划词创建新 priority；其他角色没有此控件。
- [ ] Timeline API 对 AI 条目直接返回 `author_role=system` 与顶层 provenance。
- [ ] Staff 能创建 note/comment/mention/assignment；Clinician 能打开 conflict correction；Admin 没有临床写控件。
- [ ] Staff sharing request → Clinician 精确版本审批 → Patient 可见 → Clinician 撤回 → Patient 仅保留撤回回执，整条链路通过。
- [ ] Patient 网络响应与页面不含 raw AI、内部评论、风险分数和 transcript。
- [ ] Why 面板能展示 rule-based score、clinic feedback adjustment 和失败后的动作。
- [ ] Voice 片段明确标记为合成/Mock；真实 Low 评测被如实口述。
- [ ] 成片没有真实患者信息、API key、完整 secret、内部 UUID 或技术错误码。
- [ ] 最终测试数字只引用同一提交与镜像生成的报告。
