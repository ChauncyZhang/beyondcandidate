import path from "node:path";
import { createRequire } from "node:module";
import react from "@vitejs/plugin-react";
import { defineConfig, normalizePath } from "vite";
import { viteStaticCopy } from "vite-plugin-static-copy";

const require = createRequire(import.meta.url);
const pdfjsDistPath = path.dirname(require.resolve("pdfjs-dist/package.json"));
const pdfAssetTargets = ["cmaps", "standard_fonts", "wasm"].map((directory) => ({
  src: normalizePath(path.join(pdfjsDistPath, directory, "*")),
  dest: directory,
  rename: { stripBase: true },
}));

export default defineConfig({
  plugins: [react(), viteStaticCopy({ targets: pdfAssetTargets })],
  server: {
    proxy: {
      "/api": {
        target: process.env.VITE_API_PROXY_TARGET || "http://127.0.0.1:8080",
        changeOrigin: true,
      },
    },
  },
});
