import { useCallback, useEffect, useRef, useState } from "react";
import { BadgeCheck, BriefcaseBusiness, Check, Clock3, Download, FileLock2, RefreshCw, ShieldCheck } from "lucide-react";
import { displayOfferDeadline } from "./offerDeadline.js";
import { publicOfferController } from "./publicOfferController.js";
import { publicOfferLoadState, publicOfferState, responseBody } from "./publicOfferViewState.js";
import "./product-theme-public-offer.css";

const terminal = {
  accepted: ["Offer 已接受", "您的接受结果、预计到岗日期和入职资料已登记，招聘负责人将与您联系后续安排。"],
  declined: ["回复已提交", "感谢您参与本次招聘流程，招聘负责人已收到您的回复。"],
  expired: ["Offer 已过期", "该 Offer 已超过回复期限，请联系招聘团队。"],
  withdrawn: ["Offer 已撤回", "该 Offer 已不再有效，请联系招聘团队。"],
  superseded: ["Offer 已更新", "该链接对应的版本已失效，请使用最新邮件中的链接。"],
  invalid: ["链接无效", "该 Offer 链接无效或已失效。"],
};

function todayInputValue() {
  const date = new Date();
  const offset = date.getTimezoneOffset() * 60_000;
  return new Date(date.getTime() - offset).toISOString().slice(0, 10);
}

function BrandHeader({ companyName }) {
  const name = companyName || "企业招聘";
  return <header className="public-offer-brandbar">
    <div className="public-offer-brand"><span className="public-offer-mark" aria-hidden="true">{name.slice(0, 1).toUpperCase()}</span><span><strong>{name}</strong><small>人才与组织发展</small></span></div>
    <span className="public-offer-secure"><ShieldCheck size={17} />安全录用确认页面</span>
  </header>;
}

function TerminalView({ offer, title, copy, onRetry }) {
  return <main className="public-offer-page"><div className="public-offer-shell"><BrandHeader companyName={offer?.companyName} /><section className="public-offer-terminal" role={onRetry ? "alert" : "status"}><span className="public-offer-result-icon"><Check size={24} /></span><h1>{title}</h1><p>{copy}</p>{onRetry && <button className="public-offer-button primary" type="button" onClick={onRetry}><RefreshCw size={16} />重试</button>}</section><footer className="public-offer-footer">本页面包含个人录用信息，请勿转发</footer></div></main>;
}

