const candidateTabSlugs = Object.freeze({
  "档案与简历": "profile",
  "职位申请": "applications",
  "筛选证据": "evidence",
  "面试与反馈": "interviews",
  "时间线": "timeline",
});

const candidateTabsBySlug = Object.freeze(Object.fromEntries(
  Object.entries(candidateTabSlugs).map(([label, slug]) => [slug, label]),
));

const settingsRoutes = Object.freeze({
  "组织与权限": Object.freeze({
    成员: "/settings/organization/members",
    部门: "/settings/organization/departments",
  }),
  "流程与评价模板": Object.freeze({
    招聘流程: "/settings/templates/workflows",
    淘汰原因: "/settings/templates/rejection-reasons",
    面试评价模板: "/settings/templates/interview-scorecards",
  }),
  "AI 设置": "/settings/ai",
  "飞书集成": "/settings/feishu",
  "审计与数据治理": "/settings/governance",
});

const navRoutes = Object.freeze({
  工作台: "/workbench",
  职位: "/jobs",
  筛选任务: "/screening/tasks",
  候选人: "/candidates",
  面试: "/interviews",
  人才库: "/talent",
  报表: "/reports",
  设置: "/settings/organization/members",
});

function pathnameOf(location) {
  return location?.pathname?.replace(/\/+$/, "") || "/";
}

function safeReturnTo(searchParams) {
  const value = searchParams.get("return") || "";
  return value.startsWith("/") && !value.startsWith("//") ? value : undefined;
}

export function parseAppRoute(location) {
  const pathname = pathnameOf(location);
  const searchParams = new URLSearchParams(location?.search || "");
  const base = { searchParams, returnTo: safeReturnTo(searchParams) };

  if (pathname === "/workbench") return { ...base, kind: "workbench", nav: "工作台" };
  if (pathname === "/jobs") return { ...base, kind: "jobs", nav: "职位", mode: "list" };
  if (pathname === "/jobs/new") return { ...base, kind: "jobs", nav: "职位", mode: "new" };
  let match = pathname.match(/^\/jobs\/([^/]+)(?:\/(edit))?$/);
  if (match) return { ...base, kind: "jobs", nav: "职位", mode: match[2] ? "edit" : "detail", id: decodeURIComponent(match[1]) };

  if (pathname === "/screening/tasks") return { ...base, kind: "screening", nav: "筛选任务", mode: "list" };
  match = pathname.match(/^\/screening\/tasks\/([^/]+)$/);
  if (match) {
    const statusBySlug = { processing: "处理中", success: "成功", partial: "部分成功", failed: "失败" };
    return { ...base, kind: "screening", nav: "筛选任务", mode: "detail", id: decodeURIComponent(match[1]), query: searchParams.get("q")?.trim() || "", status: statusBySlug[searchParams.get("status")] || "全部" };
  }

  if (pathname === "/candidates") {
    return {
      ...base,
      kind: "candidates",
      nav: "候选人",
      mode: "list",
      filters: {
        q: searchParams.get("q")?.trim() || "",
        jobId: searchParams.get("job") || "全部职位",
        stage: searchParams.get("stage") || "全部阶段",
        ownerId: searchParams.get("owner") || "全部负责人",
        minScore: searchParams.get("minScore") || "不限分数",
      },
    };
  }
  match = pathname.match(/^\/candidates\/([^/]+)$/);
  if (match) return { ...base, kind: "candidates", nav: "候选人", mode: "detail", id: decodeURIComponent(match[1]), tab: candidateTabsBySlug[searchParams.get("tab")] || "档案与简历" };

  if (pathname === "/interviews") return { ...base, kind: "interviews", nav: "面试", mode: "list" };
  if (pathname === "/interviews/new") return { ...base, kind: "interviews", nav: "面试", mode: "new", candidateId: searchParams.get("candidate") || undefined };
  match = pathname.match(/^\/interviews\/([^/]+)\/(reschedule|feedback)$/);
  if (match) return { ...base, kind: "interviews", nav: "面试", mode: match[2], id: decodeURIComponent(match[1]) };

  if (pathname === "/talent") return { ...base, kind: "talent", nav: "人才库", mode: "list" };
  match = pathname.match(/^\/talent\/([^/]+)$/);
  if (match) return { ...base, kind: "talent", nav: "人才库", mode: "detail", id: decodeURIComponent(match[1]) };
  if (pathname === "/reports") return { ...base, kind: "reports", nav: "报表" };

  const settings = [
    ["/settings/organization/members", "组织与权限", "成员"],
    ["/settings/organization/departments", "组织与权限", "部门"],
    ["/settings/templates/workflows", "流程与评价模板", "招聘流程"],
    ["/settings/templates/rejection-reasons", "流程与评价模板", "淘汰原因"],
    ["/settings/templates/interview-scorecards", "流程与评价模板", "面试评价模板"],
    ["/settings/ai", "AI 设置"],
    ["/settings/feishu", "飞书集成"],
    ["/settings/governance", "审计与数据治理"],
  ];
  const setting = settings.find(([path]) => path === pathname);
  if (setting) return { ...base, kind: "settings", nav: "设置", section: setting[1], ...(setting[2] ? { tab: setting[2] } : {}) };
  return { ...base, kind: "unknown", nav: null };
}

