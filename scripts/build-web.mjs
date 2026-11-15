import {cpSync,rmSync,mkdirSync} from 'node:fs';
import {fileURLToPath} from 'node:url';
const root=fileURLToPath(new URL('../',import.meta.url));
const destination=`${root}apps/api/src/agrix/static/appbuild`;
rmSync(destination,{recursive:true,force:true});mkdirSync(destination,{recursive:true});
cpSync(`${root}apps/web/dist`,destination,{recursive:true});
console.log('Built Ghayth web client copied to the API.');
