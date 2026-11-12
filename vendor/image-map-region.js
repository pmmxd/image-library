(() => {
  const timer = setInterval(() => {
    if (typeof showMapGallery !== "function") return;
    const original = showMapGallery;
    showMapGallery = (rows) => {
      const base = rows.find((row) => row.latitude != null);
      let region = rows;
      if (base && map) {
        const zoom = map.getZoom();
        const span = zoom >= 14 ? 0.001 : zoom >= 11 ? 0.006 : 0.03;
        const nearby = filtered().filter((row) =>
          row.latitude != null &&
          Math.abs(Number(row.latitude) - Number(base.latitude)) <= span &&
          Math.abs(Number(row.longitude) - Number(base.longitude)) <= span
        );
        if (nearby.length > region.length) region = nearby;
      }
      const title = document.querySelector(".mapside h2");
      if (title) title.textContent = `位置画廊 · ${region.length} 张`;
      const host = document.getElementById("mapGallery");
      host.innerHTML = "";
      region.forEach((row) => {
        const button = document.createElement("button");
        button.className = "mapitem";
        button.innerHTML = `<img data-media="${media(row)}" src="${thumb(row)}"><span>${row.name}</span>`;
        button.onclick = () => {
          if (suppressQuickClick) {
            suppressQuickClick = false;
            return;
          }
          detail(row);
        };
        host.append(button);
      });
      return region.length;
    };
    showMapGallery._regionAware = true;
    clearInterval(timer);
    void original;
  }, 80);

  document.addEventListener("wheel", (event) => {
    const preview = event.target.closest?.(".preview");
    if (!preview || !preview.classList.contains("zoom-mode")) return;
    const zoom = Number.parseFloat(preview.style.getPropertyValue("--zoom")) || 1;
    preview.classList.toggle("is-zoomed", zoom > 1);
  }, { passive: true });

  document.addEventListener("wheel", (event) => {
    const preview = event.target.closest?.(".preview");
    if (!preview || !preview.classList.contains("zoom-mode")) return;
    if (!event.ctrlKey) event.stopImmediatePropagation();
  }, true);

  document.addEventListener("keydown", (event) => {
    if (event.code !== "Space" && event.code !== "Escape") return;
    setTimeout(() => {
      const preview = document.querySelector(".preview.zoom-mode");
      const zoom = preview
        ? Number.parseFloat(preview.style.getPropertyValue("--zoom")) || 1
        : 1;
      if (zoom <= 1) preview?.classList.remove("is-zoomed");
    }, 0);
  }, true);

  setInterval(() => {
    const activeView = document.querySelector(".tab.active")?.dataset.view;
    document.body.classList.toggle("mapview", activeView === "map");
  }, 120);

  let metadataSynced = false;
  const syncTimer = setInterval(async () => {
    if (metadataSynced || typeof ROWS === "undefined" || typeof render !== "function") return;
    metadataSynced = true;
    clearInterval(syncTimer);
    try {
      const response = await fetch(`${API}/api/metadata`, { cache: "no-store" });
      const metadata = await response.json();
      ROWS.forEach((row) => Object.assign(row, metadata[row.relative_path] || {}));
      stats();
      if (typeof updateCounts === "function") updateCounts();
    } catch {
      metadataSynced = false;
    }
  }, 180);

  let lastFeaturedCount = -1;
  setInterval(() => {
    if (typeof ROWS === "undefined") return;
    const count = ROWS.filter((row) => row.featured).length;
    if (count === lastFeaturedCount) return;
    lastFeaturedCount = count;
    const label = document.getElementById("countFeatured");
    if (label) label.textContent = count;
    if (typeof stats === "function") stats();
  }, 250);

  let lastClickableMap = null;
  let pickedPoint = null;
  setInterval(() => {
    if (typeof map === "undefined" || !map || map === lastClickableMap) return;
    lastClickableMap = map;
    map.on("click", (event) => {
      if (pickedPoint) pickedPoint.remove();
      pickedPoint = L.circleMarker(event.latlng, {
        radius: 8,
        color: "#e1b866",
        weight: 3,
        fillColor: "#111417",
        fillOpacity: 0.9,
      }).addTo(map);
      document.getElementById("batchLat").value = event.latlng.lat.toFixed(7);
      document.getElementById("batchLon").value = event.latlng.lng.toFixed(7);
      if (typeof batch === "function") batch();
    });
  }, 180);

  const bindBatchActions = setInterval(() => {
    const cityButton = document.getElementById("findCity");
    const featuredButton = document.getElementById("batchFeatured");
    const gpsButton = document.getElementById("applyGps");
    if (!cityButton || !featuredButton || !gpsButton) return;
    if (typeof findCity !== "function" || typeof applyFeatured !== "function") return;
    cityButton.onclick = findCity;
    featuredButton.onclick = applyFeatured;
    gpsButton.onclick = async () => {
      if (map) {
        map.remove();
        map = null;
      }
      await applyGps();
    };
    if (typeof updateCounts === "function") updateCounts();
    clearInterval(bindBatchActions);
  }, 180);
})();
