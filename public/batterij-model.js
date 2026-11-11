// Rekenmodel van /batterij-berekenen. Wordt geladen door de pagina én als web worker
// (zie onderaan), zodat de zware jaarberekening de pagina niet bevriest. Geen DOM hier.
// ---------- data ----------
// Aanbieders, energiebelasting en btw komen uit /data/config.json: dezelfde bron als /aanbieders.
// tlv = terugleverkosten per teruggeleverde kWh bij een DYNAMISCH contract = -teruglevering.opslag_per_kwh.
//  > 0 = je betaalt per kWh teruglevering, 0 = kale marktprijs, < 0 = je krijgt er iets bovenop.
// BEWUST ZONDER ZONNEBONUS: Frank, NextEnergy en Zonneplan betalen hun bonus alleen over zonnestroom.
// Over stroom die een thuisbatterij teruglevert krijg je hem niet; dat staat in hun eigen voorwaarden.
// tlvProv = bedrag nog een aanname (teruglevering.voorlopig in config.json).
let SUPPLIERS=[{id:"average",name:"Algemeen gemiddelde",markup:0.017,tlv:0.0133}];   // terugval
let EB=0.0916, BTW=1.21, TAX_YEAR=2026, CONFIG_OK=false;
function applyConfig(c){
  const t=(c&&c.taxes)||{};
  if(+t.energiebelasting_per_kwh>0) EB=+t.energiebelasting_per_kwh;
  if(+t.btw_factor>0) BTW=+t.btw_factor;
  if(t.year) TAX_YEAR=t.year;
  const list=((c&&c.suppliers)||[]).filter(s=>s.id!=='custom'&&s.markup_per_kwh!=null&&s.zonnepanelen_dynamisch!==false).map(s=>{
    const tl=s.teruglevering||{};
    return {id:s.id,name:s.name,markup:+s.markup_per_kwh,tlv:-(+tl.opslag_per_kwh||0),tlvProv:!!tl.voorlopig};
  });
  if(list.length){ SUPPLIERS=list; CONFIG_OK=true; }
}
const SOLAR_PER_PANEL=350;
const SOLAR_MONTH=[.02,.04,.08,.12,.14,.14,.13,.11,.09,.06,.04,.03];      // Jan..Dec
const CONS_MONTH=[.095,.086,.086,.080,.076,.072,.072,.073,.078,.085,.094,.103];
// Warmtepomp-verbruik volgt de stookbehoefte: sterk winter-zwaar (graaddagen-profiel).
// Okt t/m mrt ~88% van het jaarverbruik, jun-aug vrijwel niets.
const HP_MONTH=[.210,.155,.110,.055,.020,.005,.005,.010,.030,.085,.135,.180];   // Jan..Dec, som = 1
const CONS_HOUR=[.022,.018,.016,.015,.016,.022,.038,.052,.050,.044,.042,.044,.046,.044,.042,.044,.052,.066,.074,.070,.060,.050,.040,.030];
// Warmtepomp trekt op koude dagen vrijwel de hele dag een constant vermogen (compressor blijft draaien),
// anders dan het huishoud-piekpatroon hierboven, daarom een eigen, vlak uurprofiel.
const HP_HOUR=Array(24).fill(1);
const SUNRISE=[8.4,7.7,6.9,5.7,5.0,4.5,4.7,5.4,6.2,7.0,7.0,8.0];
const SUNSET =[16.7,17.5,18.4,19.3,20.1,20.9,20.9,20.1,18.9,17.8,16.7,16.4];
const BASE_MWH=[95,92,85,75,62,55,52,55,68,78,88,96]; // EUR/MWh by month
const SHAPE=[.75,.72,.70,.70,.72,.80,.95,1.15,1.20,1.05,1.00,.95,.90,.92,.98,1.05,1.20,1.45,1.55,1.40,1.20,1.00,.90,.80];
const MODUS_TXT={
 self:"De batterij gebruikt alleen je eigen zonnestroom: overdag opslaan, &#8217;s avonds verbruiken. Hij koopt of verkoopt geen stroom.",
 balanced:"Vooral eigen zonnestroom. Op dagen met een groot prijsverschil koopt de batterij ook goedkope stroom in om die in dure uren te gebruiken.",
 max:"De batterij benut elk voordeel: laden als stroom goedkoop is, gebruiken of terugleveren als het duur is. Meeste opbrengst, maar ook de meeste laadbeurten en dus meer slijtage."
};
const STEKKER_KWH=2.7, STEKKER_KW=0.8, STEKKER_PRIJS=1195;   // stekkerbatterij: klein, 800 watt, zonder installateur
// Rendement per richting. Een vaste batterij haalt rond 90% heen en terug. Een stekkerbatterij is AC-gekoppeld met
// een kleine omvormer die vaak op laag vermogen draait; HomeWizard noemt zelf 70 tot 85%. We rekenen met 80%.
const EFF_VAST=0.95, EFF_STEKKER=0.894;