export function PublicOfferView({ token, controller = publicOfferController }) {
  const [state, setState] = useState({ loading: true, offer: null, error: "" });
  const [startDate, setStartDate] = useState("");
  const [onboardingData, setOnboardingData] = useState({ gender: "", phone: "", email: "", home_address: "" });
  const [reason, setReason] = useState("");
  const [confirm, setConfirm] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [pdf, setPdf] = useState({ loading: false, error: "" });
  const dialogRef = useRef(null);
  const openerRef = useRef(null);

  const load = useCallback(async () => {
    setState((current) => ({ ...current, loading: true, error: "" }));
    try {
      const offer = await controller.load(token);
      setState({ loading: false, offer, error: "" });
      setOnboardingData((current) => ({
        ...current,
        phone: current.phone || offer.onboardingPrefill?.phone || "",
        email: current.email || offer.onboardingPrefill?.email || "",
      }));
    } catch (error) {
      const kind = publicOfferLoadState(error);
      setState({ loading: false, offer: kind === "invalid" ? { status: "invalid" } : null, error: kind === "invalid" ? "" : "暂时无法加载 Offer，请稍后重试。" });
    }
  }, [controller, token]);

  useEffect(() => { void load(); }, [load]);
  useEffect(() => {
    const previous = document.title;
    document.title = `${state.offer?.companyName || "企业招聘"} | Offer 确认`;
    return () => { document.title = previous; };
  }, [state.offer?.companyName]);
  useEffect(() => { if (confirm) dialogRef.current?.focus(); }, [confirm]);

  const offerState = publicOfferState(state.offer);
  if (state.loading) return <main className="public-offer-page public-offer-loading" role="status" aria-live="polite">正在加载 Offer…</main>;
  if (state.error && !state.offer) return <TerminalView title="Offer 暂时无法加载" copy={state.error} onRetry={() => void load()} />;

  const offer = state.offer;
  if (terminal[offerState]) {
    const [title, copy] = terminal[offerState];
    return <TerminalView offer={offer} title={title} copy={copy} />;
  }

  async function submit(decision) {
    const body = responseBody(decision, startDate, reason, onboardingData);
    if (submitting || body === null) return;
    setSubmitting(true);
    try {
      const next = await controller.respond(token, body);
      setState({ loading: false, offer: { ...offer, ...next, status: decision }, error: "" });
      setStartDate(""); setReason(""); setOnboardingData({ gender: "", phone: "", email: "", home_address: "" }); setConfirm("");
    } catch {
      setState((current) => ({ ...current, error: "提交未完成，请稍后重试。" }));
      setSubmitting(false);
    }
  }

  async function downloadPdf() {
    if (pdf.loading) return;
    setPdf({ loading: true, error: "" });
    try {
      const blob = await controller.fetchPdf(token);
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url; link.download = "Offer.pdf"; document.body.append(link); link.click(); link.remove();
      URL.revokeObjectURL(url);
      setPdf({ loading: false, error: "" });
    } catch {
      setPdf({ loading: false, error: "Offer 文件暂时无法下载。" });
    }
  }

  function closeDialog() {
    setConfirm("");
    openerRef.current?.focus();
  }

  function updateOnboardingField(field, value) {
    setOnboardingData((current) => ({ ...current, [field]: value }));
  }

  const acceptanceReady = responseBody("accepted", startDate, "", onboardingData) !== null;

  return <main className="public-offer-page">
    <div className="public-offer-shell">
      <BrandHeader companyName={offer.companyName} />
      <article className="public-offer-content">
        <p className="public-offer-eyebrow">OFFER OF EMPLOYMENT</p>
        <h1>{offer.title || "正式录用通知"}</h1>
        {offer.candidateName && <p className="public-offer-greeting">{offer.candidateName}，您好：</p>}
        <p className="public-offer-intro">{offer.body || `感谢您参与 ${offer.companyName} 的招聘流程。经过综合评估，我们诚挚邀请您加入团队。`}</p>

        <dl className="public-offer-summary">
          <div><dt>录用岗位</dt><dd>{offer.jobTitle || "请联系招聘团队"}</dd></div>
          <div><dt>工作地点</dt><dd>{offer.location || "以入职通知为准"}</dd></div>
          <div><dt>薪酬方案</dt><dd>{offer.compensation || "以双方确认内容为准"}</dd></div>
          <div><dt>预计入职</dt><dd>由您确认到岗日期</dd></div>
        </dl>

        <section className="public-offer-section"><h2><BriefcaseBusiness size={19} />岗位与入职安排</h2><p>具体报到时间及入职材料将由招聘负责人在您接受 Offer 后与您确认。</p></section>
        {offer.benefits && <section className="public-offer-section"><h2><BadgeCheck size={19} />福利与补充说明</h2><p>{offer.benefits}</p></section>}
        <section className="public-offer-section"><h2><FileLock2 size={19} />确认说明</h2><p>本页面内容仅供您本人查阅。请在回复截止时间前确认是否接受；如有疑问，请先联系招聘负责人。</p></section>

        <div className="public-offer-deadline"><Clock3 size={19} /><span><strong>请于 {offer.deadline ? displayOfferDeadline(offer.deadline) : "请联系招聘团队"} 前回复</strong><small>超过截止时间后，确认链接将自动失效。</small></span></div>

        <div className="public-offer-actions">
          <div className="public-offer-contact"><strong>招聘联系人</strong><span>{offer.contact || "请回复 Offer 邮件联系招聘团队"}</span>{offer.pdfAvailable && <button className="public-offer-download" type="button" disabled={pdf.loading} onClick={() => void downloadPdf()}><Download size={15} />{pdf.loading ? "正在准备…" : "下载 Offer 文件"}</button>}{pdf.error && <small role="alert">{pdf.error}</small>}</div>
          <div className="public-offer-action-buttons">
            <button className="public-offer-button secondary" type="button" disabled={submitting} onClick={(event) => { openerRef.current = event.currentTarget; setConfirm("declined"); }}>婉拒 Offer</button>
            <button className="public-offer-button primary" type="button" disabled={submitting} onClick={(event) => { openerRef.current = event.currentTarget; setConfirm("accepted"); }}><Check size={17} />接受 Offer</button>
          </div>
        </div>
        {state.error && <p className="public-offer-error" role="alert">{state.error}</p>}
      </article>
      <footer className="public-offer-footer">{offer.companyName} · 本页面包含个人录用信息，请勿转发</footer>
    </div>

    {confirm && <div className="public-offer-dialog-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget && !submitting) closeDialog(); }} onKeyDown={(event) => { if (event.key === "Escape" && !submitting) closeDialog(); }}>
      <section className={`public-offer-dialog${confirm === "accepted" ? " public-offer-dialog-onboarding" : ""}`} role="dialog" aria-modal="true" aria-labelledby="offer-confirm-title" tabIndex="-1" ref={dialogRef}>
        <h2 id="offer-confirm-title">{confirm === "accepted" ? "确认接受 Offer" : "确认婉拒 Offer"}</h2>
        <p>{confirm === "accepted" ? "请确认入职资料。提交后招聘团队将据此为您办理后续入职手续。" : "提交后招聘负责人将收到您的回复。"}</p>
        {confirm === "accepted"
          ? <form className="public-offer-onboarding-form" onSubmit={(event) => { event.preventDefault(); void submit("accepted"); }}>
            <div className="public-offer-onboarding-scroll">
              <div className="public-offer-readonly-grid" aria-label="录用信息">
                <div><span>姓名</span><strong>{offer.onboardingPrefill?.candidateName || offer.candidateName || "以 Offer 信息为准"}</strong></div>
                <div><span>部门</span><strong>{offer.onboardingPrefill?.departmentName || "由招聘团队确认"}</strong></div>
                <div><span>职位</span><strong>{offer.onboardingPrefill?.jobTitle || offer.jobTitle || "由招聘团队确认"}</strong></div>
              </div>
              <div className="public-offer-form-grid">
                <label>性别<select name="gender" required value={onboardingData.gender} onChange={(event) => updateOnboardingField("gender", event.target.value)}><option value="">请选择</option><option value="male">男</option><option value="female">女</option></select></label>
                <label>手机号<input name="phone" type="tel" autoComplete="tel" required value={onboardingData.phone} onChange={(event) => updateOnboardingField("phone", event.target.value)} /></label>
                <label className="public-offer-form-wide">邮箱<input name="email" type="email" autoComplete="email" required value={onboardingData.email} onChange={(event) => updateOnboardingField("email", event.target.value)} /></label>
                <label className="public-offer-form-wide">家庭住址<textarea name="home_address" autoComplete="street-address" required value={onboardingData.home_address} placeholder="请填写当前家庭住址" onChange={(event) => updateOnboardingField("home_address", event.target.value)} /></label>
                <label className="public-offer-form-wide">预计到岗日期<input name="expected_start_date" type="date" min={todayInputValue()} required value={startDate} onChange={(event) => setStartDate(event.target.value)} /></label>
              </div>
            </div>
            {state.error && <p className="public-offer-error" role="alert">{state.error}</p>}
            <div className="public-offer-dialog-actions"><button className="public-offer-button secondary" type="button" disabled={submitting} onClick={closeDialog}>返回</button><button className="public-offer-button primary" type="submit" disabled={submitting || !acceptanceReady}>{submitting ? "正在提交…" : "确认接受并提交"}</button></div>
          </form>
          : <label>婉拒原因（选填）<textarea value={reason} placeholder="感谢您的时间，也欢迎留下原因" onChange={(event) => setReason(event.target.value)} /></label>}
        {confirm === "declined" && <div className="public-offer-dialog-actions"><button className="public-offer-button secondary" type="button" disabled={submitting} onClick={closeDialog}>返回</button><button className="public-offer-button primary" type="button" disabled={submitting} onClick={() => { void submit(confirm); }}>{submitting ? "正在提交…" : "确认提交"}</button></div>}
      </section>
    </div>}
  </main>;
}
