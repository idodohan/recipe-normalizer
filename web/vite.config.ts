import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        // changeOrigin intentionally omitted (false by default) so the
        // Host header is preserved; HttpOnly session cookie passthrough
        // works with Vite's default proxy behaviour.
      },
    },
  },
})
