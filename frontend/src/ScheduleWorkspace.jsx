import { useEffect, useMemo, useRef, useState } from "react";
import { AlertTriangle, ArrowLeft, CalendarDays, Check, CheckCircle2, ChevronLeft, ChevronRight, CircleAlert, ClipboardCopy, MapPin, Phone, RefreshCw, Send, UserRound, Users, Video } from "lucide-react";
import { buildWeekDays, moveWeek, parseLocalDate, weekLabel, weekRange } from "./interviewDateUtils.js";
import { isScheduleCandidateEligible, resolveScheduleCandidateId, shouldHydrateScheduleCandidate } from "./interviewViewState.js";

/* interview-schedule-helpers:start */
export function getScheduleConflictType(result, allowSoftConflict) {
  if (result?.hard?.length || result?.calendarHard?.length) return "hard";
  if ((result?.soft?.length || result?.calendarSoft?.length) && !allowSoftConflict) return "soft";
  return null;
}

export function getScheduleSavedMessage(record, availabilityUnconfirmed = false, manualNotificationRequired = false) {
  const saved = record ? "面试改期已保存" : "面试安排已保存";
  const invitation = record ? "新的邀请文件可下载" : "邀请文件可下载";
  const email = manualNotificationRequired ? "邮件未发送，请人工通知候选人" : "候选人邮件已进入发送队列";
  const availability = availabilityUnconfirmed ? "；飞书忙闲暂未确认，请留意后续日历同步结果" : "";
  return `${saved}；${email}；${invitation}${availability}`;
}

export function isAvailabilityUnconfirmed(availability, participantIds) {
  if (availability?.status !== "ready" || !Array.isArray(availability?.data?.participants)) return true;
  const selected = new Set(Array.isArray(participantIds) ? participantIds : []);
  return availability.data.participants.some((participant) => selected.has(participant.participantId) && participant.status !== "confirmed");
}

export function getScheduleSaveError(error) {
  if (error?.code === "schedule_hard_conflict") return "该时段存在面试冲突，请调整后重试。";
  if (error?.code === "schedule_soft_conflict") return "该时段与已有安排间隔不足，请调整后重试。";
  return "无法完成权威冲突检查或保存，当前内容已保留。请重试。";
}

export async function copyInterviewText(text, clipboard) {
  if (!clipboard || typeof clipboard.writeText !== "function") throw new Error("clipboard unavailable");
  await clipboard.writeText(text);
}

export function isScheduleSlotInPast(date, time, timezone, now = Date.now()) {
  const offset = ["Asia/Shanghai", "Asia/Singapore"].includes(timezone) ? "+08:00" : "Z";
  return new Date(`${date}T${time}:00${offset}`).getTime() <= Number(now);
}

export function recommendedInterviewRound(candidate, fallback = "一面") {
  if (typeof candidate?.nextRound === "string" && candidate.nextRound.trim()) return candidate.nextRound.trim();
  const completedRounds = Array.from(new Set((Array.isArray(candidate?.interviews) ? candidate.interviews : [])
    .map((interview) => typeof interview?.round === "string" ? interview.round.trim() : "")
    .filter(Boolean)));
  if (!completedRounds.length) return fallback;
  const commonRounds = ["一面", "二面", "三面", "四面", "终面"];
  return commonRounds[Math.min(completedRounds.length, commonRounds.length - 1)];
}

export function buildInterviewTimeSlots(startHour = 9, endHour = 22, stepMinutes = 30) {
  const startMinutes = startHour * 60;
  const endMinutes = endHour * 60;
  return Array.from({ length: Math.floor((endMinutes - startMinutes) / stepMinutes) + 1 }, (_, index) => {
    const minutes = startMinutes + index * stepMinutes;
    return `${String(Math.floor(minutes / 60)).padStart(2, "0")}:${String(minutes % 60).padStart(2, "0")}`;
  });
}
/* interview-schedule-helpers:end */

