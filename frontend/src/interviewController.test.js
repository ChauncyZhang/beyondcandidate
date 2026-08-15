import test from "node:test";
import assert from "node:assert/strict";
import { candidateEmailCorrectionContext, createInterviewController, deriveCandidateInterviews, getInterviewEmailActions, requiresCandidateEmailCorrection, requiresManualCandidateNotice, selectSchedulableCandidates } from "./interviewController.js";

const INTERVIEW_ID = "11111111-1111-4111-8111-111111111111";
const APPLICATION_ID = "22222222-2222-4222-8222-222222222222";
const CANDIDATE_ID = "33333333-3333-4333-8333-333333333333";
const JOB_ID = "44444444-4444-4444-8444-444444444444";
const USER_ID = "55555555-5555-4555-8555-555555555555";
const FEEDBACK_ID = "66666666-6666-4666-8666-666666666666";

function apiInterview(overrides = {}) {
  return {
    id: INTERVIEW_ID,
    application_id: APPLICATION_ID,
    candidate: { id: CANDIDATE_ID, display_name: "赵宁", current_title: "大模型应用工程师" },
    job: { id: JOB_ID, title: "AI 工程师" },
    round_name: "一面",
    method: "video",
    timezone: "Asia/Shanghai",
    starts_at: "2026-07-15T02:00:00Z",
    ends_at: "2026-07-15T03:00:00Z",
    location: null,
    meeting_url: "https://meeting.example.com/one",
    status: "pending_feedback",
    notification_status: "sent",
    invitation_status: "artifact_ready",
    participants: [{ user_id: USER_ID, display_name: "张小北", role: "interviewer", required_feedback: true }],
    version: 7,
    calendar_sequence: 2,
    updated_at: "2026-07-14T09:00:00+08:00",
    ...overrides,
  };
}

function queuedClient(responses) {
  const calls = [];
  return {
    calls,
    client: {
      async request(path, options = {}) {
        calls.push({ kind: "request", path, options });
        const response = responses.shift();
        if (response instanceof Error) throw response;
        return response;
      },
      async download(path, options = {}) {
        calls.push({ kind: "download", path, options });
        return responses.shift();
      },
    },
  };
}

test("lists interviews from the server envelope and maps display fields", async () => {
  const { client, calls } = queuedClient([{ data: [apiInterview()], meta: { count: 1, limit: 25, next_cursor: "CURSOR-2" } }]);
  const controller = createInterviewController({ client });
  const signal = new AbortController().signal;

  const result = await controller.list({ status: "pending_feedback", interviewerId: USER_ID, limit: 25, cursor: "CURSOR-1" }, { signal });

  assert.deepEqual(calls, [{
    kind: "request",
    path: `/api/v1/interviews?interviewer_id=${USER_ID}&status=pending_feedback&cursor=CURSOR-1&limit=25`,
    options: { signal },
  }]);
  assert.equal(result.records.length, 1);
  assert.deepEqual(result.records[0], {
    id: INTERVIEW_ID,
    serverBacked: true,
    applicationId: APPLICATION_ID,
    candidateId: CANDIDATE_ID,
    candidate: "赵宁",
    role: "大模型应用工程师",
    jobId: JOB_ID,
    position: "AI 工程师",
    round: "一面",
    date: "2026-07-15",
    dateLabel: "07-15",
    time: "10:00",
    duration: 60,
    method: "视频面试",
    timezone: "Asia/Shanghai",
    startsAt: "2026-07-15T02:00:00Z",
    endsAt: "2026-07-15T03:00:00Z",
    interviewerIds: [USER_ID],
    interviewers: ["张小北"],
    participants: [{ id: USER_ID, name: "张小北", role: "interviewer", requiredFeedback: true }],
    location: "https://meeting.example.com/one",
    status: "已完成",
    notification: "已发送",
    feishuNotification: "已发送",
    emailDelivery: null,
    feedbackStatus: "待反馈",
    owner: "张小北",
    version: 7,
    calendarSequence: 2,
    jdPriorities: [],
    suggestedQuestions: [],
    summary: "大模型应用工程师",
    history: [],
    feedback: null,
  });
  assert.equal(result.count, 1);
  assert.equal(result.nextCursor, "CURSOR-2");
});