// ---------- voorbeeldprijzen (alleen als het archief niet laadt) ----------
function mulberry32(a){return function(){a|=0;a=a+0x6D2B79F5|0;let t=Math.imul(a^a>>>15,1|a);t=t+Math.imul(t^t>>>7,61|t)^t;return((t^t>>>14)>>>0)/4294967296}}
function daysInMonth(y,m){return new Date(y,m+1,0).getDate()}
function epexExample(y,m,d){ // returns 24 EUR/MWh
  const rng=mulberry32((y*10000+(m+1)*100+d)>>>0);
  const level=BASE_MWH[m]*(0.72+rng()*0.65);
  const sunny=(m>=2&&m<=8);
  const arr=[];
  for(let h=0;h<24;h++){
    let s=SHAPE[h]*(0.92+rng()*0.16);
    if(sunny&&h>=9&&h<=15){ s*= (0.10+rng()*0.45); }
    let v=level*s;
    if(sunny&&h>=11&&h<=14&&rng()<0.45){ v-= (10+rng()*70); } // soms negatief rond de middag
    arr.push(Math.round(v*100)/100);
  }
  return arr;
}

// ---------- echte prijzen: uur-archief, kwartier-archief sinds okt 2025 ----------
const DATA_BASE="/data/archief/", DATA15_BASE="/data/archief15/";
const REAL={};        // "y-m-d" (m 0-index) -> [24] EUR/MWh
const Q15={};         // "y-m-d" -> [96] EUR/MWh (day-ahead per kwartier)
const monthState={};  // "y-m" -> 'real' | 'example'
const monthLoad={};   // "y-m" -> Promise
const pad2=n=>String(n).padStart(2,'0');
const dk=(y,m,d)=>y+'-'+m+'-'+d;
function addPrices(list,target,slots){
  const touched=new Set();
  for(const p of (list||[])){
    const t=p.time, ymd=t.slice(0,10).split('-').map(Number), key=dk(ymd[0],ymd[1]-1,ymd[2]);
    const h=+t.slice(11,13), mi=+t.slice(14,16), i= slots===96? h*4+Math.floor(mi/15) : h;
    if(!target[key]) target[key]=new Array(slots).fill(null);
    target[key][i]=p.price; touched.add(key);
  }
  for(const k of touched){ const a=target[k]; for(let i=0;i<slots;i++) if(a[i]==null) a[i]=(i>0?a[i-1]:null)??a.find(x=>x!=null)??0; }
}
async function getJson(url,fetcher){ const r=await (fetcher||fetch)(url,{cache:'force-cache'}); if(!r.ok) throw new Error(r.status); return r.json(); }
function ensureMonth(y,m,fetcher){
  const key=y+'-'+m;
  if(!monthLoad[key]) monthLoad[key]=(async()=>{
    const f=`${y}-${pad2(m+1)}.json`;
    try{ addPrices((await getJson(DATA_BASE+f,fetcher)).prices,REAL,24); monthState[key]='real'; }
    catch(e){ monthState[key]='example'; }
    if(y*12+m>=2025*12+9){ try{ addPrices((await getJson(DATA15_BASE+f,fetcher)).prices,Q15,96); }catch(e){} }
  })();
  return monthLoad[key];
}
async function ensureMonths(list,fetcher){ await Promise.all(list.map(o=>ensureMonth(o.y,o.m,fetcher))); }
const hasRealPrice=(y,m,d)=>!!(Q15[dk(y,m,d)]||REAL[dk(y,m,d)]);
// prijzen van één dag: per kwartier als die er zijn, anders per uur (archief of voorbeeld)
function priceDay(y,m,d){ const k=dk(y,m,d); if(Q15[k]) return {q:true,e:Q15[k]}; return {q:false,e:REAL[k]||epexExample(y,m,d)}; }

