import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The API is same-origin through this proxy, so the browser never needs CORS and
// the token header is never sent to a third-party host.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/health": "http://127.0.0.1:8000",
      "/models": "http://127.0.0.1:8000",
      "/route": "http://127.0.0.1:8000",
      "/chat": "http://127.0.0.1:8000",
      "/conversations": "http://127.0.0.1:8000",
      "/projects": "http://127.0.0.1:8000",
      "/tools": "http://127.0.0.1:8000",
      "/approvals": "http://127.0.0.1:8000",
      "/actions": "http://127.0.0.1:8000",
      "/documents": "http://127.0.0.1:8000",
      "/memory": "http://127.0.0.1:8000",
      "/runs": "http://127.0.0.1:8000",
      "/audit-log": "http://127.0.0.1:8000",
      "/settings": "http://127.0.0.1:8000",
      "/agents": "http://127.0.0.1:8000",
      "/assistant": "http://127.0.0.1:8000",
      "/voice": "http://127.0.0.1:8000",
      "/system": "http://127.0.0.1:8000",
      "/stop": "http://127.0.0.1:8000",
      "/resume": "http://127.0.0.1:8000",
    },
  },
});
