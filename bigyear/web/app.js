"use strict";
import {buildPlayback, playbackState, portion, travelSummary} from './playback.js';
import {speciesComparison, observerCurve} from './comparison.js';
import {buildActivity, activityAt, activityCurve} from './activity.js';
import {searchNotice} from './search-status.js';
const $=id=>document.getElementById(id);
const NS="http://www.w3.org/2000/svg";
const el=(tag,attrs={},text)=>{const e=document.createElementNS(NS,tag);for(const[k,v]of Object.entries(attrs))e.setAttribute(k,v);if(text!==undefined)e.textContent=text;return e;};
const point=([lon,lat])=>[(lon+126)*19+18,(50.7-lat)*22+15];
const path=coords=>coords.map((p,i)=>`${i?'L':'M'}${point(p).map(n=>n.toFixed(2)).join(',')}`).join(' ');
const fmt=(d,options={month:'long',day:'numeric'})=>new Date(d+'T12:00:00').toLocaleDateString('en-US',options);
let route=null,day=0,zoom=1,pan=[0,0],drag=null,runPath='';
let contextBounds=[];
let timelines=[],elapsed=0,playing=false,frame=null,lastFrame=null,activeElements=null,curveElements=null;
let activityDays=[],activityElements=[];
let searchStatus=null;
function renderSearchNotice(){
 const notice=searchNotice(searchStatus,route,runPath);$('search-notice').hidden=!notice;if(!notice)return;
 $('search-heading').textContent=notice.heading;$('search-detail').textContent=notice.detail;
 $('search-result').hidden=!notice.bestPath;
 if(notice.bestPath)$('search-result').href='/?run='+encodeURIComponent(notice.bestPath);
}
async function refreshSearchStatus(){
 try{
   const response=await fetch('/run-status.json',{cache:'no-store'});
   if(response.status===404)return;
   if(response.ok){searchStatus=await response.json();renderSearchNotice();}
 }catch{/* Saved routes remain usable when optional live status is unavailable. */}
 setTimeout(refreshSearchStatus,10000);
}
const activityMetrics=[
 {key:'miles',svg:'miles-curve',counter:'total-miles',color:'#446b8a',digits:1},
 {key:'birdingHours',svg:'birding-curve',counter:'total-birding',color:'#36824b',digits:1},
 {key:'restDays',svg:'rest-curve',counter:'total-rest',color:'#806444',digits:0},
];
const lineFor=(event,kind)=>el('path',{d:path(event.shape.coords),class:`route-line ${event.leg.mode==='walk'?'walking':kind} ${event.leg.schematic?'schematic':''}`});

function pause(){
 playing=false;cancelAnimationFrame(frame);frame=null;lastFrame=null;
 $('play').textContent='▶';$('play').setAttribute('aria-label','Play route');
}
function seek(index){
 if(!route)return;pause();day=Math.max(0,Math.min(route.days.length-1,index));
 elapsed=timelines[day].duration;render();
}
function play(replay=false){
 if(!route)return;
 if(replay){day=0;elapsed=0;render();}
 else if(elapsed>=timelines[day].duration){
   if(day===route.days.length-1)day=0;
   elapsed=0;render();
 }
 playing=true;lastFrame=null;$('play').textContent='Ⅱ';$('play').setAttribute('aria-label','Pause route');
 frame=requestAnimationFrame(animate);
}
function animate(now){
 if(!playing)return;
 if(lastFrame!==null)elapsed+=Math.min(250,now-lastFrame)*Number($('speed').value);
 lastFrame=now;
 while(elapsed>=timelines[day].duration){
   elapsed-=timelines[day].duration;
   if(day===route.days.length-1){elapsed=timelines[day].duration;updateFrame();pause();return;}
   day++;render();
 }
 updateFrame();frame=requestAnimationFrame(animate);
}

