export const DEFAULT_BRANDING = Object.freeze({
  name: "BeyondCandidate",
  short_name: "BC",
  logo_url: "/favicon.svg",
  favicon_url: "/favicon.svg",
});

function assetPath(value) {
  if (value === null) return null;
  if (typeof value !== "string") throw new Error("Invalid branding asset");
  let decoded = value;
  // Check encoded forms too: browsers and proxies may decode paths differently.
  for (let depth = 0; depth < 8; depth += 1) {
    if (!decoded.startsWith("/") || decoded.startsWith("//") || /[?#\\\u0000-\u0020\u007f]/.test(decoded) || decoded.split("/").includes("..")) {
      throw new Error("Invalid branding asset");
    }
    const next = decodeURIComponent(decoded);
    if (next === decoded) return value;
    decoded = next;
  }
  throw new Error("Invalid branding asset");
}

export function parseBranding(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("Invalid branding");
  if (Object.keys(value).some((key) => !["name", "short_name", "logo_url", "favicon_url"].includes(key))) throw new Error("Invalid branding");
  const name = typeof value.name === "string" ? value.name.trim() : "";
  if (!name || /[\u0000-\u001f\u007f]/.test(value.name) || Array.from(name).length > 80 || typeof value.short_name !== "string" || !/^[A-Za-z0-9]{1,8}$/.test(value.short_name)) {
    throw new Error("Invalid branding");
  }
  const logo = assetPath(value.logo_url);
  return Object.freeze({ name, short_name: value.short_name, logo_url: logo,
    favicon_url: value.favicon_url === undefined ? logo : assetPath(value.favicon_url) });
}

export async function loadBranding({ fetchImpl = globalThis.fetch, timeoutMs = 1500, warn = (message) => console.warn(message) } = {}) {
  const controller = new AbortController();
  let timer;
  try {
    const result = await Promise.race([
      (async () => {
        let response;
        try {
          response = await fetchImpl("/branding.json", { credentials: "omit", cache: "no-store", redirect: "error", signal: controller.signal });
          if (!response.ok) return { warning: "product_branding_unavailable" };
        } catch {
          return { warning: "product_branding_unavailable" };
        }
        try {
          return { branding: parseBranding(await response.json()) };
        } catch {
          return { warning: "product_branding_invalid" };
        }
      })(),
      new Promise((resolve) => {
        timer = setTimeout(() => {
          resolve({ warning: "product_branding_timeout" });
          controller.abort();
        }, timeoutMs);
      }),
    ]);
    if (result.warning) warn(result.warning);
    return result.branding || DEFAULT_BRANDING;
  } finally {
    clearTimeout(timer);
  }
}

export function monogramIcon(branding) {
  const short = /^[A-Za-z0-9]{1,8}$/.test(branding.short_name) ? branding.short_name : "BC";
  const fontSize = short.length > 4 ? 9 : short.length > 2 ? 14 : 21;
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 48 48"><rect width="48" height="48" rx="8" fill="#0066e6"/><text x="24" y="25" text-anchor="middle" dominant-baseline="central" fill="white" font-family="Arial,sans-serif" font-weight="700" font-size="${fontSize}">${short}</text></svg>`;
  return `data:image/svg+xml,${encodeURIComponent(svg)}`;
}

export function applyDocumentBranding(branding, doc = document) {
  doc.title = `${branding.name} | 候选人全流程招聘平台`;
  const link = doc.querySelector('link[rel="icon"]') || doc.head.appendChild(doc.createElement("link"));
  link.rel = "icon";
  link.removeAttribute("type");
  const fallback = monogramIcon(branding);
  link.href = branding.favicon_url || fallback;
  const probe = new Image();
  probe.onerror = () => { link.href = fallback; };
  if (branding.favicon_url) probe.src = branding.favicon_url;
  return () => { probe.onerror = null; };
}
