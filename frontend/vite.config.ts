import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The new front end is served at /app while pages move over one at a time;
// the old app stays at / until everything has.
//
// In development Vite serves the pages and passes /api through to the
// FastAPI server on :8080, so the same login and data are used.
export default defineConfig({
  base: '/app/',
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': { target: 'http://127.0.0.1:8080', changeOrigin: true },
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  },
})
