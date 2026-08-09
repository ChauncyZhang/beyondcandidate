import { useCallback, useEffect, useRef, useState } from "react";
import { Download, FileText, RefreshCw } from "lucide-react";
import { publicOfferController } from "./publicOfferController.js";
import { publicOfferLoadState, publicOfferState, responseBody } from "./publicOfferViewState.js";
import "./product-theme-public-offer.css";

const terminal = {
  accepted: ["已接受 Offer", "您的接受结果和预计入职日期已记录。"],
  declined: ["已婉拒 Offer", "您的决定已记录。"],
  expired: ["Offer 已过期", "该 Offer 已超过回复期限，请联系招聘团队。"],
  withdrawn: ["Offer 已撤回", "该 Offer 已不再有效，请联系招聘团队。"],
  superseded: ["Offer 已更新", "该链接对应的版本已失效，请使用最新邮件中的链接。"],
  invalid: ["链接无效", "该 Offer 链接无效或已失效。"],
};

export function PublicOfferView({ token, controller = publicOfferController }) {
  const [state, setState] = useState({ loading: true, offer: null, error: "" });
  const [startDate, setStartDate] = useState("");
  const [reason, setReason] = useState("");
  const [confirm, setConfirm] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [pdfAttempt, setPdfAttempt] = useState(0);
  const [pdf, setPdf] = useState({ loading: false, url: "", error: "" });
  const dialogRef = useRef(null);
  const openerRef = useRef(null);

  const load = useCallback(async () => {
    setState((current) => ({ ...current, loading: true, error: "" }));
    try {
      const offer = await controller.load(token);
      setState({ loading: false, offer, error: "" });
    } catch (error) {
      const kind = publicOfferLoadState(error);
      setState({
        loading: false,
        offer: kind === "invalid" ? { status: "invalid" } : null,
        error: kind === "invalid" ? "" : "暂时无法加载 Offer，请稍后重试。",
      });
    }
  }, [controller, token]);

  useEffect(() => {
    const previous = document.title;
    document.title = "BeyondCandidate | Offer 确认";
    return () => { document.title = previous; };
  }, []);
  useEffect(() => { void load(); }, [load]);
  useEffect(() => { if (confirm) dialogRef.current?.focus(); }, [confirm]);

  const offerState = publicOfferState(state.offer);
  useEffect(() => {
    let live = true;
    let objectUrl = "";
    if (offerState !== "active" || state.offer?.pdfAvailable !== true) {
      setPdf({ loading: false, url: "", error: "" });
      return () => {};
    }
    setPdf({ loading: true, url: "", error: "" });
    controller.fetchPdf(token).then((blob) => {
      if (!live) return;
      objectUrl = URL.createObjectURL(blob);
      setPdf({ loading: false, url: objectUrl, error: "" });
    }).catch(() => {
      if (live) setPdf({ loading: false, url: "", error: "Offer 文件暂时无法加载。" });
    });
    return () => {
      live = false;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [controller, token, offerState, state.offer?.pdfAvailable, pdfAttempt]);

  if (state.loading) {
    return <main className="public-offer-page public-offer-terminal" role="status" aria-live="polite">正在加载 Offer…</main>;
  }
  if (state.error && !state.offer) {
    return <main className="public-offer-page public-offer-terminal" role="alert"><h1>Offer 暂时无法加载</h1><p>{state.error}</p><button className="button" type="button" onClick={() => void load()}><RefreshCw size={16} />重试</button></main>;
  }

  const offer = state.offer;
  if (terminal[offerState]) {
    const [title, copy] = terminal[offerState];
    return <main className="public-offer-page public-offer-terminal"><h1>{title}</h1><p>{copy}</p></main>;
  }

  async function submit(decision) {
    const body = responseBody(decision, startDate, reason);
    if (submitting || body === null) return;
    setSubmitting(true);
    try {
      const next = await controller.respond(token, body);
      setState({ loading: false, offer: { ...offer, ...next, status: decision }, error: "" });
      setStartDate("");
      setReason("");
      setConfirm("");
    } catch {
      setState((current) => ({ ...current, error: "提交未完成，请稍后重试。" }));
      setSubmitting(false);
    }
  }

  function closeDialog() {
    setConfirm("");
    openerRef.current?.focus();
  }

  return <main className="public-offer-page">
    <header className="public-offer-header"><span>BeyondCandidate</span><strong>{offer.companyName}</strong></header>
    <section className="public-offer-content">
      <p className="public-offer-eyebrow">Offer 确认</p>
      {offer.candidateName && <p className="public-offer-greeting">{offer.candidateName}，您好</p>}
      <h1>{offer.jobTitle || "职位 Offer"}</h1>
      {offer.location && <p>{offer.location}</p>}
      <dl>
        <div><dt>回复截止日期</dt><dd>{offer.deadline || "请联系招聘团队"}</dd></div>
        {offer.contact && <div><dt>招聘联系人</dt><dd>{offer.contact}</dd></div>}
      </dl>
      {offer.summary && <section className="public-offer-summary"><h2>Offer 摘要</h2><p>{offer.summary}</p></section>}
      <section className="public-offer-pdf" aria-labelledby="public-offer-pdf-title">
        <h2 id="public-offer-pdf-title"><FileText size={18} />Offer 文件</h2>
        {pdf.loading && <p role="status">正在加载 Offer 文件…</p>}
        {pdf.error && <p className="public-offer-error" role="alert">{pdf.error}<button type="button" onClick={() => setPdfAttempt((value) => value + 1)}>重试</button></p>}
        {pdf.url && <><iframe title="Offer PDF 预览" src={pdf.url} /><a className="button secondary" href={pdf.url} download="Offer.pdf"><Download size={16} />下载 Offer PDF</a></>}
      </section>
      <div className="public-offer-actions">
        <button className="button" type="button" disabled={submitting} onClick={(event) => { openerRef.current = event.currentTarget; setConfirm("accepted"); }}>接受 Offer</button>
        <button className="button secondary" type="button" disabled={submitting} onClick={(event) => { openerRef.current = event.currentTarget; setConfirm("declined"); }}>婉拒 Offer</button>
      </div>
      {state.error && <p className="public-offer-error" role="alert">{state.error}</p>}
    </section>
    {confirm && <div className="public-offer-dialog-backdrop" onKeyDown={(event) => { if (event.key === "Escape") closeDialog(); }}>
      <section className="public-offer-dialog" role="dialog" aria-modal="true" aria-labelledby="offer-confirm-title" tabIndex="-1" ref={dialogRef}>
        <h2 id="offer-confirm-title">{confirm === "accepted" ? "确认接受 Offer" : "确认婉拒 Offer"}</h2>
        {confirm === "accepted"
          ? <label>预计入职日期<input name="expected_start_date" type="date" required value={startDate} onChange={(event) => setStartDate(event.target.value)} /></label>
          : <label>原因（可选）<textarea value={reason} onChange={(event) => setReason(event.target.value)} /></label>}
        <div><button className="button secondary" type="button" disabled={submitting} onClick={closeDialog}>取消</button><button className="button" type="button" disabled={submitting || (confirm === "accepted" && !startDate)} onClick={() => { void submit(confirm); }}>{submitting ? "正在提交…" : "确认提交"}</button></div>
      </section>
    </div>}
  </main>;
}
