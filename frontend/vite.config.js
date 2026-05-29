import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
export default defineConfig({
    plugins: [react()],
    publicDir: '../mock_data',
    server: {
        port: 4173,
        proxy: {
            '/runs': 'http://127.0.0.1:8000',
            '/collector': 'http://127.0.0.1:8000',
            '/schema': 'http://127.0.0.1:8000',
            '/healthz': 'http://127.0.0.1:8000'
        }
    }
});
