import React from "react";
import { createRoot } from "react-dom/client";
import { createBrowserRouter, RouterProvider } from "react-router-dom";
import { App } from "./App.jsx";
import { AppRouteError } from "./AppRouteError.jsx";
import { BrandingProvider } from "./Branding.jsx";
import { applyDocumentBranding, loadBranding } from "./branding.js";
import "./styles.css";
import "./product-theme.css";
import "./product-theme-interviews.css";

async function bootstrap() {
  const branding = await loadBranding();
  const cleanup = applyDocumentBranding(branding);
  window.addEventListener("pagehide", cleanup, { once: true });
  const router = createBrowserRouter([{ path: "*", element: <App />, errorElement: <AppRouteError /> }]);
  createRoot(document.getElementById("root")).render(
    <React.StrictMode>
      <BrandingProvider value={branding}><RouterProvider router={router} /></BrandingProvider>
    </React.StrictMode>,
  );
}
void bootstrap();
