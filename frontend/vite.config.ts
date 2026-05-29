import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  publicDir: '../mock_data',
  server: {
    port: 4173,
    proxy: {
      '/runs': 'http://127.0.0.1:4174',
      '/collector': 'http://127.0.0.1:4174',
      '/schema': 'http://127.0.0.1:4174',
      '/healthz': 'http://127.0.0.1:4174'
    }
  }
})