test("normalizes masked candidate email delivery separately from Feishu notification state", async () => {
  const { client } = queuedClient([{ data: [apiInterview({
    notification_status: "failed",
    email_delivery: {
      id: "77777777-7777-4777-8777-777777777777",
      recipient: "c*******e@example.com",
      status: "sent",
      version: 4,
      safe_error_code: null,
    },
  })] }]);

  const record = (await createInterviewController({ client }).list()).records[0];

  assert.equal(record.feishuNotification, "发送失败");
  assert.equal(record.notification, "发送失败");
  assert.deepEqual(record.emailDelivery, {
    id: "77777777-7777-4777-8777-777777777777",
    recipient: "c*******e@example.com",
    status: "sent",
    statusLabel: "已发送",
    version: 4,
    errorText: "",
  });
});

test("projects a superseded interview email as cancelled instead of failed", async () => {
  const { client } = queuedClient([{ data: [apiInterview({
    email_delivery: {
      id: "77777777-7777-4777-8777-777777777777",
      recipient: "c*******e@example.com",
      status: "cancelled",
      version: 2,
      safe_error_code: "interview_message_superseded",
    },
  })] }]);

  const delivery = (await createInterviewController({ client }).list()).records[0].emailDelivery;

  assert.equal(delivery.statusLabel, "已取消");
  assert.equal(delivery.errorText, "旧通知已被后续面试安排取代，未发送。");
});

test("never projects an unmasked recipient or raw delivery error text", async () => {
  const { client } = queuedClient([{ data: [apiInterview({
    email_delivery: {
      id: "77777777-7777-4777-8777-777777777777",
      recipient: "candidate@example.com",
      status: "failed",
      version: 2,
      safe_error_code: "raw-provider-secret-detail",
    },
  })] }]);

  const delivery = (await createInterviewController({ client }).list()).records[0].emailDelivery;

  assert.equal(delivery.recipient, "收件人已隐藏");
  assert.equal(delivery.errorText, "邮件发送失败，请联系系统管理员后重试。");
  assert.doesNotMatch(JSON.stringify(delivery), /candidate@example\.com|raw-provider-secret-detail/);
});

test("projects an unconfigured provider as a durable manual candidate notice", async () => {
  const { client } = queuedClient([{ data: [apiInterview({
    email_delivery: {
      id: null,
      recipient: null,
      status: "not_sent",
      version: null,
      safe_error_code: "email_not_configured",
    },
  })] }]);

  const record = (await createInterviewController({ client }).list()).records[0];

  assert.deepEqual(record.emailDelivery, {
    id: "",
    recipient: "请人工通知候选人",
    status: "not_sent",
    statusLabel: "未发送",
    version: null,
    errorText: "邮件服务尚未配置，请人工通知候选人。",
    manualNotificationRequired: true,
  });
  assert.equal(requiresManualCandidateNotice(record), true);
});

test("recruiter scheduling capability shows resend while interviewer capability keeps it hidden", () => {
  const record = { candidateId: CANDIDATE_ID, emailDelivery: { id: "delivery", status: "failed" } };
  const recruiterCanSchedule = true;
  const interviewerCanSchedule = false;

  assert.deepEqual(getInterviewEmailActions(record, { correctionRequired: true }), { correct: false, resend: false });
  assert.deepEqual(getInterviewEmailActions(record, { canCorrect: true, correctionRequired: true }), { correct: true, resend: false });
  assert.deepEqual(getInterviewEmailActions(record, { canResend: recruiterCanSchedule }), { correct: false, resend: true });
  assert.deepEqual(getInterviewEmailActions(record, { canResend: interviewerCanSchedule }), { correct: false, resend: false });
  assert.equal(requiresCandidateEmailCorrection({ code: "candidate_email_unconfirmed" }), true);
  assert.equal(requiresCandidateEmailCorrection({ code: "email_not_configured" }), false);
});