const timeSlots = buildInterviewTimeSlots();

function offsetFor(timezone) {
  return ["Asia/Shanghai", "Asia/Singapore"].includes(timezone) ? "+08:00" : "Z";
}

function slotWindow(date, time, duration, timezone) {
  const start = new Date(`${date}T${time}:00${offsetFor(timezone)}`).getTime();
  return { start, end: start + Number(duration) * 60000 };
}

export function getAvailabilitySlotState(availability, date, time, duration, timezone) {
  if (!availability || availability.status !== "ready") return "unknown";
  const selected = slotWindow(date, time, duration, timezone);
  const buffer = availability.data.bufferMinutes * 60000;
  let unconfirmed = false;
  for (const participant of availability.data.participants) {
    if (participant.status !== "confirmed") unconfirmed = true;
    for (const busy of participant.busy) {
      const start = new Date(busy.startsAt).getTime();
      const end = new Date(busy.endsAt).getTime();
      if (selected.start < end && start < selected.end) return "conflict";
      if (selected.start < end + buffer && start - buffer < selected.end) return "buffer";
    }
  }
  return unconfirmed ? "unconfirmed" : "available";
}

const slotLabels = { available: "可排", unconfirmed: "可排·飞书未确认", conflict: "已占用", buffer: "缓冲不足", unknown: "无法确认" };
const interviewMethods = [
  { value: "视频面试", label: "线上面试", description: "自动创建飞书视频会议", Icon: Video },
  { value: "现场面试", label: "线下面试", description: "在指定地点当面进行", Icon: MapPin },
  { value: "电话面试", label: "电话面试", description: "通过电话直接沟通", Icon: Phone },
];

