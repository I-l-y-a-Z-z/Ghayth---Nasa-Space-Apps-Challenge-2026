import {activateDemo, demoActive, demoApi} from './demo';
export type Locale = 'ary' | 'fr' | 'en';
export type Point = [number, number];
export type Polygon = {type:'Polygon';coordinates:Point[][]};
export type Availability = 'available'|'partial'|'missing'|'unsupported'|'inapplicable';
export type Reason = {code:string; values?:Record<string,string|number|boolean|null>;crop_instance_id?:string|null;evidence_ids?:string[]};
export type Evidence = {id:string;source_id:string;source_version:string;source_url?:string|null;origin:string;availability:Availability;synthetic:boolean;period?:{start:string;end:string}|null;resolution_m?:number|null;depth_cm?:[number,number]|null;quality_notes?:string[];uncertainty?:string|null};
export type Metric = {value:string|number|null;unit:string;availability:Availability;origin:string;reason?:Reason|null; evidence_ids?:string[];depth_cm?:[number,number]|null};
export type Section<T> = {availability:Availability;data:T|null;reason?:Reason|null};
export type SoilTest = {property:string;value:string;unit:string;depth_top_cm:number;depth_bottom_cm:number;sample_date:string;source_label:string};
export type Equipment = {mode:'owned'|'shared'|'rented'|'hired_with_operator'|'unavailable'|'unknown';operator_included?:boolean;operator_labor?:'paid'|'family'|'included';cost_mad_per_ha?:string|null};
export type Constraints = {
 budget_mad:string|null;production_system:'rainfed'|'irrigated'|'unknown';irrigation_reliability:'reliable'|'unreliable'|'unknown';
 machinery_access:'available'|'unavailable'|'unknown';labor_person_days_available:string|null;labor_by_month:Record<string,string>;
 previous_crops:string[]|null;priorities:string[];sale_price_mad_per_kg:Record<string,string>;yield_kg_per_ha:Record<string,string>;
 setup_cost_mad:string|null;wage_mad_per_person_day:string|null;payment_lag_days:number;
 additional_costs?:Array<{id:string;label:string;date:string;amount_mad:string;kind:'setup'|'operating'}>;soil_tests?:SoilTest[];equipment?:Record<string,Equipment>;operation_cost_overrides?:Record<string,{inputs_mad_per_ha:string|null;machinery_mad_per_ha:string|null;paid_person_days_per_ha?:string|null;family_person_days_per_ha?:string|null;operator_days_per_ha?:string|null}>;[key:string]:unknown;
};
export type Plot = {id:string;name:string;revision:number;geometry:Polygon;area_ha:string;constraints:Constraints;analysis_mode?:'demo'|'live';project_id:string};
export type Profile = {plot_id:string;plot_revision:number;fixture_version:string;zone_id:string|null;rule_coverage:Availability;metrics:Record<string,Metric>;layers:Array<{id:string;availability:Availability;state?:string;reason?:Reason|null;evidence_ids:string[]}>;evidence:Evidence[];synthetic:boolean};
export type Analysis = {id:string;plot_id:string;plot_revision:number;fixture_version:string;status:'pending'|'running'|'complete'|'partial'|'unsupported'|'failed';profile:Profile|null;created_at:string;synthetic:boolean;error?:string|null};
export type Crop = {id:string;label_key:string;group:string;duration_days:number;duration_min_days?:number|null;duration_max_days?:number|null;synthetic:boolean;fallow?:boolean;references?:Array<Record<string,unknown>>;operations:Array<{id:string}>;};
export type Catalog = {version:string;fixture_version:string;synthetic:boolean;crops:Crop[]};
export type Selection = {crop_id:string;start_date?:string;end_date?:string;date_policy?:'auto'|'explicit'|'environmental'};
export type Scenario = {id:string;label_key:string;yield_multiplier:string;price_multiplier:string;evidence_ids:string[];synthetic:boolean;historical_years?:number[];[key:string]:unknown};
export type EvalRequest = {plot_id:string;plot_revision:number;start_date:string;horizon_days:number;sequence:Selection[];constraints:Constraints;scenario?:Partial<Scenario>};
export type Totals = Record<'initial_investment'|'operating_cost'|'expected_revenue'|'operating_margin'|'net_cash_flow'|'cash_before_first_receipt'|'peak_funding_need',Metric>;
export type Operation = {id:string;crop_instance_id:string;code:string;date:string;total_cash_cost_mad:string|null;paid_person_days:string|null;family_person_days:string|null;included_operator_person_days:string|null;machinery_mode:string;machinery_id?:string|null;booking_window?:{start:string;end:string};quantities?:Array<Record<string,unknown>>};
export type Evaluation = {
 id:string;plot_id:string;plot_revision:number;analysis_id:string;status:'fits_current_inputs'|'needs_confirmation'|'doesnt_fit';synthetic:boolean;
 rotation:{crops:Array<{id:string;crop_id:string;start_date:string;end_date:string;duration_days:number}>;start_date:string;end_date:string;duration_days:number};
 checks:Array<{outcome:'pass'|'unknown'|'fail';reason:Reason}>;pros:Reason[];cons:Reason[];
 financials:{whole_plot:Totals;per_ha:Totals;first_receipt_date:string|null;last_cash_event_date:string|null;break_even_yield:Metric;break_even_price?:Metric|null};
 timeline:Section<Operation[]>;cash_events:Array<{id:string;date:string;direction:'inflow'|'outflow';kind:string;amount_mad:string|null}>;
 market_history:Section<Array<{crop_id:string;date:string;price_mad_per_kg:string;market_level:string;locality:string}>>;
 suppliers:Section<Array<{id:string;name:string;service:string;locality:string;verification_date:string;contact?:string|null;synthetic:boolean}>>;
 machinery:Section<Array<{operation_id:string;machinery_id:string;mode:string;operator_included:boolean;cost_mad:string|null}>>;
 labor:Section<{paid_person_days:string|null;family_person_days:string|null;included_operator_person_days:string|null;paid_cost_mad:string|null;farm_person_days_by_month:Record<string,string|null>}>;
 evidence:Evidence[];input_snapshot:{request:EvalRequest;area_ha:string;profile:Profile;catalog:Catalog};
 climate_scenarios?:Array<Record<string,unknown>>;comparison_period?:{start:string;end:string}|null;
 environmental?:{version:string;score:number|null;score_range:number[];ranking_score:number|null;coverage:number;confidence:string;components:Array<{id:string;score:number|null;coverage:number}>;crops:Array<{crop_id:string;start_date:string;end_date:string;timing:{duration_days:number;earliest_days?:number|null;latest_days?:number|null;range_censored?:boolean;method:string}}>;}|null;
};
export type PlanSummary = {id:string;name:string;plot_id:string;plot_revision:number;revision:number;status:'draft'|'selected';evaluation_id:string|null;evaluation_status:Evaluation['status']|null;updated_at:string};
export type Plan = PlanSummary & {evaluation_snapshot:Evaluation|null;assumptions:Constraints;draft:EvalRequest|null};
export type WeatherSample = {id:string;title:string;provider:string;source_url:string;period:{start:string;end:string};variables:Array<{id:string;label:string;unit:string}>;rows:Array<{date:string;values:Record<string,number|null>}>;notes:string[];synthetic:boolean};
export type Workspace = {plots:Plot[];plans:PlanSummary[];scenarios:Array<{plot_id:string;title:string;region:string;description:string;expected_status:string;assumptions_note:string}>;weather_samples:WeatherSample[];catalog:Catalog};
export type ChatReply = {message:string;locale:Locale;evaluation_ids:string[];facts:Array<Record<string,unknown>>;evidence_ids:string[];read_only:boolean;synthetic:boolean;what_if?:Evaluation|null;requires_confirmation?:boolean;suggested_actions?:Array<Record<string,unknown>>};
export class ApiError extends Error {constructor(public code:string,message:string,public status:number){super(message);}}
export async function api<T>(path:string,body?:unknown,method?:string):Promise<T>{
 if(demoActive()) return demoApi<T>(path,body,method);
 let response:Response;
 try{response=await fetch(path,{method:method || (body===undefined?'GET':'POST'),headers:{'Content-Type':'application/json','X-Project-ID':'demo'},...(body===undefined?{}:{body:JSON.stringify(body)})});}
 catch(error){if(location.hostname.endsWith('.pages.dev')){activateDemo();return demoApi<T>(path,body,method);}throw error;}
 const value = await response.json().catch(()=>({}));
 if(value.error?.code==='DEMO_FALLBACK'){activateDemo();return demoApi<T>(path,body,method);}
 if(!response.ok) throw new ApiError(value.error?.code || 'REQUEST_FAILED',value.error?.message || String(response.status),response.status);
 return value as T;
}
