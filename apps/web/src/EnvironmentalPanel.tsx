import {cropText,formatNumber,i18n,type TextKey} from './i18n';
import {dateText} from './Charts';
import type {Evaluation,Locale} from './types';

export default function EnvironmentalPanel({evaluation,locale}:{evaluation:Evaluation;locale:Locale}) {
 const t=i18n(locale), environment=evaluation.environmental;
 if(!environment)return null;
 const labels:Record<string,TextKey>={water:'waterBalance',temperature:'temperature',drought:'drought',soil_ph:'soil_ph',soil_texture:'soilTexture',soil_moisture:'soil_moisture',terrain:'terrain_slope',elevation:'elevation'};
 return <section className="card environmental-panel" data-testid="environmental-assessment">
  <div className="section-title"><div><h3>{t('environmentalFit')}</h3><p>{t('environmentalHelp')}</p></div><strong><bdi>{formatNumber(environment.ranking_score,locale,1)} / 100</bdi></strong></div>
  <p>{t('evidenceCoverage')}: <bdi>{formatNumber(environment.coverage*100,locale,0)}%</bdi> · {t('screeningRange')}: <bdi>{environment.score_range.map(value=>formatNumber(value,locale,1)).join(' – ')}</bdi></p>
  <div className="environment-components">{environment.components.map(component=><div key={component.id}><span>{t(labels[component.id]||'source')}</span><strong><bdi>{formatNumber(component.score,locale,0)}</bdi></strong><small>{component.score===null?t('unavailable'):`${formatNumber(component.coverage*100,locale,0)}% ${t('evidenceCoverage')}`}</small></div>)}</div>
  <div className="table-scroll"><table><thead><tr><th>{t('crop')}</th><th>{t('sowing')}</th><th>{t('harvest')}</th><th>{t('weatherDuration')}</th><th>{t('timingRange')}</th></tr></thead><tbody>{environment.crops.map((crop,index)=><tr key={index}><td>{cropText(crop.crop_id,locale)}</td><td><bdi>{dateText(crop.start_date,locale)}</bdi></td><td><bdi>{dateText(crop.end_date,locale)}</bdi></td><td><bdi>{formatNumber(crop.timing.duration_days,locale,0)}</bdi> {t('days')}<small>{t(crop.timing.method==='historical_thermal_time'?'thermalEstimate':crop.timing.method==='historical_thermal_target_unreached'?'maturityUnreached':'nominalFallback')}</small></td><td><bdi>{formatNumber(crop.timing.earliest_days,locale,0)} – {formatNumber(crop.timing.latest_days,locale,0)}</bdi> {t('days')}{crop.timing.range_censored&&<small>{t('timingIncomplete')}</small>}</td></tr>)}</tbody></table></div>
  <p className="small-note">{t('environmentalCaution')}</p>
  <details><summary>{t('details')}</summary><pre dir="ltr">{JSON.stringify(environment,null,2)}</pre></details>
 </section>;
}
