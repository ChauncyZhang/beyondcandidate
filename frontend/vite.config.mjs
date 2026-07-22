import path from "node:path";
import { createRequire } from "node:module";
import { defineConfig, loadEnv, normalizePath } from "vite";
import react from "@vitejs/plugin-react";
import { viteStaticCopy } from "vite-plugin-static-copy";

const require = createRequire(import.meta.url);
const pdfjsDistPath = path.dirname(require.resolve("pdfjs-dist/package.json"));
const pdfAssetTargets = ["cmaps", "standard_fonts", "wasm"].map((directory) => ({
  src: normalizePath(path.join(pdfjsDistPath, directory, "*")),
  dest: directory,
  rename: { stripBase: true },
}));

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const proxyTarget = env.VITE_API_PROXY_TARGET || "http://127.0.0.1:8080";

  return {
    optimizeDeps: {
      include: ["react", "react-dom/client"],
    },
    server: {
      host: "0.0.0.0",
      allowedHosts: ["terminal.local"],
      warmup: {
        clientFiles: ["./src/main.jsx"],
      },
      proxy: {
        "/api": proxyTarget,
        "/health": proxyTarget,
      },
    },
    plugins: [react(), viteStaticCopy({ targets: pdfAssetTargets })],
  };
});
