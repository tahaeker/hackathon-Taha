const { useState, useEffect, useMemo, useRef, Component } = React;

// ═══════════════════════════════════════════
//  MAP VIEW — LEAFLET SATELLITE + SPHERICAL HUD
// ═══════════════════════════════════════════

function MapView({ route, allRoutes, weatherVisible, weatherScenario, routeMode, onRouteMode,
                   mapStyle, onMapStyle, projectionMode, onProjectionMode }) {
  const mapElRef      = useRef(null);
  const leafletRef    = useRef(null);
  const tileLayerRef  = useRef(null);
  const labelLayerRef = useRef(null);
  const markersRef    = useRef([]);
  const polyRef       = useRef([]);
  const [mapReady, setMapReady]             = useState(false);
  const [settingsOpen, setSettingsOpen]     = useState(false);
  const [hoveredStop, setHoveredStop]       = useState(null);
  const [mousePos,    setMousePos]          = useState({ x: 0, y: 0 });
  const wrapRef = useRef(null);

  // ── 1. MAP INIT (tile-layer free) ────────────────────────────
  useEffect(() => {
    if (leafletRef.current || !mapElRef.current) return;
    if (typeof L === 'undefined') { console.error('[MapView] Leaflet not loaded'); return; }
    let cancelled = false;
    const tryInit = (attempt = 0) => {
      if (cancelled || leafletRef.current) return;
      const el = mapElRef.current;
      if (!el) return;
      const rect = el.getBoundingClientRect();
      if ((rect.width < 50 || rect.height < 50) && attempt < 20) {
        setTimeout(() => tryInit(attempt + 1), 120); return;
      }
      try {
        const map = L.map(el, {
          center: SIVAS_CENTER, zoom: 10,
          zoomControl: false, attributionControl: false,
          preferCanvas: false,
        });
        leafletRef.current = map;
        setMapReady(true);
        console.log('[MapView] map ready');
        [50, 200, 500, 1000].forEach(ms => setTimeout(() => {
          if (!cancelled && leafletRef.current) leafletRef.current.invalidateSize();
        }, ms));
      } catch (e) { console.error('[MapView] init failed:', e); }
    };
    tryInit(0);
    return () => { cancelled = true; };
  }, []);

  // ── 2. TILE LAYER — switches on mapStyle or first ready ──────
  useEffect(() => {
    const map = leafletRef.current;
    if (!mapReady || !map) return;
    // Tear down old layers
    if (tileLayerRef.current)  { try { map.removeLayer(tileLayerRef.current);  } catch(e){} tileLayerRef.current  = null; }
    if (labelLayerRef.current) { try { map.removeLayer(labelLayerRef.current); } catch(e){} labelLayerRef.current = null; }

    if (mapStyle === 'satellite') {
      const sat = L.tileLayer(
        'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
        { maxZoom: 19, tileSize: 256 }
      );
      sat.on('tileload',  () => console.log('[tiles] ✓ Esri sat'));
      sat.on('tileerror', e  => console.warn('[tiles] ✗ Esri sat', e?.tile?.src));
      sat.addTo(map);
      tileLayerRef.current = sat;
      const lbl = L.tileLayer(
        'https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}',
        { maxZoom: 19, opacity: 0.65 }
      ).addTo(map);
      labelLayerRef.current = lbl;
      // OSM fallback if Esri silent after 6s
      let ok = false;
      sat.once('tileload', () => { ok = true; });
      setTimeout(() => {
        if (!ok && leafletRef.current) {
          console.warn('[tiles] Esri 6s timeout → OSM fallback');
          try { map.removeLayer(sat); map.removeLayer(lbl); } catch(e){}
          tileLayerRef.current  = L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', { maxZoom: 19, subdomains: 'abc' }).addTo(map);
          labelLayerRef.current = null;
        }
      }, 6000);
    } else {
      // Flat / light style — CartoDB Light All
      tileLayerRef.current = L.tileLayer(
        'https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png',
        { maxZoom: 19, subdomains: 'abcd' }
      ).addTo(map);
      console.log('[tiles] CartoDB Light');
    }
  }, [mapStyle, mapReady]);

  // Force invalidateSize whenever route changes (layout may have shifted)
  useEffect(() => {
    const map = leafletRef.current;
    if (!map) return;
    setTimeout(() => map.invalidateSize(), 80);
  }, [route && route.route_id]);

  // Update stop markers + route polylines when route changes
  useEffect(() => {
    const map = leafletRef.current;
    if (!map) return;

    // Clear old
    markersRef.current.forEach(m => map.removeLayer(m));
    markersRef.current = [];
    polyRef.current.forEach(p => map.removeLayer(p));
    polyRef.current = [];

    if (!route || !Array.isArray(route.stops) || route.stops.length === 0) {
      console.log('[MapView] no stops to render for route', route && route.route_id);
      return;
    }

    // Filter stops with valid coordinates
    const validStops = route.stops.filter(s =>
      typeof s.latitude === 'number' && typeof s.longitude === 'number' &&
      !isNaN(s.latitude) && !isNaN(s.longitude) &&
      s.latitude !== 0 && s.longitude !== 0
    );
    console.log(`[MapView] ${route.route_id}: ${validStops.length}/${route.stops.length} stops with valid coords`);
    if (validStops.length === 0) return;

    const origLatLngs = validStops.map(s => [s.latitude, s.longitude]);
    const optSorted = [...validStops]
      .sort((a, b) => (a.optimized.predicted_arrival || '').localeCompare(b.optimized.predicted_arrival || ''))
      .map(s => [s.latitude, s.longitude]);

    if (routeMode === 'both' || routeMode === 'original') {
      // Neon kavisli arc — orijinal sıra (kırmızı kesikli)
      const arcOrig = makeArcPath(origLatLngs);
      const origPoly = L.polyline(arcOrig, {
        color: '#ef4444', weight: 2.5, opacity: 0.82,
        dashArray: '7 9', lineCap: 'round', lineJoin: 'round',
      }).addTo(map);
      polyRef.current.push(origPoly);
    }

    if (routeMode === 'both' || routeMode === 'optimized') {
      // Neon kavisli arc — optimize sıra (cyan parlak)
      const arcOpt = makeArcPath(optSorted);
      const optPoly = L.polyline(arcOpt, {
        color: '#0891b2', weight: 3.5, opacity: 0.92,
        lineCap: 'round', lineJoin: 'round',
      }).addTo(map);
      // İkinci katman: parlama efekti
      const optGlow = L.polyline(arcOpt, {
        color: '#38bdf8', weight: 6, opacity: 0.22,
        lineCap: 'round', lineJoin: 'round',
      }).addTo(map);
      polyRef.current.push(optPoly, optGlow);
    }

    // Depot marker
    const depotIcon = L.divIcon({
      html: '<div class="lf-depot-marker"></div>',
      className: '', iconSize: [22, 22], iconAnchor: [11, 11],
    });
    const depot = L.marker(SIVAS_CENTER, { icon: depotIcon }).addTo(map);
    markersRef.current.push(depot);

    // Çakışan noktaları offset ile ayır, sonra marker ekle
    const displayStops = offsetOverlaps(validStops);
    displayStops.forEach((s, i) => {
      const ok  = s.original.within_time_window;
      const d   = s.dynamic_conditions || {};
      const risk = d.delay_risk_score || 0;
      const riskClass = risk > 0.6 ? 'risk-high' : risk > 0.3 ? 'risk-med' : 'risk-low';
      const cong = ((d.congestion_ratio || 0) * 100).toFixed(0);
      const weather = d.weather || 'clear';
      const surface = d.road_surface || 'dry';

      // Build badge — if stop MISSES time window, show "!" alert
      const missAlert = !ok ? '<span class="lf-miss-alert" title="Zaman penceresi kaçırıldı">!</span>' : '';
      const badgeText = !ok
        ? `⚠ ${weather} · ${cong}%`
        : `${weather} · ${cong}%`;

      const icon = L.divIcon({
        html: `
          <div class="lf-stop-marker">
            <div class="lf-stop-pin ${ok ? 'ok' : 'miss'}" style="width:30px;height:30px;font-size:10px">${i + 1}${missAlert}</div>
            <div class="lf-stop-badge ${ok ? riskClass : 'risk-high'}">${badgeText}</div>
          </div>`,
        className: '', iconSize: [92, 58], iconAnchor: [15, 30],
      });

      const tooltip = `
        <b>${s.stop_id}</b><br/>
        Varış (orj.): <b style="color:${ok?'#16a34a':'#dc2626'}">${s.original.predicted_arrival}</b><br/>
        Pencere: ${s.time_window_open}–${s.time_window_close}<br/>
        Hava: ${weather} · Yol: ${surface}<br/>
        Tıkanıklık: ${cong}% · Risk: ${risk.toFixed(2)}
        ${!ok ? '<br/><b style="color:#dc2626">⚠ PENCERE KAÇIRILDI</b>' : ''}`;

      const marker = L.marker([s._lat ?? s.latitude, s._lon ?? s.longitude], { icon })
        .bindTooltip(tooltip, { direction: 'top', offset: [0, -28], className: 'lf-tip' })
        .addTo(map);
      markersRef.current.push(marker);
    });

    // Fit bounds with padding
    if (origLatLngs.length > 0) {
      try {
        map.fitBounds(origLatLngs, { padding: [60, 60], maxZoom: 12 });
        setTimeout(() => map.invalidateSize(), 120);
      } catch(e) { console.warn('[MapView] fitBounds failed:', e); }
    }
  }, [route, routeMode]);

  // Resize observer
  useEffect(() => {
    if (!wrapRef.current) return;
    const ro = new ResizeObserver(() => {
      if (leafletRef.current) leafletRef.current.invalidateSize();
    });
    ro.observe(wrapRef.current);
    return () => ro.disconnect();
  }, []);

  const hoveredStopData = route && route.stops && route.stops.find(s => s.stop_id === hoveredStop);

  return (
    <div className="map-wrap" ref={wrapRef}
         onMouseMove={e => {
           if (!wrapRef.current) return;
           const r = wrapRef.current.getBoundingClientRect();
           setMousePos({ x: e.clientX - r.left, y: e.clientY - r.top });
         }}>

      {/* Command Sphere — projection class toggled via prop */}
      <div className={`map-sphere-stage ${projectionMode === 'flat' ? 'proj-flat' : ''}`}>
        <div className="map-sphere-container">
          <div className="map-hud-ring r4"></div>
          <div className="map-hud-ring r3"></div>
          <div className="map-hud-ring r2"></div>
          <div className="map-hud-ring r1"></div>
          <span className="map-cardinal n">N</span>
          <span className="map-cardinal e">E</span>
          <span className="map-cardinal s">S</span>
          <span className="map-cardinal w">W</span>
          <div className="map-sphere">
            <div ref={mapElRef} className="map-el" style={{ width: '100%', height: '100%' }} />
            <div className="map-fisheye"></div>
            <div className="map-sheen"></div>
          </div>
        </div>
      </div>

      {/* Overlays — outside the tilt so they stay flat */}
      <div className="map-overlay-tl">
        <button className="map-btn" title="Yakınlaştır"  onClick={() => leafletRef.current && leafletRef.current.zoomIn()}>+</button>
        <button className="map-btn" title="Uzaklaştır"   onClick={() => leafletRef.current && leafletRef.current.zoomOut()}>−</button>
        <button className="map-btn" title="Merkeze git"  onClick={() => leafletRef.current && leafletRef.current.setView(SIVAS_CENTER, 10)}>◎</button>
        <button className={`map-btn ${settingsOpen ? 'active' : ''}`} title="Harita Ayarları"
          onClick={() => setSettingsOpen(v => !v)}>⚙</button>
      </div>

      {/* Map settings popup */}
      {settingsOpen && (
        <div className="map-settings-popup">
          <div className="sp-head">Harita Ayarları</div>
          <div className="sp-row">
            <span>Stil</span>
            <div className="sp-pills">
              {[['satellite','SAT'],['flat','FLAT']].map(([k,l]) => (
                <button key={k} className={mapStyle === k ? 'active' : ''} onClick={() => onMapStyle(k)}>{l}</button>
              ))}
            </div>
          </div>
          <div className="sp-row" style={{ marginTop: 4 }}>
            <span>Projeksiyon</span>
            <div className="sp-pills">
              {[['sphere','SPHERE'],['flat','FLAT']].map(([k,l]) => (
                <button key={k} className={projectionMode === k ? 'active' : ''} onClick={() => onProjectionMode(k)}>{l}</button>
              ))}
            </div>
          </div>
        </div>
      )}

      <div className="map-overlay-tr">
        <div className="map-hud">
          <span className="k">{window.t('map_route')}</span><span className="v">{route ? route.route_id : '—'}</span>
        </div>
        <div className="map-hud">
          <span className="k">{window.t('map_distance')}</span><span className="v">{route ? Number(route.total_distance_km).toFixed(1) : '—'} km</span>
        </div>
      </div>

      <div className="map-overlay-bl">
        <div className="map-legend">
          <div className="title">Görünüm</div>
          <div style={{ display: 'flex', gap: 2, marginBottom: 8 }}>
            {['both', 'original', 'optimized'].map(m => (
              <button key={m} onClick={() => onRouteMode(m)} style={{
                padding: '3px 8px', fontSize: 11, borderRadius: 3, fontFamily: 'var(--font-mono)',
                background: routeMode === m ? 'var(--bg-4)' : 'var(--bg-2)',
                color: routeMode === m ? 'var(--text-0)' : 'var(--text-2)',
                border: '1px solid var(--line)'
              }}>{m === 'both' ? 'İKİSİ' : m === 'original' ? 'ORJ' : 'OPT'}</button>
            ))}
          </div>
          <div className="li"><span className="swatch" style={{ background: '#ff6b6b' }}></span><span>Orijinal sıra</span></div>
          <div className="li"><span className="swatch" style={{ background: '#5dd6e6' }}></span><span>Optimize edilmiş</span></div>
        </div>
      </div>

      <div className="map-overlay-br">
        <div className="map-hud" style={{ display: 'flex', flexDirection: 'column', gap: 2, padding: '8px 12px' }}>
          <div style={{ fontSize: 9, color: 'var(--text-3)', textTransform: 'uppercase', letterSpacing: '0.08em', fontFamily: 'var(--font-ui)', marginBottom: 2 }}>Circuity Faktörü</div>
          {[['mountain','dağ',1.65],['rural','kırsal',1.45],['highway','kara yolu',1.10],['urban','kent',1.35]].map(([key,label,def]) => {
            const raw = window.LOGISTICS_DATA.CIRCUITY && window.LOGISTICS_DATA.CIRCUITY[key];
            const cf  = raw ? (typeof raw === 'object' ? raw.cf  : raw) : def;
            const n   = raw ? (typeof raw === 'object' ? raw.n   : 0)  : 0;
            return (
              <div key={key} style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
                <span className="k" style={{ minWidth: 52 }}>{label}</span>
                <span className="v">{Number(cf).toFixed(2)}×</span>
                {n > 0 && <span style={{ fontSize: 9, color: 'var(--text-3)', fontFamily: 'var(--font-mono)' }}>n={n}</span>}
              </div>
            );
          })}
        </div>
      </div>

      <div className="map-coords">39.75° N · 37.02° E — SİVAS BÖLGESİ · SAT</div>
    </div>
  );
}
