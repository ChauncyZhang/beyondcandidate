const ACTIVE_STAGES = new Set(["新简历", "待复核", "待沟通", "待安排", "面试中", "待决策"]);
const ALLOWED_STAGES = new Set([...ACTIVE_STAGES, "已录用", "已淘汰", "已撤回"]);

function nowLabel() {
  return "刚刚";
}

function cloneState(state) {
  return {
    ...state,
    positions: [...state.positions],
    candidates: [...state.candidates],
    interviews: [...state.interviews],
    pools: [...state.pools],
    memberships: [...state.memberships],
  };
}

function candidateId(file) {
  return file.candidateId || `CAN-${file.id}`;
}

function applicationId(task, file) {
  return `APP-${task.id}-${file.id}`;
}

function findCandidateIndex(candidates, file) {
  if (file.candidateId) return candidates.findIndex((candidate) => candidate.id === file.candidateId);
  if (file.email) return candidates.findIndex((candidate) => candidate.email === file.email);
  return candidates.findIndex((candidate) => candidate.name === file.candidate);
}

function candidateFromFile(task, file, stage) {
  const score = file.ruleScore ?? file.llmScore ?? 0;
  const id = candidateId(file);
  const app = {
    id: applicationId(task, file),
    position: task.position,
    state: stage,
    created: "2026-07-12",
    source: task.source,
    screeningTaskId: task.id,
  };
  return {
    id,
    name: file.candidate || file.name || "合成候选人",
    role: task.position,
    company: "合成测试经历",
    position: task.position,
    stage,
    score,
    ruleScore: file.ruleScore ?? null,
    llmScore: file.llmScore ?? null,
    recommendation: file.recommendation || "人工复核",
    source: task.source,
    owner: task.creator || "张小北",
    city: file.city || "待确认",
    phone: file.phone || "138****0000",
    email: file.email || `${file.id.toLowerCase()}@example.com`,
    lastActivity: nowLabel(),
    tags: file.scenarioTags || ["UX-08 合成数据"],
    skills: file.skills || [],
    education: file.education || "待确认",
    experience: file.experience || "合成测试经历",
    summary: file.summary || "由 UX-08 合成简历筛选结果创建。",
    matched: file.matched || "待确认",
    missing: file.missing || "待确认",
    risk: file.error || file.risk || "无明显风险",
    llmReason: file.llmReason || "合成筛选结果，仅用于可用性测试。",
    humanConclusion: null,
    notes: [],
    version: 1,
    timeline: [{ time: nowLabel(), actor: "系统", action: `完成筛选任务 ${task.id}，进入${stage}` }],
    applications: [app],
    interviews: [],
    sourceFileId: file.id,
    synthetic: true,
  };
}

export function recalculatePositionCounts(positions, candidates) {
  return positions.map((position) => {
    const scoped = candidates.flatMap((candidate) => candidate.applications || [])
      .filter((application) => application.position === position.name);
    return {
      ...position,
      candidates: scoped.filter((application) => ACTIVE_STAGES.has(application.state)).length,
      review: scoped.filter((application) => application.state === "待复核").length,
      interview: scoped.filter((application) => application.state === "面试中").length,
    };
  });
}

function updateCurrentApplication(candidate, state) {
  const applications = candidate.applications || [];
  const currentIndex = applications.findIndex((application) => application.position === candidate.position);
  return applications.map((application, index) => index === currentIndex ? { ...application, state } : application);
}

function withCounts(state) {
  return { ...state, positions: recalculatePositionCounts(state.positions, state.candidates) };
}

export function applyScreeningResults(state, { task, files, targetStage = "待复核" }) {
  if (!ALLOWED_STAGES.has(targetStage)) throw new Error(`Unsupported candidate stage: ${targetStage}`);
  const next = cloneState(state);
  const accepted = files.filter((file) => file.status === "success" || file.status === "partial");
  for (const file of accepted) {
    const index = findCandidateIndex(next.candidates, file);
    if (index === -1) {
      next.candidates.push(candidateFromFile(task, file, targetStage));
      continue;
    }
    const current = next.candidates[index];
    const appId = applicationId(task, file);
    const existingApplication = current.applications?.some((application) => (
      application.id === appId
      || (application.screeningTaskId === task.id && application.position === task.position)
    ));
    const application = {
      id: appId,
      position: task.position,
      state: targetStage,
      created: "2026-07-12",
      source: task.source,
      screeningTaskId: task.id,
    };
    next.candidates[index] = {
      ...current,
      position: task.position,
      stage: targetStage,
      score: file.ruleScore ?? current.score,
      ruleScore: file.ruleScore ?? current.ruleScore,
      llmScore: file.llmScore ?? current.llmScore,
      recommendation: file.recommendation || current.recommendation,
      lastActivity: nowLabel(),
      applications: existingApplication ? current.applications : [application, ...(current.applications || [])],
      timeline: [{ time: nowLabel(), actor: "系统", action: `应用筛选任务 ${task.id} 结果，进入${targetStage}` }, ...(current.timeline || [])],
    };
  }
  return withCounts(next);
}

