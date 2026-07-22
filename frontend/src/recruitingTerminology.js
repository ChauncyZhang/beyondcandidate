const APPLICATION_STAGE_LABELS = Object.freeze({
  "待复核": "待用人经理评审",
  "AI 初筛暂缓": "暂不进入评审",
  "待沟通": "待确认候选人意向",
  "待安排": "待安排面试",
  "面试中": "面试流程中",
  "待决策": "待用人经理录用决策",
  "已通过": "待录用确认",
  "已撤回": "候选人已退出流程",
});

const AI_RECOMMENDATION_LABELS = Object.freeze({
  "优先评审": "进入评审 · 高优先级",
  "建议评审": "进入评审",
  "暂缓": "暂不进入评审",
  "AI评分不可用": "AI评分失败 · 已保护性转交评审",
});

const INTERVIEW_STATUS_LABELS = Object.freeze({
  "待确认": "待 HR 确认排期",
  "已安排": "面试已安排",
  "已确认": "排期已确认",
  "已完成": "面试已完成",
  "待反馈": "待面试官反馈",
  "已提交": "反馈已提交",
  "待发送": "面试邀请未发送",
  "已发送": "面试邀请已发送",
  "发送失败": "面试邀请发送失败",
});

export function applicationStageLabel(stage) {
  return APPLICATION_STAGE_LABELS[stage] || stage;
}

export function aiRecommendationLabel(recommendation) {
  return AI_RECOMMENDATION_LABELS[recommendation] || recommendation;
}

export function interviewStatusLabel(status) {
  return INTERVIEW_STATUS_LABELS[status] || status;
}
