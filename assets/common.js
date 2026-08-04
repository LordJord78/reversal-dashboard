/* reversal-dashboard -- shared helpers for index.html and ticker.html
   No build step, no dependencies. Loaded with a plain <script> before the
   page-specific block, so everything here is global on purpose. */

/* ------------------------------------------------------------------ format */
const $   = id => document.getElementById(id);
const pct = v => (v >= 0 ? '+' : '') + (v * 100).toFixed(2) + '%';
const bp  = v => (v >= 0 ? '+' : '') + (v * 1e4).toFixed(1) + 'bp';
const usd = v => (v < 0 ? '-' : '') + '$' + Math.abs(v).toFixed(2);
const cls = v => v >= 0 ? 'up' : 'down';
const esc = s => String(s).replace(/[&<>"]/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const uid = () => Date.now().toString(36) + Math.random().toString(36).slice(2,7);

/* local calendar date -- toISOString() would roll an evening US trade to tomorrow */
const today = () => {
  const d = new Date();
  return new Date(d.getTime() - d.getTimezoneOffset()*6e4).toISOString().slice(0,10);
};

const FOCUS   = ['QQQ','SPY','XLK'];
const TV_SYM  = {QQQ:'NASDAQ:QQQ', SPY:'AMEX:SPY',  XLK:'AMEX:XLK',
                 DIA:'AMEX:DIA',   TLT:'NASDAQ:TLT', GLD:'AMEX:GLD'};

/* localStorage throws outright on opaque origins (a file:// page, Safari
   private mode, some embedded webviews). Falling back to memory keeps the
   page usable -- the trades just don't survive a reload, and store.ok lets
   the UI say so rather than silently losing someone's log. */
const store = (() => {
  let ok = false;
  try { localStorage.setItem('__probe','1'); localStorage.removeItem('__probe'); ok = true; }
  catch(e){ ok = false; }
  const mem = {};
  return {
    ok,
    get(k){ try{ return ok ? localStorage.getItem(k) : (mem[k] ?? null); }
            catch(e){ return mem[k] ?? null; } },
    set(k,v){ try{ if(ok) localStorage.setItem(k,v); else mem[k]=v; }
              catch(e){ mem[k]=v; } }
  };
})();

function acct(){ return Number(store.get('acct') || 25000); }

/* ------------------------------------------------------------------ charts */
function svgPath(vals, w, h, pad){
  const min = Math.min(...vals), max = Math.max(...vals), rng = (max-min) || 1;
  const X = i => vals.length > 1 ? i/(vals.length-1)*w : w/2;
  const Y = v => pad + (max-v)/rng*(h-2*pad);
  const line = vals.map((v,i) => (i?'L':'M') + X(i).toFixed(2) + ' ' + Y(v).toFixed(2)).join(' ');
  return {line, area: line + ` L ${w} ${h} L 0 ${h} Z`, min, max, X, Y};
}

function chartHTML(vals, id, h, baseline){
  const W = 1000, pad = 10;
  const p = svgPath(vals, W, h, pad);
  const base = (baseline !== undefined && baseline > p.min && baseline < p.max)
    ? `<line x1="0" y1="${p.Y(baseline).toFixed(2)}" x2="${W}" y2="${p.Y(baseline).toFixed(2)}"
        stroke="currentColor" stroke-width="1" stroke-dasharray="4 4" opacity=".35"
        vector-effect="non-scaling-stroke"/>` : '';
  return `<svg class="chsvg" style="height:${h}px;color:var(--tx3)" viewBox="0 0 ${W} ${h}"
      preserveAspectRatio="none" aria-hidden="true">
    <defs>
      <linearGradient id="fill-${id}" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0%"   stop-color="var(--vio)"  stop-opacity=".34"/>
        <stop offset="100%" stop-color="var(--blu2)" stop-opacity="0"/>
      </linearGradient>
      <linearGradient id="stroke-${id}" x1="0" y1="0" x2="1" y2="0">
        <stop offset="0%" stop-color="var(--vio)"/><stop offset="100%" stop-color="var(--blu2)"/>
      </linearGradient>
    </defs>
    <path d="${p.area}" fill="url(#fill-${id})"/>${base}
    <path d="${p.line}" fill="none" stroke="url(#stroke-${id})" stroke-width="2"
      stroke-linejoin="round" stroke-linecap="round" vector-effect="non-scaling-stroke"/>
  </svg>`;
}

/* Interactive chart with crosshair and tooltip.
   opts: {values, id, tip(i)->html, axHi, axLo, xFirst, xLast, baseline, height} */
function mountChart(host, opts){
  const v = opts.values, n = v.length;
  const H = opts.height || (window.innerWidth <= 820 ? 210 : 250);
  const p = svgPath(v, 1000, H, 10);
  const box = document.createElement('div');
  box.className = 'ch'; box.style.height = H + 'px';
  box.innerHTML = chartHTML(v, opts.id, H, opts.baseline) +
    `<div class="ax ax-hi">${opts.axHi ?? p.max.toFixed(2)}</div>
     <div class="ax ax-lo">${opts.axLo ?? p.min.toFixed(2)}</div>
     <div class="cross" hidden></div><div class="dot" hidden></div><div class="tip" hidden></div>`;
  host.innerHTML = '';
  host.appendChild(box);
  if(opts.xFirst || opts.xLast){
    const x = document.createElement('div');
    x.className = 'xlab';
    x.innerHTML = `<span>${opts.xFirst||''}</span><span>${opts.xLast||''}</span>`;
    host.appendChild(x);
  }

  const cross = box.querySelector('.cross'), dot = box.querySelector('.dot'),
        tip   = box.querySelector('.tip');
  function move(e){
    const r = box.getBoundingClientRect();
    if(!r.width) return;
    const cx = e.touches ? e.touches[0].clientX : e.clientX;
    const x  = Math.max(0, Math.min(r.width, cx - r.left));
    const i  = Math.round(x / r.width * (n-1));
    const px = i/(n-1) * r.width;
    const py = (p.max - v[i])/((p.max - p.min)||1) * (r.height - 20) + 10;
    cross.hidden = dot.hidden = tip.hidden = false;
    cross.style.left = px + 'px';
    dot.style.left = px + 'px'; dot.style.top = py + 'px';
    tip.innerHTML = opts.tip(i);
    tip.style.left = Math.min(Math.max(px - 70, 0), r.width - tip.offsetWidth) + 'px';
  }
  const out = () => { cross.hidden = dot.hidden = tip.hidden = true; };
  box.addEventListener('mousemove', move);
  box.addEventListener('mouseleave', out);
  box.addEventListener('touchmove', e => { move(e); e.preventDefault(); }, {passive:false});
  box.addEventListener('touchend', out);
}

/* ------------------------------------------------------------------ trades */
/* One store, shared by both pages. localStorage only -- never leaves the browser. */
function trades(){
  let t = null;
  try{ t = JSON.parse(store.get('trades_v2') || 'null'); }catch(e){}
  return Array.isArray(t) ? t : migrateTrades();
}
function migrateTrades(){
  let old = [];
  try{ old = JSON.parse(store.get('trades') || '[]'); }catch(e){}
  const out = old.map(x => ({
    id: uid(), ticker: x.ticker,
    side: (x.side||'').toString().toLowerCase().startsWith('s') ? 'short' : 'buy',
    size: x.size || null, entry: x.entry, entryDate: x.date,
    exit: x.exit ?? null, exitDate: x.exit != null ? x.date : null
  }));
  store.set('trades_v2', JSON.stringify(out));
  return out;
}
function saveTrades(t){
  store.set('trades_v2', JSON.stringify(t));
  if(typeof onTradesChanged === 'function') onTradesChanged();
}
const isOpen = t => t.exit === null || t.exit === undefined || t.exit === '';
const sortTrades = t => t.sort((a,b) =>
  (b.exitDate||b.entryDate||'').localeCompare(a.exitDate||a.entryDate||''));

function tradeRet(t, m){
  const exit = isOpen(t) ? m : t.exit;
  if(!exit || !t.entry) return null;
  const raw = exit/t.entry - 1;
  return t.side === 'short' ? -raw : raw;
}

function closeTrade(id, suggested){
  const t = trades(), x = t.find(v => v.id === id);
  if(!x) return;
  const val = prompt(`Exit price for ${x.ticker} (${x.side} @ ${x.entry})`,
                     suggested ? suggested.toFixed(2) : '');
  if(val === null) return;
  const ex = parseFloat(val);
  if(!isFinite(ex) || ex <= 0){ alert('not a valid price'); return; }
  x.exit = ex; x.exitDate = today();
  saveTrades(sortTrades(t));
}
function delTrade(id){ saveTrades(trades().filter(v => v.id !== id)); }

function exportTrades(){
  const b = new Blob([JSON.stringify(trades(), null, 2)], {type:'application/json'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(b); a.download = 'reversal-trades.json'; a.click();
}
function importTrades(e){
  const f = e.target.files[0]; if(!f) return;
  const r = new FileReader();
  r.onload = () => {
    try{
      const raw = JSON.parse(r.result);
      if(!Array.isArray(raw)) throw 0;
      /* id is regenerated, never carried over from the file. It is the one
         imported field that reaches an inline onclick handler rather than
         going through esc(), so a crafted export could otherwise smuggle
         script into the page that reads it back. Ids are internal and
         meaningless outside this browser, so nothing is lost by minting
         fresh ones. */
      saveTrades(sortTrades(raw.map(x => ({
        id: uid(), ticker: x.ticker,
        side: (x.side||'').toString().toLowerCase().startsWith('s') ? 'short' : 'buy',
        size: x.size ?? null, entry: x.entry,
        entryDate: x.entryDate || x.date || null,
        exit: x.exit ?? null,
        exitDate: x.exitDate || (x.exit != null ? (x.date || null) : null)
      }))));
    }catch(err){ alert('bad file'); }
  };
  r.readAsText(f);
  e.target.value = '';
}

/* ------------------------------------------------------------------ misc */
function zMeter(z, threshold){
  const lim = 3, p = v => (Math.max(-lim, Math.min(lim, v)) + lim)/(2*lim)*100;
  const pos = p(z), a = Math.min(50, pos), b = Math.max(50, pos);
  return `<div class="meter">
      <div class="mfill" style="left:${a}%;width:${(b-a).toFixed(2)}%"></div>
      <span class="mth" style="left:${p(-threshold)}%"></span>
      <span class="mth" style="left:${p(threshold)}%"></span>
    </div>
    <div class="mscale"><span>&minus;3</span><span>0</span><span>+3</span></div>`;
}

/* Say so rather than losing someone's trade log quietly. */
const storageWarning = () => store.ok ? '' :
  `<div class="banner">This browser is blocking local storage, so any trade you log
   will vanish on reload. Use <strong>Export JSON</strong> if you need to keep it.</div>`;

function statLine(s){
  if(s.sr === null || s.sr === undefined) return '';
  const bits = [`SR ${s.sr.toFixed(2)}`];
  if(s.sr_ex !== null && s.sr_ex !== undefined) bits.push(`ex-crisis ${s.sr_ex.toFixed(2)}`);
  if(s.tpy) bits.push(`${s.tpy}/yr`);
  if(s.avg_bp) bits.push(`avg +${s.avg_bp}bp`);
  if(s.spread_bp) bits.push(`spread ${s.spread_bp}bp`);
  return bits.join(' · ');
}

/* cache-busted JSON fetch; resolves to null rather than throwing on 404 */
function getJSON(path, required){
  return fetch(path + '?t=' + Date.now())
    .then(r => r.ok ? r.json() : (required ? Promise.reject(r.status) : null))
    .catch(e => { if(required) throw e; return null; });
}
