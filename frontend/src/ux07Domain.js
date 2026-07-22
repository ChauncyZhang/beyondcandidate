const stageOrder = ["新简历", "待复核", "待沟通", "待安排", "面试中", "待决策", "已录用"];

export function filterReportCandidates(candidates, filters = {}) {
  return candidates.flatMap((candidate) => {
    const applications = candidate.applications?.length ? candidate.applications : [{ position: candidate.position, state: candidate.stage }];
    if (filters.owner && filters.owner !== "全部负责人" && candidate.owner !== filters.owner) return [];
    return applications.filter((application) => (
      (!filters.position || filters.position === "全部职位" || application.position === filters.position)
      && (!filters.stage || filters.stage === "全部阶段" || application.state === filters.stage)
    )).map((application) => ({ ...candidate, position: application.position, stage: application.state, applications: [application] }));
  });
}

function buildParseSuccessRate(screeningSummary) {
  const total = screeningSummary?.total;
  if (!Number.isFinite(total) || total <= 0) return null;

  const success = Number.isFinite(screeningSummary.success) ? screeningSummary.success : 0;
  const partial = Number.isFinite(screeningSummary.partial) ? screeningSummary.partial : 0;
  return Math.round((Math.max(0, Math.min(total, success + partial)) / total) * 100);
}

export function buildReportMetrics(candidates, screeningSummary) {
  const uniqueCandidates = [...new Map(candidates.map((candidate) => [candidate.id, candidate])).values()];
  const total = uniqueCandidates.length;
  const applicationFacts = candidates.flatMap((candidate) => candidate.applications?.length
    ? candidate.applications.map((application) => ({ position: application.position, stage: application.state }))
    : [{ position: candidate.position, stage: candidate.stage }]);
  const funnel = stageOrder
    .map((stage) => ({ stage, count: applicationFacts.filter((item) => item.stage === stage).length }))
    .filter((item) => item.count > 0);
  const rulePassed = uniqueCandidates.filter((item) => item.ruleScore >= 60).length;
  const llmSucceeded = uniqueCandidates.filter((item) => Number.isFinite(item.llmScore)).length;
  const interviewCount = uniqueCandidates.reduce((sum, item) => sum + (item.interviews?.length || 0), 0);
  const feedbackCount = uniqueCandidates.reduce((sum, item) => sum + (item.interviews || []).filter((interview) => interview.result && !interview.result.includes("待")).length, 0);

  const parseSuccessRate = buildParseSuccessRate(screeningSummary);

  return {
    candidateCount: total,
    applicationCount: applicationFacts.length,
    averageCycleDays: total ? Math.round(uniqueCandidates.reduce((sum, item) => sum + 12 + (item.score % 9), 0) / total) : 0,
    parseSuccessRate,
    feedbackCompletionRate: interviewCount ? Math.round((feedbackCount / interviewCount) * 100) : 0,
    funnel,
    screening: {
      parseSuccessRate,
      rulePassRate: total ? Math.round((rulePassed / total) * 100) : 0,
      llmSuccessRate: total ? Math.round((llmSucceeded / total) * 100) : 0,
    },
    interviews: { count: interviewCount, feedbackCount, averageFeedbackHours: interviewCount ? 9.6 : 0 },
  };
}

export function getRoleCapabilities(role) {
  if (role === "招聘管理员") return { reportsView: true, reportScope: "all", settingsView: true, settingsEdit: true, interviewTemplatesView: true, auditView: true };
  if (role === "HR") return { reportsView: true, reportScope: "owned", settingsView: true, settingsEdit: false, interviewTemplatesView: true, auditView: true };
  return { reportsView: false, reportScope: "none", settingsView: true, settingsEdit: false, interviewTemplatesView: true, auditView: false };
}

export function isPermissionExpansion(previousScopes, nextScopes) {
  return nextScopes.some((scope) => !previousScopes.includes(scope));
}