// ---------- profielen (zonder meterdata) ----------
function solarDay(y,m,d,annual){
  const dayE=(annual||0)*SOLAR_MONTH[m]/daysInMonth(y,m);
  const sr=SUNRISE[m], ss=SUNSET[m];
  const w=[]; let sum=0;
  for(let h=0;h<24;h++){const x=(h+0.5-sr)/(ss-sr);const val=(x>0&&x<1)?Math.sin(Math.PI*x):0;w.push(val);sum+=val;}
  return w.map(x=> sum>0? dayE*x/sum : 0);
}
function consDay(y,m,base,hp){
  // base = huishoudverbruik (excl. warmtepomp), hp = warmtepomp-verbruik per jaar.
  // Beide krijgen hun eigen maand- én uurprofiel: warmtepomp is winter-zwaar en loopt
  // vlak door de dag (HP_HOUR), het huishoudverbruik volgt het ochtend/avond-piekpatroon (CONS_HOUR).
  const baseDayE=(base||0)*CONS_MONTH[m]/daysInMonth(y,m);
  const hpDayE=(hp||0)*HP_MONTH[m]/daysInMonth(y,m);
  const baseSum=CONS_HOUR.reduce((a,b)=>a+b,0);
  const hpSum=HP_HOUR.reduce((a,b)=>a+b,0);
  return CONS_HOUR.map((x,h)=> baseDayE*x/baseSum + hpDayE*HP_HOUR[h]/hpSum);
}
const split4=a=>{ const r=[]; for(const x of a){ const q=x/4; r.push(q,q,q,q); } return r; };   // energie per uur -> per kwartier
const rep4=a=>{ const r=[]; for(const x of a) r.push(x,x,x,x); return r; };                      // prijs per uur -> per kwartier
const sum4=a=>{ const r=[]; for(let i=0;i<a.length;i+=4) r.push(a[i]+a[i+1]+a[i+2]+a[i+3]); return r; };
const avg4=a=>sum4(a).map(x=>x/4);

// Invoer van één dag: prijzen, opwek s en verbruik l, op kwartieren als de prijzen per kwartier zijn.
// Met meterdata (p.p1) is s = teruglevering en l = afname van je meter; null = geen meterdata die dag.
function dayInputs(y,m,d,p){
  const pr=priceDay(y,m,d), q=pr.q;
  let s,l;
  if(p.p1){
    const r=p.p1.days[dk(y,m,d)];
    if(!r||!hasRealPrice(y,m,d)) return null;
    s=Array.from(r.exp); l=Array.from(r.imp);
    if(!q){ s=sum4(s); l=sum4(l); }
  } else {
    s=solarDay(y,m,d,p.solarKwh); l=consDay(y,m,p.base,p.hp);
    if(q){ s=split4(s); l=split4(l); }
  }
  return {q, cp:pr.e.map(e=>(e/1000+p.markup+EB)*BTW), tb:pr.e.map(e=>e/1000-(p.tlv||0)), s, l, e:pr.e};
}
function toRes(x,q){
  if(x.q===q) return x;
  return q? {q,cp:rep4(x.cp),tb:rep4(x.tb),s:split4(x.s),l:split4(x.l)} : {q,cp:avg4(x.cp),tb:avg4(x.tb),s:sum4(x.s),l:sum4(x.l)};
}

// ---------- dispatch ----------
// De batterij plant elke dag opnieuw, zoals een echte besturing: om 00:00 kent hij de prijzen van
// vandaag en (vanaf 13:15 gisteren bekend) die van morgenochtend. Hij kiest het laad- en
// ontlaadpatroon voor de komende 36 uur dat het goedkoopst uitkomt, voert de eerste 24 uur uit,
// en plant de volgende dag opnieuw. Rekenmethode: dynamisch programmeren over de inhoud van de
// batterij (20 stapjes, ertussen lineair), per uur of per kwartier, met laad- en ontlaadvermogen
// en capaciteit als harde grens.
const PLAN_H=36, SOC_STEPS=20;
// Slijtage-drempels per strategie (EUR per kWh): alleen om keuzes te sturen, niet in de besparing.
// grid = per kWh die de batterij uit het net laadt; exp = per kWh die de batterij aan het net levert.
// 'Eigen stroom' laadt nooit uit het net en levert nooit vanuit de batterij terug.
const WEAR={self:{grid:Infinity,exp:Infinity}, balanced:{grid:0.06,exp:0.005}, max:{grid:0.01,exp:0}};

