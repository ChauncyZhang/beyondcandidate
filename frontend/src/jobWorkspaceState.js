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
  if (!values?.name?.trim()) errors.name = "请输入职位名称";
  if (!values?.jd?.trim()) errors.jd = "请输入公开职位描述";
  if (!values?.process?.trim()) errors.process = "请输入招聘流程模板";
  return errors;
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