export function routeForNav(label) {
  return navRoutes[label] || "/workbench";
}

export function candidateListPath(filters = {}) {
  const params = new URLSearchParams();
  const query = filters.q?.trim();
  if (query) params.set("q", query);
  if (filters.jobId && filters.jobId !== "全部职位") params.set("job", filters.jobId);
  if (filters.stage && filters.stage !== "全部阶段") params.set("stage", filters.stage);
  if (filters.ownerId && filters.ownerId !== "全部负责人") params.set("owner", filters.ownerId);
  if (filters.minScore && filters.minScore !== "不限分数") params.set("minScore", String(filters.minScore));
  const search = params.toString();
  return `/candidates${search ? `?${search}` : ""}`;
}

export function candidateDetailPath(candidate, tab = "档案与简历", returnTo) {
  const id = candidate?.candidateId || candidate?.id;
  const params = new URLSearchParams();
  const slug = candidateTabSlugs[tab];
  if (slug && slug !== "profile") params.set("tab", slug);
  if (candidate?.applicationId) params.set("application", candidate.applicationId);
  if (candidate?.jobId) params.set("job", candidate.jobId);
  if (returnTo?.startsWith("/") && !returnTo.startsWith("//")) params.set("return", returnTo);
  const search = params.toString();
  return `/candidates/${encodeURIComponent(id)}${search ? `?${search}` : ""}`;
}

export function screeningTaskPath(id, viewState = {}) {
  const params = new URLSearchParams();
  const query = viewState.query?.trim();
  const statusSlugs = { "处理中": "processing", "成功": "success", "部分成功": "partial", "失败": "failed" };
  if (query) params.set("q", query);
  if (statusSlugs[viewState.status]) params.set("status", statusSlugs[viewState.status]);
  const search = params.toString();
  return `/screening/tasks/${encodeURIComponent(id)}${search ? `?${search}` : ""}`;
}

export function settingsPath(section, tab, returnTo) {
  const configured = settingsRoutes[section];
  const pathname = typeof configured === "string" ? configured : configured?.[tab] || navRoutes["设置"];
  if (!returnTo) return pathname;
  const params = new URLSearchParams({ return: returnTo });
  return `${pathname}?${params}`;
}

export function safeNavigateBack(navigate, fallback, historyState = globalThis.history?.state) {
  if (Number(historyState?.idx) > 0) {
    navigate(-1);
    return true;
  }
  navigate(fallback, { replace: true });
  return false;
}

function jobDraftKey(userId) {
  return userId ? `beyondcandidate:job-create-draft:${userId}` : "";
}

export function readJobCreateDraft(storage, userId) {
  const key = jobDraftKey(userId);
  if (!storage || !key) return null;
  try {
    const value = JSON.parse(storage.getItem(key));
    return value && typeof value === "object" && !Array.isArray(value) ? value : null;
  } catch {
    return null;
  }
}

export function writeJobCreateDraft(storage, userId, draft) {
  const key = jobDraftKey(userId);
  if (!storage || !key || !draft || typeof draft !== "object") return;
  try { storage.setItem(key, JSON.stringify(draft)); } catch { /* session storage may be unavailable */ }
}

export function clearJobCreateDraft(storage, userId) {
  const key = jobDraftKey(userId);
  if (!storage || !key) return;
  try { storage.removeItem(key); } catch { /* session storage may be unavailable */ }
}
