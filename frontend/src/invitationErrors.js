export function invitationAcceptErrorMessage(error) {
  if (error?.kind === "unavailable") return "服务暂时不可用，请稍后重试。";
  if (error?.code === "invitation_invalid_or_expired") return "邀请无效或已过期，请联系管理员重新邀请，并使用最新链接。";
  if (error?.code === "csrf_validation_failed") return "安全验证失败，请刷新页面后重试。";
  if (error?.status === 429) return "请求过于频繁，请稍后重试。";
  if (error?.status === 422) return "密码设置未通过验证，请检查密码后重试。";
  return "密码设置失败，请稍后重试。";
}

export function reinvitationErrorMessage(error) {
  if (error?.code === "user_not_found") return "成员不存在，请刷新成员列表。";
  if (error?.code === "user_not_invited") return "成员已不处于待激活状态，请刷新成员列表。";
  if (error?.code === "role_assignment_forbidden" || error?.status === 403) return "无权重新邀请该成员，请联系系统管理员。";
  if (error?.kind === "unavailable") return "邀请暂时无法重新生成，请稍后重试。";
  return "邀请重新生成失败，请稍后重试。";
}
