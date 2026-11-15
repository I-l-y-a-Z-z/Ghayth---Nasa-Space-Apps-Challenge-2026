import {cpSync, mkdirSync, rmSync, writeFileSync} from 'node:fs';
const out = '.cache/demo-host';
rmSync(out, {recursive: true, force: true});
mkdirSync(`${out}/app`, {recursive: true});
cpSync('apps/web/dist', `${out}/app`, {recursive: true});
cpSync('infrastructure/cloudflare/pages-worker.js', `${out}/_worker.js`);
cpSync('apps/web/dist/_headers', `${out}/_headers`);
writeFileSync(`${out}/_routes.json`, JSON.stringify({version: 1, include: ['/*'], exclude: ['/app/assets/*', '/app/demo-fixtures.json']}));
console.log(`Demo host prepared at ${out}`);
