export function invitationAcceptErrorMessage(error) {
  if (error?.kind === "unavailable") return "服务暂时不可用，请稍后重试。";
  if (error?.code === "invitation_invalid_or_expired") return "邀请无效、已使用或已过期。若尚未设置密码，请联系管理员重新邀请，并使用最新链接；若已设置密码，请直接登录，忘记密码请联系管理员重置。";
  if (error?.code === "csrf_validation_failed") return "安全验证失败，请刷新页面后重试。";
  if (error?.status === 429) return "请求过于频繁，请稍后重试。";
  if (error?.status === 422) return "密码设置未通过验证，请检查密码后重试。";
  return "密码设置失败，请稍后重试。";
}

export function passwordResetErrorMessage(error) {
  if (error?.code === "password_reset_invalid_or_expired") return "密码重置链接无效、已使用或已过期，请联系管理员重新生成，并使用最新链接。";
  if (error?.code === "user_not_found") return "成员不存在，请刷新成员列表。";
  if (error?.code === "user_not_active") return "成员已不处于启用状态，请刷新成员列表；待激活成员请使用重新邀请。";
  if (error?.code === "self_password_reset_forbidden") return "请在个人设置中修改自己的密码。";
  if (error?.code === "csrf_validation_failed") return "安全验证失败，请刷新页面后重试。";
  if (error?.status === 403) return "无权重置该成员的密码，请联系系统管理员。";
  if (error?.status === 429) return "请求过于频繁，请稍后重试。";
  if (error?.kind === "unavailable") return "密码重置服务暂时不可用，请稍后重试。";
  if (error?.status === 422) return "密码未通过验证，请使用 12 至 128 个字符。";
  return "密码重置未完成，请稍后重试。";
}

export function reinvitationErrorMessage(error) {
  if (error?.code === "user_not_found") return "成员不存在，请刷新成员列表。";
  if (error?.code === "user_not_invited") return "成员已不处于待激活状态，请刷新成员列表。";
  if (error?.code === "role_assignment_forbidden" || error?.status === 403) return "无权重新邀请该成员，请联系系统管理员。";
  if (error?.kind === "unavailable") return "邀请暂时无法重新生成，请稍后重试。";
  return "邀请重新生成失败，请稍后重试。";
}