// Welk deel van het restverbruik in een tijdstap kan de batterij écht dekken? Het verbruik binnen een kwartier of
// uur is niet vlak: een waterkoker of kookplaat trekt een paar minuten 2 kW, de rest van de tijd loopt alleen de
// basislast. De batterij volgt de meter en levert per moment hooguit zijn vermogen. Aanname: de helft van het
// gemiddelde is vaste basislast, de andere helft komt in pieken (exponentieel verdeeld). Bij 5 kW merk je daar
// vrijwel niets van, bij 800 watt in de avondpiek wel.
function coverable(lRem,pwr,dt){
  if(!(lRem>0)) return 0;
  const m=lRem/dt, b=0.5*m, k=0.5*m;
  if(pwr>=b+12*k) return lRem;
  const c= pwr<=b? pwr : b+k*(1-Math.exp(-(pwr-b)/k));
  return Math.min(lRem,c*dt);
}
// Eén tijdstap afrekenen bij een verandering van de batterij-inhoud dS (opgeslagen kWh, + = laden).
// lc = deel van het restverbruik dat de batterij kan dekken (zie coverable); standaard alles.
function hourFlows(s,l,dS,cp,tb,ce,de,lc){
  const d=Math.min(s,l), sRem=s-d, lRem=l-d, lCap= lc==null? lRem : Math.min(lRem,lc);
  let imp=lRem, expK=sRem, sold=0, batL=0, fromGrid=0;
  if(dS>0){ const inp=dS/ce, fs=Math.min(inp,sRem); fromGrid=inp-fs; expK=sRem-fs; imp=lRem+fromGrid; }
  else if(dS<0){ const out=-dS*de; batL=Math.min(out,lCap); sold=out-batL; imp=lRem-batL; }
  return {cost:imp*cp-expK*tb-sold*tb, imp,expK,sold,batL,fromGrid,solL:d};
}
// Alleen de plan-kosten van een stap (zelfde regels als hourFlows plus slijtagedrempel, zonder object).
function planCost(s,l,dS,cp,tb,ce,de,wg,we,lc){
  const d=s<l?s:l, sRem=s-d, lRem=l-d;
  if(dS>0){ const inp=dS/ce, fs=inp<sRem?inp:sRem, fg=inp-fs;
    if(fg>1e-9){ if(wg===Infinity) return Infinity; return (lRem+fg)*cp-(sRem-fs)*tb+fg*wg; }
    return lRem*cp-(sRem-fs)*tb; }
  if(dS<0){ const lCap=lc<lRem?lc:lRem, out=-dS*de, bl=out<lCap?out:lCap, so=out-bl;
    if(so>1e-9){ if(we===Infinity) return Infinity; return (lRem-bl)*cp-sRem*tb-so*tb+so*we; }
    return (lRem-bl)*cp-sRem*tb; }
  return lRem*cp-sRem*tb;
}
// bat = {cap, pwr, ce, de, dt (uur per stap), nExec (uit te voeren stappen)}
function planDay(cpA,tbA,solarA,loadA,bat,mode,socIn){
  const H=cpA.length, cap=bat.cap, ce=bat.ce, de=bat.de, pwr=bat.pwr, dt=bat.dt||1, nExec=bat.nExec||24, wear=WEAR[mode];
  const N= cap>0.01? SOC_STEPS : 0, st= N? cap/N : 1, N1=N+1;
  const maxUp=pwr*dt*ce, maxDn=pwr*dt/de;           // grootste verandering van de inhoud per stap
  const V=new Float64Array((H+1)*N1);
  const wg=wear.grid, we= bat.noExport? Infinity : wear.exp;
  const lcA=new Float64Array(H); for(let t=0;t<H;t++){ const s=solarA[t], l=loadA[t]; lcA[t]=coverable(l-(s<l?s:l),pwr,dt); }
  // waarde van de rest van de planning vanaf stap t bij inhoud x (lineair tussen de rasterpunten)
  const val=(t,x)=>{ if(!N) return V[t]; const u=Math.max(0,Math.min(N,x/st)), i=Math.min(N-1,Math.floor(u)), w=u-i, o=t*N1+i; return V[o]*(1-w)+V[o+1]*w; };
  // beste keuze in stap t bij inhoud x: 'niets doen', 'laad precies het zonne-overschot',
  // 'dek precies het restverbruik', en elk rasterpunt binnen bereik (ix = rasterindex van x, of -1)
  let bdS=0;
  const best=(t,x,ix)=>{
    const s=solarA[t], l=loadA[t], d=s<l?s:l, cp=cpA[t], tb=tbA[t], lc=lcA[t];
    const lo=Math.max(-x,-maxDn), hi=Math.min(cap-x,maxUp);
    let bc=Infinity; bdS=0;
    let dS=0, c=planCost(s,l,0,cp,tb,ce,de,wg,we,lc)+val(t+1,x);
    if(c<bc-1e-12){bc=c;bdS=0;}
    dS=Math.min(hi,(s-d)*ce);
    if(dS>=lo-1e-9&&dS<=hi+1e-9){ c=planCost(s,l,dS,cp,tb,ce,de,wg,we,lc)+val(t+1,x+dS); if(c<bc-1e-12){bc=c;bdS=dS;} }
    dS=Math.max(lo,-lc/de);
    if(dS>=lo-1e-9&&dS<=hi+1e-9){ c=planCost(s,l,dS,cp,tb,ce,de,wg,we,lc)+val(t+1,x+dS); if(c<bc-1e-12){bc=c;bdS=dS;} }
    if(N){
      const jLo=Math.ceil((x+lo)/st-1e-9), jHi=Math.floor((x+hi)/st+1e-9), o=(t+1)*N1;
      for(let j=jLo;j<=jHi;j++){
        if(j===ix) continue;                                   // = niets doen, al geprobeerd
        dS=j*st-x; if(dS<lo-1e-9||dS>hi+1e-9) continue;
        c=planCost(s,l,dS,cp,tb,ce,de,wg,we,lc)+V[o+(j<0?0:j>N?N:j)];
        if(c<bc-1e-12){bc=c;bdS=dS;}
      }
    }
    return bc;
  };
  for(let t=H-1;t>=0;t--){ if(!N){ V[t]=best(t,0,-1); continue; } for(let i=0;i<=N;i++) V[t*N1+i]=best(t,i*st,i); }
  // eerste nExec stappen uitvoeren en boeken
  let x= N? Math.max(0,Math.min(cap,socIn||0)) : 0, gridPool=0;   // gridPool = deel van de inhoud uit het net
  const r={solToLoad:0,batToLoad:0,expKwh:0,soldKwh:0,soldGridKwh:0,solarGen:0,charged:0,loadTot:0,impKwh:0,costWithout:0,costWith:0,solSave:0,batSave:0,expRev:0,soldRev:0};
  for(let t=0;t<Math.min(nExec,H);t++){
    best(t,x,-1); const dS=bdS;
    const f=hourFlows(solarA[t],loadA[t],dS,cpA[t],tbA[t],ce,de,lcA[t]);
    if(dS>0){ gridPool+=f.fromGrid*ce; r.charged+=dS; }
    else if(dS<0&&x>1e-9){ const frac=Math.min(1,gridPool/x); r.soldGridKwh+=f.sold*frac; gridPool-=(-dS)*frac; }
    x=Math.max(0,Math.min(cap,x+dS)); gridPool=Math.max(0,Math.min(gridPool,x));
    r.solToLoad+=f.solL; r.batToLoad+=f.batL; r.expKwh+=f.expK; r.soldKwh+=f.sold; r.impKwh+=f.imp;
    r.solarGen+=solarA[t]; r.loadTot+=loadA[t];
    r.costWithout+=loadA[t]*cpA[t]; r.costWith+=f.cost;
    r.solSave+=f.solL*cpA[t]; r.batSave+=f.batL*cpA[t]; r.expRev+=f.expK*tbA[t]; r.soldRev+=f.sold*tbA[t];
  }
  r.socOut=x;
  r.arbProfit=(r.costWithout-r.costWith)-r.solSave-r.batSave-r.expRev;   // restterm: slim terugleveren + schuiven
  return r;
}
function simulateMonth(y,m,p,lastDay){
  const dn=lastDay||daysInMonth(y,m); let soc=0;
  const agg={days:0,solToLoad:0,batToLoad:0,expKwh:0,soldKwh:0,soldGridKwh:0,solarGen:0,charged:0,loadTot:0,impKwh:0,costWithout:0,costWith:0,solSave:0,batSave:0,expRev:0,soldRev:0,arbProfit:0};
  for(let d=1;d<=dn;d++){
    const today=dayInputs(y,m,d,p);
    if(!today){ soc=0; continue; }
    let {cp,tb,s,l}=today;
    // morgenochtend meenemen als die prijzen er (in het archief) zijn
    const nx=new Date(y,m,d+1), ny=nx.getFullYear(), nm=nx.getMonth(), nd=nx.getDate();
    if(hasRealPrice(ny,nm,nd) || monthState[y+'-'+m]==='example'){
      const t=dayInputs(ny,nm,nd,p);
      if(t){ const tt=toRes(t,today.q), n=(PLAN_H-24)*(today.q?4:1);
        cp=cp.concat(tt.cp.slice(0,n)); tb=tb.concat(tt.tb.slice(0,n)); s=s.concat(tt.s.slice(0,n)); l=l.concat(tt.l.slice(0,n)); }
    }
    const f=planDay(cp,tb,s,l,{cap:p.cap,pwr:p.pwr,ce:p.ce,de:p.de,dt:today.q?0.25:1,nExec:today.q?96:24,noExport:p.noExport},p.mode,soc);
    soc=Math.min(f.socOut,p.cap); agg.days++;
    for(const k of Object.keys(f)) if(k in agg) agg[k]+=f[k];
  }
  return agg;
}

