import {useEffect,useRef,useState} from 'react';
import * as maplibregl from 'maplibre-gl';
import type {GeoJSONSource,Map as LibreMap,StyleSpecification} from 'maplibre-gl';
import mapWorkerUrl from 'maplibre-gl/dist/maplibre-gl-worker.mjs?url';
import type * as GeoJSON from 'geojson';
import 'maplibre-gl/dist/maplibre-gl.css';
import type {Locale,Point,Polygon} from './types';
import {i18n,formatNumber} from './i18n';

// MapLibre 6 uses a separate module worker; let Vite emit its portable asset URL.
maplibregl.setWorkerUrl(mapWorkerUrl);

type Props={locale:Locale;points:Point[];holes:Point[][];onImport:(polygon:Polygon)=>void;onChange:(points:Point[])=>void;onError:(message:string)=>void;selectedId:string|null;tileUrl?:string;mapStyleUrl?:string;geocoderUrl?:string};
const towns:Array<{names:string[];point:Point}>= [
 {names:['Settat','سطات'],point:[-7.62,33.0]}, {names:['Meknes','Meknès','مكناس'],point:[-5.55,33.89]},
 {names:['Casablanca','الدار البيضاء','الدارالبيضاء'],point:[-7.59,33.57]}, {names:['Rabat','الرباط'],point:[-6.84,34.02]},
 {names:['Marrakech','مراكش'],point:[-7.98,31.63]}, {names:['Fes','Fès','فاس'],point:[-5,34.03]},
 {names:['Tangier','Tanger','طنجة'],point:[-5.81,35.76]}, {names:['Agadir','أكادير','اكادير'],point:[-9.6,30.43]},
 {names:['Ouarzazate','ورزازات'],point:[-6.91,30.92]}, {names:['Oujda','وجدة'],point:[-1.91,34.68]},
 {names:['Beni Mellal','Béni Mellal','بني ملال'],point:[-6.36,32.34]}, {names:['El Jadida','الجديدة'],point:[-8.5,33.25]},
 {names:['Safi','آسفي','اسفي'],point:[-9.23,32.3]}, {names:['Kenitra','Kénitra','القنيطرة'],point:[-6.58,34.26]},
 {names:['Taza','تازة'],point:[-4.01,34.21]}, {names:['Khouribga','خريبكة'],point:[-6.9,32.88]},
 {names:['Berkane','بركان'],point:[-2.32,34.92]}, {names:['Nador','الناظور'],point:[-2.93,35.17]},
 {names:['Al Hoceima','الحسيمة'],point:[-3.93,35.25]}, {names:['Larache','العرائش'],point:[-6.15,35.19]},
 {names:['Errachidia','الراشيدية'],point:[-4.42,31.93]}, {names:['Taroudant','تارودانت'],point:[-8.88,30.47]},
 {names:['Tiznit','تزنيت'],point:[-9.73,29.7]}, {names:['Guelmim','كلميم'],point:[-10.06,28.99]},
 {names:['Laayoune','Laâyoune','العيون'],point:[-13.2,27.15]}, {names:['Dakhla','الداخلة'],point:[-15.96,23.7]},
 {names:['Essaouira','الصويرة'],point:[-9.77,31.51]}, {names:['Sidi Bennour','سيدي بنور'],point:[-8.43,32.65]},
 {names:['Khemisset','الخميسات'],point:[-6.07,33.83]}, {names:['Berrechid','برشيد'],point:[-7.58,33.27]},
 {names:['Ifrane','إفران','افران'],point:[-5.11,33.53]}, {names:['Midelt','ميدلت'],point:[-4.74,32.68]},
];
function norm(value:string){return value.toLocaleLowerCase().normalize('NFD').replace(/[\u0300-\u036f]/g,'').replace(/\s+/g,' ').trim();}
export function closedPolygon(points:Point[],holes:Point[][]=[]):Polygon{return {type:'Polygon',coordinates:[[...points,...(points.length?[points[0]]:[])] ,...holes]};}
export function approximateArea(points:Point[],holes:Point[][]=[]):number{
 if(points.length<3)return 0;const rad=Math.PI/180;let sum=0;
 for(let i=0;i<points.length;i++){const a=points[i],b=points[(i+1)%points.length];sum+=(b[0]-a[0])*rad*(2+Math.sin(a[1]*rad)+Math.sin(b[1]*rad));}
 return Math.max(0,Math.abs(sum*6371008.8**2/2)/10000-holes.reduce((total,ring)=>total+approximateArea(ring),0));
}
const emptyGeo=():GeoJSON.FeatureCollection=>({type:'FeatureCollection',features:[]});
export default function MapPanel({locale,points,holes,onChange,onError,selectedId,tileUrl,mapStyleUrl,geocoderUrl}:Props){
 const t=i18n(locale);const container=useRef<HTMLDivElement>(null);const map=useRef<LibreMap|null>(null);
 const pointsRef=useRef(points),changeRef=useRef(onChange),modeRef=useRef('view');
 const markers=useRef<maplibregl.Marker[]>([]);const history=useRef<Point[][]>([]);const lastSearch=useRef(0);const searchCache=useRef(new Map<string,Point>());
 const [mode,setMode]=useState('view'),[query,setQuery]=useState(''),[unavailable,setUnavailable]=useState(false),[basemapFailed,setBasemapFailed]=useState(false),[searching,setSearching]=useState(false);
 const fitted=useRef<string|null>(null);
 const [loaded,setLoaded]=useState(false);pointsRef.current=points;changeRef.current=onChange;modeRef.current=mode;
 const change=(next:Point[])=>{history.current.push(pointsRef.current.map(p=>[...p] as Point));onChange(next);};
 useEffect(()=>{
  if(!container.current)return;setLoaded(false);setBasemapFailed(false);
  const style:StyleSpecification={version:8,sources:{osm:{type:'raster',tiles:[tileUrl||'https://tile.openstreetmap.org/{z}/{x}/{y}.png'],tileSize:256,attribution:'© <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a> contributors'}},layers:[{id:'osm',type:'raster',source:'osm',minzoom:0,maxzoom:19}]};
  try{
   const instance=new maplibregl.Map({container:container.current,style:mapStyleUrl||style,center:[-7.6,33],zoom:6,attributionControl:false,maxZoom:19,locale:{'Map.Title':t('map')}});
   map.current=instance;instance.addControl(new maplibregl.AttributionControl({compact:false}));
   instance.on('style.load',()=>{instance.addSource('boundary',{type:'geojson',data:emptyGeo()});instance.addLayer({id:'parcel-fill',type:'fill',source:'boundary',filter:['==','$type','Polygon'],paint:{'fill-color':'#567e43','fill-opacity':.28}});instance.addLayer({id:'parcel-line',type:'line',source:'boundary',paint:{'line-color':'#173f32','line-width':3}});setLoaded(true);});
   instance.on('click',event=>{if(modeRef.current!=='draw')return;const target=event.originalEvent.target as HTMLElement;if(target.closest?.('.vertex-marker'))return;const last=pointsRef.current.at(-1);if(last&&instance.project(last).dist(event.point)<5)return;history.current.push(pointsRef.current.map(p=>[...p] as Point));changeRef.current([...pointsRef.current,[event.lngLat.lng,event.lngLat.lat]]);});
   instance.on('dblclick',event=>{if(modeRef.current==='draw'){event.preventDefault();setMode('edit');}});
   instance.on('error',event=>{if(String(event.error).includes('WebGL'))setUnavailable(true);else setBasemapFailed(true);});
   return()=>{markers.current.forEach(m=>m.remove());instance.remove();map.current=null;};
  }catch{setUnavailable(true);}
 },[mapStyleUrl,tileUrl]);
 useEffect(()=>{map.current?.getCanvas().setAttribute('aria-label',t('map'));},[locale,loaded]);
 useEffect(()=>{const m=map.current;if(!m||!loaded)return;const source=m.getSource('boundary') as GeoJSONSource|undefined;
  const feature:GeoJSON.Feature=points.length>=3?{type:'Feature',properties:{},geometry:closedPolygon(points,holes)}:{type:'Feature',properties:{},geometry:{type:'LineString',coordinates:points}};
  source?.setData(points.length?{type:'FeatureCollection',features:[feature]}:emptyGeo());
  markers.current.forEach(marker=>marker.remove());markers.current=[];
  if(mode!=='view')points.forEach((p,index)=>{const element=document.createElement('button');element.type='button';element.className='vertex-marker';element.textContent=String(index+1);element.setAttribute('aria-label',`${t('edit')} ${index+1}`);element.addEventListener('click',e=>e.stopPropagation());const marker=new maplibregl.Marker({element,draggable:true}).setLngLat(p).addTo(m);marker.on('dragend',()=>{const loc=marker.getLngLat();history.current.push(pointsRef.current.map(p=>[...p] as Point));changeRef.current(pointsRef.current.map((v,i)=>i===index?[loc.lng,loc.lat]:v));});markers.current.push(marker);});
 },[points,holes,mode,loaded,locale]);
 useEffect(()=>{if(!selectedId){fitted.current=null;return;}const m=map.current;if(!m||!loaded||points.length===0||fitted.current===selectedId)return;const bounds=new maplibregl.LngLatBounds();points.forEach(p=>bounds.extend(p));m.fitBounds(bounds,{padding:70,maxZoom:15,duration:600});fitted.current=selectedId;history.current=[];},[selectedId,loaded,points]);
 useEffect(()=>{const m=map.current;if(!m)return;if(mode==='draw'){m.doubleClickZoom.disable();m.getCanvas().style.cursor='crosshair';}else{m.doubleClickZoom.enable();m.getCanvas().style.cursor='';}},[mode]);
 const fly=(point:Point)=>map.current?.flyTo({center:point,zoom:12});
 async function search(){
  if(!query.trim())return;const key=norm(query);const cached=searchCache.current.get(key);if(cached){fly(cached);return;}
  const town=towns.find(v=>v.names.some(name=>norm(name)===key||norm(name).startsWith(key)));if(town){fly(town.point);return;}
  // External search is opt-in. Local towns work without a geocoding account or network.
  if(!geocoderUrl){onError(t('noResults'));return;}
  if(Date.now()-lastSearch.current<1100){onError(t('searchWait'));return;}lastSearch.current=Date.now();setSearching(true);
  try{const url=new URL(geocoderUrl);url.searchParams.set('q',query);url.searchParams.set('countrycodes','ma');url.searchParams.set('format','json');url.searchParams.set('limit','3');const response=await fetch(url);if(!response.ok)throw Error();const places=await response.json();if(!places.length){onError(t('noResults'));return;}const point:[number,number]=[Number(places[0].lon),Number(places[0].lat)];searchCache.current.set(key,point);fly(point);}catch{onError(t('noResults'));}finally{setSearching(false);}
 }
 return <section className="map-card" aria-label={t('map')}>
  <div className="map-search"><label className="sr-only" htmlFor="location-search">{t('search')}</label><input id="location-search" value={query} placeholder={t('searchPlaceholder')} onChange={e=>setQuery(e.target.value)} onKeyDown={e=>{if(e.key==='Enter'){e.preventDefault();void search();}}}/><button type="button" disabled={searching} onClick={()=>void search()}>{searching?t('checking'):t('searchButton')}</button><button className="square" type="button" title={t('locate')} aria-label={t('locate')} onClick={()=>{if(!navigator.geolocation){onError(t('locationUnavailable'));return;}navigator.geolocation.getCurrentPosition(p=>fly([p.coords.longitude,p.coords.latitude]),()=>onError(t('locationUnavailable')));}}>◎</button></div>
  <div ref={container} className="map-canvas" data-testid="map"/>
  <div className="map-zoom"><button type="button" aria-label={t('zoomIn')} onClick={()=>map.current?.zoomIn()}>＋</button><button type="button" aria-label={t('zoomOut')} onClick={()=>map.current?.zoomOut()}>−</button></div>
  {(unavailable||basemapFailed)&&<p className="map-fallback" role="status">{t(unavailable?'mapUnavailable':'basemapUnavailable')}</p>}
  <div className="map-tools">{mode==='view'?<>{points.length<3?<button type="button" onClick={()=>setMode('draw')}>＋ {t('draw')}</button>:<><button type="button" onClick={()=>setMode('edit')}>{t('edit')}</button><button type="button" onClick={()=>{change([]);setMode('draw');}}>{t('redraw')}</button></>}</>:<><button type="button" disabled={!history.current.length} onClick={()=>{const previous=history.current.pop();if(previous)onChange(previous);}}>↶ {t('undo')}</button><button type="button" disabled={points.length<3} onClick={()=>setMode('view')}>{t('finish')} ✓</button></>}</div>
  <div className="map-caption"><span>{t('drawingHelp')}</span><strong dir="auto">{formatNumber(approximateArea(points,holes),locale)} <bdi>{t('hectares')}</bdi></strong></div>

 </section>;
}