export function transitionCandidate(state, candidateIdValue, target, metadata = {}) {
  if (!ALLOWED_STAGES.has(target)) throw new Error(`Unsupported candidate stage: ${target}`);
  const next = cloneState(state);
  next.candidates = next.candidates.map((candidate) => {
    if (candidate.id !== candidateIdValue) return candidate;
    const applications = updateCurrentApplication(candidate, target);
    return {
      ...candidate,
      stage: target,
      applications,
      humanConclusion: metadata.humanConclusion ?? candidate.humanConclusion,
      lastActivity: nowLabel(),
      timeline: [{ time: nowLabel(), actor: metadata.actor || "张小北", action: metadata.action || `推进至${target}` }, ...(candidate.timeline || [])],
    };
  });
  return withCounts(next);
}

function interviewSummary(interview) {
  return {
    interviewId: interview.id,
    round: interview.round,
    time: `${interview.dateLabel || interview.date || "待定"} ${interview.time || ""}`.trim(),
    interviewer: (interview.interviewers || []).join("、"),
    result: interview.feedback?.conclusion || interview.feedbackStatus || interview.status,
    feedback: interview.feedback?.strengths || `面试状态：${interview.status || "已安排"}`,
  };
}

export function saveInterview(state, interview) {
  const next = cloneState(state);
  const existing = next.interviews.some((item) => item.id === interview.id);
  next.interviews = existing ? next.interviews.map((item) => item.id === interview.id ? { ...item, ...interview } : item) : [{ ...interview }, ...next.interviews];
  next.candidates = next.candidates.map((candidate) => {
    if (candidate.id !== interview.candidateId) return candidate;
    const interviews = [...(candidate.interviews || []).filter((item) => item.interviewId !== interview.id), interviewSummary(interview)];
    const applications = updateCurrentApplication(candidate, "面试中");
    return {
      ...candidate,
      stage: "面试中",
      applications,
      interviews,
      lastActivity: nowLabel(),
      timeline: [{ time: nowLabel(), actor: "系统", action: `更新${interview.round}安排：${interview.dateLabel || interview.date} ${interview.time}` }, ...(candidate.timeline || [])],
    };
  });
  return withCounts(next);
}

export function submitInterviewFeedback(state, interviewId, feedback) {
  const interview = state.interviews.find((item) => item.id === interviewId);
  if (!interview) return state;
  const updated = { ...interview, status: "已完成", feedbackStatus: "已提交", feedback: { ...feedback } };
  let next = cloneState(state);
  next.interviews = next.interviews.map((item) => item.id === interviewId ? updated : item);
  next = transitionCandidate(next, interview.candidateId, "待决策", {
    actor: feedback.submittedBy || "面试官",
    action: `收到${interview.round}反馈：${feedback.conclusion || "待 HR 决策"}`,
  });
  next.candidates = next.candidates.map((candidate) => candidate.id === interview.candidateId ? {
    ...candidate,
    interviews: (candidate.interviews || []).map((item) => item.interviewId === interviewId ? interviewSummary(updated) : item),
  } : candidate);
  return withCounts(next);
}

