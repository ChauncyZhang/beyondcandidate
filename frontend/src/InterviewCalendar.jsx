import { useEffect, useMemo, useState } from "react";
import { CalendarDays, ChevronLeft, ChevronRight, Clock3, Users } from "lucide-react";
import { buildWeekDays, moveWeek, parseLocalDate, weekLabel, weekRange } from "./interviewDateUtils.js";
import { interviewStatusLabel } from "./recruitingTerminology.js";

export function InterviewCalendar({ query, status, mineOnly, canSchedule, interviewerId, onLoadRange, onOpen }) {
  const [reference, setReference] = useState(new Date());
  const [selectedDay, setSelectedDay] = useState(() => buildWeekDays(new Date()).find((day) => day.isToday)?.key || buildWeekDays(new Date())[0].key);
  const [state, setState] = useState({ status: "loading", records: [], error: "" });
  const days = useMemo(() => buildWeekDays(reference), [reference]);

  useEffect(() => {
    const abortController = new AbortController();
    setState((current) => ({ ...current, status: "loading", error: "" }));
    void onLoadRange(weekRange(reference), { signal: abortController.signal }).then((records) => setState({ status: "ready", records, error: "" })).catch((error) => {
      if (error?.name !== "AbortError") setState((current) => ({ ...current, status: "error", error: "所选周面试加载失败，请重试。" }));
    });
    return () => abortController.abort();
  }, [onLoadRange, reference]);

  const filtered = useMemo(() => state.records.filter((item) => {
    const text = `${item.candidate}${item.position}${item.round}${item.interviewers.join("")}`.toLowerCase();
    return (!query || text.includes(query.toLowerCase()))
      && (status === "全部状态" || item.status === status || item.feedbackStatus === status)
      && (!(mineOnly || !canSchedule) || item.interviewerIds.includes(interviewerId));
  }), [canSchedule, interviewerId, mineOnly, query, state.records, status]);

  function selectReference(next) {
    setReference(next);
    const nextDays = buildWeekDays(next);
    setSelectedDay(nextDays.find((day) => day.isToday)?.key || nextDays[0].key);
  }

  return <div className="calendar-workspace" aria-busy={state.status === "loading"}>
    <div className="calendar-navigation"><button type="button" aria-label="上一周" onClick={() => selectReference(moveWeek(reference, -1))}><ChevronLeft size={16} /></button><button type="button" onClick={() => selectReference(new Date())}>今天</button><button type="button" aria-label="下一周" onClick={() => selectReference(moveWeek(reference, 1))}><ChevronRight size={16} /></button><strong>{weekLabel(reference)}</strong><label>选择日期<input type="date" value={selectedDay} onChange={(event) => { const date = parseLocalDate(event.target.value); if (date) { setSelectedDay(event.target.value); setReference(date); } }} /></label></div>
    <div className="mobile-date-strip" aria-label="选择日历日期">{days.map((day) => <button type="button" key={day.key} className={selectedDay === day.key ? "active" : ""} onClick={() => setSelectedDay(day.key)}><span>{day.weekday}</span><strong>{day.date.getDate()}</strong></button>)}</div>
    {state.status === "error" && <div className="workbench-inline-error" role="alert">{state.error}<button type="button" onClick={() => selectReference(new Date(reference))}>重试</button></div>}
    {state.status === "loading" && !state.records.length && <div className="calendar-loading" role="status"><CalendarDays size={20} />正在加载所选周全部面试</div>}
    <div className="week-calendar full-week">{days.map((day) => { const items = filtered.filter((item) => item.date === day.key); return <section key={day.key} className={selectedDay === day.key ? "selected-day" : ""}><header><strong>{day.label}</strong><span>{day.weekday} · {items.length} 场</span></header><div>{items.map((item) => <button type="button" className={`calendar-interview ${item.status === "已完成" ? "complete" : item.notification === "发送失败" ? "failed" : ""}`} key={item.id} onClick={() => onOpen(item)}><span><Clock3 size={13} />{item.time} · {item.duration} 分钟</span><strong>{item.candidate}</strong><small>{item.position} · {item.round}</small><small><Users size={12} />{item.interviewers.join("、")}</small><span className="interview-status info">{interviewStatusLabel(item.feedbackStatus === "待反馈" ? item.feedbackStatus : item.status)}</span></button>)}{!items.length && <div className="calendar-empty-slot">暂无面试</div>}</div></section>; })}</div>
  </div>;
}

export default InterviewCalendar;
