import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    // In dev, the API runs separately; in production FastAPI serves this build.
    // 127.0.0.1, not localhost: Node may resolve localhost to IPv6 (::1) first, while
    // uvicorn listens on IPv4 only, which makes every API call fail with a 502.
    proxy: { '/api': 'http://127.0.0.1:8000' },
  },
})