export function addTalentMemberships(state, { candidateIds, poolId, actor = "张小北" }) {
  const next = cloneState(state);
  const pool = next.pools.find((item) => item.id === poolId);
  if (!pool) return state;
  const additions = candidateIds.filter((id) => next.candidates.some((candidate) => candidate.id === id) && !next.memberships.some((membership) => membership.poolId === poolId && membership.candidateId === id));
  next.memberships = [
    ...next.memberships,
    ...additions.map((id, index) => {
      const candidate = next.candidates.find((item) => item.id === id);
      return {
        id: `MEM-UX08-${id}-${index}`,
        poolId,
        candidateId: id,
        suitableRoles: [candidate.position],
        tags: candidate.tags || [],
        owner: actor,
        joinedAt: "2026-07-12",
        reason: "从候选人流程加入人才库",
        source: `${candidate.position}申请`,
        nextContact: "2026-07-19",
        retentionUntil: "2028-07-11",
        recentInteraction: nowLabel(),
        latestConclusion: candidate.humanConclusion || candidate.recommendation || "待补充",
        status: "正常",
      };
    }),
  ];
  next.pools = next.pools.map((item) => item.id === poolId ? { ...item, memberIds: [...new Set([...(item.memberIds || []), ...additions])], recentActivity: nowLabel(), activity: `${actor}新增了 ${additions.length} 位人才` } : item);
  next.candidates = next.candidates.map((candidate) => additions.includes(candidate.id) ? { ...candidate, timeline: [{ time: nowLabel(), actor, action: `加入人才库：${pool.name}` }, ...(candidate.timeline || [])] } : candidate);
  return next;
}

export function reactivateTalentCandidate(state, { candidateId: id, position, poolId, resumeVersion, actor = "张小北" }) {
  const candidate = state.candidates.find((item) => item.id === id);
  if (!candidate || !position) return { state, created: false, reason: "not-found" };
  const duplicate = (candidate.applications || []).some((application) => application.position === position.name && ACTIVE_STAGES.has(application.state));
  if (duplicate) return { state, created: false, reason: "active-duplicate" };
  const sourceApplication = candidate.applications?.[0];
  const createdApplication = {
    id: `APP-REACT-${id}-${position.id}`,
    position: position.name,
    state: "新简历",
    created: "2026-07-12",
    source: "人才库重新激活",
    linkedPoolId: poolId,
    resumeVersion,
    sourceApplicationId: sourceApplication?.id || null,
  };
  const next = cloneState(state);
  next.candidates = next.candidates.map((item) => item.id === id ? {
    ...item,
    applications: [createdApplication, ...(item.applications || [])],
    position: position.name,
    stage: "新简历",
    owner: position.owner,
    lastActivity: nowLabel(),
    timeline: [{ time: nowLabel(), actor, action: `从人才库重新激活到${position.name}；保留历史申请` }, ...(item.timeline || [])],
  } : item);
  next.pools = next.pools.map((pool) => pool.id === poolId ? { ...pool, recentActivity: nowLabel(), activity: `重新激活候选人到${position.name}` } : pool);
  return { state: withCounts(next), created: true, application: createdApplication };
}

export function validateWorkflowState(state) {
  const errors = [];
  const candidateIds = new Set(state.candidates.map((candidate) => candidate.id));
  const poolIds = new Set(state.pools.map((pool) => pool.id));
  for (const interview of state.interviews) {
    if (!candidateIds.has(interview.candidateId)) errors.push(`面试 ${interview.id} 引用了不存在的候选人 ${interview.candidateId}`);
  }
  for (const membership of state.memberships) {
    if (!candidateIds.has(membership.candidateId)) errors.push(`人才关系 ${membership.id} 引用了不存在的候选人 ${membership.candidateId}`);
    if (!poolIds.has(membership.poolId)) errors.push(`人才关系 ${membership.id} 引用了不存在的人才库 ${membership.poolId}`);
  }
  for (const candidate of state.candidates) {
    if (!ALLOWED_STAGES.has(candidate.stage)) errors.push(`候选人 ${candidate.id} 的阶段 ${candidate.stage} 无效`);
    const activePositions = (candidate.applications || []).filter((application) => ACTIVE_STAGES.has(application.state)).map((application) => application.position);
    if (new Set(activePositions).size !== activePositions.length) errors.push(`候选人 ${candidate.id} 存在同职位重复活跃申请`);
  }
  const expectedPositions = recalculatePositionCounts(state.positions, state.candidates);
  expectedPositions.forEach((expected, index) => {
    const actual = state.positions[index];
    if (actual.candidates !== expected.candidates || actual.review !== expected.review || actual.interview !== expected.interview) errors.push(`职位 ${actual.id} 的候选人计数与共享状态不一致`);
  });
  return errors;
}
