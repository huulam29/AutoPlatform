/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// `npm run dev` serves the UI on :5173 and proxies the API to the dev server
// (`python -m agentline.api.dev ... --port 8787`).
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": "http://127.0.0.1:8787",
      "/healthz": "http://127.0.0.1:8787",
    },
  },
  build: { outDir: "dist", emptyOutDir: true, chunkSizeWarningLimit: 1000 },
  test: { environment: "node", include: ["src/**/*.test.ts"] },
});
