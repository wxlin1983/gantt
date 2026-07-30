import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // The API is same-origin in production; proxying keeps the session
    // cookie working in development without CORS or SameSite exceptions.
    proxy: {
      "/api": { target: "http://localhost:8000", changeOrigin: true },
    },
  },
  test: { environment: "node", globals: true },
});
