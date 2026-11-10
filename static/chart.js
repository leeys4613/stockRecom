// 공용 차트 위젯: 매크로 지표·관심종목·강세 종목이 같이 쓴다. 외부 라이브러리 없이 SVG 로 직접 그린다.
// 사용: KMSChart.mount(hostElement, { url: (interval, days, flags) => '/…', dailyOnly: false, compare: true|false|null, levels: false })
//   compare: null=비교 버튼 없음, false=버튼 있고 꺼짐, true=버튼 있고 켜짐(종목 vs 소속 지수, 시작점 맞춤)
(function () {
  var NS = 'http://www.w3.org/2000/svg';
  var KINDS = [['5m', '분봉(5분)'], ['1d', '일봉'], ['1wk', '주봉']];
  var SETS = { '5m': [[1, '1일'], [5, '5일']], '1d': [[30, '1개월'], [90, '3개월'], [365, '1년']], '1wk': [[365, '1년'], [1095, '3년'], [1825, '5년']] };
  var DEFAULT = { '5m': 1, '1d': 90, '1wk': 1095 };
  var UP = '#d33', DOWN = '#36c', GRAY = '#8d95a5', cache = {};

  function fmt(v, unit) {
    var a = Math.abs(v), d = a >= 1000 ? 0 : a >= 100 ? 1 : 2;
    if (unit === '%') return (v >= 0 ? '+' : '') + v.toFixed(2) + '%';
    return v.toLocaleString('ko-KR', { minimumFractionDigits: 0, maximumFractionDigits: d });
  }
  function sgn(v, d) { return (v >= 0 ? '+' : '') + v.toFixed(d == null ? 1 : d); }
  function el(tag, attrs, text) {
    var e = document.createElementNS(NS, tag);
    for (var k in attrs) e.setAttribute(k, attrs[k]);
    if (text != null) e.textContent = text;
    return e;
  }
  function div(cls, text) { var e = document.createElement('div'); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }

  function draw(box, d) {
    box.textContent = '';
    var W = 320, H = 170, L = 8, R = 8, T = 14, B = 22, pts = d.points, n = pts.length;
    var idx = d.index && d.index.points && d.index.points.length ? d.index : null;
    var lo = d.low, hi = d.high;
    if (idx) idx.points.forEach(function (p) { lo = Math.min(lo, p[1]); hi = Math.max(hi, p[1]); });
    var range = hi - lo || Math.abs(hi) * 0.02 || 1, lv = [];
    if (d.levels) {                                                 // 지지·저항선은 가격대가 크게 벗어나지 않을 때만 그린다(차트가 납작해지지 않게)
      (d.levels.supports || []).forEach(function (p) { if (p >= lo - range * 0.25 && p <= hi + range * 0.25) lv.push([p, 'sup']); });
      (d.levels.resistances || []).forEach(function (p) { if (p >= lo - range * 0.25 && p <= hi + range * 0.25) lv.push([p, 'res']); });
      lv.forEach(function (x) { lo = Math.min(lo, x[0]); hi = Math.max(hi, x[0]); });
    }
    var pad = (hi - lo) * 0.08 || Math.abs(hi) * 0.01 || 1;
    lo -= pad; hi += pad;
    var X = function (i, m) { m = m || n; return L + (W - L - R) * (m === 1 ? 0.5 : i / (m - 1)); };
    var Y = function (v) { return T + (H - T - B) * (1 - (v - lo) / (hi - lo)); };
    var up = d.last >= d.first, color = up ? UP : DOWN;
    var svg = el('svg', { viewBox: '0 0 ' + W + ' ' + H, role: 'img', 'aria-label': d.name + ' 추이 차트' });
    [d.high, d.low].forEach(function (v) {
      svg.appendChild(el('line', { x1: L, x2: W - R, y1: Y(v), y2: Y(v), stroke: 'currentColor', 'stroke-opacity': '.25', 'stroke-dasharray': '3 4' }));
      svg.appendChild(el('text', { x: W - R, y: Y(v) - 3, 'text-anchor': 'end', 'font-size': '10', fill: 'currentColor', 'fill-opacity': '.7' }, fmt(v, d.unit)));
    });
    lv.forEach(function (x) {
      var c = x[1] === 'sup' ? DOWN : UP;
      svg.appendChild(el('line', { x1: L, x2: W - R, y1: Y(x[0]), y2: Y(x[0]), stroke: c, 'stroke-opacity': '.75', 'stroke-width': '1.2', 'stroke-dasharray': '6 3' }));
      svg.appendChild(el('text', { x: L + 2, y: Y(x[0]) - 3, 'font-size': '10', fill: c }, (x[1] === 'sup' ? '지지 ' : '저항 ') + fmt(x[0], '')));
    });
    var line = pts.map(function (p, i) { return (i ? 'L' : 'M') + X(i).toFixed(1) + ' ' + Y(p[1]).toFixed(1); }).join(' ');
    svg.appendChild(el('path', { d: line + ' L' + X(n - 1).toFixed(1) + ' ' + (H - B) + ' L' + X(0).toFixed(1) + ' ' + (H - B) + ' Z', fill: color, 'fill-opacity': '.08' }));
    if (idx) {
      var m = idx.points.length;
      svg.appendChild(el('path', { d: idx.points.map(function (p, i) { return (i ? 'L' : 'M') + X(i, m).toFixed(1) + ' ' + Y(p[1]).toFixed(1); }).join(' '),
        fill: 'none', stroke: GRAY, 'stroke-width': '1.8', 'stroke-dasharray': '4 3', 'stroke-linejoin': 'round' }));
    }
    svg.appendChild(el('path', { d: line, fill: 'none', stroke: color, 'stroke-width': '2', 'stroke-linejoin': 'round', 'stroke-linecap': 'round' }));
    svg.appendChild(el('text', { x: L, y: H - 6, 'font-size': '10', fill: 'currentColor', 'fill-opacity': '.7' }, pts[0][0]));
    svg.appendChild(el('text', { x: W - R, y: H - 6, 'text-anchor': 'end', 'font-size': '10', fill: 'currentColor', 'fill-opacity': '.7' }, pts[n - 1][0]));
    var dot = el('circle', { r: 4, fill: color, visibility: 'hidden' }), tip = el('text', { 'font-size': '11', 'font-weight': '700', fill: 'currentColor', visibility: 'hidden' });
    svg.appendChild(dot); svg.appendChild(tip);
    function move(ev) {
      var r = svg.getBoundingClientRect(), x = (ev.clientX - r.left) / r.width * W;
      var i = Math.max(0, Math.min(n - 1, Math.round((x - L) / (W - L - R) * (n - 1))));
      dot.setAttribute('cx', X(i)); dot.setAttribute('cy', Y(pts[i][1])); dot.setAttribute('visibility', 'visible');
      tip.textContent = pts[i][0] + '  ' + fmt(pts[i][1], d.unit);
      tip.setAttribute('x', Math.min(W - 150, Math.max(L, X(i) - 60))); tip.setAttribute('y', 11); tip.setAttribute('visibility', 'visible');
    }
    svg.addEventListener('pointermove', move); svg.addEventListener('pointerdown', move);
    svg.addEventListener('pointerleave', function () { dot.setAttribute('visibility', 'hidden'); tip.setAttribute('visibility', 'hidden'); });
    box.appendChild(svg);
    var chg = d.unit === '%' ? '변화 ' + sgn(d.diff, 2) + '%p' : (d.chg == null ? '' : '기간 변화 ' + sgn(d.chg) + '%');
    box.appendChild(div('chart-sum mut', '현재 ' + fmt(d.last, d.unit) + ' · 최고 ' + fmt(d.high, d.unit) + ' · 최저 ' + fmt(d.low, d.unit) + (chg ? ' · ' + chg : '') + ' · 기준일 ' + d.as_of));
    if (idx) {
      var gap = idx.stock_chg - idx.chg;
      box.appendChild(div('chart-sum mut', '점선 = ' + idx.name + '(시작점을 맞춰 겹침) · 종목 ' + sgn(idx.stock_chg) + '% vs ' + idx.name + ' ' + sgn(idx.chg) + '% → 지수 대비 ' + sgn(gap) + '%p'));
    }
    if (lv.length) box.appendChild(div('chart-sum mut', '파란 점선 = 지지선, 빨간 점선 = 저항선(일봉 기준으로 기계적으로 계산한 값, 보장 아님)'));
    if (d.note) box.appendChild(div('chart-sum mut', d.note));
    if (d.behind) box.appendChild(div('chart-sum warn', '최신 일봉(' + d.expected + ')이 아직 반영되지 않아 ' + d.as_of + '까지만 표시합니다. 카드의 현재가와 다를 수 있습니다(데이터 제공처 지연).'));
    if (d.stale) box.appendChild(div('chart-sum warn', '최신 데이터를 받지 못해 저장된 데이터로 그렸습니다.'));
  }

  function load(opts, state, box) {
    var url = opts.url(state.interval, state.days, { compare: state.compare, levels: opts.levels });
    box.textContent = '불러오는 중…';
    if (cache[url]) { draw(box, cache[url]); return; }
    fetch(url, { cache: 'no-store' })
      .then(function (r) { return r.json().then(function (j) { if (!r.ok) throw new Error(j.error || '불러오지 못했습니다'); return j; }); })
      .then(function (j) { cache[url] = j; if (state.url === url || true) draw(box, j); })
      .catch(function (e) { box.textContent = '차트를 불러오지 못했습니다: ' + (e.message || '오류'); });
  }
  function pressed(group, btn) {
    Array.prototype.forEach.call(group.children, function (x) { x.setAttribute('aria-pressed', 'false'); });
    btn.setAttribute('aria-pressed', 'true');
  }

  function mount(host, opts) {
    host.textContent = '';
    var state = { interval: '1d', days: 90, compare: opts.compare === true };
    var kinds = div('chart-tabs'), ranges = div('chart-tabs'), box = div('chart-box');
    function buildRanges() {
      ranges.textContent = '';
      SETS[state.interval].forEach(function (r) {
        var b = document.createElement('button'); b.type = 'button'; b.textContent = r[1];
        b.setAttribute('aria-pressed', r[0] === state.days ? 'true' : 'false');
        b.addEventListener('click', function (e) { e.stopPropagation(); state.days = r[0]; pressed(ranges, b); load(opts, state, box); });
        ranges.appendChild(b);
      });
      if (opts.compare !== null && opts.compare !== undefined) {   // 종목 vs 지수 겹쳐 보기 토글
        var c = document.createElement('button'); c.type = 'button'; c.textContent = '지수와 비교';
        c.setAttribute('aria-pressed', state.compare ? 'true' : 'false');
        c.addEventListener('click', function (e) {
          e.stopPropagation(); state.compare = !state.compare; c.setAttribute('aria-pressed', state.compare ? 'true' : 'false'); load(opts, state, box);
        });
        ranges.appendChild(c);
      }
    }
    KINDS.forEach(function (k) {
      if (opts.dailyOnly && k[0] !== '1d') return;                  // 10거래일 기준 파생 지표는 일봉만
      var b = document.createElement('button'); b.type = 'button'; b.textContent = k[1];
      b.setAttribute('aria-pressed', k[0] === state.interval ? 'true' : 'false');
      b.addEventListener('click', function (e) {
        e.stopPropagation(); state.interval = k[0]; state.days = DEFAULT[k[0]]; pressed(kinds, b); buildRanges(); load(opts, state, box);
      });
      kinds.appendChild(b);
    });
    buildRanges();
    host.appendChild(kinds); host.appendChild(ranges); host.appendChild(box);
    load(opts, state, box);
  }

  // 종목 카드의 <details class="stock-chart" data-code data-compare="0|1" data-levels="0|1"> 를 처음 펼칠 때 차트를 만든다
  function initStockCharts() {
    Array.prototype.forEach.call(document.querySelectorAll('details.stock-chart'), function (d) {
      d.addEventListener('toggle', function () {
        var host = d.querySelector('.chart-host');
        if (!d.open || !host || host.dataset.ready) return;
        host.dataset.ready = '1';
        var code = d.dataset.code;
        mount(host, { compare: d.dataset.compare === '1', levels: d.dataset.levels === '1',
          url: function (interval, days, f) {
            return '/stock/chart/' + encodeURIComponent(code) + '?interval=' + interval + '&days=' + days +
              (f.compare ? '&compare=1' : '') + (f.levels ? '&levels=1' : '');
          } });
      });
    });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', initStockCharts); else initStockCharts();

  window.KMSChart = { mount: mount };
})();
