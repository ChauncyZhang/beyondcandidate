import { normalizeRules } from "./jobController.js";

const EMPTY_FILTERS = Object.freeze({
  q: "",
  status: "全部",
  departmentId: "",
  ownerId: "",
});

const EMPTY_STATUS_COUNTS = Object.freeze({
  草稿: 0,
  招聘中: 0,
  已暂停: 0,
  已关闭: 0,
  已归档: 0,
});

export function createInitialJobWorkspaceState() {
  return {
    status: "idle",
    records: [],
    nextCursor: null,
    departments: [],
    owners: [],
    statusCounts: { ...EMPTY_STATUS_COUNTS },
    filters: { ...EMPTY_FILTERS },
    error: "",
    requestId: 0,
  };
}

export function startJobRequest(state, requestId, filters) {
  return {
    ...state,
    status: "loading",
    filters: { ...EMPTY_FILTERS, ...filters },
    error: "",
    requestId,
  };
}

export function succeedJobRequest(state, requestId, page) {
  if (state.requestId !== requestId) return state;
  return {
    ...state,
    status: "ready",
    records: page.records,
    nextCursor: page.nextCursor,
    departments: page.departments,
    owners: page.owners,
    statusCounts: page.statusCounts,
    error: "",
  };
}

export function succeedJobMutationRefresh(state, requestId, page) {
  return succeedJobRequest(state, requestId, page);
}

export function appendJobPage(state, requestId, page) {
  if (state.requestId !== requestId) return state;
  const existingIds = new Set(state.records.map((record) => record.id));
  return {
    ...state,
    status: "ready",
    records: [...state.records, ...page.records.filter((record) => !existingIds.has(record.id))],
    nextCursor: page.nextCursor,
    departments: page.departments,
    owners: page.owners,
    statusCounts: page.statusCounts,
    error: "",
  };
}

export function failJobRequest(state, requestId, error) {
  if (state.requestId !== requestId) return state;
  return {
    ...state,
    status: "error",
    error: error instanceof Error ? error.message : String(error || ""),
  };
}

export function getJobDefinitionErrors(values) {
  const errors = {};
  const hiringManagerIds = Array.isArray(values?.hiringManagerIds) ? values.hiringManagerIds : [];
  if (!values?.name?.trim()) errors.name = "请输入职位名称";
  if (!values?.jd?.trim()) errors.jd = "请输入公开职位描述";
  if (!values?.process?.trim()) errors.process = "请输入招聘流程模板";
  // Keep these bounds aligned with JobDefinitionCommand and screening.rules.
  for (const [field, label, limit] of [["name", "职位名称", 200], ["jd", "公开 JD", 50000], ["location", "工作地点", 200], ["process", "招聘流程模板", 100]]) {
    if (Array.from(values?.[field]?.trim() || "").length > limit) errors[field] = `${label}不能超过 ${limit} 字`;
  }
  for (const [field, label] of [["mustHave", "必须条件"], ["niceToHave", "加分项"]]) {
    const rules = normalizeRules(values?.[field]);
    if (rules.length > 50) errors[field] = `${label}不能超过 50 条`;
    else if (rules.some((rule) => Array.from(rule).length > 100)) errors[field] = `${label}每条不能超过 100 字`;
  }
  if (values?.headcount !== undefined && (!Number.isInteger(values.headcount) || values.headcount < 1 || values.headcount > 1000)) errors.headcount = "招聘人数须为 1 至 1000 的整数";
  if (hiringManagerIds.length === 0) errors.hiringManagerIds = "请至少选择一位用人经理";
  else if (hiringManagerIds.length > 100) errors.hiringManagerIds = "用人经理不能超过 100 位";
  if (!values?.hiringOwnerId) errors.hiringOwnerId = "请选择默认评审人";
  else if (!hiringManagerIds.includes(values.hiringOwnerId)) errors.hiringOwnerId = "默认评审人必须来自已选用人经理";
  return errors;
}

export function getJobPublicationChecks(values, { workflowUnavailable = false } = {}) {
  const errors = getJobDefinitionErrors(values);
  return [
    ["name", "职位名称"],
    ["jd", "职位描述与筛选标准"],
    ["headcount", "招聘人数"],
    ["hiringManagerIds", "用人经理"],
    ["hiringOwnerId", "默认评审人"],
    ["process", "招聘流程"],
  ].map(([field, label]) => {
    const error = errors[field] || (field === "jd" && (errors.location || errors.mustHave || errors.niceToHave)) || (field === "process" && workflowUnavailable ? "请加载并选择可用的流程模板" : "");
    return { field, label, complete: !error, error };
  });
}

export async function retryJobRefresh(record, refresh) {
  try {
    const refreshed = await refresh(record);
    if (!refreshed) throw new Error("refresh unavailable");
    return { record: refreshed, refreshError: "" };
  } catch {
    return { record, refreshError: "最新数据加载失败，请重试读取。" };
  }
}

export async function commitJobMutation(mutate, refresh) {
  const record = await mutate();
  return retryJobRefresh(record, refresh);
}
