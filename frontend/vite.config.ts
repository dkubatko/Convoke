import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      // Non-docker dev: `npm run dev` against a locally running backend.
      '/api': 'http://localhost:8000',
    },
  },
})