function stopMarker(stop,index,selectedDay){
 const [x,y]=point([stop.lon,stop.lat]),radius=selectedDay===day?7:4.1;
 const circle=el('circle',{cx:x,cy:y,r:radius/zoom,'data-radius':radius,class:`stop ${selectedDay===day?'active':''}`,tabindex:0,role:'button','aria-label':`${stop.name}, ${route.days[selectedDay].date}, ${stop.new_species.length} new species`});
 circle.append(el('title',{},`${route.days[selectedDay].date} · ${stop.name} · +${stop.new_species.length}`));
 const choose=()=>{seek(selectedDay);selectStop(stop,index);};
 circle.addEventListener('click',choose);circle.addEventListener('keydown',e=>{if(e.key==='Enter')choose();});
 return circle;
}
function selectStop(stop,index){
  [...$('stops-list').children].forEach((b,i)=>b.classList.toggle('chosen',i===index));
  const host=$('stop-detail');host.replaceChildren();
  const h=document.createElement('h3');h.textContent=stop.name;host.append(h);
  const caption=document.createElement('p');caption.textContent=`${stop.new_species.length} new species · ${stop.species.length} credited at this exact location and date`;host.append(caption);
  if(stop.marine_trip){const note=document.createElement('p');note.textContent=stop.landing
    ? `Santa Cruz Island via ${stop.landing==='prisoners'?'Prisoners Harbor':'Scorpion Anchorage'}. Modeled two-hour crossing each way from Ventura, one hour total boarding, two hours birding, and ${stop.walking_minutes.toFixed(1)} minutes of round-trip island walking at 2.2 km/h. Exact dated terrestrial source pin; ferry availability, weather and footpaths are assumed.`
    : `Exact offshore observation pin, reached by a modeled 12-knot return charter. Two hours of observation time at this pin, full sailing, boarding, and dock walking count toward the 12-hour day. Vessel availability and weather are assumed.`;host.append(note);}
  const access=route.locations[stop.location].road_access;
  if(access){const note=document.createElement('p');note.textContent=route.config.walking_speed_kmh != null
    ? `Road access is approximately ${Math.round(stop.access_gap_m)} m from the reported pin. Add ${stop.walking_minutes.toFixed(1)} minutes of round-trip walking at ${route.config.walking_speed_kmh.toFixed(2)} km/h to ${route.config.birding_minutes/60} hours of birding. Gaps up to ${route.config.walking_threshold_m ?? 100} m remain within the birding allowance; footpaths are assumed.`
    : `Road access is approximately ${Math.round(access.offset_meters)} m from the reported pin. Walking and birding share the ${route.config.birding_minutes/60} hour stop allowance.`;host.append(note);}
  const species=new Map(route.species.map(s=>[s.id,s.name]));
  for(const id of [...stop.new_species,...stop.species.filter(s=>!stop.new_species.includes(s))]){
    const row=document.createElement('div');row.className='species-row';
    const label=document.createElement('div');label.textContent=(stop.new_species.includes(id)?'+ ':'· ')+(species.get(id)||id);row.append(label);
    const evidence=document.createElement('div');evidence.className='evidence-links';
    const refs=[...new Set((stop.evidence[id]||[]).map(e=>e.checklist||e.record).filter(Boolean))];
    refs.forEach((checklist,i)=>{if(i)evidence.append(document.createTextNode(' · '));
      const gbif=/^GBIF:\d+$/.test(checklist);
      const link=document.createElement(gbif||/^S\d+$/.test(checklist)?'a':'span');link.textContent=checklist;
      if(link.tagName==='A'){link.href=gbif?'https://www.gbif.org/occurrence/'+encodeURIComponent(checklist.slice(5)):'https://ebird.org/checklist/'+encodeURIComponent(checklist);link.target='_blank';link.rel='noopener noreferrer';}
      evidence.append(link);
    });row.append(evidence);host.append(row);
  }
}
function render(){
 if(!route)return;
 const d=route.days[day];$('date-slider').value=day;$('date-input').value=d.date;
 $('map-date').textContent=fmt(d.date,{month:'long',day:'numeric',year:'numeric'});
 const label=d.kind.replaceAll('_',' ');$('day-mode').textContent=label.toUpperCase();
 $('detail-date').textContent=fmt(d.date);$('detail-mode').textContent=label[0].toUpperCase()+label.slice(1)+(d.kind==='rest'?' · position retained':'');
 $('route-lines').replaceChildren();$('route-stops').replaceChildren();$('cursor').replaceChildren();
 // Completed days stay static. Only today's active trace and marker change per frame.
 for(let i=0;i<day;i++){
   for(const event of timelines[i].events)if(event.kind==='travel')$('route-lines').append(lineFor(event,route.days[i].kind));
   route.days[i].stops.forEach((stop,j)=>$('route-stops').append(stopMarker(stop,j,i)));
 }
 const travels=timelines[day].events.filter(e=>e.kind==='travel').map(event=>{
   const line=lineFor(event,d.kind);line.classList.add('current');line.dataset.progress='0';
   $('route-lines').append(line);return {event,line,progress:-1};
 });
 const stops=d.stops.map((stop,j)=>{const marker=stopMarker(stop,j,day);$('route-stops').append(marker);return marker;});
 const cursor=el('g',{'aria-label':'Traveler position'});
 cursor.append(el('circle',{r:10/zoom,'data-radius':10,class:'cursor-halo'}),el('circle',{r:3.5/zoom,'data-radius':3.5,class:'cursor-dot'}));$('cursor').append(cursor);
 activeElements={travels,stops,cursor,activeStop:-1};
 const host=$('stops-list');host.replaceChildren();$('stop-detail').replaceChildren();$('stop-count').textContent=d.stops.length?`${d.stops.length} stops`:'';
 d.stops.forEach((s,i)=>{const b=document.createElement('button');b.textContent=s.name;const small=document.createElement('small');small.textContent=`+${s.new_species.length} new · ${s.species.length} available species`;b.append(small);b.onclick=()=>selectStop(s,i);host.append(b);});
 if(d.stops.length)selectStop(d.stops[0],0);
 else {const p=document.createElement('p');p.className='empty-day';p.textContent=d.kind==='rest'?'No birding or travel scheduled. The traveler stays at the previous day’s position.':'A transit day. No species are credited. '+d.legs.map(l=>`${route.locations[l.from].name} → ${route.locations[l.to].name}`).join(' · ');host.append(p);}
 renderCurve();
 for(const chart of activityElements)chart.completed=activityCurve(activityDays,chart.key,chart.xy,day);
 updateFrame();
}
function updateFrame(){
 const state=playbackState(timelines[day],elapsed),d=route.days[day];
 activeElements.travels.forEach((entry,i)=>{
   const progress=state.travels[i]??0;
   if(progress!==entry.progress){
     entry.line.style.display=progress>0?'':'none';
     entry.line.setAttribute('d',path(portion(entry.event.shape,0,progress)));
     entry.line.dataset.progress=progress.toFixed(5);entry.progress=progress;
   }
 });
 activeElements.stops.forEach((marker,i)=>{const arrived=state.stops.includes(i);marker.style.display=arrived?'':'none';marker.setAttribute('tabindex',arrived?'0':'-1');});
 activeElements.cursor.setAttribute('transform',`translate(${point(state.cursor)})`);
 $('day-new').textContent=state.newSpecies;$('day-miles').textContent=state.miles.toFixed(1);$('day-hours').textContent=(state.minutes/60).toFixed(1);
 $('metrics-label').textContent=state.progress<1?'SO FAR TODAY':'SELECTED DAY · TOTALS';
 $('playback-status').textContent=state.status;$('day-progress').value=state.progress;
 $('day-progress').setAttribute('aria-label',`${Math.round(state.progress*100)}% of selected day illustrated`);
 const total=(day?route.days[day-1].cumulative_species:0)+state.newSpecies;
 $('curve-total').textContent=total;$('map-species-total').textContent=total;
 const comparison=speciesComparison(total,route.observer_coverage,d.date);
 $('comparison-date').textContent=`Through ${fmt(d.date,{month:'short',day:'numeric',year:'numeric'})}`;
 $('observer-count').hidden=!comparison;$('comparison-result').hidden=!comparison;
 if(comparison){
   $('observer-total').textContent=comparison.observed;
   $('comparison-share').textContent=comparison.percentage===null?'—':`${comparison.percentage.toFixed(1)}%`;
   $('comparison-gap').textContent=comparison.gap===0?'All reported species':`${comparison.gap} species behind`;
   const [observerX,observerY]=curveElements.xy(day+state.progress,comparison.observed);
   curveElements.observerMarker.setAttribute('cx',observerX);curveElements.observerMarker.setAttribute('cy',observerY);
 }
 const [x,y]=curveElements.xy(day+state.progress,total);
 curveElements.marker.setAttribute('cx',x);curveElements.marker.setAttribute('cy',y);
 curveElements.line.setAttribute('x1',x);curveElements.line.setAttribute('x2',x);
 const completed=[[0,0],...route.days.slice(0,day).map((d,i)=>[i+1,d.cumulative_species])].map(([i,s])=>curveElements.xy(i,s));
 completed.push([x,y]);curveElements.trace.setAttribute('d',completed.map((p,i)=>(i?'L':'M')+p).join(' '));
 const activity=activityAt(activityDays,day,state);
 $('activity-date').textContent=`Through ${fmt(d.date,{month:'short',day:'numeric',year:'numeric'})}`;
 for(const chart of activityElements){
   $(chart.counter).textContent=activity[chart.key].toLocaleString('en-US',{minimumFractionDigits:chart.digits,maximumFractionDigits:chart.digits});
   const [cx,cy]=chart.xy(day+state.progress,activity[chart.key]);
   const restStep=chart.key==='restDays'?` L${cx},${chart.xy(day,activityDays[day].before.restDays)[1]}`:'';
   chart.trace.setAttribute('d',chart.completed+restStep+` L${cx},${cy}`);
   chart.marker.setAttribute('cx',cx);chart.marker.setAttribute('cy',cy);
   chart.line.setAttribute('x1',cx);chart.line.setAttribute('x2',cx);
 }
 if(state.activeStop!==activeElements.activeStop){
   [...$('stops-list').children].forEach((button,i)=>button.classList.toggle('visiting',i===state.activeStop&&state.progress<1));
   activeElements.activeStop=state.activeStop;
 }
}
function renderCurve(){
 const svg=$('curve');svg.replaceChildren();const max=Math.max(route.annual_recorded_species_ceiling,route.observer_coverage?.annual_species??0,1),n=route.days.length;
 const xy=(i,s)=>[34+i/n*1140,135-s/max*113];
 for(let i=0;i<=4;i++){let y=135-i/4*113;svg.append(el('line',{x1:34,x2:1174,y1:y,y2:y,stroke:'#dbe9df','stroke-width':1}));svg.append(el('text',{x:0,y:y+3,fill:'#000','font-size':10},Math.round(max*i/4)));}
 const points=[xy(0,0),...route.days.map((d,i)=>xy(i+1,d.cumulative_species))];
 const d=points.map((p,i)=>(i?'L':'M')+p).join(' ');
 svg.append(el('path',{d:d+` L1174,135 L34,135 Z`,fill:'#36824b',opacity:.08}));svg.append(el('path',{d,fill:'none',stroke:'#36824b','stroke-opacity':.3,'stroke-width':2}));
 let observerMarker=null;
 if(route.observer_coverage){
   const reference=el('path',{d:observerCurve(route.days,route.observer_coverage,xy),fill:'none',stroke:'#5b6573','stroke-width':2,'stroke-dasharray':'7 4','data-series':'all-observers'});
   reference.append(el('title',{},`All observers · ${route.observer_coverage.scope_label}`));svg.append(reference);
   observerMarker=el('circle',{r:4,fill:'#fff',stroke:'#5b6573','stroke-width':2});
 }
 const trace=el('path',{fill:'none',stroke:'#36824b','stroke-width':2.5});
 const line=el('line',{y1:12,y2:135,stroke:'#000','stroke-dasharray':'3 3'}),marker=el('circle',{r:4,fill:'#000'});
 svg.append(trace,line,marker);if(observerMarker)svg.append(observerMarker);curveElements={xy,trace,line,marker,observerMarker};
}
function renderActivityCurves(){
 const n=activityDays.length;
 activityElements=activityMetrics.map(metric=>{
   const svg=$(metric.svg);svg.replaceChildren();
   const max=Math.max(activityDays.at(-1)[metric.key],1);
   const xy=(i,value)=>[48+i/n*340,124-value/max*108];
   for(let i=0;i<=3;i++){
     const y=124-i/3*108;
     svg.append(el('line',{x1:48,x2:388,y1:y,y2:y,stroke:'#dbe9df','stroke-width':1}));
     svg.append(el('text',{x:40,y:y+4,'text-anchor':'end',fill:'#000','font-size':13},Math.round(max*i/3).toLocaleString('en-US')));
   }
   for(const i of [0,Math.floor(n/2),n-1]){
     const x=xy(i===n-1?n:i,0)[0];
     svg.append(el('text',{x,y:148,'text-anchor':i===0?'start':i===n-1?'end':'middle',fill:'#000','font-size':12},fmt(activityDays[i].date,{month:'short',day:'numeric'})));
   }
   const full=activityCurve(activityDays,metric.key,xy);
   svg.append(el('path',{d:full+' L388,124 L48,124 Z',fill:metric.color,opacity:.06}),el('path',{d:full,fill:'none',stroke:metric.color,'stroke-opacity':.25,'stroke-width':2}));
   const trace=el('path',{fill:'none',stroke:metric.color,'stroke-width':2.5});
   const line=el('line',{y1:12,y2:124,stroke:'#000','stroke-dasharray':'3 3'}),marker=el('circle',{r:4,fill:metric.color});
   svg.append(trace,line,marker);
   return {...metric,xy,trace,line,marker,completed:''};
 });
}
async function loadRoute(file){
 pause();runPath=file;
 const response=await fetch('/api/route/'+file.split('/').map(encodeURIComponent).join('/'));if(!response.ok)throw Error('Could not load saved route');
 route=await response.json();day=route.days.map(d=>d.new_species.length>0).lastIndexOf(true);if(day<0)day=route.days.map(d=>d.stops.length>0).lastIndexOf(true);if(day<0)day=0;
 const audited=route.taxonomy_policy?.startsWith('lower48-2024-publisher-reviewed-countability-')||route.taxonomy_policy?.startsWith('lower48-2024-primary-plus-documented-provisionals-');$('countability-notice').hidden=!audited;
 $('countability-notice').querySelector('p').textContent=route.scoring_tiers
   ? `${route.scoring_tiers.primary} primary-list species + ${route.scoring_tiers.provisional} provisional species. Provisional credit follows the dated 2024 ABA allowance and remains labeled separately. Unresolved occurrence/access cases receive no credit; individual wildness remains a publisher-review proxy.`
   : 'Only supported evidence contributes to this score. Unresolved and excluded records receive no credit. Public eBird review is an explicit proxy for native wild occurrences; individual wild status remains unverified.';
 timelines=buildPlayback(route);elapsed=timelines[day].duration;
 activityDays=buildActivity(route);renderActivityCurves();
 $('activity-note').textContent=`Miles include roads${route.marine_extension?' and modeled boat paths':''}, including vehicle ferries; flights are excluded from the mileage curve. Birding hours use the planned ${route.config.birding_minutes/60}-hour allowance per exact stop${route.config.walking_speed_kmh != null ? '; round-trip access walking is added separately to the daily activity budget' : ', including walking'}. Rest days count completed days marked as rest. Click any curve to select a date.`;
 $('observer-label').textContent=route.observer_coverage?`All observers${audited?' (supported)':''} · ${route.observer_coverage.scope_label}`:'All observers';
 $('comparison-note').textContent=route.observer_coverage?'Unique species reported by everyone in the same geographic cohort, using the same eligibility and quality rules. Totals start January 1 and include the whole selected day; the route count follows its illustrated visits.':'Distinct species on the route through the selected date. All-observer comparison has not been prepared for this dataset.';
 if(route.marine_extension&&route.observer_coverage)$('comparison-note').textContent=route.observer_coverage.method;
 $('date-slider').max=route.days.length-1;$('date-input').min=route.days[0].date;$('date-input').max=route.days.at(-1).date;
 $('banner').style.display=route.synthetic?'block':'none';$('banner').textContent='SYNTHETIC DEMONSTRATION · All sightings, species, eligibility decisions and road costs in this experiment are fictional test fixtures. This is not a historical birding result.';
 const exp=$('experiment');exp.replaceChildren();const strong=document.createElement('strong');const period=route.days.length<300?`${fmt(route.days[0].date,{month:'short',day:'numeric'})} – ${fmt(route.days.at(-1).date,{month:'short',day:'numeric',year:'numeric'})}`:route.year;strong.textContent=`${period} · ${route.species_total} ${audited?'supported modeled':'distinct'} species`;const replay=!route.search.study&&!route.search.lineage&&route.search.candidate_evaluations===0&&route.search.warm_start&&Array.isArray(route.search.selected_model_opportunities);exp.append(strong,document.createTextNode(replay?`${route.search.elapsed_seconds.toFixed(1)} sec final calendar replay · optimization stages recorded separately`:`${(route.search.study?.elapsed_seconds??route.search.elapsed_seconds).toFixed(1)} sec ${route.search.study?route.search.study.arm+' study':route.search.lineage?'corrective search':'search'} · ${(route.search.study?.candidate_evaluations??route.search.candidate_evaluations).toLocaleString()} native candidate evaluations`));
 if(route.scoring_tiers){const tier=document.createElement('span');tier.className='transport-summary';tier.textContent=`${route.scoring_tiers.primary} primary + ${route.scoring_tiers.provisional} provisional`;exp.append(tier);}
 if(route.search.lineage){const history=document.createElement('span');history.className='transport-summary';history.textContent=`${route.search.lineage.prior_native_seconds/60} min joint search + ${route.search.lineage.repair_native_seconds/60} min access repair`;exp.append(history);}
 const travel=travelSummary(route),transport=document.createElement('span');transport.className='transport-summary';transport.textContent=travel.label;exp.append(transport);$('flight-key').hidden=!travel.hasFlights;$('schematic-key').hidden=!travel.hasSchematicRoads;
 $('boat-key').hidden=!travel.hasBoats;$('boat-key').lastChild.nodeValue=route.marine_extension?.island_days?'Modeled boat trips':'Modeled charter';$('mileage-unit').textContent=travel.hasBoats?'road + boat miles':'road miles';$('day-mile-unit').textContent=travel.hasBoats?'ROAD + BOAT MILES':'ROAD MILES';
 if(route.marine_extension){const note=document.createElement('span');note.className='transport-summary';const islands=route.marine_extension.island_days??0;const boats=islands?`${route.marine_extension.boat_days-islands} pelagic charter days + ${islands} island days`:`${route.marine_extension.boat_days} charter days`;note.textContent=route.marine_extension.native_joint_search?`${boats} · driving, flights and boats jointly searched`:`${boats} · +${route.marine_extension.new_species.length} species · terrestrial route fixed`;exp.append(note);}
 $('solution-status').textContent=route.status;$('annual-ceiling').textContent=route.annual_recorded_species_ceiling+' species';$('candidate-ceiling').textContent=route.candidate_species_ceiling+' species';$('upper-bound').textContent=route.analytical_count_bound?.upper_bound??route.optimization_upper_bound??'Not established';
 const list=document.createElement('ul');const dataNotes=route.coverage?[`Data release: ${route.data_release}; taxonomy policy: ${route.taxonomy_policy}.`,`${route.coverage.dispositions.eligible||0} eligible source rows; ${route.coverage.locations} source locations; ${route.candidate_reduction?.selected_birding_locations??'—'} retained locations.`]:[];if(route.display_geometry)dataNotes.push(route.display_geometry.note);if(travel.hasVehicleFerries)dataNotes.push('The supplied car profile includes identified vehicle ferries. Their network costs count toward the travel budgets; historical timetables, queues and boarding availability are not reconstructed.');if(route.analytical_count_bound)dataNotes.push(`Independent species-count bound: ${route.analytical_count_bound.upper_bound}. ${route.analytical_count_bound.scope}`);for(const a of [...dataNotes,...route.assumptions]){const li=document.createElement('li');li.textContent=a;list.append(li);}$('assumptions').replaceChildren(list);
 const ticks=document.querySelector('.months');ticks.replaceChildren();
 const labels=route.days.length>300?Array.from({length:12},(_,i)=>`${route.year}-${String(i+1).padStart(2,'0')}-01`):[...new Set(Array.from({length:6},(_,i)=>route.days[Math.round(i*(route.days.length-1)/5)].date))];
 for(const value of labels){const span=document.createElement('span');span.textContent=fmt(value,route.days.length>300?{month:'short'}:{month:'short',day:'numeric'}).toUpperCase();ticks.append(span);}
 fitMap();render();renderSearchNotice();
}
async function init(){
 const [states,runs,context]=await Promise.all([fetch('/states.geojson').then(r=>r.json()),fetch('/api/runs').then(r=>r.json()),fetch('/api/context').then(r=>r.ok?r.json():{features:[]})]);
 const caption=document.querySelector('.map-caption');caption.replaceChildren(document.createTextNode('Scroll to zoom · drag to pan · select a stop'),document.createElement('br'),document.createTextNode(context.features.length?'Natural Earth + U.S. Census boundaries · display only':'Natural Earth, public domain · display only'));
 for(const f of context.features){const polys=f.geometry.type==='Polygon'?[f.geometry.coordinates]:f.geometry.coordinates;const d=polys.map(poly=>poly.map(ring=>path(ring)+' Z').join(' ')).join(' ');const island=f.properties.role==='island_coast';if(!island)contextBounds.push(...polys.flat(2));const outline=el('path',{d,fill:island?'#fff':'#36824b','fill-opacity':island?1:.035,stroke:island?'#c5cec7':'#36824b','stroke-width':island ? 0.7 : 1.2,'stroke-dasharray':island?'':'5 4','vector-effect':'non-scaling-stroke','fill-rule':'evenodd'});outline.append(el('title',{},f.properties.name));$('context').append(outline);if(!island)$('context-label').textContent=f.properties.name;}
 if(context.features.some(f=>f.properties.role==='island_coast'))document.querySelector('.map-caption').lastChild.textContent='Basemap: Natural Earth · island shoreline: © OpenStreetMap contributors · display only';
 for(const f of states.features){
   const polys=f.geometry.type==='Polygon'?[f.geometry.coordinates]:f.geometry.coordinates;
   const d=polys.map(poly=>poly.map(ring=>path(ring)+' Z').join(' ')).join(' ');
   const g=el('path',{d,'fill-rule':'evenodd'});g.append(el('title',{},f.properties.name));$('states').append(g);
   const coords=polys[0][0];const center=coords.reduce((a,p)=>[a[0]+p[0]/coords.length,a[1]+p[1]/coords.length],[0,0]);const[x,y]=point(center);
   if(f.properties.postal)$('states').append(el('text',{x,y,'text-anchor':'middle'},f.properties.postal));
 }
 if(!runs.length){$('empty').hidden=false;$('map-date').textContent='No saved experiments';return;}
 runs.sort((a,b)=>b.year-a.year||b.species-a.species||Number(!a.path.endsWith('/route.json'))-Number(!b.path.endsWith('/route.json'))||a.label.localeCompare(b.label));
 for(const r of runs){const option=document.createElement('option');option.value=r.path;option.textContent=`${r.year}${r.synthetic?' · DEMO':''} · ${r.label}`;$('runs').append(option);}
 $('runs').onchange=e=>loadRoute(e.target.value).catch(showError);const requested=new URLSearchParams(location.search).get('run');const initial=runs.find(r=>r.path===requested)||runs[0];$('runs').value=initial.path;await loadRoute(initial.path);
}
function showError(e){$('banner').style.display='block';$('banner').textContent=e.message;}
$('date-slider').oninput=e=>seek(+e.target.value);
$('date-input').onchange=e=>{if(!route)return;const i=route.days.findIndex(d=>d.date===e.target.value);if(i>=0)seek(i);};
$('previous').onclick=()=>seek(day-1);
$('next').onclick=()=>seek(day+1);
$('play').onclick=()=>playing?pause():play();
$('replay').onclick=()=>{pause();play(true);};
document.addEventListener('visibilitychange',()=>{if(document.hidden)pause();});
$('curve').onclick=e=>{if(route){const box=e.currentTarget.getBoundingClientRect();seek(Math.floor(((e.clientX-box.left)/box.width*1200-34)/1140*route.days.length));}};
for(const metric of activityMetrics)$(metric.svg).onclick=e=>{if(route){const box=e.currentTarget.getBoundingClientRect();seek(Math.floor(((e.clientX-box.left)/box.width*400-48)/340*route.days.length));}};
function transform(){$('map-content').setAttribute('transform',`translate(${pan}) scale(${zoom})`);document.querySelectorAll('#route-stops circle, #cursor circle').forEach(c=>c.setAttribute('r',Number(c.dataset.radius)/zoom));document.querySelectorAll('#states text').forEach(t=>t.style.display=zoom>4?'none':'');document.querySelectorAll('.ocean-label').forEach(t=>t.style.display=zoom>1.5?'none':'');}
function fitMap(){
 if(!route)return;const coords=[...route.locations.map(l=>[l.lon,l.lat]),...contextBounds,...route.days.flatMap(d=>d.legs.flatMap(l=>l.geometry?.coordinates||[]))].map(point);
 const [x0,x1,y0,y1]=coords.reduce((b,[x,y])=>[Math.min(b[0],x),Math.max(b[1],x),Math.min(b[2],y),Math.max(b[3],y)],[Infinity,-Infinity,Infinity,-Infinity]);
 zoom=Math.max(1,Math.min(80,1080/Math.max(x1-x0,1),500/Math.max(y1-y0,1)));pan=[600-(x0+x1)/2*zoom,325-(y0+y1)/2*zoom];transform();
}
$('reset-map').onclick=fitMap;
$('map').addEventListener('wheel',e=>{e.preventDefault();const box=e.currentTarget.getBoundingClientRect(),x=(e.clientX-box.left)/box.width*1200,y=(e.clientY-box.top)/box.height*650;const next=Math.max(1,Math.min(150,zoom*(e.deltaY<0?1.15:1/1.15)));pan=[x-(x-pan[0])*next/zoom,y-(y-pan[1])*next/zoom];zoom=next;transform();},{passive:false});
$('map').onpointerdown=e=>{if(e.target.closest('.stop'))return;drag=[e.clientX,e.clientY,...pan];e.currentTarget.setPointerCapture(e.pointerId);};
$('map').onpointermove=e=>{if(drag){const box=e.currentTarget.getBoundingClientRect();pan=[drag[2]+(e.clientX-drag[0])*1200/box.width,drag[3]+(e.clientY-drag[1])*650/box.height];transform();}};
$('map').onpointerup=()=>drag=null;$('map').onpointercancel=()=>drag=null;
init().then(refreshSearchStatus).catch(showError);
