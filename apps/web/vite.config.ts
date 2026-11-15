import { defineConfig, type Plugin } from 'vite';
import react from '@vitejs/plugin-react';
import { DEV_SECURITY_HEADERS, SECURITY_HEADERS, META_CSP, serializeHeaders } from '../../infrastructure/security/headers.mjs';

function productionSecurity(): Plugin {
  return {
    name: 'agrix-production-security',
    apply: 'build',
    transformIndexHtml: {
      order: 'post',
      handler: () => [{ tag: 'meta', attrs: { 'http-equiv': 'Content-Security-Policy', content: META_CSP }, injectTo: 'head-prepend' }],
    },
    generateBundle() {
      this.emitFile({ type: 'asset', fileName: '_headers', source: serializeHeaders() });
    },
  };
}

export default defineConfig({
  base: '/app/',
  plugins: [react(), productionSecurity()],
  server: { proxy: Object.fromEntries(['/workspace','/plots','/analyses','/rotations','/evaluations','/plans','/chat','/catalog','/client-config','/session','/speech','/health','/records'].map(path=>[path, 'http://127.0.0.1:8000'])), headers: DEV_SECURITY_HEADERS, host: '127.0.0.1', strictPort: true },
  preview: { headers: SECURITY_HEADERS, host: '127.0.0.1', strictPort: true },
  build: { sourcemap: false, target: 'es2022' },
});
