import type {Locale} from './types';
import {formatNumber} from './i18n';
export function dateText(date:string|null|undefined,locale:Locale){if(!date)return '—';const d=new Date(`${date.slice(0,10)}T12:00:00Z`);return Number.isNaN(d.getTime())?date:new Intl.DateTimeFormat(locale==='ary'?'ar-MA':locale==='fr'?'fr-MA':'en-GB',{day:'numeric',month:'short',year:'numeric',timeZone:'UTC'}).format(d);}
export function LineChart({data,locale,label,unit}:{data:Array<{date:string;value:number|null}>;locale:Locale;label:string;unit:string}){
 const values=data.filter(d=>d.value!==null&&Number.isFinite(d.value));if(!values.length)return null;
 const width=700,height=230,left=65,right=20,top=20,bottom=42;
 const nums=values.map(d=>d.value as number);let low=Math.min(...nums,0),high=Math.max(...nums,0);if(high===low)high=low+1;const pad=(high-low)*.1;low-=pad;high+=pad;
 const start=Date.parse(data[0].date),end=Date.parse(data[data.length-1].date);
 const x=(date:string)=>left+(Date.parse(date)-start)/Math.max(1,end-start)*(width-left-right);
 const y=(n:number)=>top+(high-n)/(high-low)*(height-top-bottom);
 let pen=false;const path=data.map(d=>{if(d.value===null){pen=false;return '';}const next=`${pen?'L':'M'}${x(d.date)},${y(d.value)}`;pen=true;return next;}).join(' ');
 return <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label={label} className="chart"><title>{label}</title>{Array.from({length:4},(_,i)=>{const n=low+(high-low)*i/3;return <g key={i}><line x1={left} x2={width-right} y1={y(n)} y2={y(n)} className="chart-grid"/><text x={left-10} y={y(n)+4} textAnchor="end" className="chart-label">{formatNumber(n,locale,1)}</text></g>})}<line x1={left} x2={width-right} y1={y(0)} y2={y(0)} className="chart-zero"/><path d={path} fill="none" stroke="#537e43" strokeWidth="3"/>{values.map((d,i)=><circle key={i} cx={x(d.date)} cy={y(d.value as number)} r={values.length>50?1.5:3} fill="#173f32"><title>{dateText(d.date,locale)}: {formatNumber(d.value,locale)} {unit}</title></circle>)}{[values[0],values[Math.floor((values.length-1)/2)],values[values.length-1]].map((d,i)=><text key={i} x={x(d.date)} y={height-9} textAnchor={i===0?'start':i===2?'end':'middle'} className="chart-label">{dateText(d.date,locale)}</text>)}</svg>;
}
