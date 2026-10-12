/// <reference types="vitest/config" />
import path from 'node:path'
import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'
import pkg from './package.json' with { type: 'json' }

// `npm run dev` serves the app on http://localhost:5173 and sends /api to the dashboard API: by default the one the
// SSH tunnel forwards to localhost:8080 (docs/dashboard.md); set DASHBOARD_API to use another.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  define: { __APP_VERSION__: JSON.stringify(pkg.version) },
  resolve: {
    alias: { '@': path.resolve(import.meta.dirname, 'src') },
  },
  server: {
    proxy: {
      '/api': { target: process.env.DASHBOARD_API ?? 'http://localhost:8080', changeOrigin: true },
    },
  },
  build: {
    // Every asset as a file: the API's Content-Security-Policy refuses data: fonts (only images may be data: URLs).
    assetsInlineLimit: 0,
  },
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
    // no styles in tests, except the two files the contrast test reads the colours from
    css: { include: [/tailwindcss\/theme\.css/, /src\/index\.css/] },
    restoreMocks: true,
  },
})