test("unconfirmed resend recovery preserves candidate application and interview context", () => {
  const context = candidateEmailCorrectionContext({
    id: INTERVIEW_ID,
    candidateId: CANDIDATE_ID,
    applicationId: APPLICATION_ID,
    candidate: "赵宁",
  }, "resend");

  assert.deepEqual(context, {
    id: CANDIDATE_ID,
    name: "赵宁",
    applicationId: APPLICATION_ID,
    interviewId: INTERVIEW_ID,
    operation: "resend",
  });
  assert.equal(candidateEmailCorrectionContext({ id: INTERVIEW_ID }, "resend"), null);
});

test("loads every interview page in a requested calendar range", async () => {
  const second = apiInterview({ id: "77777777-7777-4777-8777-777777777777" });
  const { client, calls } = queuedClient([
    { data: [apiInterview()], meta: { next_cursor: "NEXT" } },
    { data: [second], meta: { next_cursor: null } },
  ]);
  const controller = createInterviewController({ client });

  const records = await controller.listRange({ from: "2026-07-13", to: "2026-07-19", limit: 100 });

  assert.equal(records.length, 2);
  assert.deepEqual(calls.map((call) => call.path), [
    "/api/v1/interviews?from=2026-07-13T00%3A00%3A00%2B08%3A00&to=2026-07-19T23%3A59%3A59%2B08%3A00&limit=100",
    "/api/v1/interviews?from=2026-07-13T00%3A00%3A00%2B08%3A00&to=2026-07-19T23%3A59%3A59%2B08%3A00&cursor=NEXT&limit=100",
  ]);
});

test("requests privacy-safe availability with range, participants, buffer, timezone, and exclude", async () => {
  const { client, calls } = queuedClient([{ data: { participants: [] } }]);
  const controller = createInterviewController({ client });

  await controller.availability({
    from: "2026-07-13", to: "2026-07-19", participantIds: [USER_ID], timezone: "Asia/Shanghai", buffer: 15, exclude: INTERVIEW_ID,
  });

  assert.equal(calls[0].path, `/api/v1/interview-availability?from=2026-07-13T00%3A00%3A00%2B08%3A00&to=2026-07-19T23%3A59%3A59%2B08%3A00&participant_ids=${USER_ID}&timezone=Asia%2FShanghai&buffer=15&exclude=${INTERVIEW_ID}`);
});

test("serializes the exact adjacent-week availability range in the selected timezone", async () => {
  const { client, calls } = queuedClient([{ data: { participants: [] } }]);
  const controller = createInterviewController({ client });

  await controller.availability({
    from: "2026-07-27", to: "2026-08-02", participantIds: [USER_ID], timezone: "Asia/Shanghai", buffer: 15,
  });

  assert.equal(calls[0].path, `/api/v1/interview-availability?from=2026-07-27T00%3A00%3A00%2B08%3A00&to=2026-08-02T23%3A59%3A59%2B08%3A00&participant_ids=${USER_ID}&timezone=Asia%2FShanghai&buffer=15`);
});

test("gets one interview and propagates abort errors unchanged", async () => {
  const abortError = new DOMException("aborted", "AbortError");
  const first = queuedClient([{ data: apiInterview({ status: "confirmed" }) }]);
  const signal = new AbortController().signal;

  const record = await createInterviewController({ client: first.client }).get(INTERVIEW_ID, { signal });
  assert.equal(record.status, "已确认");
  assert.deepEqual(first.calls[0], { kind: "request", path: `/api/v1/interviews/${INTERVIEW_ID}`, options: { signal } });

  await assert.rejects(
    () => createInterviewController({ client: queuedClient([abortError]).client }).get(INTERVIEW_ID),
    (error) => error === abortError,
  );
});

