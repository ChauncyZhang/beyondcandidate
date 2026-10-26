import { useEffect, useRef } from "react";
import { selectUpcomingInterviews } from "./interviewDateUtils.js";

const EMPTY = [];
const ACTIVE_STATUSES = new Set(["已安排", "已确认", "scheduled", "rescheduled", "confirmed"]);
const RETRY_MS = 30000;
const MAX_TIMEOUT = 2147483647;

export function useInterviewAutoRefresh({
  enabled = true, navigationKey, records = EMPTY, upcomingRecords = EMPTY,
  tasks = EMPTY, candidateRecords = EMPTY, setInterviewState, setCandidateRecords, controller,
}) {
  const attempts = useRef(new Map());
  const unavailable = useRef(new Set());
  const taskRetryAt = useRef(Infinity);
  const latest = useRef(null);
  latest.current = { records, upcomingRecords, tasks, candidateRecords, controller, enabled, navigationKey };

  useEffect(() => {
    if (!enabled || typeof controller?.get !== "function") return undefined;
    const abortController = new AbortController();
    let active = true;
    let timer;
    let inFlight = false;
    const isCurrent = () => {
      const value = latest.current;
      return active && value.records === records && value.upcomingRecords === upcomingRecords
        && value.tasks === tasks && value.candidateRecords === candidateRecords && value.controller === controller
        && value.enabled === enabled && value.navigationKey === navigationKey;
    };
    const keepAvailable = items => items.filter(item => !unavailable.current.has(item.interviewId || item.id));
    if ([...records, ...upcomingRecords, ...candidateRecords, ...tasks].some(item => unavailable.current.has(item.interviewId || item.id))) {
      setInterviewState(current => current.records === records && (current.upcomingRecords || EMPTY) === upcomingRecords && (current.tasks || EMPTY) === tasks ? {
        ...current, records: keepAvailable(current.records),
        ...(Array.isArray(current.upcomingRecords) ? { upcomingRecords: keepAvailable(current.upcomingRecords) } : {}),
        ...(Array.isArray(current.tasks) ? { tasks: keepAvailable(current.tasks) } : {}),
      } : current);
      setCandidateRecords?.(current => current === candidateRecords ? keepAvailable(current) : current);
      return undefined;
    }
    const deadlines = new Map();
    const sources = new Map();
    for (const item of [...records, ...upcomingRecords, ...candidateRecords]) {
      if (!item.id || item.serverBacked !== true) continue;
      const prior = sources.get(item.id);
      if (!prior || (item.version ?? -1) > (prior.version ?? -1)
        || ((item.version ?? -1) === (prior.version ?? -1) && Date.parse(item.endsAt) > Date.parse(prior.endsAt))) sources.set(item.id, item);
    }
    for (const item of [...sources.values(), ...tasks.filter(item => !sources.has(item.interviewId))]) {
      const task = Boolean(item.interviewId);
      const id = task ? item.interviewId : item.id;
      const end = Date.parse(item.endsAt);
      if (!id || (!task && item.serverBacked !== true) || !ACTIVE_STATUSES.has(item.status) || !Number.isFinite(end)) continue;
      const key = `${id}:${item.version ?? ""}:${item.endsAt}:${item.status}`;
      const existing = deadlines.get(id);
      if (!existing || end > existing.end) deadlines.set(id, { id, end, key });
    }
    const keys = new Set([...deadlines.values()].map(item => item.key));
    for (const key of attempts.current.keys()) if (!keys.has(key)) attempts.current.delete(key);

    function schedule() {
      window.clearTimeout(timer);
      if (!active || document.hidden || inFlight) return;
      const next = Math.min(taskRetryAt.current, ...[...deadlines.values()].map(item => Math.max(item.end, (attempts.current.get(item.key) ?? -Infinity) + RETRY_MS)));
      if (!Number.isFinite(next)) return;
      timer = window.setTimeout(() => void refresh(), Math.min(MAX_TIMEOUT, Math.max(0, next - Date.now())));
    }

    async function refresh() {
      if (!active || document.hidden || inFlight) return;
      const now = Date.now();
      const due = [...deadlines.values()].filter(item => item.end <= now && now >= (attempts.current.get(item.key) ?? -Infinity) + RETRY_MS);
      const readTasks = tasks !== EMPTY && typeof controller.listMyTasks === "function" && (due.length > 0 || taskRetryAt.current <= now);
      if (!due.length && !readTasks) { schedule(); return; }
      inFlight = true;
      for (const item of due) attempts.current.set(item.key, now);
      try {
        const options = { signal: abortController.signal };
        const settled = await Promise.allSettled([
          ...due.map(item => Promise.resolve().then(() => controller.get(item.id, options))),
          ...(readTasks ? [Promise.resolve().then(() => controller.listMyTasks(options))] : []),
        ]);
        if (!isCurrent()) return;
        const byId = new Map();
        for (let index = 0; index < due.length; index += 1) {
          const result = settled[index];
          if (result.status === "fulfilled" && result.value) byId.set(result.value.id, result.value);
          else if (result.status === "rejected" && [403, 404].includes(result.reason?.status)) {
            unavailable.current.add(due[index].id);
            deadlines.delete(due[index].id);
          }
        }
        const taskResult = readTasks ? settled.at(-1) : null;
        const nextTasks = taskResult?.status === "fulfilled" && Array.isArray(taskResult.value) ? taskResult.value : null;
        if (readTasks) taskRetryAt.current = nextTasks ? Infinity : Date.now() + RETRY_MS;
        const merge = items => keepAvailable(items).map(item => {
          const next = byId.get(item.id);
          if (!next || (Number.isInteger(item.version) && Number.isInteger(next.version) && next.version < item.version)) return item;
          return { ...item, ...next };
        });
        // Array identity guards also cover mutations queued before effect cleanup.
        setInterviewState(current => {
          if (current.records !== records || (current.upcomingRecords || EMPTY) !== upcomingRecords || (current.tasks || EMPTY) !== tasks) return current;
          return {
            ...current,
            records: merge(current.records),
            ...(Array.isArray(current.upcomingRecords) ? { upcomingRecords: merge(current.upcomingRecords).filter(item => !byId.has(item.id) || selectUpcomingInterviews([item]).length > 0) } : {}),
            ...(Array.isArray(current.tasks) ? { tasks: keepAvailable(nextTasks || current.tasks) } : {}),
          };
        });
        setCandidateRecords?.(current => current === candidateRecords ? merge(current) : current);
      } catch {
        // Keep the last readable state; retry only these overdue IDs, not the list.
      } finally {
        inFlight = false;
        schedule();
      }
    }

    function onVisibilityChange() {
      if (document.hidden) window.clearTimeout(timer);
      else void refresh();
    }
    document.addEventListener("visibilitychange", onVisibilityChange);
    schedule();
    return () => {
      active = false;
      window.clearTimeout(timer);
      abortController.abort();
      document.removeEventListener("visibilitychange", onVisibilityChange);
    };
  }, [candidateRecords, controller, enabled, navigationKey, records, setCandidateRecords, setInterviewState, tasks, upcomingRecords]);
}
