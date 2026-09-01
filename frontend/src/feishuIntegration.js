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
    validationStatus: safeString(value.validation_status ?? value.validationStatus) || "unvalidated",
    validatedAt: safeString(value.validated_at ?? value.validatedAt),
    validationSafeErrorCode: safeString(value.validation_safe_error_code ?? value.validationSafeErrorCode),
    version: Number.isInteger(value.version) ? value.version : 0,
  };
}

export function buildFeishuOnboardingApprovalPayload(draft) {
  const fieldMapping = safeObject(draft?.fieldMapping ?? draft?.field_mapping);
  const departmentMappings = Array.isArray(draft?.departmentMappings ?? draft?.department_mappings)
    ? (draft.departmentMappings ?? draft.department_mappings)
    : [];
  return {
    enabled: draft?.enabled === true,
    approval_code: safeString(draft?.approvalCode ?? draft?.approval_code).trim(),
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
    })).filter((item) => item.department_id),
  };
}

export function getFeishuOnboardingApprovalErrorMessage(errorOrCode) {
  const code = typeof errorOrCode === "string" ? errorOrCode : errorOrCode?.code;
  const messages = {
    feishu_approval_control_unsupported: "审批模板包含不支持的入职专用控件，请改用普通文本、单选、部门、电话和日期控件。",
    feishu_approval_option_metadata_missing: "无法读取性别控件的选项，请确认使用普通单选控件后重新校验。",
    feishu_approval_option_invalid: "性别选项 ID 与飞书模板不一致，请重新填写男、女对应的选项 ID。",
    feishu_approval_option_duplicate: "男、女必须分别映射到两个不同的飞书选项 ID。",
    feishu_approval_permission_denied: "飞书应用缺少审批定义读取或审批实例创建权限，请补充权限并发布最新版本。",
    feishu_onboarding_initiator_unbound: "办理入职的 HR 必须先在个人设置中绑定飞书账号。",
    feishu_department_unmapped: "仍有本地部门未填写飞书部门 ID，请补充后再校验。",
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