test("resends a failed candidate email with quoted delivery version and idempotency", async () => {
  const deliveryId = "77777777-7777-4777-8777-777777777777";
  const { client, calls } = queuedClient([{ data: {
    id: "88888888-8888-4888-8888-888888888888",
    recipient: "c*******e@example.com",
    status: "queued",
    version: 1,
    safe_error_code: null,
  } }]);
  const controller = createInterviewController({ client, idempotencyKey: () => "resend-key" });
  const signal = new AbortController().signal;

  const resent = await controller.resendEmail({ id: INTERVIEW_ID, emailDelivery: { id: deliveryId, status: "failed", version: 5 } }, { signal });

  assert.equal(resent.statusLabel, "待发送");
  assert.deepEqual(calls, [{
    kind: "request",
    path: `/api/v1/email-deliveries/${deliveryId}/resend`,
    options: { method: "POST", ifMatch: '"5"', idempotencyKey: "resend-key", signal },
  }]);
});

test("resend failure remains a single request without an implicit retry", async () => {
  const unavailable = Object.assign(new Error("unavailable"), { code: "service_unavailable", status: 503 });
  const { client, calls } = queuedClient([unavailable]);
  const controller = createInterviewController({ client, idempotencyKey: () => "resend-failure" });

  await assert.rejects(
    () => controller.resendEmail({ id: INTERVIEW_ID, emailDelivery: { id: "delivery", status: "failed", version: 2 } }),
    unavailable,
  );
  assert.equal(calls.length, 1);
});

test("resend version conflict refreshes the interview exactly once and never retries resend", async () => {
  const conflict = Object.assign(new Error("stale"), { code: "resource_version_conflict", status: 409 });
  const { client, calls } = queuedClient([
    conflict,
    { data: apiInterview({ email_delivery: { id: "latest-delivery", recipient: "c*******e@example.com", status: "sent", version: 7, safe_error_code: null } }) },
  ]);
  const controller = createInterviewController({ client, idempotencyKey: () => "stale-resend" });

  await assert.rejects(
    () => controller.resendEmail({ id: INTERVIEW_ID, emailDelivery: { id: "stale-delivery", status: "failed", version: 2 } }),
    (error) => error === conflict && error.latestInterview?.emailDelivery?.status === "sent",
  );
  assert.deepEqual(calls.map((call) => call.path), [
    "/api/v1/email-deliveries/stale-delivery/resend",
    `/api/v1/interviews/${INTERVIEW_ID}`,
  ]);
});

test("creates and reschedules interviews with idempotency and quoted versions", async () => {
  const { client, calls } = queuedClient([
    { data: apiInterview({ version: 1, status: "scheduled" }) },
    { data: apiInterview({ version: 8, status: "rescheduled" }) },
  ]);
  const keys = ["create-key", "patch-key"];
  const controller = createInterviewController({ client, idempotencyKey: () => keys.shift() });
  const signal = new AbortController().signal;
  const form = {
    applicationId: APPLICATION_ID,
    round: "一面",
    method: "视频面试",
    timezone: "Asia/Shanghai",
    date: "2026-07-15",
    time: "10:00",
    duration: 60,
    location: "https://meeting.example.com/one",
    participants: [{ id: USER_ID, role: "interviewer", requiredFeedback: true }],
    allowSoftConflict: true,
  };

  await controller.save(null, form, { signal });
  await controller.save({ id: INTERVIEW_ID, version: 7 }, form, { signal });

  const body = {
    application_id: APPLICATION_ID,
    round_name: "一面",
    method: "video",
    timezone: "Asia/Shanghai",
    starts_at: "2026-07-15T10:00:00+08:00",
    ends_at: "2026-07-15T11:00:00+08:00",
    location: null,
    meeting_url: null,
    participants: [{ user_id: USER_ID, role: "interviewer", required_feedback: true }],
    allow_soft_conflict: true,
  };
  assert.deepEqual(calls, [
    { kind: "request", path: "/api/v1/interviews", options: { method: "POST", body, idempotencyKey: "create-key", signal } },
    { kind: "request", path: `/api/v1/interviews/${INTERVIEW_ID}`, options: { method: "PATCH", body: Object.fromEntries(Object.entries(body).filter(([key]) => key !== "application_id")), ifMatch: '"7"', idempotencyKey: "patch-key", signal } },
  ]);
});

