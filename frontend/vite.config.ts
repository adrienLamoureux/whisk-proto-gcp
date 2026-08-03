import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Everything the app calls is same-origin in production (one Cloud Run service
// serves the bundle and the API), so dev proxies the same paths rather than
// hardcoding a base URL the bundle would then have to carry.
const backend = process.env.WHISK_API ?? "http://localhost:8080";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: Object.fromEntries(
      ["/api", "/media", "/config.json", "/health", "/a2a", "/mcp"].map((path) => [
        path,
        { target: backend, changeOrigin: true },
      ]),
    ),
  },
  build: { outDir: "dist", sourcemap: true },
});
