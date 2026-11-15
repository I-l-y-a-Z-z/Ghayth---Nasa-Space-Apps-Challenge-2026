import assert from 'node:assert/strict';
import test from 'node:test';
import {DEV_SECURITY_HEADERS,SECURITY_HEADERS} from './headers.mjs';

test('web map requests retain the origin required by the OSM tile policy',()=>{
  for(const headers of [SECURITY_HEADERS,DEV_SECURITY_HEADERS]){
    assert.equal(headers['Referrer-Policy'],'strict-origin-when-cross-origin');
    const connect=headers['Content-Security-Policy'].split('; ').find(value=>value.startsWith('connect-src '));
    assert.ok(connect.includes('https://tile.openstreetmap.org'));
  }
});
