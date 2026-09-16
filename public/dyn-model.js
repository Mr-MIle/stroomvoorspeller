/*
 * dyn-model.js - rekenmodel achter /dynamisch-berekenen
 *
 * Vergelijkt een vast contract met een dynamisch contract op echte uurprijzen
 * uit het ENTSO-E-archief. Draait in de browser (window.DynModel) en in Node
 * (module.exports), zodat build-dynamisch.js exact hetzelfde rekent als de pagina.
 *
 * Profielen en constanten komen uit batterij-berekenen.html, zodat beide
 * pagina's dezelfde aannames gebruiken.
 */
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.DynModel = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  // ---------- vaste aannames ----------
  var EB = 0.0916;                 // energiebelasting per kWh, excl. btw (2026, tot 10.000 kWh)
  var BTW = 1.21;
  var SOLAR_PER_PANEL = 350;       // kWh per paneel per jaar
  var SOLAR_MONTH = [.02,.04,.08,.12,.14,.14,.13,.11,.09,.06,.04,.03];
  var CONS_MONTH  = [.095,.086,.086,.080,.076,.072,.072,.073,.078,.085,.094,.103];
  var HP_MONTH    = [.210,.155,.110,.055,.020,.005,.005,.010,.030,.085,.135,.180];
  // huishoudverbruik per uur: avondpiek (standaard) en een dagprofiel voor wie thuis werkt
  var CONS_HOUR_AVOND = [.022,.018,.016,.015,.016,.022,.038,.052,.050,.044,.042,.044,.046,.044,.042,.044,.052,.066,.074,.070,.060,.050,.040,.030];
  var CONS_HOUR_DAG   = [.020,.016,.015,.014,.015,.020,.033,.045,.048,.050,.050,.052,.054,.052,.050,.050,.052,.060,.064,.060,.052,.044,.036,.028];
  var HP_HOUR = [1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1];
  var SUNRISE = [8.4,7.7,6.9,5.7,5.0,4.5,4.7,5.4,6.2,7.0,7.0,8.0];
  var SUNSET  = [16.7,17.5,18.4,19.3,20.1,20.9,20.9,20.1,18.9,17.8,16.7,16.4];

  // laden van de auto: thuis-uren (avond en nacht) en het maximum per uur bij 16A, 1 fase
  var EV_WINDOW = [0,1,2,3,4,5,6,7,16,17,18,19,20,21,22,23];
  var EV_MAX_PER_HOUR = 3.7;
  var EV_AVOND = [17,18,19,20,21];
  // wie apparaten verschuift, verplaatst dit deel van het huishoudverbruik naar de goedkoopste uren
  var SHIFT_SHARE = 0.20;
  var SHIFT_HOURS = 4;

  var MAXCYC = { self: 0, balanced: 1.2, max: 3.5 };
  var SOLAR_THR = { self: 0, balanced: 0.005, max: 0.0 };
  var GRID_THR = { self: 0, balanced: 0.06, max: 0.01 };

  function daysInMonth(y, m) { return new Date(y, m + 1, 0).getDate(); }
  function sum(a) { var t = 0; for (var i = 0; i < a.length; i++) t += a[i]; return t; }

  // ---------- profielen ----------
  function solarDay(y, m, d, panels) {
    var annual = panels * SOLAR_PER_PANEL;
    var dayE = annual * SOLAR_MONTH[m] / daysInMonth(y, m);
    var sr = SUNRISE[m], ss = SUNSET[m], w = [], s = 0;
    for (var h = 0; h < 24; h++) {
      var x = (h + 0.5 - sr) / (ss - sr);
      var val = (x > 0 && x < 1) ? Math.sin(Math.PI * x) : 0;
      w.push(val); s += val;
    }
    return w.map(function (x) { return s > 0 ? dayE * x / s : 0; });
  }

  // huishoudverbruik + warmtepomp, zonder de auto
  function consDay(y, m, base, hp, profiel) {
    var shape = profiel === "dag" ? CONS_HOUR_DAG : CONS_HOUR_AVOND;
    var baseDay = (base || 0) * CONS_MONTH[m] / daysInMonth(y, m);
    var hpDay = (hp || 0) * HP_MONTH[m] / daysInMonth(y, m);
    var bs = sum(shape), hs = sum(HP_HOUR);
    return shape.map(function (x, h) { return baseDay * x / bs + hpDay * HP_HOUR[h] / hs; });
  }

  // verplaatst een deel van het huishoudverbruik naar de goedkoopste uren van de dag
  function shiftLoad(load, cp, share, nHours) {
    var order = [];
    for (var h = 0; h < 24; h++) order.push(h);
    order.sort(function (a, b) { return cp[a] - cp[b]; });
    var target = order.slice(0, nHours);
    var moved = 0, out = load.slice();
    for (var i = 0; i < 24; i++) {
      if (target.indexOf(i) >= 0) continue;
      var take = out[i] * share;
      out[i] -= take; moved += take;
    }
    for (var j = 0; j < target.length; j++) out[target[j]] += moved / target.length;
    return out;
  }

  // laadprofiel van de auto voor een dag
  function evDay(kwhPerDay, cp, mode) {
    var out = new Array(24).fill(0);
    if (!kwhPerDay) return out;
    var hours;
    if (mode === "avond") {
      hours = EV_AVOND.slice();
    } else {
      hours = EV_WINDOW.slice().sort(function (a, b) { return cp[a] - cp[b]; });
    }
    var left = kwhPerDay;
    for (var i = 0; i < hours.length && left > 0.0001; i++) {
      var put = Math.min(EV_MAX_PER_HOUR, left);
      out[hours[i]] += put; left -= put;
    }
    if (left > 0.0001) { // past niet in het raam: verdeel de rest gelijk
      for (var h = 0; h < 24; h++) out[h] += left / 24;
    }
    return out;
  }

  // ---------- dagsimulatie ----------
  // cp = inkoopprijs per uur (EUR/kWh, all-in), tb = teruglever-opbrengst per uur (EUR/kWh)
  function simulateDay(cp, tb, solar, load, bat, mode, socIn) {
    var cap = bat.cap, pwr = bat.pwr, ce = bat.ce, de = bat.de, rt = ce * de;
    var soc = Math.min(socIn || 0, cap);
    var imp = new Array(24).fill(0), expK = new Array(24).fill(0), sold = new Array(24).fill(0);
    var solL = new Array(24).fill(0), batL = new Array(24).fill(0);
    var charged = 0, loadTot = 0, solarGen = 0, h;

    for (h = 0; h < 24; h++) {
      loadTot += load[h]; solarGen += solar[h];
      var s = solar[h], l = load[h];
      var d = Math.min(s, l); solL[h] = d; s -= d; l -= d;
      if (l > 0 && soc > 0) { var out = Math.min(l, pwr, soc * de); soc -= out / de; batL[h] = out; l -= out; }
      if (l > 0) imp[h] = l;
      if (s > 0 && soc < cap) { var cin = Math.min(s, pwr, (cap - soc) / ce); soc += cin * ce; charged += cin * ce; s -= cin; }
      if (s > 0) expK[h] = s;
    }

    if (mode !== "self" && cap > 0) {
      var budget = Math.max(0, cap * MAXCYC[mode] - charged);
      var sthr = SOLAR_THR[mode], gthr = GRID_THR[mode], step = 0.25;
      var idx = []; for (h = 0; h < 24; h++) idx.push(h);
      var sellH = idx.slice().sort(function (a, b) { return tb[b] - tb[a]; });
      var cheapH = idx.slice().sort(function (a, b) { return cp[a] - cp[b]; });
      var sellRoom = function (x) { return pwr - sold[x]; };

      // zon-overschot bewaren en in het duurste uur terugleveren
      var expH = idx.filter(function (x) { return expK[x] > 0; }).sort(function (a, b) { return tb[a] - tb[b]; });
      for (var ei = 0; ei < expH.length; ei++) {
        var xh = expH[ei];
        while (expK[xh] > 0.001 && budget > 0.001) {
          var sh = -1;
          for (var si = 0; si < sellH.length; si++) {
            if (tb[sellH[si]] <= tb[xh]) break;
            if (sellRoom(sellH[si]) > 0.001) { sh = sellH[si]; break; }
          }
          if (sh < 0) break;
          if (tb[sh] * rt - tb[xh] <= sthr) break;
          var chunk = Math.min(step, expK[xh], budget, pwr, sellRoom(sh) / rt);
          if (chunk <= 0.001) break;
          expK[xh] -= chunk; sold[sh] += chunk * rt; charged += chunk; budget -= chunk;
        }
        if (budget <= 0.001) break;
      }
      // goedkoop laden om dure eigen inkoop te vermijden
      var impH = idx.filter(function (x) { return imp[x] > 0; }).sort(function (a, b) { return cp[b] - cp[a]; });
      for (var ii = 0; ii < impH.length; ii++) {
        var eh = impH[ii];
        while (imp[eh] > 0.001 && budget > 0.001) {
          var bh = cheapH[0];
          if (cp[eh] - cp[bh] / rt <= gthr) break;
          var ck = Math.min(step, imp[eh], budget, pwr);
          imp[eh] -= ck; imp[bh] += ck / rt; charged += ck / de; budget -= ck;
        }
        if (budget <= 0.001) break;
      }
      // goedkoop inkopen en in de piek terugleveren
      for (var qi = 0; qi < sellH.length; qi++) {
        var qh = sellH[qi];
        if (budget <= 0.001) break;
        while (sellRoom(qh) > 0.001 && budget > 0.001) {
          var cb = cheapH[0];
          if (tb[qh] * rt - cp[cb] <= gthr) break;
          var c2 = Math.min(step, budget, pwr, sellRoom(qh) / rt);
          if (c2 <= 0.001) break;
          imp[cb] += c2; sold[qh] += c2 * rt; charged += c2 * ce; budget -= c2;
        }
      }
    }

    var impCost = 0, expRev = 0, impKwh = 0, expKwh = 0;
    for (h = 0; h < 24; h++) {
      impCost += imp[h] * cp[h];
      impKwh += imp[h];
      var e = expK[h] + sold[h];
      expRev += e * tb[h];
      expKwh += e;
    }
    return { impKwh: impKwh, impCost: impCost, expKwh: expKwh, expRev: expRev,
             loadTot: loadTot, solarGen: solarGen, solToLoad: sum(solL), batToLoad: sum(batL),
             charged: charged, socOut: soc };
  }

  // ---------- jaarsimulatie ----------
  /*
   * opts:
   *   months      [{y,m}]            twaalf volledige maanden
   *   epexFor(y,m,d) -> [24]         kale marktprijs in EUR/MWh
   *   contract    'vast' | 'dyn'
   *   vastPrijs   EUR/kWh all-in bij een vast contract
   *   vastTerug   EUR/kWh vergoeding voor teruglevering bij een vast contract (na 2027)
   *   markup      opslag van de dynamische aanbieder (EUR/kWh)
   *   tlv         terugleverkosten per teruggeleverde kWh (EUR/kWh)
   *   verbruik    huishoudverbruik per jaar, zonder warmtepomp en auto
   *   hpKwh, evKwh
   *   evMode      'slim' | 'avond'
   *   profiel     'avond' | 'dag' | 'schuiven'
   *   panelen, batKwh
   *   saldering   true = salderen (t/m 2026), false = geen saldering (vanaf 2027)
   */
  function runYear(opts) {
    var months = opts.months;
    var bat = { cap: (opts.batKwh || 0) * 0.9, pwr: Math.max(0.01, (opts.batKwh || 0) * 0.5), ce: 0.95, de: 0.95 };
    var mode = (opts.batKwh > 0) ? "balanced" : "self";
    var tlv = opts.contract === "dyn" ? (opts.tlv || 0) : 0;
    var tot = { impKwh: 0, impCost: 0, expKwh: 0, expRev: 0, loadTot: 0, solarGen: 0, solToLoad: 0, batToLoad: 0 };
    var soc = 0;

    for (var i = 0; i < months.length; i++) {
      var y = months[i].y, m = months[i].m, dn = daysInMonth(y, m);
      for (var d = 1; d <= dn; d++) {
        var epex = opts.epexFor(y, m, d);
        var cp = new Array(24), tb = new Array(24);
        for (var h = 0; h < 24; h++) {
          if (opts.contract === "dyn") {
            cp[h] = (epex[h] / 1000 + opts.markup + EB) * BTW;
            tb[h] = epex[h] / 1000 - tlv;
          } else {
            cp[h] = opts.vastPrijs;
            tb[h] = opts.vastTerug;
          }
        }
        var solar = solarDay(y, m, d, opts.panelen || 0);
        var load = consDay(y, m, opts.verbruik || 0, opts.hpKwh || 0, opts.profiel === "dag" ? "dag" : "avond");
        if (opts.profiel === "schuiven") load = shiftLoad(load, cp, SHIFT_SHARE, SHIFT_HOURS);
        var ev = evDay((opts.evKwh || 0) / 365, cp, opts.evMode === "avond" ? "avond" : "slim");
        for (var k = 0; k < 24; k++) load[k] += ev[k];

        var f = simulateDay(cp, tb, solar, load, bat, mode, soc);
        soc = f.socOut;
        tot.impKwh += f.impKwh; tot.impCost += f.impCost;
        tot.expKwh += f.expKwh; tot.expRev += f.expRev;
        tot.loadTot += f.loadTot; tot.solarGen += f.solarGen;
        tot.solToLoad += f.solToLoad; tot.batToLoad += f.batToLoad;
      }
    }

    var avgImp = tot.impKwh > 0 ? tot.impCost / tot.impKwh : 0;
    var avgExp = tot.expKwh > 0 ? tot.expRev / tot.expKwh : 0;
    var jaarkosten;
    if (opts.saldering) {
      // Salderen = de teruggeleverde kWh gaan van je afname af op de jaarrekening.
      // We rekenen dat op jaarbasis: de gesaldeerde kWh krijg je terug tegen je eigen
      // gemiddelde inkoopprijs, wat je meer terugleverde krijgt de marktvergoeding.
      // Terugleverkosten betaal je ook over gesaldeerde kWh.
      var gesaldeerd = Math.min(tot.expKwh, tot.impKwh);
      var overschot = tot.expKwh - gesaldeerd;
      jaarkosten = tot.impCost - gesaldeerd * avgImp + gesaldeerd * tlv - overschot * avgExp;
      tot.gesaldeerd = gesaldeerd;
    } else {
      jaarkosten = tot.impCost - tot.expRev;
      tot.gesaldeerd = 0;
    }
    tot.jaarkosten = jaarkosten;
    tot.avgImp = avgImp;
    tot.avgExp = avgExp;
    return tot;
  }

  // ---------- de zes uitkomsten van de pagina ----------
  function scenarios(base) {
    function run(extra) {
      var o = {}; var k;
      for (k in base) o[k] = base[k];
      for (k in extra) o[k] = extra[k];
      return runYear(o);
    }
    return {
      nu_vast:   run({ contract: "vast", saldering: true,  batKwh: 0 }),
      nu_dyn:    run({ contract: "dyn",  saldering: true,  batKwh: 0 }),
      later_vast:run({ contract: "vast", saldering: false, batKwh: 0 }),
      later_dyn: run({ contract: "dyn",  saldering: false, batKwh: 0 }),
      bat_vast:  run({ contract: "vast", saldering: false, batKwh: base.batKwh || 10 }),
      bat_dyn:   run({ contract: "dyn",  saldering: false, batKwh: base.batKwh || 10 })
    };
  }

  // twaalf volledige maanden terug vanaf de maand voor nu
  function lastTwelveMonths(now) {
    var y = now.getFullYear(), m = now.getMonth() - 1, out = [];
    for (var i = 0; i < 12; i++) { if (m < 0) { m = 11; y--; } out.push({ y: y, m: m }); m--; }
    return out.reverse();
  }

  return {
    EB: EB, BTW: BTW, SOLAR_PER_PANEL: SOLAR_PER_PANEL,
    daysInMonth: daysInMonth, solarDay: solarDay, consDay: consDay,
    evDay: evDay, shiftLoad: shiftLoad, simulateDay: simulateDay,
    runYear: runYear, scenarios: scenarios, lastTwelveMonths: lastTwelveMonths
  };
});
