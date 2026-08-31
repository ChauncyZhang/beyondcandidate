import { AlertTriangle, Home, RefreshCw } from "lucide-react";
import { Link, useRouteError } from "react-router-dom";

function isDynamicImportFailure(error) {
  const message = String(error?.message || error || "");
  return /dynamically imported module|module script failed|chunkloaderror/i.test(message);
}

export function AppRouteError() {
  const error = useRouteError();
  const assetLoadFailed = isDynamicImportFailure(error);

  return <main className="route-error-page">
    <section className="route-error-panel" role="alert">
      <span className="route-error-icon" aria-hidden="true"><AlertTriangle size={24} /></span>
      <div>
        <h1>{assetLoadFailed ? "页面资源加载失败" : "页面暂时无法打开"}</h1>
        <p>{assetLoadFailed ? "网络或系统更新可能中断了本次加载。重新加载后即可继续。" : "请重新加载页面；如果问题仍然存在，请返回工作台后重试。"}</p>
      </div>
      <div className="route-error-actions">
        <button className="button primary" type="button" onClick={() => window.location.reload()}><RefreshCw size={16} />重新加载</button>
        <Link className="button" to="/"><Home size={16} />返回工作台</Link>
      </div>
    </section>
  </main>;
}

export default AppRouteError;