// ---------- meterdata (P1-export als CSV), alles in de browser ----------
// Herkent: één tijdkolom (ISO, "2026-06-01 00:15", "01-06-2026 00:15", Unix-seconden) of een
// datum- plus tijdkolom; komma of punt als decimaalteken; ; , of tab als scheidingsteken;
// meterstanden (lopen op) of verbruik per periode; Wh of kWh.
const AMS_FMT=(typeof Intl!=='undefined')? new Intl.DateTimeFormat('en-GB',{timeZone:'Europe/Amsterdam',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hourCycle:'h23'}) : null;
function amsLocal(ms){ const o={}; for(const p of AMS_FMT.formatToParts(new Date(ms))) o[p.type]=p.value; return {y:+o.year,m:+o.month-1,d:+o.day,h:+o.hour,mi:+o.minute}; }
function p1Num(x){
  if(x==null) return NaN; let s=String(x).trim().replace(/^"|"$/g,'').replace(/\s/g,'');
  if(!s) return NaN;
  if(s.includes(',')&&s.includes('.')) s= s.lastIndexOf(',')>s.lastIndexOf('.')? s.replace(/\./g,'').replace(',','.') : s.replace(/,/g,'');
  else if(s.includes(',')) s=s.replace(',','.');
  return /^-?\d*\.?\d+(e-?\d+)?$/i.test(s)? +s : NaN;
}
function p1Time(a,b){
  const s=String(a||'').trim().replace(/^"|"$/g,''), t=b!=null? String(b).trim().replace(/^"|"$/g,'') : '';
  let m;
  if(/^\d{9,13}$/.test(s)){ const n=+s; return amsLocal(n>1e11? n : n*1000); }
  // ISO met Z of +00:00 = UTC: omrekenen naar Nederlandse tijd
  if((m=s.match(/^(\d{4})-(\d{2})-(\d{2})[T ](\d{1,2}):(\d{2})(?::\d{2}(?:\.\d+)?)?\s*(Z|[+-]\d{2}:?\d{2})?$/))){
    const [,Y,M,D,h,mi,z]=m;
    if(z==='Z'||/^[+-]00:?00$/.test(z||'')) return amsLocal(Date.UTC(+Y,+M-1,+D,+h,+mi));
    return {y:+Y,m:+M-1,d:+D,h:+h,mi:+mi};
  }
  const date=s.match(/^(\d{1,2})[-\/.](\d{1,2})[-\/.](\d{4})(?:[ T]+(\d{1,2}):(\d{2})(?::\d{2})?)?$/) || s.match(/^(\d{4})-(\d{2})-(\d{2})$/);
  if(!date) return null;
  let Y,M,D,h=null,mi=null;
  if(date[0].match(/^\d{4}-/)){ [Y,M,D]=[+date[1],+date[2],+date[3]]; } else { [D,M,Y]=[+date[1],+date[2],+date[3]]; if(date[4]!=null){h=+date[4];mi=+date[5];} }
  if(h==null&&t){ const tm=t.match(/^(\d{1,2}):(\d{2})/); if(tm){h=+tm[1];mi=+tm[2];} }
  if(h==null) return {y:Y,m:M-1,d:D,h:0,mi:0,dateOnly:true};
  return {y:Y,m:M-1,d:D,h,mi};
}
function p1Split(line,sep){
  const out=[]; let cur='', q=false;
  for(let i=0;i<line.length;i++){ const c=line[i];
    if(c==='"'){ if(q&&line[i+1]==='"'){cur+='"';i++;} else q=!q; }
    else if(c===sep&&!q){ out.push(cur); cur=''; } else cur+=c; }
  out.push(cur); return out.map(x=>x.trim());
}
function p1Analyse(text){
  const lines=String(text).replace(/^﻿/,'').split(/\r?\n/).filter(x=>x.trim());
  if(lines.length<3) return {error:'Het bestand bevat te weinig regels.'};
  const first=lines.slice(0,5).join('\n'), cnt=c=>first.split(c).length;
  const sep=[';','\t',','].sort((a,b)=>cnt(b)-cnt(a))[0];
  const rows=lines.map(l=>p1Split(l,sep));
  let hi=rows.findIndex(r=>r.some(c=>/[a-z]/i.test(c)&&!p1Time(c)));
  if(hi<0||hi>5) hi=-1;
  const header= hi>=0? rows[hi] : rows[0].map((_,i)=>'kolom '+(i+1));
  const data=rows.slice(hi+1).filter(r=>r.length>=2);
  const sample=data.slice(0,50), nc=header.length;
  // tijdkolom(men)
  let tc=-1, tc2=-1;
  for(let i=0;i<nc&&tc<0;i++){ const ok=sample.filter(r=>{const t=p1Time(r[i]); return t&&!t.dateOnly;}).length; if(ok>sample.length*0.8) tc=i; }
  if(tc<0){ for(let i=0;i<nc&&tc<0;i++){ const ok=sample.filter(r=>{const t=p1Time(r[i]); return t&&t.dateOnly;}).length; if(ok>sample.length*0.8) tc=i; }
    if(tc>=0) for(let j=0;j<nc;j++){ if(j===tc) continue; if(sample.filter(r=>/^\d{1,2}:\d{2}/.test(r[j]||'')).length>sample.length*0.8){ tc2=j; break; } } }
  if(tc<0) return {error:'Ik vind geen kolom met datum en tijd.'};
  if(tc2<0&&sample.every(r=>{const t=p1Time(r[tc]); return t&&t.dateOnly;})) return {error:'Je bestand heeft één waarde per dag. Daarmee kan de tool niet per uur rekenen. Exporteer per kwartier of per uur.'};
  const cols=[];
  for(let i=0;i<nc;i++){ if(i===tc||i===tc2) continue;
    const ok=sample.filter(r=>!isNaN(p1Num(r[i]))).length; if(ok<sample.length*0.8) continue;
    const name=header[i]||('kolom '+(i+1)), n=name.toLowerCase();
    const exp=/export|teruglever|terug|return|injectie|produc|geleverd aan|feed/.test(n);
    const imp=!exp&&/import|levering|afname|consum|verbruik|delivered|afgenomen|t1|t2|tarief|normaal|dal|piek|laag|hoog/.test(n);
    cols.push({i,name,guess:exp?'exp':imp?'imp':null,wh:/(^|[^k])wh\b/.test(n)});
  }
  if(!cols.length) return {error:'Ik vind geen kolommen met getallen.'};
  return {header,data,tc,tc2,cols};
}
function p1Build(an,impCols,expCols,kind){
  const fac=i=>{ const c=an.cols.find(x=>x.i===i); return c&&c.wh? 0.001 : 1; };
  const pts=[];
  for(const r of an.data){
    const t=p1Time(r[an.tc], an.tc2>=0? r[an.tc2] : null); if(!t||t.dateOnly&&an.tc2<0) continue;
    let imp=0, exp=0, bad=false;
    for(const i of impCols){ const x=p1Num(r[i]); if(isNaN(x)) bad=true; else imp+=x*fac(i); }
    for(const i of expCols){ const x=p1Num(r[i]); if(isNaN(x)) bad=true; else exp+=x*fac(i); }
    if(bad) continue;
    pts.push({ms:Date.UTC(t.y,t.m,t.d,t.h,t.mi),imp,exp});   // lokale kloktijd als 'UTC' om mee te rekenen
  }
  if(pts.length<48) return {error:'Te weinig bruikbare regels gevonden.'};
  pts.sort((a,b)=>a.ms-b.ms);
  const gaps=[]; for(let i=1;i<Math.min(pts.length,2000);i++){ const g=(pts[i].ms-pts[i-1].ms)/60000; if(g>0) gaps.push(g); }
  gaps.sort((a,b)=>a-b); const step=gaps[Math.floor(gaps.length/2)]||0;
  if(!(step>0)) return {error:'Ik kan de tijdstappen in het bestand niet herkennen.'};
  if(step>60) return {error:'Je bestand heeft '+(step>=1440?'één waarde per dag':'meer dan een uur tussen de waarden')+'. Daarmee kan de tool niet per uur rekenen. Exporteer per kwartier of per uur.'};
  // meterstanden of per periode?
  if(kind==='auto'){
    const ser=pts.map(p=>p.imp+p.exp); let up=0,n=0; const diffs=[];
    for(let i=1;i<ser.length;i++){ const d=ser[i]-ser[i-1]; n++; if(d>=-1e-9) up++; if(d>0) diffs.push(d); }
    diffs.sort((a,b)=>a-b); const md=diffs[Math.floor(diffs.length/2)]||0, med=[...ser].sort((a,b)=>a-b)[Math.floor(ser.length/2)];
    kind= (up/n>0.98 && med>20*md)? 'cum' : 'int';
  }
  const days={}, cover={};
  const put=(ms,imp,exp)=>{ const t=new Date(ms), key=dk(t.getUTCFullYear(),t.getUTCMonth(),t.getUTCDate()), qi=t.getUTCHours()*4+Math.floor(t.getUTCMinutes()/15);
    if(!days[key]){ days[key]={imp:new Float64Array(96),exp:new Float64Array(96)}; cover[key]=new Uint8Array(96); }
    days[key].imp[qi]+=imp; days[key].exp[qi]+=exp; cover[key][qi]=1; };
  const Q=900000;
  const spread=(ms0,ms1,imp,exp)=>{ const n=Math.max(1,Math.round((ms1-ms0)/Q)); for(let k=0;k<n;k++) put(Math.floor(ms0/Q)*Q+k*Q,imp/n,exp/n); };
  if(kind==='cum'){
    for(let i=1;i<pts.length;i++){ const a=pts[i-1], b=pts[i], g=(b.ms-a.ms)/60000, di=b.imp-a.imp, de=b.exp-a.exp;
      if(g<=0||g>180||di<-1e-9||de<-1e-9||di+de>g/60*40) continue;   // gat, meterwissel of onmogelijk hoog
      spread(a.ms,b.ms,di,de); }
  } else {
    for(const p of pts){ if(p.imp<0||p.exp<0) continue; spread(p.ms,p.ms+step*60000,p.imp,p.exp); }
  }
  // alleen (vrijwel) complete dagen, en maximaal het laatste jaar
  let keys=Object.keys(days).filter(k=>cover[k].reduce((a,b)=>a+b,0)>=90);
  const kms=k=>{ const [y,m,d]=k.split('-').map(Number); return Date.UTC(y,m,d); };
  keys.sort((a,b)=>kms(a)-kms(b));
  if(keys.length){ const last=kms(keys[keys.length-1]); keys=keys.filter(k=>kms(k)>last-365*86400000); }
  if(keys.length<7) return {error:'Er zitten minder dan 7 complete dagen in je bestand.'};
  const out={}, mset=new Map(); let impTot=0, expTot=0;
  for(const k of keys){ out[k]=days[k]; impTot+=days[k].imp.reduce((a,b)=>a+b,0); expTot+=days[k].exp.reduce((a,b)=>a+b,0);
    const [y,m]=k.split('-').map(Number); mset.set(y*12+m,{y,m}); }
  const months=[...mset.values()].sort((a,b)=>(a.y*12+a.m)-(b.y*12+b.m));
  return {days:out, months, nDays:keys.length, first:keys[0], last:keys[keys.length-1], impTot, expTot, kind, step, id:keys[0]+'|'+keys.length+'|'+Math.round(impTot)+'|'+Math.round(expTot)};
}