test("sends a location only for onsite interviews", async () => {
  const { client, calls } = queuedClient([
    { data: apiInterview({ method: "onsite", location: "深圳研发中心" }) },
    { data: apiInterview({ method: "phone", location: null }) },
  ]);
  const controller = createInterviewController({ client, idempotencyKey: () => "method-key" });
  const baseForm = {
    applicationId: APPLICATION_ID,
    round: "一面",
    timezone: "Asia/Shanghai",
    date: "2026-07-15",
    time: "10:00",
    duration: 60,
    participants: [{ id: USER_ID, role: "interviewer", requiredFeedback: true }],
  };

  await controller.save(null, { ...baseForm, method: "现场面试", location: "深圳研发中心" });
  await controller.save(null, { ...baseForm, method: "电话面试", location: "不应提交的旧地址" });

  assert.deepEqual(calls.map((call) => ({
    method: call.options.body.method,
    location: call.options.body.location,
    meetingUrl: call.options.body.meeting_url,
  })), [
    { method: "onsite", location: "深圳研发中心", meetingUrl: null },
    { method: "phone", location: null, meetingUrl: null },
  ]);
});

test("checks conflicts, transitions, downloads calendar, and lists my tasks", async () => {
  const calendar = { blob: new Blob(["BEGIN:VCALENDAR"]), filename: "interview.ics" };
  const { client, calls } = queuedClient([
    { data: { hard: [INTERVIEW_ID], soft: [] } },
    { data: apiInterview({ status: "confirmed", version: 8 }) },
    calendar,
    { data: [{ id: `feedback:${INTERVIEW_ID}`, type: "interview_feedback", interview_id: INTERVIEW_ID }], meta: { count: 1 } },
  ]);
  const controller = createInterviewController({ client, idempotencyKey: () => "transition-key" });
  const signal = new AbortController().signal;

  const conflicts = await controller.checkConflicts(INTERVIEW_ID, {
    date: "2026-07-15", time: "10:00", duration: 60, timezone: "Asia/Shanghai", participantIds: [USER_ID],
  }, { signal });
  const transitioned = await controller.transition({ id: INTERVIEW_ID, version: 7 }, "confirmed", { reason: "候选人确认", signal });
  const downloaded = await controller.downloadCalendar(INTERVIEW_ID, { signal });
  const tasks = await controller.listMyTasks({ signal });

  assert.deepEqual(conflicts, { hard: [INTERVIEW_ID], soft: [], calendarHard: [], calendarSoft: [], unconfirmed: [] });
  assert.equal(transitioned.status, "已确认");
  assert.equal(downloaded, calendar);
  assert.equal(tasks[0].interviewId, INTERVIEW_ID);
  assert.deepEqual(calls, [
    { kind: "request", path: `/api/v1/interviews/${INTERVIEW_ID}/conflicts`, options: { method: "POST", body: { starts_at: "2026-07-15T10:00:00+08:00", ends_at: "2026-07-15T11:00:00+08:00", participant_ids: [USER_ID], buffer_minutes: 15 }, signal } },
    { kind: "request", path: `/api/v1/interviews/${INTERVIEW_ID}/transitions`, options: { method: "POST", body: { target: "confirmed", reason: "候选人确认" }, ifMatch: '"7"', idempotencyKey: "transition-key", signal } },
    { kind: "download", path: `/api/v1/interviews/${INTERVIEW_ID}/calendar-file`, options: { signal } },
    { kind: "request", path: "/api/v1/me/tasks", options: { signal } },
  ]);
});

