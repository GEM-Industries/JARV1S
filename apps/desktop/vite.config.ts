import { defineConfig } from 'vite'
import { resolve } from 'node:path'

export default defineConfig({
  root: 'shell-src',
  build: {
    outDir: resolve(__dirname, 'shell-dist'),
    emptyOutDir: true,
  },
})