export function ScheduleWorkspace({ record, candidateId, candidates, participantOptions, participantStatus, onBack, backLabel, onSave, onGetAvailability, onNotify }) {
  const recordCandidate = record ? { id: record.candidateId, candidateId: record.candidateId, name: record.candidate, position: record.position, role: record.role } : null;
  const fallback = candidates.find((item) => item.id === candidateId || item.candidateId === candidateId) || recordCandidate || candidates.find((item) => item.stage === "待安排") || candidates[0];
  const [step, setStep] = useState(1);
  const [errors, setErrors] = useState({});
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState("");
  const initialReference = parseLocalDate(record?.date) || new Date();
  const [weekReference, setWeekReference] = useState(initialReference);
  const [availability, setAvailability] = useState({ status: "idle", data: null });
  const availabilityRequestId = useRef(0);
  const availabilityAbortController = useRef(null);
  const [form, setForm] = useState(() => ({
    candidateId: resolveScheduleCandidateId(record, candidateId, fallback), position: record?.position || fallback?.position || "", round: record?.round || recommendedInterviewRound(fallback),
    method: record?.method || "视频面试", timezone: record?.timezone || "Asia/Shanghai", date: record?.date || "", time: record?.time || "", duration: record?.duration || 60,
    interviewerIds: record?.interviewerIds || [], location: record?.location === "未填写" ? "" : record?.location || "",
    candidateMessage: "您好，诚邀您参加本次面试，请提前 5 分钟做好准备。", interviewerMessage: "您有一场新的面试任务，请提前查看候选人材料与职位重点。",
  }));
  useEffect(() => {
    if (record || !candidateId) return;
    const loadedCandidate = candidates.find((item) => item.id === candidateId || item.candidateId === candidateId);
    if (!loadedCandidate) return;
    setForm((current) => shouldHydrateScheduleCandidate(current.candidateId, candidateId) ? {
      ...current,
      candidateId: loadedCandidate.candidateId || loadedCandidate.id,
      position: current.position || loadedCandidate.position || "",
      round: recommendedInterviewRound(loadedCandidate, current.round),
    } : current);
  }, [candidateId, candidates, record]);
  useEffect(() => () => {
    availabilityRequestId.current += 1;
    availabilityAbortController.current?.abort();
  }, []);
  const candidate = candidates.find((item) => item.id === form.candidateId || item.candidateId === form.candidateId) || fallback;
  const roundOptions = Array.from(new Set([recommendedInterviewRound(candidate, form.round), candidate?.nextRound, form.round, "电话沟通", "一面", "二面", "三面", "四面", "终面", "技术面", "加面"].filter(Boolean)));
  const candidateOptions = candidate && !candidates.some((item) => (item.candidateId || item.id) === (candidate.candidateId || candidate.id)) ? [candidate, ...candidates] : candidates;
  const selectedInterviewers = participantOptions.filter((item) => form.interviewerIds.includes(item.id));
  const weekDays = useMemo(() => buildWeekDays(weekReference), [weekReference]);

  function update(field, value) {
    setForm((current) => ({ ...current, [field]: value }));
    setErrors((current) => ({ ...current, [field]: "" }));
    setSubmitError("");
  }

  function selectMethod(method) {
    setForm((current) => current.method === method ? current : { ...current, method, location: "" });
    setErrors((current) => ({ ...current, location: "" }));
    setSubmitError("");
  }

  function moveMethodSelection(event, index) {
    if (!['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown'].includes(event.key)) return;
    event.preventDefault();
    const direction = ['ArrowRight', 'ArrowDown'].includes(event.key) ? 1 : -1;
    const nextIndex = (index + direction + interviewMethods.length) % interviewMethods.length;
    selectMethod(interviewMethods[nextIndex].value);
    event.currentTarget.parentElement?.querySelectorAll('[role="radio"]')[nextIndex]?.focus();
  }

  function toggleInterviewer(id) {
    availabilityRequestId.current += 1;
    availabilityAbortController.current?.abort();
    availabilityAbortController.current = null;
    update("interviewerIds", form.interviewerIds.includes(id) ? form.interviewerIds.filter((item) => item !== id) : [...form.interviewerIds, id]);
    setAvailability({ status: "idle", data: null });
  }

  function continueFromBasics() {
    const next = {};
    if (!form.candidateId) next.candidateId = "请选择候选人";
    else if (!record && candidate && !isScheduleCandidateEligible(candidate)) next.candidateId = `当前处于“${candidate.stage}”，该阶段不能安排新面试`;
    if (!form.position.trim()) next.position = "请确认应聘职位";
    setErrors(next);
    if (!Object.keys(next).length) setStep(2);
  }

  async function loadAvailability(reference = weekReference) {
    if (!form.interviewerIds.length) { setErrors((current) => ({ ...current, interviewers: "至少选择一位面试官" })); return; }
    const requestId = ++availabilityRequestId.current;
    availabilityAbortController.current?.abort();
    const abortController = new AbortController();
    availabilityAbortController.current = abortController;
    const range = weekRange(reference);
    setAvailability({ status: "loading", data: null });
    try {
      const data = await onGetAvailability(
        { ...range, participantIds: form.interviewerIds, timezone: form.timezone, buffer: 15, exclude: record?.id || "" },
        { signal: abortController.signal },
      );
      if (requestId !== availabilityRequestId.current) return;
      setAvailability({ status: "ready", data });
    } catch (error) {
      if (requestId === availabilityRequestId.current && error?.name !== "AbortError") setAvailability({ status: "error", data: null });
    } finally {
      if (availabilityAbortController.current === abortController) availabilityAbortController.current = null;
    }
  }

  function backToInterviewers() {
    availabilityRequestId.current += 1;
    availabilityAbortController.current?.abort();
    availabilityAbortController.current = null;
    setAvailability({ status: "idle", data: null });
    setStep(2);
  }

  function changeWeek(amount) {
    const reference = moveWeek(weekReference, amount);
    setWeekReference(reference);
    update("date", ""); update("time", "");
    void loadAvailability(reference);
  }

  function continueToTime() {
    if (!form.interviewerIds.length) {
      setErrors((current) => ({ ...current, interviewers: "至少选择一位面试官" }));
      return;
    }
    setStep(3);
    void loadAvailability();
  }

  function chooseSlot(date, time) {
    if (!["available", "unconfirmed"].includes(getAvailabilitySlotState(availability, date, time, form.duration, form.timezone))) return;
    if (isScheduleSlotInPast(date, time, form.timezone)) return;
    setForm((current) => ({ ...current, date, time }));
    setStep(4);
  }

  async function save() {
    const next = {};
    if (form.method === "现场面试" && !form.location.trim()) next.location = "请填写面试地点";
    setErrors(next); if (Object.keys(next).length) return;
    if (isScheduleSlotInPast(form.date, form.time, form.timezone)) {
      setSubmitError("该面试时间已经过去，请返回重新选择时间。");
      return;
    }
    setSubmitting(true); setSubmitError("");
    try {
      const saved = await onSave(record, { ...form, applicationId: candidate?.applicationId || candidate?.application?.id || "", participants: selectedInterviewers.map((item) => ({ id: item.id, role: "interviewer", requiredFeedback: true })), allowSoftConflict: false });
      onNotify(getScheduleSavedMessage(record, isAvailabilityUnconfirmed(availability, form.interviewerIds), saved?.emailDelivery?.manualNotificationRequired));
    } catch (error) {
      if (error?.name !== "AbortError") setSubmitError(getScheduleSaveError(error));
    } finally { setSubmitting(false); }
  }

  async function copyInvitation(text, label) {
    try { await copyInterviewText(text, typeof navigator === "undefined" ? null : navigator.clipboard); onNotify(`${label}已复制`); }
    catch { onNotify(`${label}复制失败，请手动选择文本复制`); }
  }

  const steps = ["候选人与轮次", "选择面试官", "选择日期时间", "确认邀请"];
  return <div className="interview-page schedule-page"><button className="back-link" type="button" onClick={onBack}><ArrowLeft size={17} />{backLabel}</button>
    <div className="schedule-heading"><div><h2>{record ? "改期面试" : "安排面试"}</h2><p>{candidate?.name || "选择候选人"} · {form.position}</p></div><div className="schedule-steps">{steps.map((label, index) => <span key={label} className={step >= index + 1 ? "active" : ""}><i>{step > index + 1 ? <Check size={13} /> : index + 1}</i>{label}</span>)}</div></div>
    <div className="schedule-layout"><main className="schedule-main">
      {step === 1 && <section className="schedule-section"><header><CalendarDays size={19} /><div><h3>候选人与面试设置</h3><p>先确认对象、轮次和时长，不预设面试时间。</p></div></header><div className="schedule-grid"><label>候选人<select value={form.candidateId} disabled={Boolean(record)} onChange={(event) => { const selected = candidates.find((item) => item.id === event.target.value || item.candidateId === event.target.value); setForm((current) => ({ ...current, candidateId: event.target.value, position: selected?.position || current.position, round: recommendedInterviewRound(selected, current.round) })); }}><option value="">请选择候选人</option>{candidateOptions.map((item) => <option value={item.candidateId || item.id} key={item.applicationId || item.id}>{item.name} · {item.position}{item.nextRound ? ` · 待安排${item.nextRound}` : ""}</option>)}</select>{errors.candidateId && <small className="field-error">{errors.candidateId}</small>}</label><label>应聘职位<input value={form.position} onChange={(event) => update("position", event.target.value)} />{errors.position && <small className="field-error">{errors.position}</small>}</label><label>面试轮次<select value={form.round} onChange={(event) => update("round", event.target.value)}>{roundOptions.map((round) => <option key={round}>{round}</option>)}</select>{candidate?.nextRound ? <small className="field-hint">已按职位流程预选{candidate.nextRound}</small> : candidate?.stage === "待决策" ? <small className="field-hint">流程轮次已完成，可追加三面、终面或加面</small> : null}</label><label>时长<select value={form.duration} onChange={(event) => update("duration", Number(event.target.value))}><option value="30">30 分钟</option><option value="45">45 分钟</option><option value="60">60 分钟</option><option value="90">90 分钟</option></select></label><fieldset className="schedule-method-field"><legend>面试方式</legend><div className="schedule-method-options" role="radiogroup" aria-label="面试方式">{interviewMethods.map(({ value, label, description, Icon }, index) => <button key={value} className={form.method === value ? "selected" : ""} type="button" role="radio" aria-checked={form.method === value} tabIndex={form.method === value ? 0 : -1} onClick={() => selectMethod(value)} onKeyDown={(event) => moveMethodSelection(event, index)}><Icon size={19} aria-hidden="true" /><span><strong>{label}</strong><small>{description}</small></span><Check size={15} className="schedule-method-check" aria-hidden="true" /></button>)}</div></fieldset><label>时区<select value={form.timezone} onChange={(event) => update("timezone", event.target.value)}><option value="Asia/Shanghai">北京时间 GMT+8</option><option value="Asia/Singapore">新加坡 GMT+8</option></select></label></div><footer><button className="button primary" type="button" onClick={continueFromBasics}>下一步：选择面试官<ChevronRight size={16} /></button></footer></section>}
      {step === 2 && <section className="schedule-section"><header><Users size={19} /><div><h3>选择面试官</h3><p>下一步会直接在日期时间表中标出所选面试官的已占用时段。</p></div></header><div className="interviewer-picker"><strong>面试官</strong><div>{participantOptions.map((person) => <label key={person.id} className={form.interviewerIds.includes(person.id) ? "selected" : ""}><input type="checkbox" checked={form.interviewerIds.includes(person.id)} onChange={() => toggleInterviewer(person.id)} /><span><UserRound size={16} /></span><strong>{person.name}</strong><small>面试参与人</small></label>)}</div>{participantStatus === "loading" && <p><RefreshCw size={14} />正在加载可选面试官</p>}{participantStatus === "error" && <p className="field-error"><CircleAlert size={14} />面试官目录加载失败</p>}{errors.interviewers && <p className="field-error">{errors.interviewers}</p>}</div><footer><button className="button secondary" type="button" onClick={() => setStep(1)}>上一步</button><button className="button primary" type="button" disabled={!form.interviewerIds.length} onClick={continueToTime}>下一步：选择日期时间<ChevronRight size={16} /></button></footer></section>}
      {step === 3 && <section className="schedule-section"><header><CalendarDays size={19} /><div><h3>选择可用日期时间</h3><p>红色表示所选面试官已有安排；缓冲不足和已过去的时段同样不可选。</p></div></header><div className="availability-toolbar schedule-time-toolbar"><button type="button" aria-label="上一周" disabled={availability.status === "loading"} onClick={() => changeWeek(-1)}><ChevronLeft size={16} /></button><strong>{weekLabel(weekReference)}</strong><button type="button" aria-label="下一周" disabled={availability.status === "loading"} onClick={() => changeWeek(1)}><ChevronRight size={16} /></button><input aria-label="选择排期周" type="date" disabled={availability.status === "loading"} value={weekDays[0].key} onChange={(event) => { const reference = parseLocalDate(event.target.value); if (reference) { setWeekReference(reference); update("date", ""); update("time", ""); void loadAvailability(reference); } }} /></div><div className="availability-legend"><span className="available">可排</span><span className="conflict">已占用</span><span className="buffer">缓冲不足</span><span className="unconfirmed">可排·飞书未确认</span></div><div className="schedule-availability-state" aria-live="polite">{availability.status === "loading" && <p><RefreshCw size={16} />正在读取所选面试官的日历</p>}{availability.status === "error" && <p role="alert"><AlertTriangle size={16} />忙闲服务暂时不可用<button className="button secondary" type="button" onClick={() => void loadAvailability()}>重新查询</button></p>}</div>{availability.status === "ready" && <div className="schedule-slot-grid">{weekDays.map((day) => <section key={day.key}><header><strong>{day.weekday}</strong><span>{day.label}</span></header><div>{timeSlots.map((time) => { const state = getAvailabilitySlotState(availability, day.key, time, form.duration, form.timezone); const inPast = isScheduleSlotInPast(day.key, time, form.timezone); const disabled = !["available", "unconfirmed"].includes(state) || inPast; return <button type="button" key={time} className={`${state}${disabled ? " unavailable" : ""}`} disabled={disabled} onClick={() => chooseSlot(day.key, time)}><strong>{time}</strong><span>{inPast ? "已过期" : slotLabels[state]}</span></button>; })}</div></section>)}</div>}<footer><button className="button secondary" type="button" onClick={backToInterviewers}>上一步</button></footer></section>}
      {step === 4 && <section className="schedule-section"><header><Send size={19} /><div><h3>确认邀请</h3><p>保存前会再次执行服务端权威冲突检查。</p></div></header><div className="schedule-summary"><div><span>候选人</span><strong>{candidate?.name} · {form.position}</strong></div><div><span>时间</span><strong>{form.date} {form.time} · {form.duration} 分钟</strong></div><div><span>面试官</span><strong>{selectedInterviewers.map((item) => item.name).join("、")}</strong></div><div><span>方式</span><strong>{form.method}</strong></div></div>{form.method === "现场面试" ? <label className="schedule-full-field">面试地点<span className="required-mark" aria-hidden="true">*</span><input value={form.location} aria-required="true" placeholder="例如：深圳研发中心 3 楼会议室" onChange={(event) => update("location", event.target.value)} />{errors.location && <small className="field-error">{errors.location}</small>}</label> : <div className="schedule-method-note" role="status">{form.method === "视频面试" ? <><Video size={18} aria-hidden="true" /><p><strong>飞书视频会议将自动创建</strong><span>保存后自动创建飞书视频会议并通过日历邀请发送。</span></p></> : <><Phone size={18} aria-hidden="true" /><p><strong>电话面试无需额外填写</strong><span>保存后将按当前时间和参与人发送日历邀请。</span></p></>}</div>}<div className="invitation-preview"><section><header><strong>候选人邀请文本</strong><button type="button" onClick={() => void copyInvitation(form.candidateMessage, "候选人邀请文本")}><ClipboardCopy size={14} />复制</button></header><textarea rows="4" value={form.candidateMessage} onChange={(event) => update("candidateMessage", event.target.value)} /></section><section><header><strong>面试官任务文本</strong><button type="button" onClick={() => void copyInvitation(form.interviewerMessage, "面试官任务文本")}><ClipboardCopy size={14} />复制</button></header><textarea rows="4" value={form.interviewerMessage} onChange={(event) => update("interviewerMessage", event.target.value)} /></section></div>{submitError && <div className="feedback-submit-error" role="alert"><CircleAlert size={18} /><p>{submitError}</p></div>}<footer><button className="button secondary" type="button" disabled={submitting} onClick={() => setStep(3)}>上一步</button><button className="button primary" type="button" disabled={submitting} onClick={() => void save()}><CheckCircle2 size={16} />{submitting ? "正在检查并保存" : "确认并保存"}</button></footer></section>}
    </main><aside className="schedule-aside"><section><h3>候选人摘要</h3><strong>{candidate?.name || "待选择"}</strong><p>{candidate?.role || "当前职称未填写"}</p><p>{candidate?.summary || "候选人详情以服务端档案为准。"}</p></section><section><h3>本次安排</h3><dl><div><dt>轮次</dt><dd>{form.round}</dd></div><div><dt>时间</dt><dd>{form.date && form.time ? `${form.date} ${form.time}` : "待选时段"}</dd></div><div><dt>面试官</dt><dd>{selectedInterviewers.map((item) => item.name).join("、") || "待选择"}</dd></div></dl></section></aside></div>
  </div>;
}

export default ScheduleWorkspace;