test("loads an interview-scoped resume for preview and original download", async () => {
  const preview = { blob: new Blob(["preview"], { type: "application/pdf" }), filename: "候选人.pdf" };
  const original = { blob: new Blob(["download"], { type: "application/pdf" }), filename: "候选人.pdf" };
  const { client, calls } = queuedClient([preview, original]);
  const controller = createInterviewController({ client });
  const signal = new AbortController().signal;

  assert.equal(await controller.getResumeFile(INTERVIEW_ID, { signal }), preview);
  assert.equal(await controller.downloadResumeFile(INTERVIEW_ID, { signal }), original);
  assert.deepEqual(calls, [
    { kind: "download", path: `/api/v1/interviews/${INTERVIEW_ID}/resume-file`, options: { signal } },
    { kind: "download", path: `/api/v1/interviews/${INTERVIEW_ID}/resume-file?download=true`, options: { signal } },
  ]);
});

test("lists authorized participant options for the selected application", async () => {
  const { client, calls } = queuedClient([{ data: [
    { id: USER_ID, display_name: "张小北", roles: ["interviewer"] },
    { id: "77777777-7777-4777-8777-777777777777", display_name: "王磊", roles: ["hiring_manager"] },
  ], meta: { count: 2 } }]);
  const controller = createInterviewController({ client });
  const signal = new AbortController().signal;

  const options = await controller.listParticipantOptions(APPLICATION_ID, { signal });

  assert.deepEqual(calls, [{
    kind: "request",
    path: `/api/v1/applications/${APPLICATION_ID}/interview-participant-options`,
    options: { signal },
  }]);
  assert.deepEqual(options, [
    { id: USER_ID, name: "张小北", roles: ["interviewer"] },
    { id: "77777777-7777-4777-8777-777777777777", name: "王磊", roles: ["hiring_manager"] },
  ]);
});

test("lists submitted feedback summaries for an authorized reviewer", async () => {
  const { client, calls } = queuedClient([{ data: [{
    ...apiInterview(),
    id: FEEDBACK_ID,
    interview_id: INTERVIEW_ID,
    author: { id: USER_ID, display_name: "张小北" },
    status: "submitted",
    ratings: { professional_ability: 4, problem_solving: 3, communication: 4, role_fit: 3 },
    strengths: "项目证据充分",
    risks: "规模化经验待确认",
    conclusion: "recommend",
    notes: "建议推进",
    submitted_at: "2026-07-15T12:00:00Z",
    version: 2,
  }], meta: { count: 1 } }]);
  const controller = createInterviewController({ client });

  const feedbacks = await controller.listFeedbacks(INTERVIEW_ID);

  assert.deepEqual(calls, [{ kind: "request", path: `/api/v1/interviews/${INTERVIEW_ID}/feedbacks`, options: {} }]);
  assert.equal(feedbacks.length, 1);
  assert.deepEqual(feedbacks[0].author, { id: USER_ID, name: "张小北" });
  assert.equal(feedbacks[0].conclusion, "推荐");
  assert.equal(feedbacks[0].ratings.professional, "优秀");
});

