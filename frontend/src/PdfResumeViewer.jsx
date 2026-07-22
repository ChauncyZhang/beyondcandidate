import { useEffect, useRef, useState } from "react";
import { Document, Page, pdfjs } from "react-pdf";
import "react-pdf/dist/Page/AnnotationLayer.css";
import "react-pdf/dist/Page/TextLayer.css";
import {
  ChevronLeft,
  ChevronRight,
  Download,
  FileText,
  Maximize2,
  Minus,
  Plus,
  RefreshCw,
} from "lucide-react";
import { clampPdfPage, nextPdfZoom } from "./pdfResumeViewerState.js";

pdfjs.GlobalWorkerOptions.workerSrc = new URL(
  "pdfjs-dist/build/pdf.worker.min.mjs",
  import.meta.url,
).toString();

const pdfAssetBase = import.meta.env.BASE_URL || "/";
const PDF_DOCUMENT_OPTIONS = {
  cMapUrl: `${pdfAssetBase}cmaps/`,
  standardFontDataUrl: `${pdfAssetBase}standard_fonts/`,
  wasmUrl: `${pdfAssetBase}wasm/`,
};

function isPdfFile(file) {
  return file?.blob?.type === "application/pdf" || file?.filename?.toLowerCase().endsWith(".pdf");
}

function isSafeTextFile(file) {
  const name = file?.filename?.toLowerCase() || "";
  return file?.blob?.type?.startsWith("text/") || [".txt", ".md", ".csv"].some((suffix) => name.endsWith(suffix));
}

export function PdfResumeViewer({ file, status, error, onRetry, onDownload, downloading = false }) {
  const viewportRef = useRef(null);
  const [pageNumber, setPageNumber] = useState(1);
  const [pageCount, setPageCount] = useState(0);
  const [zoom, setZoom] = useState(1);
  const [fitWidth, setFitWidth] = useState(true);
  const [pageWidth, setPageWidth] = useState(720);
  const [documentError, setDocumentError] = useState("");
  const [textContent, setTextContent] = useState("");

  useEffect(() => {
    const viewport = viewportRef.current;
    if (!viewport || typeof ResizeObserver === "undefined") return undefined;
    const observer = new ResizeObserver(([entry]) => {
      setPageWidth(Math.max(240, Math.floor(entry.contentRect.width - 32)));
    });
    observer.observe(viewport);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    let active = true;
    setTextContent("");
    if (!file || !isSafeTextFile(file) || isPdfFile(file)) return () => { active = false; };
    void file.blob.text().then((text) => {
      if (active) setTextContent(text);
    }).catch(() => {
      if (active) setDocumentError("文本预览暂时不可用，可下载原始文件后查看。");
    });
    return () => { active = false; };
  }, [file]);

  useEffect(() => {
    setPageNumber(1);
    setPageCount(0);
    setDocumentError("");
  }, [file]);

  const showPdf = file && isPdfFile(file);
  const showText = file && !showPdf && isSafeTextFile(file);
  const canDownload = Boolean(file) && status === "ready";

  return <section className="pdf-resume-viewer" aria-label="原始简历预览">
    <header className="pdf-viewer-toolbar">
      <div className="pdf-file-title"><FileText size={17} aria-hidden="true" /><span title={file?.filename || "原始简历"}>{file?.filename || "原始简历"}</span></div>
      {showPdf && <div className="pdf-page-controls" aria-label="PDF 页码控制">
        <button type="button" aria-label="上一页" title="上一页" disabled={pageNumber <= 1} onClick={() => setPageNumber((value) => clampPdfPage(value - 1, pageCount))}><ChevronLeft size={17} /></button>
        <span aria-live="polite"><strong>{pageNumber}</strong> / {pageCount || "—"}</span>
        <button type="button" aria-label="下一页" title="下一页" disabled={!pageCount || pageNumber >= pageCount} onClick={() => setPageNumber((value) => clampPdfPage(value + 1, pageCount))}><ChevronRight size={17} /></button>
      </div>}
      <div className="pdf-zoom-controls" aria-label="PDF 缩放控制">
        {showPdf && <>
          <button type="button" aria-label="缩小" title="缩小" disabled={zoom <= 0.5} onClick={() => { setFitWidth(false); setZoom((value) => nextPdfZoom(value, -1)); }}><Minus size={16} /></button>
          <span>{Math.round(zoom * 100)}%</span>
          <button type="button" aria-label="放大" title="放大" disabled={zoom >= 2} onClick={() => { setFitWidth(false); setZoom((value) => nextPdfZoom(value, 1)); }}><Plus size={16} /></button>
          <button type="button" className={fitWidth ? "active" : ""} aria-label="适合宽度" title="适合宽度" onClick={() => setFitWidth(true)}><Maximize2 size={16} /><span>适宽</span></button>
        </>}
        <button type="button" aria-label="下载原始文件" title="下载原始文件" disabled={!canDownload || downloading} onClick={onDownload}><Download size={16} /><span>{downloading ? "下载中" : "原文件"}</span></button>
      </div>
    </header>
    <div className="pdf-viewer-viewport" ref={viewportRef} tabIndex="0">
      {status === "loading" && <div className="pdf-viewer-state" role="status" aria-live="polite"><RefreshCw className="spin" size={24} /><strong>正在加载原始简历</strong><span>文件仅在当前授权范围内读取。</span></div>}
      {status === "error" && <div className="pdf-viewer-state error" role="alert"><FileText size={25} /><strong>原始简历暂时无法加载</strong><span>{error}</span><button className="button secondary" type="button" onClick={onRetry}><RefreshCw size={15} />重试</button></div>}
      {status === "ready" && showPdf && <Document
        file={file.url}
        options={PDF_DOCUMENT_OPTIONS}
        loading={<div className="pdf-viewer-state" role="status" aria-live="polite">正在解析 PDF</div>}
        error={<div className="pdf-viewer-state error" role="alert">{documentError || "PDF 无法解析，可下载原始文件后查看。"}</div>}
        onLoadSuccess={({ numPages }) => { setPageCount(numPages); setPageNumber((value) => clampPdfPage(value, numPages)); setDocumentError(""); }}
        onLoadError={() => setDocumentError("PDF 无法解析，可下载原始文件后查看。")}
      ><Page pageNumber={pageNumber} width={fitWidth ? pageWidth : undefined} scale={fitWidth ? 1 : zoom} renderAnnotationLayer renderTextLayer /></Document>}
      {status === "ready" && showText && <pre className="safe-text-preview">{textContent || "正在读取文本预览…"}</pre>}
      {status === "ready" && file && !showPdf && !showText && <div className="pdf-viewer-state"><FileText size={25} /><strong>此格式不支持在线预览</strong><span>可通过工具栏下载原始文件，并使用本机安全应用打开。</span></div>}
    </div>
  </section>;
}

export default PdfResumeViewer;
