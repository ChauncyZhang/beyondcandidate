function safeString(value) {
  return typeof value === "string" ? value : "";
}

function safeObject(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

export const FEISHU_ONBOARDING_FIELDS = Object.freeze([
  { key: "candidate_name", label: "姓名", defaultType: "input" },
  { key: "gender", label: "性别", defaultType: "radio" },
  { key: "department", label: "部门", defaultType: "department" },
  { key: "job_title", label: "职位", defaultType: "input" },
  { key: "phone", label: "手机号", defaultType: "telephone" },
  { key: "email", label: "邮箱", defaultType: "input" },
  { key: "home_address", label: "家庭住址", defaultType: "textarea" },
]);

export const FEISHU_APPROVAL_CONTROL_TYPES = Object.freeze([
  { value: "input", label: "单行文本" },
  { value: "textarea", label: "多行文本" },
  { value: "radio", label: "单选" },
  { value: "radioV2", label: "单选（新版）" },
  { value: "department", label: "部门" },
  { value: "telephone", label: "电话" },
  { value: "date", label: "日期" },
]);

export function buildFeishuConfigPayload(draft) {
  const payload = {
    app_id: safeString(draft?.app_id).trim(),
    redirect_uri: safeString(draft?.redirect_uri).trim(),
    calendar_id: safeString(draft?.calendar_id).trim() || "primary",
    enabled: draft?.enabled === true,
  };
  for (const key of ["app_secret", "verification_token", "encrypt_key"]) {
    const value = safeString(draft?.[key]);
    if (value) payload[key] = value;
  }
  return payload;
}

export function normalizeFeishuConfig(value = {}) {
  return {
    configured: value.configured === true,
    appId: safeString(value.app_id),
    redirectUri: safeString(value.redirect_uri),
    calendarId: safeString(value.calendar_id),
    enabled: value.enabled === true,
    appSecretConfigured: value.app_secret_configured === true,
    verificationTokenConfigured: value.verification_token_configured === true,
    encryptKeyConfigured: value.encrypt_key_configured === true,
    version: Number.isInteger(value.version) ? value.version : 0,
    lastTestStatus: safeString(value.last_test_status),
    lastTestedAt: safeString(value.last_tested_at),
    lastTestErrorCode: safeString(value.last_test_error_code),
  };
}

function normalizeFieldMapping(value) {
  const source = safeObject(value);
  return Object.fromEntries(FEISHU_ONBOARDING_FIELDS.map((field) => {
    const mapping = safeObject(source[field.key]);
    return [field.key, {
      controlId: safeString(mapping.control_id ?? mapping.controlId),
      type: safeString(mapping.type) || field.defaultType,
      ...(field.key === "gender" ? {
        options: {
          male: safeString(mapping.options?.male) || "男",
          female: safeString(mapping.options?.female) || "女",
        },
      } : {}),
    }];
  }));
}

function normalizeDepartmentMappings(value) {
  if (Array.isArray(value)) return value.map((item) => ({
    departmentId: safeString(item?.department_id ?? item?.departmentId),
    departmentName: safeString(item?.department_name ?? item?.departmentName),
    feishuDepartmentId: safeString(item?.feishu_department_id ?? item?.feishuDepartmentId),
  })).filter((item) => item.departmentId);
  return Object.entries(safeObject(value)).map(([departmentId, feishuDepartmentId]) => ({
    departmentId: safeString(departmentId),
    departmentName: "",
    feishuDepartmentId: safeString(feishuDepartmentId),
  })).filter((item) => item.departmentId);
}

export function normalizeFeishuOnboardingApprovalConfig(value = {}) {
  return {
    enabled: value.enabled === true,
    approvalCode: safeString(value.approval_code ?? value.approvalCode),
    fieldMapping: normalizeFieldMapping(value.field_mapping ?? value.fieldMapping),
    departmentMappings: normalizeDepartmentMappings(value.department_mappings ?? value.departmentMappings),
    nodeApprovers: (value.node_approvers ?? value.nodeApprovers ?? []).map((item) => ({
      nodeId: safeString(item.node_id ?? item.nodeId),
      userIds: [...(item.user_ids ?? item.userIds ?? [])],
    })),
    approvalNodes: value.approval_nodes ?? value.approvalNodes ?? null,
    validationStatus: safeString(value.validation_status ?? value.validationStatus) || "unvalidated",
    validatedAt: safeString(value.validated_at ?? value.validatedAt),
    validationSafeErrorCode: safeString(value.validation_safe_error_code ?? value.validationSafeErrorCode),
    version: Number.isInteger(value.version) ? value.version : 0,
  };
}

export function normalizeFeishuDepartmentDirectory(value = []) {
  if (!Array.isArray(value)) return [];
  const seen = new Set();
  return value.map((item) => ({
    openDepartmentId: safeString(item?.open_department_id ?? item?.openDepartmentId).trim(),
    name: safeString(item?.name).trim(),
  })).filter((item) => {
    if (!item.openDepartmentId.startsWith("od-") || !item.name || seen.has(item.openDepartmentId)) return false;
    seen.add(item.openDepartmentId);
    return true;
  }).sort((left, right) => left.name.localeCompare(right.name, "zh-CN") || left.openDepartmentId.localeCompare(right.openDepartmentId));
}

export function autoMatchFeishuDepartmentMappings(mappings = [], directory = []) {
  const candidatesByName = new Map();
  for (const department of normalizeFeishuDepartmentDirectory(directory)) {
    const key = department.name.normalize("NFKC").trim().toLocaleLowerCase("zh-CN");
    candidatesByName.set(key, [...(candidatesByName.get(key) || []), department]);
  }
  let matchedCount = 0;
  const nextMappings = (Array.isArray(mappings) ? mappings : []).map((mapping) => {
    if (safeString(mapping?.feishuDepartmentId).trim()) return mapping;
    const key = safeString(mapping?.departmentName).normalize("NFKC").trim().toLocaleLowerCase("zh-CN");
    const candidates = candidatesByName.get(key) || [];
    if (candidates.length !== 1) return mapping;
    matchedCount += 1;
    return { ...mapping, feishuDepartmentId: candidates[0].openDepartmentId };
  });
  return { mappings: nextMappings, matchedCount };
}

export function buildFeishuOnboardingApprovalPayload(draft) {
  const fieldMapping = safeObject(draft?.fieldMapping ?? draft?.field_mapping);
  const departmentMappings = Array.isArray(draft?.departmentMappings ?? draft?.department_mappings)
    ? (draft.departmentMappings ?? draft.department_mappings)
    : [];
  return {
    enabled: draft?.enabled === true,
    approval_code: safeString(draft?.approvalCode ?? draft?.approval_code).trim(),
    node_approvers: (draft?.nodeApprovers ?? draft?.node_approvers ?? []).map((item) => ({
      node_id: safeString(item.nodeId ?? item.node_id).trim(),
      user_ids: [...(item.userIds ?? item.user_ids ?? [])].sort(),
    })).filter((item) => item.user_ids.length).sort((left, right) => left.node_id.localeCompare(right.node_id)),
    field_mapping: Object.fromEntries(FEISHU_ONBOARDING_FIELDS.map((field) => {
      const mapping = safeObject(fieldMapping[field.key]);
      return [field.key, {
        control_id: safeString(mapping.controlId ?? mapping.control_id).trim(),
        type: safeString(mapping.type).trim() || field.defaultType,
        ...(field.key === "gender" ? {
          options: {
            male: safeString(mapping.options?.male).trim(),
            female: safeString(mapping.options?.female).trim(),
          },
        } : {}),
      }];
    })),
    department_mappings: departmentMappings.map((item) => ({
      department_id: safeString(item?.departmentId ?? item?.department_id).trim(),
      feishu_department_id: safeString(item?.feishuDepartmentId ?? item?.feishu_department_id).trim(),
    })).filter((item) => item.department_id && item.feishu_department_id),
  };
}

export function getFeishuApprovalValidationDisabledReason(draft, status, directoryStatus) {
  const pending = {
    loading: "正在加载入职审批配置，请稍候。",
    saving: "正在保存入职审批配置，请稍候。",
    validating: "正在校验审批模板，请稍候。",
    refreshing: "正在刷新配置版本，请稍候。",
    creating_department: "正在创建部门，请稍候。",
  };
  if (pending[status]) return pending[status];
  if (directoryStatus === "loading") return "正在读取并匹配飞书部门，请稍候。";
  const payload = buildFeishuOnboardingApprovalPayload(draft);
  if (!payload.approval_code) return "请先填写 Approval Code。";
  const missing = FEISHU_ONBOARDING_FIELDS.filter((field) => !payload.field_mapping[field.key].control_id);
  if (missing.length) return `请填写以下字段的控件 ID：${missing.map((field) => field.label).join("、")}。`;
  const { male, female } = payload.field_mapping.gender.options;
  if (!male || !female) return "请填写性别选项男、女对应的值。";
  if (male === female) return "男、女必须填写不同的性别选项值。";
  return "";
}

export function getFeishuOnboardingApprovalErrorMessage(errorOrCode) {
  const code = typeof errorOrCode === "string" ? errorOrCode : errorOrCode?.code;
  const messages = {
    feishu_onboarding_not_configured: "请先保存并启用飞书集成，再读取和校验入职审批模板。",
    feishu_approval_control_type_mismatch: "控件类型与飞书模板不一致，请核对性别、手机号等字段的实际控件类型。",
    feishu_approval_node_metadata_invalid: "无法读取审批节点，请检查飞书模板后重新读取。",
    feishu_approval_node_metadata_missing: "审批节点信息尚未读取，请重新校验审批模板。",
    feishu_approval_node_approvers_required: "请为每个需要指定审批人的节点选择固定审批人。",
    feishu_approver_required: "请为每个需要指定审批人的节点选择固定审批人。",
    feishu_approver_node_invalid: "审批节点已变更，请重新读取节点并配置审批人。",
    feishu_approver_selection_invalid: "审批人配置格式无效，请重新选择成员。",
    feishu_approver_multiple_not_allowed: "该审批节点只允许选择一名审批人，请调整名单。",
    feishu_approval_approvers_invalid: "审批人配置无效，请重新选择并校验。",
    feishu_approver_unbound: "所选审批人尚未绑定飞书账号，请完成绑定或重新选择。",
    feishu_approver_unavailable: "所选审批人已停用或不可用，请重新选择。",
    feishu_approval_control_unsupported: "审批模板包含不支持的入职专用控件，请改用普通文本、单选、部门、电话和日期控件。",
    feishu_approval_option_metadata_missing: "无法读取性别控件的选项，请确认使用普通单选控件后重新校验。",
    feishu_approval_option_invalid: "性别选项 ID 与飞书模板不一致，请重新填写男、女对应的选项 ID。",
    feishu_approval_option_duplicate: "男、女必须分别映射到两个不同的飞书选项 ID。",
    feishu_approval_permission_denied: "飞书应用缺少审批定义读取或审批实例创建权限，请补充权限并发布最新版本。",
    feishu_onboarding_initiator_unbound: "办理入职的 HR 必须先在个人设置中绑定飞书账号。",
    feishu_department_unmapped: "仍有本地部门未选择对应的飞书部门，请补充后再校验。",
    feishu_approval_definition_invalid: "Approval Code 或控件映射与飞书审批模板不一致，请核对后重试。",
    feishu_onboarding_config_invalid: "入职审批配置未通过校验，请核对模板和部门映射。",
    feishu_onboarding_validation_required: "请先保存配置并校验审批模板，再启用入职审批。",
    feishu_config_changed: "配置已被其他管理员更新，请刷新后重新修改。",
    precondition_required: "当前配置版本已失效，请刷新后重新修改。",
    resource_version_conflict: "配置已被其他管理员更新，请刷新后重新修改。",
  };
  if (messages[code]) return messages[code];
  if (errorOrCode?.status === 403) return "当前账号没有管理飞书入职审批的权限。";
  if (errorOrCode?.status === 422) return "配置内容不完整或格式错误，请检查后重试。";
  return "飞书入职审批配置暂时无法处理，请稍后重试。";
}

export function getFeishuApproverSelectionError(draft, options) {
  const selections = buildFeishuOnboardingApprovalPayload(draft).node_approvers;
  const nodes = options.nodes.filter((node) => node.need_approver);
  const members = new Map(options.members.map((member) => [member.user_id, member]));
  if (selections.some((selection) => !nodes.some((node) => node.node_id === selection.node_id))) {
    return "已保存的审批节点已变更，请清除失效节点并重新选择审批人。";
  }
  for (const node of nodes) {
    const selected = selections.find((item) => item.node_id === node.node_id)?.user_ids || [];
    if (!selected.length) return `请为“${node.name || "审批"}”选择固定审批人。`;
    if (!node.allow_multiple && selected.length > 1) return `“${node.name || "审批"}”只能选择一名审批人。`;
    if (selected.some((id) => members.get(id)?.available !== true)) return `“${node.name || "审批"}”包含不可用审批人，请重新选择。`;
  }
  return "";
}

export function getFeishuDepartmentDirectoryErrorMessage(error) {
  if (error?.code === "feishu_not_configured") return "请先保存飞书 App ID 和 App Secret，再读取飞书部门。";
  if (error?.status === 403) return "当前账号没有读取飞书部门配置的权限。";
  return "无法读取飞书通讯录部门，请确认应用已开通通讯录部门读取权限，且权限范围包含需要映射的部门。";
}

export function normalizeFeishuBinding(value = {}) {
  if (value.bound !== true) return { bound: false, unionId: "", openId: "" };
  return { bound: true, unionId: safeString(value.union_id), openId: safeString(value.open_id) };
}

export function getFeishuLoginErrorMessage(error) {
  if (error?.code === "feishu_disabled") {
    return "当前组织尚未启用飞书登录，请联系管理员前往“设置 → 飞书集成”完成配置并启用。";
  }
  if (error?.code === "feishu_account_not_invited_or_bound") {
    return "当前飞书账号尚未绑定。已有账号请先使用密码登录，再到“个人设置 → 飞书账号”完成绑定；新用户请联系管理员邀请。";
  }
  return "飞书登录服务暂时不可用，请稍后重试。";
}

export function getFeishuCallbackErrorCode(search = globalThis.location?.search || "") {
  return safeString(new URLSearchParams(search).get("feishu_error")).trim();
}

export function getFeishuConfigErrorMessage(error) {
  if (error?.status === 422 || error?.code === "feishu_secret_required") {
    return "配置格式不正确，请检查必填项后重试。当前输入已保留。";
  }
  if (error?.status === 403 || error?.code === "forbidden") {
    return "当前账号没有管理飞书集成的权限。当前输入已保留。";
  }
  return "飞书配置暂时无法保存，请稍后重试。当前输入已保留。";
}

export function getFeishuConnectionTestErrorMessage(error) {
  if (error?.code === "feishu_test_user_unbound") {
    return "当前账号尚未绑定飞书，请先到“个人设置 → 飞书账号”完成绑定，再发送测试提醒。";
  }
  if (["feishu_request_failed", "feishu_response_invalid"].includes(error?.code)) {
    return "测试消息发送失败，请确认飞书应用已开启机器人能力和“以应用身份发消息”权限，并已发布最新版本。";
  }
  return "飞书凭据或消息服务暂时不可用，现有招聘功能不受影响。";
}

export async function startFeishuAuthorization(authorize, navigate = (url) => window.location.assign(url)) {
  const result = await authorize();
  const authorizationUrl = new URL(result?.authorization_url || "");
  if (authorizationUrl.protocol !== "https:" || authorizationUrl.hostname !== "accounts.feishu.cn") {
    throw new Error("invalid_feishu_authorization_url");
  }
  navigate(authorizationUrl.toString());
  return authorizationUrl.toString();
}