test("loads only interview-scoped redacted candidate materials", async () => {
  const { client, calls } = queuedClient([{ data: {
    interview_id: INTERVIEW_ID,
    candidate: { id: CANDIDATE_ID, display_name: "赵宁", current_title: "大模型应用工程师" },
    job: { id: JOB_ID, title: "AI 工程师" },
    jd: { id: "88888888-8888-4888-8888-888888888888", version_number: 3, description: "负责 RAG 与 Agent 交付" },
    resume: { id: "99999999-9999-4999-8999-999999999999", version_number: 2, preview_text: "[REDACTED_NAME]\nRAG 项目经验" },
    screening: { id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", required_missing: ["Kubernetes"], risks: ["团队规模待确认"], questions: ["如何评估 RAG 质量？"] },
  } }]);
  const controller = createInterviewController({ client });

  const materials = await controller.getMaterials(INTERVIEW_ID);

  assert.deepEqual(calls, [{ kind: "request", path: `/api/v1/interviews/${INTERVIEW_ID}/materials`, options: {} }]);
  assert.equal(materials.resume.previewText, "[REDACTED_NAME]\nRAG 项目经验");
  assert.deepEqual(materials.interviewFocus.requiredMissing, ["Kubernetes"]);
  assert.deepEqual(materials.interviewFocus.suggestedQuestions, ["如何评估 RAG 质量？"]);
  assert.equal(materials.candidate.name, "赵宁");
});

test("checks a new schedule through the application-scoped conflict endpoint", async () => {
  const { client, calls } = queuedClient([{ data: { hard: [], soft: [INTERVIEW_ID] } }]);
  const controller = createInterviewController({ client });

  const result = await controller.checkConflicts(null, {
    applicationId: APPLICATION_ID,
    date: "2026-07-15",
    time: "10:00",
    duration: 60,
    timezone: "Asia/Shanghai",
    participantIds: [USER_ID],
  });

  assert.deepEqual(result, { hard: [], soft: [INTERVIEW_ID], calendarHard: [], calendarSoft: [], unconfirmed: [] });
  assert.deepEqual(calls, [{
    kind: "request",
    path: "/api/v1/interview-conflicts",
    options: {
      method: "POST",
      body: {
        application_id: APPLICATION_ID,
        starts_at: "2026-07-15T10:00:00+08:00",
        ends_at: "2026-07-15T11:00:00+08:00",
        participant_ids: [USER_ID],
        buffer_minutes: 15,
      },
    },
  }]);
});

test("loads, creates, updates, submits, and amends the current user's feedback", async () => {
  const draft = { id: FEEDBACK_ID, interview_id: INTERVIEW_ID, author_id: USER_ID, status: "draft", ratings: {}, strengths: null, risks: null, conclusion: null, notes: null, version: 1, submitted_at: null };
  const submitted = { ...draft, status: "submitted", version: 3, submitted_at: "2026-07-15T12:00:00+08:00" };
  const { client, calls } = queuedClient([
    { data: { status: "draft", version: 0 } },
    { data: draft },
    { data: submitted },
    { data: { ...submitted, status: "amended", conclusion: "strong_recommend", version: 4 } },
  ]);
  const keys = ["submit-key"];
  const controller = createInterviewController({ client, idempotencyKey: () => keys.shift() });
  const form = {
    ratings: { professional: "优秀", problem: "良好", communication: "良好", fit: "优秀" },
    strengths: "技术基础扎实",
    risks: "管理经验待确认",
    conclusion: "推荐",
    notes: "进入下一轮",
  };

  const empty = await controller.getMyFeedback(INTERVIEW_ID);
  const saved = await controller.saveMyFeedback(INTERVIEW_ID, form, 0);
  const completed = await controller.submitMyFeedback(INTERVIEW_ID);
  const amended = await controller.amendFeedback(saved, { ...form, conclusion: "强烈推荐" }, "补充技术证据");

  const body = { ratings: { professional_ability: 4, problem_solving: 3, communication: 3, role_fit: 4 }, strengths: "技术基础扎实", risks: "管理经验待确认", conclusion: "recommend", notes: "进入下一轮" };
  assert.equal(empty.version, 0);
  assert.equal(saved.id, FEEDBACK_ID);
  assert.equal(completed.status, "submitted");
  assert.equal(amended.conclusion, "强烈推荐");
  assert.deepEqual(calls, [
    { kind: "request", path: `/api/v1/interviews/${INTERVIEW_ID}/my-feedback`, options: {} },
    { kind: "request", path: `/api/v1/interviews/${INTERVIEW_ID}/my-feedback`, options: { method: "PUT", body, ifMatch: '"0"' } },
    { kind: "request", path: `/api/v1/interviews/${INTERVIEW_ID}/my-feedback/submit`, options: { method: "POST", idempotencyKey: "submit-key" } },
    { kind: "request", path: `/api/v1/interview-feedback/${FEEDBACK_ID}/amendments`, options: { method: "POST", body: { ...body, ratings: { professional_ability: 4, problem_solving: 3, communication: 3, role_fit: 4 }, conclusion: "strong_recommend", reason: "补充技术证据" }, ifMatch: '"1"' } },
  ]);
});

test("ambiguous feedback submission retries reuse one idempotency key", async () => {
  const unavailable = Object.assign(new Error("response lost"), { status: 0, kind: "unavailable" });
  const definitive = Object.assign(new Error("invalid state"), { status: 409, code: "invalid_state_transition" });
  const submitted = {
    id: FEEDBACK_ID,
    interview_id: INTERVIEW_ID,
    author_id: USER_ID,
    status: "submitted",
    ratings: {},
    version: 3,
  };
  const { client, calls } = queuedClient([unavailable, { data: submitted }, definitive, { data: submitted }]);
  const keys = ["ambiguous-key", "definitive-key", "fresh-key"];
  const controller = createInterviewController({ client, idempotencyKey: () => keys.shift() });

  await assert.rejects(() => controller.submitMyFeedback(INTERVIEW_ID), unavailable);
  await controller.submitMyFeedback(INTERVIEW_ID);
  await assert.rejects(() => controller.submitMyFeedback(INTERVIEW_ID), definitive);
  await controller.submitMyFeedback(INTERVIEW_ID);

  assert.deepEqual(
    calls.map((call) => call.options.idempotencyKey),
    ["ambiguous-key", "ambiguous-key", "definitive-key", "fresh-key"],
  );
});

test("rejects missing resource identity and version before network I/O", async () => {
  const { client, calls } = queuedClient([]);
  const controller = createInterviewController({ client });

  await assert.rejects(() => controller.get(""), { code: "INTERVIEW_ID_REQUIRED" });
  await assert.rejects(() => controller.save({ id: INTERVIEW_ID }, {}), { code: "INTERVIEW_VERSION_REQUIRED" });
  await assert.rejects(() => controller.saveMyFeedback(INTERVIEW_ID, {}, null), { code: "FEEDBACK_VERSION_REQUIRED" });
  await assert.rejects(() => controller.amendFeedback({ id: FEEDBACK_ID }, {}, "reason"), { code: "FEEDBACK_VERSION_REQUIRED" });
  assert.equal(calls.length, 0);
});

test("candidate interview history is derived only from matching server records", () => {
  const records = [
    { id: INTERVIEW_ID, serverBacked: true, candidateId: CANDIDATE_ID, round: "一面", date: "2026-07-15", time: "10:00", interviewers: ["张小北"], status: "已完成", feedbackStatus: "已提交", feedback: { conclusion: "推荐", strengths: "技术基础扎实" } },
    { id: "other", serverBacked: true, candidateId: "other-candidate", round: "二面", date: "2026-07-16", time: "11:00", interviewers: ["王磊"], status: "已安排", feedbackStatus: "未开始", feedback: null },
    { id: "local", serverBacked: false, candidateId: CANDIDATE_ID, round: "本地夹具" },
  ];

  assert.deepEqual(deriveCandidateInterviews(CANDIDATE_ID, records), [{
    interviewId: INTERVIEW_ID,
    round: "一面",
    time: "2026-07-15 10:00",
    interviewer: "张小北",
    result: "推荐",
    feedback: "技术基础扎实",
  }]);
});

test("schedule candidates come only from server-backed pending applications", () => {
  const pending = {
    id: APPLICATION_ID,
    candidateId: CANDIDATE_ID,
    applicationId: APPLICATION_ID,
    serverBacked: true,
    stage: "待安排",
    jobStatus: "open",
  };
  const records = [
    { ...pending, id: "fixture", serverBacked: false },
    pending,
    { ...pending, id: "interviewing", applicationId: "other", stage: "面试中" },
    { ...pending, id: "missing-application", applicationId: "" },
    { ...pending, id: "closed-job", applicationId: "closed", jobStatus: "closed" },
    { ...pending, id: "archived-job", applicationId: "archived", jobStatus: "archived" },
    pending,
  ];

  assert.deepEqual(selectSchedulableCandidates(records), [pending]);
});
