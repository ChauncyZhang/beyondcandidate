import { createContext, useContext, useState } from "react";
import { DEFAULT_BRANDING } from "./branding.js";
import "./branding.css";

const BrandingContext = createContext(DEFAULT_BRANDING);
export const BrandingProvider = BrandingContext.Provider;
export function useBranding() { return useContext(BrandingContext); }

export function BrandMark({ className = "" }) {
  const branding = useBranding();
  const [failedUrl, setFailedUrl] = useState(null);
  const showImage = branding.logo_url && failedUrl !== branding.logo_url;
  return <div className={`product-brand-mark ${className}`}>
    {showImage
      ? <img key={branding.logo_url} src={branding.logo_url} alt={branding.name} onError={() => setFailedUrl(branding.logo_url)} />
      : <span className={`product-brand-monogram${branding.short_name.length > 4 ? " is-long" : branding.short_name.length > 2 ? " is-medium" : ""}`} role="img" aria-label={branding.name}>{branding.short_name}</span>}
  </div>;
}
