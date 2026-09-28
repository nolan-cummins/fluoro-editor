(function(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.ViewportGeometry = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function() {
  function clampPan(panX, panY, zoom, viewportW, viewportH, imageW, imageH) {
    // The image is rendered at the viewport size before zoom.  With CSS
    // transform origin at (0,0), pan is measured in untransformed image px.
    const minX = viewportW / zoom - imageW;
    const minY = viewportH / zoom - imageH;
    return { x: Math.max(minX, Math.min(0, panX)), y: Math.max(minY, Math.min(0, panY)) };
  }
  function viewportToImage(x, y, zoom, panX, panY) {
    return { x: x / zoom - panX, y: y / zoom - panY };
  }
  function imageToViewport(x, y, zoom, panX, panY) {
    return { x: (x + panX) * zoom, y: (y + panY) * zoom };
  }
  function zoomAtPoint(zoom, nextZoom, panX, panY, viewportX, viewportY) {
    const image = viewportToImage(viewportX, viewportY, zoom, panX, panY);
    return { zoom: nextZoom, panX: viewportX / nextZoom - image.x, panY: viewportY / nextZoom - image.y };
  }
  return { clampPan, viewportToImage, imageToViewport, zoomAtPoint };
});
