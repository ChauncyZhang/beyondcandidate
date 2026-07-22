import { lazy, Suspense, useEffect, useState } from "react";
import { CircleAlert } from "lucide-react";

const PdfResumeViewer = lazy(() => import("./PdfResumeViewer.jsx").then((module) => ({ default: module.PdfResumeViewer })));

function saveBrowserFile({ blob, filename }) {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename || "resume";
  anchor.hidden = true;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}

export function InterviewFeedbackWorkspace({ record, controller, children }) {
  const [activePane, setActivePane] = useState("resume");
  const [resumeState, setResumeState] = useState({ status: "loading", file: null, error: "" });
  const [reload, setReload] = useState(0);
  const [downloading, setDownloading] = useState(false);
  const [downloadError, setDownloadError] = useState("");

  useEffect(() => {
    const abortController = new AbortController();
    let active = true;
    let previewUrl = "";
    setResumeState((current) => ({ status: "loading", file: current.file, error: "" }));
    void controller.getResumeFile(record.id, { signal: abortController.signal }).then((file) => {
      if (active) {
        previewUrl = URL.createObjectURL(file.blob);
        setResumeState({ status: "ready", file: { ...file, url: previewUrl }, error: "" });
      }
    }).catch((error) => {
      if (active && error?.name !== "AbortError") {
        setResumeState((current) => ({ status: "error", file: current.file, error: error?.status === 404 ? "你没有查看此面试原始简历的权限。" : "请检查网络后重试，评价草稿不会丢失。" }));
      }
    });
    return () => {
      active = false;
      abortController.abort();
      if (previewUrl) URL.revokeObjectURL(previewUrl);
    };
  }, [controller, record.id, reload]);

  async function downloadOriginal() {
    setDownloading(true);
    setDownloadError("");
    try {
      saveBrowserFile(await controller.downloadResumeFile(record.id));
    } catch (error) {
      if (error?.name !== "AbortError") setDownloadError(error?.status === 404 ? "你没有下载此面试原始简历的权限。" : "原始文件下载失败，请重试。");
    } finally {
      setDownloading(false);
    }
  }

  return <div className="interview-feedback-workspace">
    <div className="interview-workspace-tabs" role="tablist" aria-label="简历与评价">
      <button type="button" role="tab" id="resume-tab" aria-controls="resume-panel" aria-selected={activePane === "resume"} className={activePane === "resume" ? "active" : ""} onClick={() => setActivePane("resume")}>简历</button>
      <button type="button" role="tab" id="evaluation-tab" aria-controls="evaluation-panel" aria-selected={activePane === "evaluation"} className={activePane === "evaluation" ? "active" : ""} onClick={() => setActivePane("evaluation")}>评价</button>
    </div>
    <div id="resume-panel" role="tabpanel" aria-labelledby="resume-tab" className={`interview-workspace-pane resume-pane ${activePane === "resume" ? "is-active" : ""}`}>
      <Suspense fallback={<div className="pdf-viewer-state" role="status" aria-live="polite">正在加载 PDF 阅读器</div>}><PdfResumeViewer file={resumeState.file} status={resumeState.status} error={resumeState.error} onRetry={() => setReload((value) => value + 1)} onDownload={() => void downloadOriginal()} downloading={downloading} /></Suspense>
      {downloadError && <div className="workspace-download-error" role="alert"><CircleAlert size={16} />{downloadError}</div>}
    </div>
    <div id="evaluation-panel" role="tabpanel" aria-labelledby="evaluation-tab" className={`interview-workspace-pane evaluation-pane ${activePane === "evaluation" ? "is-active" : ""}`}>
      {children}
    </div>
  </div>;
}

export default InterviewFeedbackWorkspace;