// ---------- jaarberekening ----------
const BAT_LIFESPAN=15;           // jaar; stap loont als hij zich binnen de levensduur terugverdient
const DEGRADE=0.98;              // 2% minder opbrengst per jaar door slijtage
// Eén terugverdientijd voor resultaatpaneel én adviestabel: het moment waarop de opgetelde
// besparing (met slijtage) de aanschafprijs haalt. Infinity = niet binnen de levensduur.
function paybackYears(price, yearly){
  if(!(yearly>0)||!(price>0)) return Infinity;
  let tot=0;
  for(let k=0;k<BAT_LIFESPAN;k++){ const y=yearly*Math.pow(DEGRADE,k); if(tot+y>=price) return k+(price-tot)/y; tot+=y; }
  return Infinity;
}
// hoogste aanschafprijs waarbij de batterij zich binnen 'jaren' terugverdient
function breakEven(yearly,jaren){ let t=0; for(let k=0;k<jaren;k++) t+=yearly*Math.pow(DEGRADE,k); return t; }
// laadvermogen bij een batterijmaat: stekker 0,8 kW, eigen invoer voor jouw maat, anders 0,5 x capaciteit
const isStekker=c=>Math.abs(c-STEKKER_KWH)<0.01;
function pwrFor(capKwh,p){
  if(Math.abs(capKwh-STEKKER_KWH)<0.01) return STEKKER_KW;
  if(p&&p.pwrUser>0&&Math.abs(capKwh-p.capKwh)<0.01) return p.pwrUser;
  return Math.max(0.01,capKwh*0.5);
}
let MONTHS=[];
const effFor=c=> isStekker(c)? EFF_STEKKER : EFF_VAST;
function yearSaving(p, capKwh){
  const cap=capKwh*0.9, pwr=pwrFor(capKwh,p), noExport=isStekker(capKwh), ce=effFor(capKwh), de=ce;
  let tot=0, days=0;
  for(const o of (p.p1? p.p1.months : MONTHS)){ const a=simulateMonth(o.y,o.m,Object.assign({},p,{cap,pwr,noExport,ce,de})); tot+=(a.costWithout-a.costWith); days+=a.days; }
  return p.p1? (days? tot*365/days : 0) : tot;   // meterdata: omgerekend naar een jaar
}
// memo: dezelfde jaarberekening wordt door resultaatpaneel én adviestabel gebruikt
const ysCache={};
function yearSavingM(p, capKwh){
  const key=ysKey(p,capKwh);
  if(!(key in ysCache)) ysCache[key]=yearSaving(p,capKwh);
  return ysCache[key];
}

function ysKey(p,capKwh){ return [p.p1?p.p1.id:'-',p.solarKwh,p.base,p.hp,p.markup,p.tlv,p.mode,capKwh,pwrFor(capKwh,p),EB,BTW].join('|'); }

// ---------- web worker ----------
// Bericht in: {id, cfg (config.json), months (jaar), load (te laden prijsmaanden), p, capKwh}; uit: {id, val}.
// Zonder p alleen de prijzen alvast laden (opwarmen).
if(typeof window==='undefined'&&typeof importScripts==='function'){
  let cfgDone=false;
  self.onmessage=async e=>{
    const {id,cfg,months,load,p,capKwh}=e.data;
    try{
      if(cfg&&!cfgDone){ applyConfig(cfg); cfgDone=true; }
      await ensureMonths(load||[]);
      if(!p){ self.postMessage({id,warm:true}); return; }
      MONTHS=months;
      self.postMessage({id,val:yearSaving(p,capKwh)});
    }catch(err){ self.postMessage({id,err:String(err&&err.message||err)}); }
  };
}
