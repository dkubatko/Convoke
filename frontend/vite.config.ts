import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      // Non-docker dev: `npm run dev` against a locally running backend.
      // CONVOKE_API_PROXY can point elsewhere, e.g. the dockerized nginx
      // (http://localhost:8080) to develop against the running stack.
      '/api': process.env.CONVOKE_API_PROXY ?? 'http://localhost:8000',
    },
  },
})
