/* Nerve Factory V3: illustrated, data-backed mission control. No external dependencies. */
window.NerveFactory = (function () {
  "use strict";
  const NS = "http://www.w3.org/2000/svg";
  const ZONES = [
    {code:"A",name:"RESEARCH & BUILD",label:"EXECUTION / BUILD",accent:"#58fc9b",codeword:"EXECUTE",roles:["OPERATE","BUILD","TOOLS","SOURCE"]},
    {code:"B",name:"TESTING & EVALUATION",label:"TEST / VERIFY",accent:"#42c3ff",codeword:"VERIFY",roles:["TEST","REVIEW","QUALITY","EVIDENCE"]},
    {code:"C",name:"MEMORY & ROUTING",label:"RETRIEVE / DISPATCH",accent:"#ffc15c",codeword:"ROUTE",roles:["MEMORY","SEARCH","RETRIEVE","ROUTE"]}
  ];
  const VIEW_W = 1600, VIEW_H = 680;
  const ZONE_WIDTH = 512, GAP = 16, ZONE_X = [9, 537, 1065], ZONE_Y = 76, ZONE_H = 558;
  const $ = (tag,cls,text) => {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = String(text);
    return e;
  };
  const svg = (name, attrs, parent) => {
    const e = document.createElementNS(NS,name);
    for (const [k,v] of Object.entries(attrs||{})) e.setAttribute(k,String(v));
    if (parent) parent.appendChild(e);
    return e;
  };
  const add = (root,child) => {root.appendChild(child);return child;};
  const value = (x,fallback="--") => x === undefined||x===null||x==="" ? fallback:String(x);
  const num = x => typeof x==="number" && Number.isFinite(x) ? x:null;
  const fmt = (x,d=0) => num(x)===null ? "--" : x.toFixed(d);
  const slice = (v,n=20) => String(v??"UNRECORDED").slice(0,n);
  const on = (e,type,fn,opts) => {e.addEventListener(type,fn,opts);return e;};
  const stamp = s => {const d=new Date(s||"");return Number.isFinite(d.getTime())?d.toLocaleTimeString([], {hour12:false}):"--:--:--"};
  let context=null, stage=null, world=null, nav=[], camera={s:1,x:0,y:0}, fitted=false;
  let agentList=[], zoneAgents=[[],[],[]], selectedKey=null, fullscreen=false;
  let drag=null, pendingRun=null, runMonitor=null, resizeObs=null, flyTimeout=null;
  let visible=false, hostRows=null;

  function instrument(label, sub, cls="") {
    const box = $("section","fc-instrument "+cls);
    add(box,$("div","fc-instrument-title",label));
    if (sub) add(box,$("span","fc-instrument-sub",sub));
    return box;
  }
  function flag(text,kind="unknown") {
    return $("span","fc-flag fc-flag-"+kind,String(text).toUpperCase());
  }
  function tierStatus(a) {
    if (!a) return "missing";
    const s=String(a.lease_status||"unknown").toLowerCase();
    return s==="live"?"live":s==="stale"?"stale":s==="exited"?"completed":"unknown";
  }
  function zoneFor(a) {
    const role=[a.role,a.last_tool,a.phase].map(x=>String(x||"")).join(" ").toLowerCase();
    if (/review|test|eval|check|verify|regress|benchmark|pytest/.test(role)) return 1;
    if (/search|memory|retriev|route|dispatch|cache|index|recall/.test(role)) return 2;
    return 0;  // unclassified activity belongs to general execution, not an inferred role
  }
  function sourceAgents(snap,live,fleet) {
    const direct=(live&&live.agents&&Array.isArray(live.agents.items))?live.agents.items.slice(0,200):[];
    const processRows=(fleet&&fleet.status==="ok"&&Array.isArray(fleet.instances))?
      fleet.instances.filter(p=>["running","blocked_io","defunct"].includes(p.status)):[];
    const seen=new Set(), linkedSessions=new Set(), result=[];
    // Every actual OS process is a distinct robot identity, even if Nerve hooks
    // have never loaded. A process in S state is NOT assumed to be doing work.
    for(const p of processRows) {
      if(!p||typeof p.key!=="string"||!p.key||seen.has(p.key))continue;
      seen.add(p.key);
      const sessions=Array.isArray(p.sessions)?p.sessions:[];
      sessions.forEach(s=>{if(s&&s.id)linkedSessions.add(s.id);});
      const reporting=sessions.some(s=>s&&s.lease_status==="live")&&
        typeof p.last_observation_age_s==="number"&&p.last_observation_age_s<=30;
      const state=p.status==="blocked_io"?"blocked":p.status==="defunct"?"defunct":
        reporting?"live":"process";
      const agent={key:p.key,label:p.profile+" / "+p.mode+" #"+p.pid,
        state,raw:p,kind:"process",zone:zoneFor({
          role:p.mode==="gateway"?"general":p.mode,last_tool:p.last_tool,phase:p.last_phase
        })};
      result.push(agent);
    }
    for(const a of direct) {
      if(!a||typeof a.key!=="string"||!a.key||seen.has(a.key)||linkedSessions.has(a.key))continue;
      seen.add(a.key);
      result.push({key:a.key,label:String(a.label||"OBSERVER SESSION"),state:tierStatus(a),
        raw:a,kind:"observer",zone:zoneFor(a)});
    }
    // Old supervision workers remain visible but do NOT gain live badges.
    for(const a of (snap&&Array.isArray(snap.agents)?snap.agents:[]).slice(0,60)) {
      if(!a||typeof a.worker_id!=="string"||!a.worker_id)continue;
      const k="recorded:"+a.worker_id;
      if(seen.has(k))continue;
      seen.add(k);
      result.push({key:k,label:a.worker_id,state:"recorded",
        raw:a,kind:"historical",zone:0});
    }
    const weight={live:0,process:1,blocked:2,defunct:3,stale:4,
      completed:5,unknown:6,recorded:7,missing:8};
    result.sort((a,b) => (weight[a.state]??8)-(weight[b.state]??8));
    return result;
  }
  function fleetCounts() {
    const fleet=context&&context.fleet;
    const counts=fleet&&fleet.counts?fleet.counts:{};
    const observers=agentList.filter(a=>a.kind==="observer");
    return {processes:typeof counts.running==="number"?counts.running:null,
      linked:typeof counts.observer_linked==="number"?counts.observer_linked:null,
      blocked:typeof counts.blocked_io==="number"?counts.blocked_io:null,
      ended:typeof counts.ended==="number"?counts.ended:null,
      active:agentList.filter(a=>a.state==="live").length,
      recorded:agentList.length, stale:observers.filter(a=>a.state==="stale").length,
      historical:agentList.filter(a=>a.kind==="historical").length,
      runs:(context.snapshot&&Array.isArray(context.snapshot.runs))?context.snapshot.runs.length:0};
  }
  function statGauge(valuePercent) {
    const s=svg("svg",{viewBox:"0 0 166 105",class:"fc-dial",role:"img",
      "aria-label":num(valuePercent)===null?"CPU unavailable":"CPU "+fmt(valuePercent,0)+" percent"});
    const center=82,cy=89,r=67;
    for(let i=0;i<=32;i++){
      const a=(210+i*120/32)*Math.PI/180;
      const inner=i%4===0?54:58;
      svg("line",{x1:center+inner*Math.cos(a),y1:cy+inner*Math.sin(a),
        x2:center+r*Math.cos(a),y2:cy+r*Math.sin(a),
        stroke:i<12?"#51ff9a":i<26?"#43bdea":"#f6b75b",
        "stroke-width":i%4===0?3:1.8,opacity:i%4===0?1:.82},s);
    }
    svg("path",{d:"M15 89 A67 67 0 0 1 149 89",fill:"none",stroke:"#12374c","stroke-width":2},s);
    if(num(valuePercent)!==null) {
      const a=(210+Math.min(100,Math.max(0,valuePercent))*1.2)*Math.PI/180;
      svg("line",{x1:center,y1:cy,x2:center+55*Math.cos(a),y2:cy+55*Math.sin(a),
        stroke:"#ffbe58","stroke-width":3.2,"stroke-linecap":"round"},s);
    }
    svg("circle",{cx:center,cy,r:5,fill:"#e6b76b",stroke:"#10181e","stroke-width":3},s);
    svg("text",{x:center,y:52,fill:"#90d9f6","font-size":11,"text-anchor":"middle"},s).textContent="CPU";
    svg("text",{x:center,y:76,fill:num(valuePercent)===null?"#7d91a2":"#ffd06c",
      "font-size":28,"font-weight":800,"text_anchor":"middle","text-anchor":"middle"},s).textContent=num(valuePercent)===null?"--":fmt(valuePercent,0)+"%";
    return s;
  }
  function meter(pct) {
    const wrap=$("div","fc-bar-meter");
    const known=num(pct)!==null;
    for(let i=0;i<12;i++) {
      const seg=$("i",known&&pct/100>(i/12)?"lit":"");
      wrap.appendChild(seg);
    }
    return wrap;
  }
  function computerPanel(host,id) {
    const live=context.live||{}, hostData=live.host||{}, m=hostData.metrics&&hostData.metrics[host],
      g=hostData.gpu&&hostData.gpu[host], ok=m&&m.state==="ok", cpu=ok?num(m.cpu_percent):null;
    const name=host?host.toUpperCase():"NOT CONNECTED";
    const box=instrument("COMPUTER "+id, name,"fc-computer");
    add(box,flag(ok?"MEASURED":"NO SOURCE",ok?"live":"unknown"));
    const body=add(box,$("div","fc-computer-body"));
    add(body,statGauge(cpu));
    const stack=add(body,$("div","fc-computer-readouts"));
    const total=ok?num(m.memory_total_mib):null, avail=ok?num(m.memory_available_mib):null;
    let used=total!==null&&avail!==null?Math.max(0,total-avail):null;
    add(stack,$("div","fc-readout-name","RAM / MiB"));
    add(stack,$("div","fc-readout-number",used!==null?Math.round(used)+" / "+Math.round(total):"-- / --"));
    add(stack,meter(total?used/total*100:null));
    const gpu=g&&g.state==="ok"&&Array.isArray(g.gpus)?g.gpus[0]:null;
    add(stack,$("div","fc-readout-row","GPU "+(gpu&&num(gpu.utilization_percent)!==null?fmt(gpu.utilization_percent)+"%":"--")));
    add(stack,$("div","fc-readout-row",host==="win4060"?"CPU / WSL2 GUEST":"LINUX / HOST"));
    if(gpu)add(stack,$("div","fc-readout-tiny",slice(gpu.name,23)));
    else add(stack,$("div","fc-readout-tiny","GPU NOT INSTRUMENTED"));
    return box;
  }
  function miniPlot(nums) {
    const plot=svg("svg",{viewBox:"0 0 240 70",class:"fc-miniplot",role:"img","aria-label":"Evidence-based event histogram"});
    for(let i=0;i<4;i++)svg("line",{x1:0,y1:15+i*17,x2:240,y2:15+i*17,stroke:"#24475b","stroke-width":1},plot);
    if(nums&&nums.length) {
      const max=Math.max(1,...nums);
      nums.slice(-24).forEach((v,i) => {
        const x=4+i*10,h=Math.max(1,v/max*54);
        svg("rect",{x,y:64-h,width:7,height:h,fill:"#48d7a1",opacity:.75},plot);
      });
    } else svg("text",{x:120,y:40,"text-anchor":"middle",fill:"#6c8a9f","font-size":11},plot).textContent="NO RATE SOURCE";
    return plot;
  }
  function eventBins(items) {
    // Only event counts are observable; never mislabel this as token throughput.
    const now=Date.now(), arr=new Array(24).fill(0);
    (items||[]).forEach(ev=>{const t=Date.parse(ev.ts||"");if(!Number.isFinite(t))return;
      const age=(now-t)/1000,slot=23-Math.floor(age/5);
      if(slot>=0&&slot<arr.length)arr[slot]++;});
    return arr;
  }
  function topDashboard(parent) {
    const top=add(parent,$("div","fc-topstrip"));
    add(top,computerPanel("j2","01"));
    add(top,computerPanel("win4060","02"));
    add(top,computerPanel(null,"03"));
    const counts=fleetCounts();
    const fleet=add(top,instrument("FLEET","OBSERVED SESSIONS","fc-fleet"));
    add(fleet,flag(counts.processes===null?"CENSUS UNKNOWN":counts.processes+" OS PROCESSES",
      counts.processes===null?"unknown":"recorded"));
    const line=add(fleet,$("div","fc-fleet-numbers"));
    for(const [label,n] of [["OS PROCESSES",counts.processes],["OBSERVER-LINKED",counts.linked],
      ["KNOWN IDENTITIES",counts.recorded],["RUN HISTORY",counts.runs]]) {
      let e=add(line,$("div","fc-fleet-line"));
      add(e,$("strong","",n===null?"--":n));add(e,$("span","",label));
    }
    const faces=add(fleet,$("div","fc-fleet-faces"));
    agentList.slice(0,20).forEach(a => {
      const e=add(faces,$("span","fc-fleet-face fc-status-"+a.state,"▣"));
      e.title=slice(a.label,60)+" | "+a.state;});
    const rate=add(top,instrument("ACTIVITY","5-SECOND EVENT BINS · NOT TOKENS/SEC","fc-throughput"));
    const events=context.live&&context.live.events&&Array.isArray(context.live.events.items)?context.live.events.items:[];
    add(rate,flag(events.length?events.length+" RECORDED":"NO EVENT FEED",events.length?"recorded":"unknown"));
    add(rate,$("div","fc-throughput-number",events.length?String(events.length)+" events":"--"));
    add(rate,miniPlot(events.length?eventBins(events):null));
    return top;
  }
  // Industrial architecture is decorative; all lamps and operative labels are data-backed.
  function makeArchitecture(zone) {
    const s=svg("svg",{viewBox:"0 0 512 558",class:"fc-architecture","aria-hidden":"true",
      preserveAspectRatio:"none"});
    const accent=zone.accent;
    const defs=svg("defs",{},s);
    const metal=svg("linearGradient",{id:"fc-metal-"+zone.code,x1:"0",x2:"0",y1:"0",y2:"1"},defs);
    for(const [offset,color] of [[0,"#263f4c"],[.16,"#526779"],[.22,"#08141e"],[.6,"#172739"],[1,"#45546a"]])
      svg("stop",{offset, "stop-color":color},metal);
    const glow=svg("linearGradient",{id:"fc-glow-"+zone.code,x1:"0",x2:"1"},defs);
    for(const [offset,opacity] of [[0,.1],[.25,.9],[.75,.9],[1,.08]])
      svg("stop",{offset,"stop-color":accent,"stop-opacity":opacity},glow);
    const frame="url(#fc-metal-"+zone.code+")", beam="url(#fc-glow-"+zone.code+")";
    svg("rect",{x:1,y:1,width:510,height:556,rx:6,fill:"#091525",stroke:"#57738a","stroke-width":3},s);
    for(let y=8;y<558;y+=21)svg("line",{x1:0,y1:y,x2:512,y2:y,stroke:"#173344","stroke-width":.7,opacity:.38},s);
    for(let x=12;x<=508;x+=43)svg("line",{x1:x,y1:4,x2:x,y2:554,stroke:"#214055","stroke-width":.7,opacity:.45},s);
    // Large steel pillars, recessed machinery and backlit circuit racks.
    for(let i=0;i<8;i++) {
      const x=9+i*69;
      svg("rect",{x,y:12,width:14,height:530,fill:frame,stroke:"#07101b","stroke-width":2},s);
      for(let y=33;y<540;y+=106) {
        svg("circle",{cx:x+7,cy:y,r:3,fill:"#f0c06c",stroke:"#0a1622","stroke-width":1.5},s);
      }
    }
    for(let row=0;row<3;row++)for(let col=0;col<4;col++) {
      const x=31+col*120,y=63+row*139;
      svg("rect",{x,y,width:108,height:91,rx:5,fill:"#091826",stroke:"#45677b","stroke-width":2},s);
      svg("rect",{x:x+6,y:y+6,width:96,height:75,rx:3,fill:"#102c3d",stroke:accent,
        "stroke-opacity":.21,"stroke-width":1},s);
      for(let n=0;n<5;n++){
        const xx=x+15+n*17;
        svg("rect",{x:xx,y:y+18,width:11,height:32,rx:1,fill:"#031521",stroke:"#3a8696","stroke-width":1},s);
        svg("line",{x1:xx+2,y1:y+42,x2:xx+9,y2:y+42,stroke:accent,"stroke-width":1.4,opacity:.6},s);
      }
      for(let n=0;n<3;n++)svg("circle",{cx:x+17+n*25,cy:y+65,r:2.4,fill:(n+col)%3===0?"#fac967":"#31859c"},s);
    }
    // Pneumatic transfer rails with recessed, continuous soft lights (not status telemetry).
    for(const y of [47,231,402,516]) {
      svg("path",{d:"M 9 "+y+" H 503",stroke:"#030d16","stroke-width":19,fill:"none"},s);
      svg("path",{d:"M 9 "+y+" H 503",stroke:frame,"stroke-width":12,fill:"none"},s);
      svg("path",{d:"M 9 "+y+" H 503",stroke:beam,"stroke-width":3.2,opacity:.86,fill:"none"},s);
    }
    for(const x of [19,246,491]){
      svg("path",{d:"M "+x+" 41 V 522",stroke:frame,"stroke-width":12,fill:"none"},s);
      svg("path",{d:"M "+x+" 41 V 522",stroke:beam,"stroke-width":2.8,fill:"none"},s);
    }
    for(let x=40;x<510;x+=83) for(let y=48;y<555;y+=184){
      svg("rect",{x,y,width:30,height:11,rx:2,fill:frame,stroke:"#091420","stroke-width":1.5},s);
      svg("circle",{cx:x+6,cy:y+5.5,r:2,fill:"#e2c086"},s);
      svg("circle",{cx:x+24,cy:y+5.5,r:2,fill:"#e2c086"},s);
    }
    // Mechanical flywheels, instrument towers and articulated overhead cranes.
    for(let col=0;col<6;col++){
      let x=38+col*88;
      for(let row=0;row<2;row++){
        let y=192+row*169;
        svg("circle",{cx:x,cy:y,r:16,fill:"#0b1e2b",stroke:"#81909b","stroke-width":5},s);
        svg("circle",{cx:x,cy:y,r:10,fill:"#173849",stroke:accent,"stroke-opacity":.65,"stroke-width":2},s);
        for(let n=0;n<8;n++){
          let a=n*Math.PI/4;
          svg("line",{x1:x+11*Math.cos(a),y1:y+11*Math.sin(a),x2:x+16*Math.cos(a),
            y2:y+16*Math.sin(a),stroke:"#c2a46e","stroke-width":2},s);
        }
        svg("circle",{cx:x,cy:y,r:4,fill:"#b6cdd0",stroke:"#122a39","stroke-width":2},s);
      }
    }
    for(let col=0;col<4;col++){
      let x=75+col*119,y=90;
      svg("path",{d:"M "+x+" "+y+" l 22 29 -13 22",stroke:"#162636","stroke-width":13,
        "stroke-linejoin":"round",fill:"none"},s);
      svg("path",{d:"M "+x+" "+y+" l 22 29 -13 22",stroke:"#7792a0","stroke-width":7,
        "stroke-linejoin":"round",fill:"none"},s);
      svg("circle",{cx:x+22,cy:y+29,r:7,fill:"#293f4e",stroke:"#e3af59","stroke-width":2},s);
      svg("path",{d:"M "+(x+9)+" "+(y+141)+" V "+(y+160)+" H "+(x+38),
        stroke:accent,opacity:.55,"stroke-width":2.6,fill:"none"},s);
    }
    // Cable bundles, robotic cranes and structural braces add high-frequency detail.
    for(let c=0;c<5;c++){
      let x=57+c*97;
      svg("path",{d:"M"+x+" 75 C"+(x+20)+" 106 "+(x-15)+" 150 "+(x+18)+" 173",stroke:"#040d14","stroke-width":8,fill:"none"},s);
      svg("path",{d:"M"+x+" 75 C"+(x+20)+" 106 "+(x-15)+" 150 "+(x+18)+" 173",stroke:"#44687f","stroke-width":3,fill:"none"},s);
    }
    svg("path",{d:"M0 0H512V558H0Z M27 31H485V527H27Z",fill:"none",
      stroke:"#7c9dae","stroke-width":4},s);
    return s;
  }
  function robotSprite(active) {
    const s=svg("svg",{viewBox:"0 0 140 133",class:"fc-robot-svg",
      "aria-hidden":"true",preserveAspectRatio:"xMidYMid meet"});
    const metal="#728ba0", hi="#b3cad4", shadow="#1a293a",blue="#07334b";
    const rdefs=svg("defs",{},s);
    const steel=svg("linearGradient",{id:"factoryRobotMetal",x1:"0%",x2:"100%",y1:"0%",y2:"65%"},rdefs);
    [["0%","#324c5e"],["19%","#c5e4ec"],["34%","#5a859c"],["66%","#bed6e1"],["100%","#253948"]]
      .forEach(p=>svg("stop",{offset:p[0],"stop-color":p[1]},steel));
    const visor=svg("radialGradient",{id:"factoryRobotVisor",cx:"53%",cy:"25%",r:"78%"},rdefs);
    [["0%","#215a70"],["38%","#092a41"],["100%","#010b17"]]
      .forEach(p=>svg("stop",{offset:p[0],"stop-color":p[1]},visor));
    // Workstation monitors & test equipment in the rear.
    for(let i=0;i<2;i++) {
      const x=i?107:4;
      svg("rect",{x,y:58,width:29,height:34,rx:3,fill:shadow,stroke:"#8a9faa","stroke-width":2},s);
      svg("rect",{x:x+4,y:63,width:21,height:19,rx:2,fill:blue,stroke:"#2c83a9"},s);
      for(let j=0;j<3;j++)svg("line",{x1:x+7,y1:69+j*4,x2:x+21,y2:69+j*4,stroke:"#55d8e9","stroke-width":1.4},s);
    }
    // Arms with joints and glove tips.
    for(const side of [-1,1]){
      const x=70+side*35;
      svg("path",{d:"M "+x+" 78 L "+(x+side*15)+" 92 L "+(x+side*9)+" 108",
        fill:"none",stroke:"#0b1721","stroke-width":14,"stroke-linecap":"round"},s);
      svg("path",{d:"M "+x+" 78 L "+(x+side*15)+" 92 L "+(x+side*9)+" 108",
        fill:"none",stroke:metal,"stroke-width":9,"stroke-linecap":"round"},s);
      svg("circle",{cx:x+side*15,cy:92,r:6,fill:"#314a5d",stroke:"#d3b36c","stroke-width":2},s);
      svg("circle",{cx:x+side*9,cy:108,r:5.5,fill:"#8eaabb",stroke:"#172e41","stroke-width":2},s);
    }
    // Chassis & body with backlit battery/circuit display.
    svg("rect",{x:47,y:70,width:47,height:44,rx:11,fill:"url(#factoryRobotMetal)",stroke:"#0c1c29","stroke-width":4},s);
    svg("rect",{x:54,y:77,width:32,height:28,rx:5,fill:"#0d2534",stroke:"#dae1e7","stroke-width":1},s);
    for(let i=0;i<3;i++)svg("rect",{x:60+i*8,y:85,width:5,height:10,rx:1,fill:active?"#5aff9b":"#547a83"},s);
    // Head & glow ears.
    svg("rect",{x:37,y:12,width:66,height:64,rx:17,fill:"url(#factoryRobotMetal)",stroke:"#c8d8df","stroke-width":3},s);
    svg("rect",{x:32,y:34,width:8,height:17,rx:2,fill:"#5a7f98",stroke:"#172a39"},s);
    svg("rect",{x:101,y:34,width:8,height:17,rx:2,fill:"#5a7f98",stroke:"#172a39"},s);
    svg("rect",{x:42,y:20,width:57,height:46,rx:12,fill:"url(#factoryRobotVisor)",stroke:"#8cc1d7","stroke-width":2.5},s);
    svg("path",{d:"M50 29 Q70 15 92 30",stroke:"#5790a8",opacity:.35,"stroke-width":2,fill:"none"},s);
    svg("path",{d:"M46 45 Q49 22 69 24",stroke:"#bbefff",opacity:.48,"stroke-width":2.1,fill:"none"},s);
    svg("path",{d:"M78 67 L92 61",stroke:"#6a91a6",opacity:.66,"stroke-width":1.5},s);
    for(let i=0;i<6;i++)svg("circle",{cx:50+i*8,cy:71,r:1,fill:"#162f40"},s);
    for(let side of [-1,1]) {
      const x=70+side*14;
      svg("rect",{x:x-4,y:39,width:8,height:10,rx:3,fill:active?"#5fffb7":"#5a9bb4",
        class:active?"fc-eye-live":""},s);
    }
    svg("path",{d:"M61 56 Q70 61 79 56",stroke:active?"#6dffc0":"#5095ad",
      "stroke-width":2,fill:"none","stroke-linecap":"round"},s);
    svg("rect",{x:68,y:8,width:4,height:8,fill:hi},s);
    svg("circle",{cx:70,cy:6,r:5,fill:active?"#5cff94":"#8599ac",
      stroke:"#092024","stroke-width":2,class:active?"fc-robot-live-lamp":""},s);
    // Foreground reinforced desk and keyboard, actual label rendered separately in HTML.
    svg("rect",{x:4,y:110,width:132,height:13,rx:2,fill:"#647a8a",stroke:"#0b1a28","stroke-width":3},s);
    svg("rect",{x:15,y:114,width:110,height:3,fill:"#c5a36e"},s);
    for(let i=0;i<14;i++)svg("rect",{x:24+i*6.8,y:119,width:4,height:2,fill:"#293d4c"},s);
    svg("path",{d:"M20 125V133 M121 125V133",stroke:"#3b5364","stroke-width":6},s);
    return s;
  }
  function station(a,zone,index) {
    const block=$("button","fc-station fc-state-"+(a?a.state:"missing"));
    block.type="button";
    block.setAttribute("data-district",zone.code);
    block.setAttribute("data-station",String(index));
    const active=!!a&&a.state==="live";
    block.setAttribute("aria-label",a?("Inspect "+a.label+"; "+a.state):
      "Uninstrumented work bay; no agent observed");
    const lamp=add(block,$("span","fc-status-light "+(active?"is-live":
      a&&a.kind==="process"?"is-process":a?"is-recorded":"is-off")));
    lamp.title=a?a.state:"No agent instrumented";
    const robot=add(block,$("div","fc-robot-shell"+(active?" fc-performing":"")));
    robot.appendChild(robotSprite(active));
    const plate=add(block,$("div","fc-station-plate"));
    add(plate,$("strong","",a?slice(a.label,18):"UNINSTRUMENTED"));
    add(plate,$("span","",a?(a.kind==="process"?
      (a.state==="live"?"LIVE INSTRUMENTED":a.state==="blocked"?"PROCESS I/O BLOCKED":
       a.state==="defunct"?"DEFUNCT":"PROCESS PRESENT / TASK UNKNOWN"):
      a.state==="live"?"LIVE SESSION":a.state==="recorded"?"OLD WORKER":"RECORD ONLY"):
      "NO OBSERVED AGENT"));
    if(a) {
      block.dataset.agent=a.key;
      on(block,"click",()=>drill({level:"agent",zone:zone.code,key:a.key,station:index}));
    } else block.disabled=true;
    return block;
  }
  function districtPanel(zone,i) {
    const card=$("section","fc-district fc-district-"+zone.code);
    card.style.setProperty("--zone-accent",zone.accent);
    card.style.left=ZONE_X[i]+"px";
    card.style.top=ZONE_Y+"px";
    card.style.width=ZONE_WIDTH+"px";
    card.style.height=ZONE_H+"px";
    card.dataset.zone=zone.code;
    card.appendChild(makeArchitecture(zone));
    const cap=add(card,$("div","fc-district-cap"));
    const title=add(cap,$("button","fc-district-drill"));
    title.type="button";
    add(title,$("strong","",zone.code+" / "+zone.name));
    add(title,$("span","","ZOOM INSIDE  ›"));
    on(title,"click",()=>drill({level:"district",zone:zone.code}));
    add(cap,$("span","fc-district-count",zoneAgents[i].length+" OBSERVED"));
    const control=add(card,$("div","fc-zone-meta",zone.label+"  |  "+zone.codeword+"  ·  VIEW / NOT AUTHORITY"));
    const menu=add(card,$("aside","fc-side-console"));
    menu.setAttribute("aria-label",zone.name+" schematic legend");
    const menuTerms=zone.code==="A"?["BUILD","TOOLS","DATA","MODELS","FILES"]:
      zone.code==="B"?["TEST","EVAL","DIFF","SCORE","REPORT"]:
      ["STORE","SEARCH","ROUTE","CACHE","QUEUE"];
    add(menu,$("strong","",zone.code));
    menuTerms.forEach(t=>add(menu,$("span","",t)));
    const cells=add(card,$("div","fc-workcells"));
    for(let k=0;k<6;k++) cells.appendChild(station(zoneAgents[i][k]||null,zone,k));
    const lower=add(card,$("div","fc-subrooms"));
    const subLabels=zone.code==="A"?["A.1 / RECORDED RUNS","A.2 / TOOL ACTIVITY"]:
      zone.code==="B"?["B.1 / EVALUATIONS","B.2 / EVIDENCE"]:
      ["C.1 / SOURCES","C.2 / DISPATCH"];
    subLabels.forEach((label,j)=>{
      const sub=add(lower,$("button","fc-subroom"));
      sub.type="button";
      add(sub,$("span","fc-subroom-heading",label));
      add(sub,$("span","fc-subroom-machinery"));
      add(sub,$("span","fc-subroom-action","EXPLORE ›"));
      on(sub,"click",()=>drill({level:"subdistrict",zone:zone.code,sub:j}));
    });
    return card;
  }
  function conduitGraphic() {
    const s=svg("svg",{viewBox:"0 0 1600 680",class:"fc-conduits","aria-hidden":"true"});
    [
      {stroke:"#50b2fd",path:"M0 71 H1600 M0 644 H1600"},
      {stroke:"#f6b764",path:"M519 134 H537 M1048 134 H1064 M519 410 H537 M1048 410 H1064"},
      {stroke:"#61d3a1",path:"M519 250 H537 M1048 250 H1064"}
    ].forEach(item=>{
      svg("path",{d:item.path,fill:"none",stroke:"#06101a","stroke-width":16},s);
      svg("path",{d:item.path,fill:"none",stroke:item.stroke,"stroke-width":4,
        opacity:.8},s);
    });
    for(const x of [525,1057])for(let y=110;y<560;y+=70) {
      svg("circle",{cx:x,cy:y,r:6,fill:"#131d2b",stroke:"#9ba3a5","stroke-width":2},s);
      svg("circle",{cx:x,cy:y,r:2,fill:"#ffc56a"},s);
    }
    return s;
  }
  function current() {return nav.length?nav[nav.length-1]:{level:"fleet"};}
  function linkedRuns(a) {
    if(!a||a.kind!=="historical")return [];
    const id=a.raw&&a.raw.worker_id;
    return ((context&&context.snapshot&&context.snapshot.runs)||[])
      .filter(r=>r.worker_id===id).slice(0,150);
  }
  function findAgent(key){return agentList.find(a=>a.key===key)||null;}
  function findZone(z){return ZONES.findIndex(x=>x.code===z);}
  function updateCrumbs(){
    const e=context&&context.nav;
    if(!e)return;
    e.textContent="";
    const root=$("button","fc-crumb","FLEET");
    root.type="button";on(root,"click",()=>goTo(-1));add(e,root);
    nav.forEach((node,i)=>{
      add(e,$("span","fc-crumb-divider","›"));
      let name=node.level==="district"?"DISTRICT "+node.zone:
        node.level==="subdistrict"?node.zone+"."+String((node.sub||0)+1):
        node.level==="agent"?slice((findAgent(node.key)||{}).label,17):"RUN "+(node.run&&node.run.run_id);
      const b=$("button","fc-crumb",name);
      b.type="button";b.setAttribute("aria-current",i===nav.length-1?"page":"false");
      on(b,"click",()=>goTo(i));
      add(e,b);
    });
    if(context.onDepth)context.onDepth(nav.length);
  }
  function boundsFor(node){
    if(!node||node.level==="fleet")return {x:0,y:0,w:VIEW_W,h:VIEW_H};
    const zi=findZone(node.zone);
    if(zi<0)return {x:0,y:0,w:VIEW_W,h:VIEW_H};
    if(node.level==="subdistrict") {
      if(node.sub===0)return {x:ZONE_X[zi]+16,y:489,w:235,h:142};
      return {x:ZONE_X[zi]+259,y:489,w:235,h:142};
    }
    if(node.level==="district")return {x:ZONE_X[zi],y:ZONE_Y,w:ZONE_WIDTH,h:ZONE_H};
    if(node.level==="agent"||node.level==="run") {
      const n=Number.isInteger(node.station)?node.station:0;
      return {x:ZONE_X[zi]+25+(n%3)*156,y:164+Math.floor(n/3)*150,w:151,h:143};
    }
    return {x:0,y:0,w:VIEW_W,h:VIEW_H};
  }
  function targetCam(region){
    if(!stage||!region)return null;
    let w=stage.clientWidth,h=stage.clientHeight;
    if(!(w>0&&h>0))return null;
    const pad=region.w===VIEW_W?0.99:0.87;
    const scale=Math.min(9,Math.max(.15,Math.min(w/region.w,h/region.h)*pad));
    return {s:scale,x:w/2-(region.x+region.w/2)*scale,y:h/2-(region.y+region.h/2)*scale};
  }
  function applyCam(animate=true){
    if(!world||!stage)return;
    world.style.transition=animate&&!window.matchMedia("(prefers-reduced-motion: reduce)").matches?
      "transform 680ms cubic-bezier(.17,.82,.24,1)" : "none";
    world.style.transform="translate("+camera.x+"px,"+camera.y+"px) scale("+camera.s+")";
    const label=context&&context.scaleLabel;
    if(label)label.textContent="ZOOM "+camera.s.toFixed(2)+"x · "+(current().level||"fleet").toUpperCase();
    const map=context&&context.mapViewport;
    if(map){
      const x=-camera.x/camera.s,y=-camera.y/camera.s;
      map.setAttribute("x",String(x));map.setAttribute("y",String(y));
      map.setAttribute("width",String(stage.clientWidth/camera.s));
      map.setAttribute("height",String(stage.clientHeight/camera.s));
    }
  }
  function focusCurrent(animate){
    const target=targetCam(boundsFor(current()));
    if(target){camera=target;applyCam(animate);}
  }
  function goTo(i){
    nav=i<0?[]:nav.slice(0,i+1);
    updateCrumbs();focusCurrent(true);updateMicroscope();
  }
  function drill(node){
    if(!node)return;
    if(node.level==="agent"&&!findAgent(node.key))return;
    if(node.level==="district")nav=[node];
    else if(node.level==="subdistrict")nav=[{level:"district",zone:node.zone},node];
    else if(node.level==="agent")nav=[{level:"district",zone:node.zone},node];
    else if(node.level==="run"&&node.run) {
      const agent=findAgent(node.key);if(!agent)return;
      nav=[{level:"district",zone:node.zone},
        {level:"agent",zone:node.zone,key:node.key,station:node.station},node];
    }
    updateCrumbs();focusCurrent(true);updateMicroscope();
    if(node.level==="run")loadRunMonitor(node);
    if(stage)stage.focus();
  }
  function stageButton(label,handler,cl=""){
    const b=$("button","fc-control "+cl,label);b.type="button";on(b,"click",handler);return b;
  }
  function updateMicroscope(){
    const aside=context&&context.microscope;if(!aside)return;
    aside.textContent="";
    const node=current();
    aside.classList.toggle("is-open", node.level!=="fleet");
    if(node.level==="fleet"){
      add(aside,$("div","fc-monitor-caption","MISSION CONTROL / FLEET"));
      add(aside,$("h3","","RECURSIVE FACTORY"));
      add(aside,$("p","","Select a real district, then approach an observed robot station. Status lights require verified live leases."));
      const p=$("p","fc-monitor-hint","WHEEL TO ZOOM · DRAG TO PAN · CLICK A BAY TO DRILL DOWN");
      add(aside,p);
      return;
    }
    const zi=findZone(node.zone),zone=ZONES[zi];
    add(aside,$("div","fc-monitor-caption",node.level.toUpperCase()+" / "+zone.label));
    if(node.level==="district" || node.level==="subdistrict"){
      add(aside,$("h3","",node.level==="subdistrict"?
        "MICRO ROOM "+zone.code+"."+(node.sub+1):"DISTRICT "+zone.code));
      add(aside,$("p","",zone.name+" is a visual grouping based on observed tool/role metadata; it does not assert a physical execution location."));
      add(aside,flag(zoneAgents[zi].length+" RECORDED AGENTS",zoneAgents[zi].some(a=>a.state==="live")?"live":"recorded"));
      const actions=add(aside,$("div","fc-monitor-actions"));
      zoneAgents[zi].slice(0,200).forEach(a=>{
        const btn=stageButton(slice(a.label,20)+" ›",()=>{
          drill({level:"agent",zone:zone.code,key:a.key,station:Math.max(0,zoneAgents[zi].indexOf(a))%6});});
        add(actions,btn);
      });
      if(!zoneAgents[zi].length)add(aside,$("p","fc-muted","NO INSTRUMENTED AGENTS CLASSIFIED IN THIS VISUAL LANE."));
    } else if(node.level==="agent"||node.level==="run"){
      const a=findAgent(node.key);
      if(!a){add(aside,$("p","","AGENT NO LONGER RECORDED."));return;}
      add(aside,$("h3","",slice(a.label,48)));
      add(aside,flag(a.state,a.state));
      const dl=add(aside,$("dl","fc-agent-facts"));
      const facts=a.kind==="process"?[
        ["ORIGIN",a.raw.source||"OS CENSUS"],["PROFILE",a.raw.profile],
        ["HOST",a.raw.host],["PID",a.raw.pid],
        ["PURPOSE",a.raw.purpose],["OBSERVED",a.raw.status],
        ["HEALTH",a.raw.health],["LAST ACTION",a.raw.last_phase||"UNKNOWN"],
        ["MODEL",a.raw.model||"UNKNOWN"],["PROVIDER",a.raw.provider||"UNKNOWN"],
        ["RAM MiB",censusFmt(a.raw.rss_mib)],["CPU %",censusFmt(a.raw.cpu_percent)],
        ["API",censusFmt(a.raw.calls)],["TOOL",censusFmt(a.raw.tools)]
      ]:[["ORIGIN",a.kind==="observer"?"OBSERVER FEED":"SUPERVISION HISTORY"],
        ["ROLE",value(a.raw.role)],["PHASE",value(a.raw.phase)],
        ["MODEL",value(a.raw.model)],["PROVIDER",value(a.raw.provider)],
        ["TOOL CALLS",num(a.raw.tool_calls)===null?"--":a.raw.tool_calls],
        ["API CALLS",num(a.raw.api_requests)===null?"--":a.raw.api_requests]];
      facts.forEach(([k,v])=>{add(dl,$("dt","",k));add(dl,$("dd","",v));});
      if(a.kind==="process"){
        const open=add(aside,stageButton("OPEN FULL PROCESS DOSSIER",()=>{
          const deck=document.querySelector(".fc-census");
          if(!deck)return;
          censusOpen=true;censusSelected=a.key;censusFilter="running";
          const search=deck.querySelector(".fc-census-search");
          if(search){search.value=String(a.raw.pid);search.dispatchEvent(new Event("input",{bubbles:true}));}
          deck.open=true;
          const row=Array.from(deck.querySelectorAll(".fc-census-entry")).find(
            n=>n.dataset.instanceKey===a.key);
          if(row)row.click();
          deck.scrollIntoView({behavior:"smooth",block:"start"});
        }));
        open.classList.add("fc-focus-dossier");
        return;
      }
      const related=linkedRuns(a),actions=add(aside,$("div","fc-run-links"));
      add(actions,$("div","fc-monitor-caption","LINKED RUNS / VERIFIED RELATIONSHIP"));
      if(!related.length) add(actions,$("p","fc-muted",
        a.kind==="observer"?"OBSERVER IDS ARE PRIVACY HASHED; HISTORICAL RUN ATTRIBUTION IS UNKNOWN.":
        "NO MATCHING RUNS RECORDED."));
      else related.slice(0,24).forEach(r=>{
        const label=slice(r.task_id,13)+" / #"+String(r.run_id);
        add(actions,stageButton(label,()=>drill({level:"run",zone:zone.code,key:a.key,station:node.station,run:r}),"fc-run-link"));
      });
    }
  }
  function loadRunMonitor(node){
    const detail=context&&context.runDetail;if(!detail)return;
    detail.textContent="";
    const r=node.run,identity=value(r.task_id)+" / #"+value(r.run_id);
    add(detail,$("div","fc-monitor-caption","RECORDED RUN / AUTHENTICATED"));
    add(detail,$("h3","",identity));
    const dl=add(detail,$("dl","fc-agent-facts"));
    [["EVENTS",r.events],["LAST SEEN",r.last_at],["FRESHNESS",r.freshness&&r.freshness.state],
      ["WORKER",r.worker_id]].forEach(([k,v])=>{add(dl,$("dt","",k));add(dl,$("dd","",v));});
    if(!context.api)return;
    const target=detail;
    context.api("/api/run?task_id="+encodeURIComponent(r.task_id)+"&run_id="+encodeURIComponent(r.run_id))
      .then(response=>{
        if(target!==context.runDetail||current().level!=="run")return;
        const events=Array.isArray(response.events)?response.events.slice(0,40):[];
        if(!events.length){add(target,$("p","fc-muted","NO AUTHENTICATED EVENT DETAIL FOUND."));return;}
        add(target,$("div","fc-monitor-caption",events.length+" OBSERVED EVENT RECORDS"));
        events.forEach(e=>{
          const l=add(target,$("div","fc-run-event"));
          add(l,$("span","",stamp(e.at||e.timestamp||e.ts)));
          add(l,$("strong","",slice(e.type||e.kind||"OBSERVED EVENT",35)));
        });
      }).catch(()=>{if(target===context.runDetail)add(target,$("p","fc-muted","RUN EVIDENCE UNAVAILABLE."));});
  }
  function zoomRelative(factor,atX,atY) {
    const s=Math.min(12,Math.max(.15,camera.s*factor)),k=s/camera.s;
    camera={s,x:atX-(atX-camera.x)*k,y:atY-(atY-camera.y)*k};
    applyCam(false);
  }
  function bindNavigation() {
    const v=stage;
    on(v,"wheel",e=>{
      e.preventDefault();const r=v.getBoundingClientRect();
      zoomRelative(Math.exp(-Math.max(-150,Math.min(150,e.deltaY*(e.deltaMode===1?12:1)))*.008),
        e.clientX-r.left,e.clientY-r.top);
    },{passive:false});
    on(v,"pointerdown",e=>{
      if(e.button!==0||e.target.closest("button"))return;
      drag={id:e.pointerId,x:e.clientX,y:e.clientY,startX:e.clientX,startY:e.clientY};
      try{v.setPointerCapture(e.pointerId);}catch(_){}
      v.classList.add("is-dragging");
    });
    on(v,"pointermove",e=>{
      if(!drag||e.pointerId!==drag.id)return;
      camera={...camera,x:camera.x+(e.clientX-drag.x),y:camera.y+(e.clientY-drag.y)};
      drag.x=e.clientX;drag.y=e.clientY;applyCam(false);
    });
    const cancel=()=>{drag=null;v.classList.remove("is-dragging");};
    on(v,"pointerup",cancel);on(v,"pointercancel",cancel);on(v,"lostpointercapture",cancel);
    on(v,"blur",cancel);
    on(v,"keydown",e=>{
      if(e.key==="Escape"||e.key==="Backspace") {e.preventDefault();goTo(nav.length-2);}
      else if(e.key==="Home"){e.preventDefault();goTo(-1);}
      else if(e.key==="+"||e.key==="="){e.preventDefault();zoomRelative(1.25,v.clientWidth/2,v.clientHeight/2);}
      else if(e.key==="-"){e.preventDefault();zoomRelative(.8,v.clientWidth/2,v.clientHeight/2);}
      else if(["ArrowUp","ArrowDown","ArrowLeft","ArrowRight"].includes(e.key)){
        e.preventDefault();
        camera.x+=(e.key==="ArrowLeft"?42:e.key==="ArrowRight"?-42:0);
        camera.y+=(e.key==="ArrowUp"?42:e.key==="ArrowDown"?-42:0);
        applyCam(false);
      }
    });
  }
  function eventLog(parent) {
    const left=add(parent,instrument("EVENT LOG","SANITIZED OBSERVER EVIDENCE","fc-eventlog"));
    const body=add(left,$("div","fc-eventlog-body"));
    const records=context.live&&context.live.events&&Array.isArray(context.live.events.items)?
      context.live.events.items.slice(0,9):[];
    const feed=context.live&&context.live.agent_feed||{};
    const summary=add(body,$("div","fc-event-observation"));
    const first=records[0],at=first&&Date.parse(first.ts);
    const age=Number.isFinite(at)?Math.round((Date.now()-at)/1000):null;
    const label=age===null?"EVENT TIME UNAVAILABLE":age < -5?"CLOCK SKEW":
      age<60?"LAST EVENT "+Math.max(0,age)+"s AGO":
      age<3600?"LAST EVENT "+Math.floor(age/60)+"m AGO":
      "LAST EVENT "+Math.floor(age/3600)+"h AGO";
    add(summary,$("strong","",label));
    add(summary,$("span","","SHOWING "+records.length+" MOST RECENT OBSERVER EVENTS"));
    if(feed.lease_files_truncated)
      add(body,$("p","fc-event-coverage","PARTIAL COVERAGE: "+
        feed.lease_files_read+"/"+feed.lease_files_seen+
        " LEASE GENERATIONS SAMPLED. OLDER OBSERVATIONS MAY BE OMITTED."));
    if(!records.length)add(body,$("p","fc-muted","NO RECORDED OBSERVER EVENTS IN THIS VIEW. ACTIVITY MAY EXIST OUTSIDE OBSERVER COVERAGE."));
    records.forEach(e=>{
      const line=add(body,$("div","fc-log-line"));
      add(line,$("span","fc-log-dot "+(e.lease_status==="live"?"live":"historical")));
      const at=add(line,$("time","",stamp(e.ts)));
      if(e.ts){at.dateTime=String(e.ts);at.title=String(e.ts);}
      add(line,$("span","fc-log-kind",slice(e.kind||"OBSERVED",22).replace(/_/g," ")));
      add(line,$("span","fc-log-message",slice(e.tool||e.status||e.lease_status||"NO DETAIL",34)));
    });
    return left;
  }
  function completeRoster(parent) {
    const box=$("details","fc-roster");
    const summary=$("summary","","AGENT REGISTRY / "+agentList.length+" RECORDED IDENTITIES · CLICK TO DRILL IN");
    add(box,summary);
    const search=add(box,$("input","fc-roster-search"));
    search.type="search";search.placeholder="Filter observed ID or role";search.setAttribute("aria-label","Filter recorded agent registry");
    const list=add(box,$("div","fc-roster-grid"));
    let page=0;
    const bar=add(box,$("div","fc-roster-paging"));
    function draw(){
      const term=search.value.toLowerCase().trim();
      const matched=agentList.filter(a=>[a.label,a.raw&&a.raw.role,a.raw&&a.raw.model,a.state]
        .some(v=>String(v||"").toLowerCase().includes(term)));
      let maxPage=Math.max(0,Math.ceil(matched.length/30)-1);
      page=Math.min(maxPage,page);list.textContent="";bar.textContent="";
      matched.slice(page*30,(page+1)*30).forEach(a=>{
        const b=$("button","fc-roster-entry fc-status-"+a.state);
        b.type="button";
        add(b,$("strong","",slice(a.label,33)));
        add(b,$("span","",a.state+" / LANE "+ZONES[a.zone].code));
        on(b,"click",()=>drill({level:"agent",zone:ZONES[a.zone].code,key:a.key,
          station:Math.min(5,zoneAgents[a.zone].indexOf(a))}));
        add(list,b);
      });
      const prev=stageButton("← PREV",()=>{page--;draw();});prev.disabled=page===0;
      const next=stageButton("NEXT →",()=>{page++;draw();});next.disabled=page===maxPage;
      add(bar,prev);add(bar,$("span","","PAGE "+(page+1)+"/"+(maxPage+1)+"  ·  "+matched.length+" MATCHES"));add(bar,next);
    }
    on(search,"input",()=>{page=0;draw();});draw();add(parent,box);
  }
  function factsGrid(parent) {
    const right=add(parent,instrument("REAL-TIME INSTRUMENTS","ONLY PROVEN SOURCE METRICS","fc-factspanel"));
    const grid=add(right,$("div","fc-factgrid"));
    const counts=fleetCounts();
    const events=context.live&&context.live.events&&Array.isArray(context.live.events.items)?context.live.events.items:[];
    const latencies=events.map(e=>num(e.dur_ms)).filter(v=>v!==null && v>=0).sort((a,b)=>a-b);
    const median=latencies.length?latencies[Math.floor(latencies.length/2)]/1000:null;
    const services=context.live&&context.live.services&&Array.isArray(context.live.services.sources)?
      context.live.services.sources:[];
    const items=[
      ["QUEUE","--","NO QUEUE FEED",null],["LIVE AGENTS",String(counts.active),"LEASE VERIFIED",counts.active],
      ["RUNS",String(counts.runs),"HISTORICAL RECORDS",counts.runs],
      ["ERRORS","--","NO ERROR-RATE FEED",null],
      ["MEDIAN EVENT",median===null?"--":fmt(median,2)+"s","OBSERVED DURATION",median],
      ["SERVICES",services.length?String(services.filter(s=>s.state==="ok").length)+"/"+services.length:"--",
        "DIRECT HEALTH PROBES",services.length?services.filter(s=>s.state==="ok").length:null]
    ];
    items.forEach(([label,big,sub,count],i)=>{
      const cell=add(grid,$("div","fc-fact"));
      add(cell,$("div","fc-fact-title",label));
      add(cell,$("div","fc-fact-number",big));
      add(cell,$("div","fc-fact-note",sub));
      if(i===1||i===2||i===4) add(cell,miniPlot(events.length?eventBins(events):null));
      else if(num(count)!==null) add(cell,meter(Math.min(100,count*10)));
    });
    return right;
  }
  // The census is process truth, distinct from task/session truth.
  // Persist view controls across 15s refreshes; no text is interpreted as HTML.
  let censusOpen=false, censusTerm="", censusFilter="running", censusPage=0;
  let censusSelected=null, censusExtra=[], censusTechnicalOpen=false, censusProfileOpen=false;
  function censusFmt(v, suffix="") {
    return typeof v==="number" && Number.isFinite(v)?String(v)+suffix:"--";
  }
  function censusStatus(r) {
    if(!r)return "unknown";
    if(r.status==="blocked_io")return "I/O BLOCKED";
    if(r.status==="remote_unreachable")return "HOST UNREACHABLE";
    if(r.status==="defunct")return "DEFUNCT";
    if(r.status==="ended")return "ENDED";
    return "OS PROCESS RUNNING";
  }
  function censusMetric(label,val,note) {
    const e=$("div","fc-census-metric");
    add(e,$("span","fc-census-metric-label",label));
    add(e,$("strong","",val));
    if(note)add(e,$("small","",note));
    return e;
  }
  function observationAge(seconds) {
    if(typeof seconds!=="number"||!Number.isFinite(seconds)||seconds<0)return "UNKNOWN";
    if(seconds<60)return Math.floor(seconds)+"s AGO";
    if(seconds<3600)return Math.floor(seconds/60)+"m AGO";
    return Math.floor(seconds/3600)+"h AGO";
  }
  function watchSummary(p) {
    const panel=$("section","fc-watch-hero");
    const linked=p.observer==="instrumented";
    const fresh=linked&&typeof p.last_observation_age_s==="number"&&
      p.last_observation_age_s>=0&&p.last_observation_age_s<=30;
    const liveState=p.status==="running"||p.status==="blocked_io"||p.status==="defunct";
    const title=add(panel,$("div","fc-monitor-caption","MISSION STATUS / EVIDENCE GRADE"));
    title.setAttribute("role","heading");title.setAttribute("aria-level","4");
    const headline=add(panel,$("div","fc-watch-banner"));
    const state=add(headline,$("div","fc-watch-current"));
    add(state,$("span","fc-watch-eyebrow","MOST RECENT VERIFIED PHASE"));
    add(state,$("strong","",p.last_phase||"NO LIVE TASK PHASE OBSERVED"));
    add(state,$("small","",
      p.last_phase?"Linked observer event, not a completion guarantee":
      "Process presence does not establish task progress or intent"));
    const signal=add(headline,$("div","fc-watch-signal"));
    add(signal,flag(fresh?"RECENT HOOK EVIDENCE":linked?"OBSERVER DATA STALE":
      liveState?"PROCESS ONLY":"HISTORICAL",
      fresh?"live":linked?"stale":"unknown"));
    add(signal,$("span","",linked?observationAge(p.last_observation_age_s):
      "NO PER-PROCESS EVENT AGE"));
    const metrics=add(panel,$("div","fc-watch-kpis"));
    const t=p.tokens||{};
    const measured=[
      ["OS STATE",censusStatus(p),"SAMPLED PROCESS"],
      ["MODEL",p.model||"NOT LINKED","DIRECT MODEL EVENT ONLY"],
      ["OUTPUT",censusFmt(t.output),"OBSERVED TOKENS, LOWER BOUND"],
      ["CPU",censusFmt(p.cpu_percent,"%"),"SAMPLED PROCESS"],
      ["RAM",censusFmt(p.rss_mib," MiB"),"OS RESIDENT"],
      ["FAILURES",censusFmt(p.errors),"LINKED OBSERVER EVENTS"]
    ];
    measured.forEach(([label,val,note])=>metrics.appendChild(censusMetric(label,val,note)));
    const consumption=add(panel,$("div","fc-watch-consumption"));
    const expense=typeof p.cost_usd==="number"&&Number.isFinite(p.cost_usd)?
      "$"+p.cost_usd.toFixed(4):"UNPRICED";
    for(const [label,val] of [
      ["INPUT TOKENS",censusFmt(t.input)],
      ["LAST API LATENCY",censusFmt(p.last_api_ms," ms")],
      ["OUTPUT SPEED",censusFmt(p.speed_tps," tok/s")],
      ["COST",expense],
      ["TIMEOUTS",censusFmt(p.timeouts)],
      ["FALLBACKS",censusFmt(p.fallbacks)]
    ]) {
      const item=add(consumption,$("div","fc-watch-consumption-item"));
      add(item,$("span","",label));add(item,$("strong","",val));
    }
    add(panel,$("p","fc-watch-scope","SOURCE: "+
      (linked?"LINKED OBSERVER + PROCESS CENSUS":"OS PROCESS CENSUS ONLY")+
      ". UNKNOWN FIELDS ARE NOT ZERO. NATIVE HERMES PROFILE HISTORY IS CONTEXT, NOT PID ATTRIBUTION."));
    return panel;
  }

  function censusFacts(p){
    const metrics=$("section","fc-census-facts");
    const t=p.tokens||{};
    const facts=[
      ["WHO",p.profile+" / PID "+p.pid,"Hermes CLI process"],
      ["WHERE",p.host+" / "+(p.cwd_label||"LOCATION UNKNOWN"),"Host and sanitized working-directory name"],
      ["WHAT",p.purpose||"UNCLASSIFIED","Inferred from launch mode, not a task title"],
      ["WHY",p.task_intent||"TASK INTENT UNKNOWN","Requires explicitly linked mission evidence"],
      ["WHEN",p.started_at||"UNKNOWN","OS launch time"],
      ["LAST SEEN",p.last_seen||"UNKNOWN",p.source||"CENSUS"],
      ["HEALTH",censusStatus(p),"Process state; not proof of task success"],
      ["LIVENESS",p.observer==="instrumented"?"HOOK EVIDENCE":"PROCESS ONLY",
        "Uninstrumented does not mean idle"],
      ["CPU",censusFmt(p.cpu_percent,"%"),"Sampled per-process CPU; -- on first reading"],
      ["RAM",censusFmt(p.rss_mib," MiB"),"OS resident memory"],
      ["MODEL",p.model||"UNKNOWN","Only linked model observation"],
      ["PROVIDER",p.provider||"UNKNOWN","Only linked provider observation"],
      ["CURRENT ACTION",p.last_phase||"UNOBSERVED","Most recent verified lifecycle event"],
      ["LAST TOOL",p.last_tool||"UNOBSERVED","No raw tool arguments"],
      ["INPUT TOKENS",censusFmt(t.input),t.scope||"NO TOKEN OBSERVATION"],
      ["OUTPUT TOKENS",censusFmt(t.output),t.scope||"NO TOKEN OBSERVATION"],
      ["CACHE READ",censusFmt(t.cache_read),"Observed token count, lower bound"],
      ["CACHE WRITE",censusFmt(t.cache_write),"Observed token count, lower bound"],
      ["THINKING TOKENS",censusFmt(t.reasoning),"Only if reported by the provider"],
      ["OUTPUT SPEED",censusFmt(p.speed_tps," tok/s"),"Last output tokens / request duration"],
      ["API LATENCY",censusFmt(p.last_api_ms," ms"),"Last observed API response"],
      ["API CALLS",censusFmt(p.calls),"Deduplicated observer events; lower bound"],
      ["TOOL CALLS",censusFmt(p.tools),"Deduplicated observer events; lower bound"],
      ["FAILURES",censusFmt(p.errors),"Observed failures only; -- = unknown"],
      ["TIMEOUTS",censusFmt(p.timeouts),"Observed timeouts only; -- = unknown"],
      ["FALLBACKS",censusFmt(p.fallbacks),"-- until a real fallback event exists"],
      ["COST",p.cost_usd===null||p.cost_usd===undefined?"UNPRICED":
        "$"+censusFmt(p.cost_usd),"No billing rates are inferred"],
    ];
    facts.forEach(([label,value,note])=>metrics.appendChild(censusMetric(label,String(value),note)));
    return metrics;
  }
  function fleetExplorer(parent) {
    const fleet=context.fleet;
    const primary=fleet&&Array.isArray(fleet.instances)?fleet.instances:[];
    const known=new Set(primary.map(p=>p.key));
    const rows=primary.concat(censusExtra.filter(p=>!known.has(p.key)));
    const priority=p=>{
      if(p.status==="running"&&p.observer==="instrumented")return 0;
      if(p.status==="running")return 1;
      if(p.status==="blocked_io")return 2;
      if(p.status==="defunct")return 3;
      return 4;
    };
    rows.sort((a,b)=>priority(a)-priority(b)||
      String(b.last_event_at||b.last_seen||"").localeCompare(String(a.last_event_at||a.last_seen||""))||
      String(a.key).localeCompare(String(b.key)));
    if(!rows.some(p=>p.key===censusSelected)) {
      const first=rows.find(p=>p.status==="running")||rows[0];
      censusSelected=first?first.key:null;
    }
    const counts=fleet&&fleet.counts?fleet.counts:{}, src=fleet&&fleet.sources?fleet.sources:{};
    const frame=$("details","fc-census");
    frame.open=censusOpen;
    const summary=$("summary","fc-census-summary");
    add(summary,$("strong","","HERMES OBSERVATION WINDOW / LIVE FLEET INTELLIGENCE"));
    add(summary,$("span","fc-census-summary-count",
      fleet&&fleet.status==="ok"?censusFmt(counts.running)+" RUNNING  /  "+
      censusFmt(counts.observer_linked)+" INSTRUMENTED  /  "+
      censusFmt(counts.ended)+" PREVIOUS":"PROCESS CENSUS UNAVAILABLE"));
    add(summary,$("span","fc-census-summary-action",censusOpen?"CLOSE DOSSIERS":"INSPECT LIVE FLEET"));
    add(frame,summary);
    const body=add(frame,$("div","fc-census-body"));
    on(frame,"toggle",()=>{
      censusOpen=frame.open;
      const action=frame.querySelector(".fc-census-summary-action");
      if(action)action.textContent=censusOpen?"CLOSE DOSSIERS":"INSPECT LIVE FLEET";
      if(censusOpen&&!detail.querySelector(".fc-watch-hero"))selectedPane();
    });
    parent.appendChild(frame);
    if(!fleet||fleet.status!=="ok"){
      add(body,$("p","fc-census-unknown",context.fleetError||"PROCESS CENSUS NOT YET COLLECTED. DO NOT ASSUME ZERO."));
      return;
    }
    if(fleet.records_truncated || fleet.records_total>rows.length){
      const loadBar=add(body,$("div","fc-census-loadall"));
      add(loadBar,$("strong","","CENSUS TOO LARGE FOR ONE RESPONSE; MORE VERIFIED RECORDS AVAILABLE."));
      const more=add(loadBar,stageButton("LOAD MORE PROCESSES",()=>{
        more.disabled=true;
        context.api("/api/fleet/page?offset="+(fleet.records_returned+censusExtra.length)+
          "&limit=250").then(page=>{
          if(!frame.isConnected||!page||!Array.isArray(page.instances))return;
          const present=new Set(rows.map(p=>p.key));
          page.instances.forEach(p=>{
            if(!p||!p.key||present.has(p.key))return;
            present.add(p.key);rows.push(p);censusExtra.push(p);
          });
          if(fleet.records_returned+censusExtra.length>=page.records_total)loadBar.remove();
          else more.disabled=false;
          draw();
        }).catch(()=>{more.disabled=false;});
      }));
    }
    const quality=add(body,$("div","fc-watch-coverage"));
    const running=typeof counts.running==="number"?counts.running:null;
    const linked=typeof counts.observer_linked==="number"?counts.observer_linked:null;
    const age=fleet.collector_age_s;
    for(const [label,value,note] of [
      ["PROCESS DISCOVERY",running===null?"UNKNOWN":running+" RUNNING","OS CENSUS"],
      ["ACTIVITY COVERAGE",running===null||linked===null?"UNKNOWN":linked+" / "+running+" LINKED",
        "PER-PROCESS HOOK ATTRIBUTION"],
      ["PROCESS-ONLY",running===null||linked===null?"UNKNOWN":Math.max(0,running-linked)+" UNKNOWN",
        "NOT PROOF OF IDLE"],
      ["COLLECTOR FRESHNESS",observationAge(age),
        typeof age==="number"&&age>36?"CENSUS MAY BE STALE":"LAST MEASURED SAMPLE"]
    ]) {
      const q=add(quality,$("div","fc-watch-coverage-item"));
      add(q,$("span","",label));add(q,$("strong","",value));add(q,$("small","",note));
    }
    const sources=add(body,$("div","fc-census-sources"));
    for(const host of ["j2","win4060"]){
      const s=src[host]||{};
      const flagNode=add(sources,$("div","fc-census-source"));
      add(flagNode,$("strong","",host.toUpperCase()));
      add(flagNode,flag(s.state==="ok"?"LIST VERIFIED":"UNREACHABLE",s.state==="ok"?"recorded":"unknown"));
      add(flagNode,$("span","",s.state==="ok"?censusFmt(s.observed)+" DISCOVERED":"COUNT UNKNOWN"));
    }
    const caption=add(body,$("p","fc-census-caveat",
      "PROCESS ALIVE != AGENT WORKING. HOOK DATA != PROVIDER BILLING. UNKNOWN NEVER MEANS ZERO. " +
      "Each entry is one identified Hermes OS process, not a fictitious robot or session."));
    const layout=add(body,$("div","fc-census-layout"));
    const left=add(layout,$("section","fc-census-left"));
    const controls=add(left,$("div","fc-census-controls"));
    const filterInput=add(controls,$("input","fc-census-search"));
    filterInput.type="search";filterInput.placeholder="Find host, profile, model, PID, tool...";
    filterInput.value=censusTerm;
    filterInput.setAttribute("aria-label","Search observed Hermes instances");
    const filterBar=add(controls,$("div","fc-census-filters"));
    for(const [label,val] of [["RUNNING","running"],["HISTORY","history"],["ALL","all"]]) {
      const b=add(filterBar,stageButton(label,()=>{censusFilter=val;censusPage=0;draw();}));
      b.classList.toggle("selected",censusFilter===val);
    }
    const list=add(left,$("div","fc-census-list"));
    const paging=add(left,$("div","fc-census-pages"));
    const detail=add(layout,$("section","fc-census-detail"));
    function selectedPane(){
      detail.textContent="";
      const p=rows.find(r=>r.key===censusSelected);
      if(!p){
        add(detail,$("div","fc-monitor-caption","INSTANCE INTELLIGENCE"));
        add(detail,$("p","fc-census-empty","Select any discovered Hermes process to inspect its telemetry, progress evidence, resource history and failure counters."));
        return;
      }
      add(detail,$("div","fc-monitor-caption",p.host.toUpperCase()+" / PROCESS "+p.pid+" / "+p.mode.toUpperCase()));
      add(detail,$("h3","fc-census-detail-title",p.profile+" / "+p.key));
      const choose=add(detail,stageButton("← CHOOSE ANOTHER HERMES PROCESS",()=>{
        const listbox=frame.querySelector(".fc-census-left");
        if(listbox)listbox.scrollIntoView({block:"start",
          behavior:window.matchMedia("(prefers-reduced-motion: reduce)").matches?"auto":"smooth"});
      }));
      choose.classList.add("fc-watch-back");
      add(detail,flag(censusStatus(p),p.status==="running"?"recorded":p.status==="blocked_io"?"stale":"unknown"));
      detail.appendChild(watchSummary(p));
      const telemetry=add(detail,$("section","fc-census-timeline"));
      add(telemetry,$("div","fc-monitor-caption","RECORDED OBSERVATIONS / NEVER RAW PROMPTS"));
      const sessions=Array.isArray(p.sessions)?p.sessions:[];
      if(sessions.length){
        add(telemetry,$("p","fc-census-caveat",sessions.length+" VERIFIED OBSERVER SESSION IDENTITIES"));
        sessions.forEach(s=>{
          const event=add(telemetry,$("div","fc-census-event"));
          add(event,$("time","",stamp(s.last_at)));
          add(event,$("span","",String(s.role||"session")+" / "+String(s.phase||"unknown")));
          add(event,flag(s.lease_status||"unknown",s.lease_status==="live"?"live":"recorded"));
        });
      }
      const detailed=add(detail,$("details","fc-watch-technical"));
      detailed.open=censusTechnicalOpen;
      add(detailed,$("summary","","TECHNICAL DOSSIER / 27 RAW OBSERVATION FIELDS"));
      detailed.appendChild(censusFacts(p));
      on(detailed,"toggle",()=>{censusTechnicalOpen=detailed.open;});
      const eventTarget=add(telemetry,$("div","fc-census-event-detail"));
      add(eventTarget,$("p","fc-census-caveat","Loading correlated events and process resource samples..."));
      if(!context.api)return;
      const selected=censusSelected;
      context.api("/api/fleet/instance?key="+encodeURIComponent(p.key)+"&limit=65").then(result=>{
        if(!detail.isConnected||selected!==censusSelected||!result||!result.instance)return;
        const native=result.profile_sessions;
        const profileBox=$("details","fc-native-profile");
        profileBox.open=censusProfileOpen;
        detail.insertBefore(profileBox,detailed);
        add(profileBox,$("summary","fc-profile-toggle",
          "PROFILE SESSION ARCHIVE / "+p.profile.toUpperCase()+" / NOT ATTRIBUTED TO THIS PID"));
        on(profileBox,"toggle",()=>{censusProfileOpen=profileBox.open;});
        if(!native||native.state!=="ok"){
          add(profileBox,$("p","fc-census-unknown",
            "Hermes state.db metadata unavailable for this profile. No task intent will be inferred."));
        }else{
          add(profileBox,$("p","fc-profile-disclaimer",
            native.total_sessions+" NATIVE SESSION RECORDS. THIS IS PROFILE-LEVEL EVIDENCE, NOT A CLAIM THAT PID "+
            p.pid+" EXECUTED EVERY SESSION. OPEN DOES NOT MEAN ACTIVELY RUNNING."));
          const stats=native.summary||{};
          const totals=add(profileBox,$("div","fc-profile-totals"));
          totals.appendChild(censusMetric("PROFILE INPUT TOKENS",
            censusFmt(stats.input_tokens),"Across the native session index, not this PID"));
          totals.appendChild(censusMetric("PROFILE OUTPUT TOKENS",
            censusFmt(stats.output_tokens),"Recorded session metadata totals"));
          totals.appendChild(censusMetric("STORED ESTIMATED COST",
            typeof stats.stored_estimate_usd==="number"?"$"+stats.stored_estimate_usd:"--",
            "Sum of available Hermes estimates; NOT a provider invoice"));
          totals.appendChild(censusMetric("OPEN SESSION RECORDS",
            censusFmt(stats.open_session_records),"Open does not mean actively executing"));
          const sessionList=add(profileBox,$("div","fc-profile-sessions"));
          let offset=0;
          const cursor=add(profileBox,$("div","fc-census-pages"));
          function appendSessionRows(batch){
            const items=Array.isArray(batch.sessions)?batch.sessions:[];
            for(const s of items){
              const card=$("article","fc-profile-session");
              add(card,$("div","fc-profile-session-header"));
              const header=card.lastChild;
              add(header,$("strong","",s.title||"UNTITLED HERMES SESSION / "+s.id));
              add(header,flag(s.state==="open_metadata"?"OPEN RECORD":"ENDED RECORD",
                s.state==="open_metadata"?"recorded":"unknown"));
              add(card,$("div","fc-profile-session-source",
                "SOURCE "+(s.source||"UNKNOWN")+"  /  "+(s.model||"MODEL UNKNOWN")+"  /  "+
                (s.provider||"PROVIDER UNKNOWN")+"  /  "+(s.last_at||"TIME UNKNOWN")));
              add(card,$("p","fc-profile-session-activity",
                s.activity||"No safe native activity description recorded."));
              const kpis=add(card,$("div","fc-profile-session-kpis"));
              const t=s.tokens||{},c=s.cost||{};
              const price=c.actual_usd!==null&&c.actual_usd!==undefined?
                "REPORTED USD $"+c.actual_usd:
                c.estimated_usd!==null&&c.estimated_usd!==undefined?
                  "ESTIMATED USD $"+c.estimated_usd+" ("+(c.status||"UNPRICED")+")":"COST UNAVAILABLE";
              for(const x of [
                "INPUT "+censusFmt(t.input),"OUTPUT "+censusFmt(t.output),
                "TOOLS "+censusFmt(s.tool_calls),"API "+censusFmt(s.api_calls),price
              ])add(kpis,$("span","",x));
              if(Array.isArray(s.models)&&s.models.length){
                const more=$("details","fc-profile-models");
                add(more,$("summary","",s.models.length+" RECORDED MODEL/PROVIDER USAGE ROWS"));
                s.models.forEach(m=>{
                  const line=add(more,$("p","",
                    String(m.provider||"unknown")+" / "+String(m.model||"unknown")+
                    "  CALLS "+censusFmt(m.api_calls)+
                    "  INPUT "+censusFmt(m.input_tokens)+
                    "  OUTPUT "+censusFmt(m.output_tokens)+
                    "  EST $"+censusFmt(m.estimated_cost_usd)));
                  line.classList.add("fc-profile-model-line");
                });
                add(card,more);
              }
              add(sessionList,card);
            }
            offset+=items.length;
            cursor.textContent="";
            add(cursor,$("span","","SHOWING "+offset+" / "+native.total_sessions+" PROFILE SESSIONS"));
            if(offset<native.total_sessions && items.length){
              const more=add(cursor,stageButton("LOAD MORE HISTORY",()=>{
                more.disabled=true;
                context.api("/api/fleet/profile?name="+encodeURIComponent(p.profile)+
                  "&offset="+offset+"&limit=20").then(report=>{
                    if(!detail.isConnected||selected!==censusSelected||!report||report.state!=="ok")return;
                    appendSessionRows(report);
                }).catch(()=>{if(more.isConnected)more.disabled=false;});
              }));
              more.classList.add("fc-profile-next");
            }
          }
          appendSessionRows(native);
        }
        const section=eventTarget;
        section.textContent="";
        const history=Array.isArray(result.samples)?result.samples.slice().reverse():[];
        const events=Array.isArray(result.events)?result.events:[];
        add(section,$("div","fc-monitor-caption",events.length+" CORRELATED EVENTS / NEWEST FIRST"));
        if(!events.length)add(section,$("p","fc-census-empty",
          "No linked lifecycle events for this specific PID. Native profile sessions below are not proof of this process's activity."));
        events.slice(0,12).forEach(e=>{
          const row=add(section,$("div","fc-census-event"));
          const time=add(row,$("time","",stamp(e.at)));
          if(e.at){time.dateTime=String(e.at);time.title=String(e.at);}
          add(row,$("span","",String(e.kind||"EVENT")+"  "+String(e.tool||e.model||e.status||"")));
          add(row,$("small","",e.dur_ms===null||e.dur_ms===undefined?"":"  "+e.dur_ms+"ms"));
        });
        if(events.length>12)add(section,$("p","fc-census-caveat",
          "SHOWING 12 OF "+events.length+" EVENTS. ALL COUNTS ARE BOUNDED BY RECORDER COVERAGE."));
        if(history.length>=2){
          const graphs=add(section,$("details","fc-watch-resource"));
          add(graphs,$("summary","","PROCESS CPU / RAM HISTORY (DIRECT OS SAMPLES)"));
          const measured=history.map(x=>x.cpu_percent).filter(x=>typeof x==="number"&&Number.isFinite(x));
          if(measured.length>=2)add(graphs,miniPlot(measured));
          const ram=history.map(x=>x.rss_mib).filter(x=>typeof x==="number"&&Number.isFinite(x));
          if(ram.length>=2){
            add(graphs,$("div","fc-monitor-caption","RESIDENT MEMORY / MiB"));
            add(graphs,miniPlot(ram));
          }
          add(graphs,$("small","","Samples available only while fleet collection is running. Missing samples are omitted, not converted to zero."));
        }
        if(context.restore&&context.restore.selectedKey===selected)
          restoreUIState(detail.closest(".fc-shell"),context.restore,true);
      }).catch(()=>{
        if(detail.isConnected&&selected===censusSelected)
          add(telemetry,$("p","fc-census-unknown","Detailed evidence unavailable. No simulated records."));
      });
    }
    function draw(){
      list.textContent="";paging.textContent="";
      [...filterBar.querySelectorAll("button")].forEach(b=>
        b.classList.toggle("selected",b.textContent.toLowerCase()===censusFilter));
      const q=censusTerm.trim().toLowerCase();
      const matching=rows.filter(p=>{
        const available=p.status==="running"||p.status==="blocked_io"||p.status==="defunct";
        if(censusFilter==="running"&&!available)return false;
        if(censusFilter==="history"&&available)return false;
        return !q||[p.host,p.pid,p.profile,p.mode,p.model,p.provider,p.last_tool,p.purpose,p.cwd_label]
          .some(v=>String(v===null||v===undefined?"":v).toLowerCase().includes(q));
      });
      // A filter must not leave an unrelated dossier selected offscreen.
      // This matters when switching RUNNING/HISTORY, or narrowing a search.
      if(!matching.some(p=>p.key===censusSelected)){
        censusSelected=matching.length?matching[0].key:null;
        if(frame.open)selectedPane();
      }
      const totalPages=Math.max(1,Math.ceil(matching.length/12));
      censusPage=Math.min(censusPage,totalPages-1);
      for(const p of matching.slice(censusPage*12,(censusPage+1)*12)){
        const card=$("button","fc-census-entry "+(p.key===censusSelected?"selected":""));
        card.type="button";card.dataset.instanceKey=p.key;
        add(card,$("strong","",p.profile+"  /  "+p.mode.toUpperCase()));
        add(card,$("span","",p.host+"  PID "+p.pid+"  "+censusStatus(p)));
        add(card,$("small","",p.observer==="instrumented"?
          "HOOK "+observationAge(p.last_observation_age_s)+"  /  "+
          (p.last_phase||p.model||"NO PHASE")+
          "  /  "+censusFmt(p.tokens&&p.tokens.output)+" OBS OUTPUT TOKENS":
          "PROCESS-ONLY EVIDENCE  /  TASK UNKNOWN"));
        on(card,"click",()=>{
          censusSelected=p.key;draw();selectedPane();
          if(window.matchMedia("(max-width:780px)").matches)
            detail.scrollIntoView({block:"start",
              behavior:window.matchMedia("(prefers-reduced-motion: reduce)").matches?"auto":"smooth"});
        });
        add(list,card);
      }
      if(!matching.length)add(list,$("p","fc-census-empty","NO MATCHING PROCESS RECORDS IN THIS CENSUS."));
      const prev=add(paging,stageButton("PREVIOUS",()=>{censusPage--;draw();}));
      prev.disabled=censusPage===0;
      add(paging,$("span","","PAGE "+(censusPage+1)+"/"+totalPages+"  /  "+matching.length+" MATCHES"));
      const next=add(paging,stageButton("NEXT",()=>{censusPage++;draw();}));
      next.disabled=censusPage>=totalPages-1;
    }
    on(filterInput,"input",()=>{censusTerm=filterInput.value;censusPage=0;draw();});
    draw();
    if(frame.open)selectedPane();
  }

  // The app refreshes the entire Factory every 15s. Capture operator state
  // BEFORE app.js removes the old DOM, then restore both scroll and focus.
  function captureUIState(main) {
    if(!main||!main.querySelector)return null;
    const root=main.querySelector(".fc-census");
    if(!root)return null;
    const detail=root.querySelector(".fc-census-detail");
    const list=root.querySelector(".fc-census-list");
    const profile=root.querySelector(".fc-profile-sessions");
    const search=root.querySelector(".fc-census-search");
    const eventLog=main.querySelector(".fc-eventlog-body");
    return {
      pageScroll:document.scrollingElement?document.scrollingElement.scrollTop:0,
      detailScroll:detail?detail.scrollTop:0,
      listScroll:list?list.scrollTop:0,
      profileScroll:profile?profile.scrollTop:0,
      eventScroll:eventLog?eventLog.scrollTop:0,
      searchFocus:!!search&&document.activeElement===search,
      searchStart:search?search.selectionStart:0,
      searchEnd:search?search.selectionEnd:0,
      selectedKey:censusSelected
    };
  }
  function restoreUIState(main,saved,late) {
    if(!main||!saved)return;
    const root=main.querySelector(".fc-census");
    if(!root||!root.open)return;
    const detail=root.querySelector(".fc-census-detail");
    const profile=root.querySelector(".fc-profile-sessions");
    if(detail)detail.scrollTop=saved.detailScroll||0;
    if(profile&&censusSelected===saved.selectedKey)profile.scrollTop=saved.profileScroll||0;
    if(late)return;
    const list=root.querySelector(".fc-census-list");
    if(list)list.scrollTop=saved.listScroll||0;
    const eventLog=main.querySelector(".fc-eventlog-body");
    if(eventLog)eventLog.scrollTop=saved.eventScroll||0;
    const search=root.querySelector(".fc-census-search");
    if(search&&saved.searchFocus){
      search.focus({preventScroll:true});
      if(typeof saved.searchStart==="number"&&typeof saved.searchEnd==="number")
        search.setSelectionRange(saved.searchStart,saved.searchEnd);
    }
    if(document.scrollingElement&&typeof saved.pageScroll==="number")
      document.scrollingElement.scrollTop=saved.pageScroll;
  }

  function buildShell(main){
    const root=add(main,$("div","fc-shell"));
    const title=add(root,$("header","fc-head"));
    const emblem=add(title,$("div","fc-emblem","N"));
    const group=add(title,$("div","fc-head-copy"));
    add(group,$("div","fc-heading","NERVE / INFINITE ZOOM CONTROL ROOM"));
    add(group,$("div","fc-subheading","LLM FLEET OBSERVATORY · EVIDENCE FIRST · READ ONLY"));
    const right=add(title,$("div","fc-head-right"));
    const live=context.live&&context.live.agent_feed;
    add(right,flag(live&&live.state==="available"?"OBSERVER FEED":"NO FRESH FEED",live&&live.state==="available"?"recorded":"unknown"));
    const atlas=add(right,stageButton("SPATIAL ATLAS ›",()=>{if(context.onAtlas)context.onAtlas();},"fc-atlas-switch"));
    atlas.id="factory-to-atlas";
    const now=$("time","fc-clock","");add(right,now);
    const updateClock=()=>{if(now.isConnected)now.textContent=new Date().toLocaleTimeString([], {hour12:false});};
    updateClock();
    const top=topDashboard(root);
    fleetExplorer(root);
    const heading=add(root,$("div","fc-map-heading"));
    const titlePanel=add(heading,$("div","fc-map-title"));
    add(titlePanel,$("strong","","AGENT DISTRICTS"));
    add(titlePanel,$("span","","VISUAL WORK LANES / FLEET → DISTRICT → AGENT → RECORDED RUN"));
    const navArea=add(heading,$("nav","fc-breadcrumb"));
    navArea.setAttribute("aria-label","Factory depth navigation");
    context.nav=navArea;
    const surround=add(root,$("section","fc-map-surround"));
    const vp=add(surround,$("div","fc-stage"));
    vp.setAttribute("role","application");
    vp.setAttribute("tabindex","0");
    vp.setAttribute("aria-label","Interactive illustrated factory; click a district or an observed robot, wheel to zoom, drag to pan, Home to fleet");
    stage=vp;
    world=add(vp,$("div","fc-world"));
    world.style.width=VIEW_W+"px";world.style.height=VIEW_H+"px";
    for(let i=0;i<ZONES.length;i++)world.appendChild(districtPanel(ZONES[i],i));
    world.appendChild(conduitGraphic());
    const overlay=add(vp,$("div","fc-stage-overlay"));
    const scale=add(overlay,$("span","fc-stage-scale","ZOOM 1x"));
    const hudActions=add(overlay,$("div","fc-hud-actions"));
    add(hudActions,stageButton("−",()=>zoomRelative(.8,vp.clientWidth/2,vp.clientHeight/2)));
    add(hudActions,stageButton("+",()=>zoomRelative(1.25,vp.clientWidth/2,vp.clientHeight/2)));
    add(hudActions,stageButton("FIT",()=>goTo(-1)));
    add(hudActions,stageButton("↑",()=>goTo(nav.length-2)));
    context.scaleLabel=scale;
    const consolePanel=add(surround,$("section","fc-microscope"));
    context.microscope=consolePanel;
    const minimap=add(consolePanel,$("div","fc-radar"));
    const map=svg("svg",{viewBox:"0 0 "+VIEW_W+" "+VIEW_H,class:"fc-radar-map",role:"img",
      "aria-label":"Click to recenter the factory map"});
    add(minimap,map);
    ZONES.forEach((z,i)=>svg("rect",{x:ZONE_X[i],y:ZONE_Y,width:ZONE_WIDTH,height:ZONE_H,
      fill:z.accent,"fill-opacity":.12,stroke:z.accent,"stroke-width":3},map));
    const view=svg("rect",{x:0,y:0,width:VIEW_W,height:VIEW_H,fill:"none",stroke:"#fbe1a2",
      "stroke-width":9,"stroke-dasharray":"27 12"},map);
    context.mapViewport=view;
    on(map,"click",e=>{
      const b=map.getBoundingClientRect();
      if(b.width<=0||b.height<=0)return;
      let cx=(e.clientX-b.left)/b.width*VIEW_W,cy=(e.clientY-b.top)/b.height*VIEW_H;
      camera.x=stage.clientWidth/2-cx*camera.s;
      camera.y=stage.clientHeight/2-cy*camera.s;applyCam();
      stage.focus();
    });
    context.runDetail=add(consolePanel,$("section","fc-run-monitor"));
    const bottom=add(root,$("section","fc-bottomstrip"));
    eventLog(bottom);factsGrid(bottom);
    completeRoster(root);
    const footer=add(root,$("footer","fc-footnote","ILLUSTRATION IS DECORATIVE · WORKER LIGHTS REQUIRE LIVE LEASE EVIDENCE · NO SYNTHETIC COUNTERS · AUTHORITY REMAINS WITH HERMES"));
    bindNavigation();
    return root;
  }
  function render(main,info) {
    if(!main||!info||!info.snapshot)return;
    context={snapshot:info.snapshot,live:info.live||null,
      fleet:info.fleet||null,fleetError:info.fleetError||null,
      api:info.api,onAtlas:info.onAtlas,restore:info.restore||null};
    agentList=sourceAgents(context.snapshot,context.live,context.fleet);
    zoneAgents=ZONES.map((_,i)=>agentList.filter(a=>a.zone===i));
    if(nav.length) {
      nav=nav.filter(n=>!n.key||!!findAgent(n.key));
      if(nav.length&&findZone(nav[0].zone)<0)nav=[];
    }
    // An interrupted recording never acquires a green lamp in a refresh.
    main.textContent="";
    buildShell(main);
    updateCrumbs();
    const remembered=info.restore||null;
    requestAnimationFrame(()=>{
      if(remembered)restoreUIState(main,remembered,false);
      if(!stage||!stage.isConnected)return;
      focusCurrent(false);
      updateMicroscope();
      if(resizeObs)resizeObs.disconnect();
      if(typeof ResizeObserver!=="undefined") {
        resizeObs=new ResizeObserver(function () {
          if(!stage||!stage.isConnected||drag)return;
          focusCurrent(false);
        });
        resizeObs.observe(stage);
      }
      if(current().level==="run")loadRunMonitor(current());
    });
    return main;
  }
  function up(){goTo(nav.length-2);}
  function reset(){nav=[];goTo(-1);}
  function dispose(){
    // The rendered DOM owns its listeners; dropping it avoids stale callbacks.
    if(resizeObs){resizeObs.disconnect();resizeObs=null;}
    if(flyTimeout){clearTimeout(flyTimeout);flyTimeout=null;}
    drag=null;stage=null;world=null;
    if(context){context.microscope=null;context.mapViewport=null;context.runDetail=null;}
  }
  return {render,up,reset,dispose,captureUIState,getDepth:()=>nav.length,
    getNavigation:()=>nav.map(n=>({level:n.level,zone:n.zone,key:n.key}))};
})();
