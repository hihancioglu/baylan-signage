# Baylan Hub `production-widget` Auto Fit integration spec

> The Baylan Hub source is not part of this repository. Apply this change in the
> Hub repository that owns `/automation/production-widget`.

## Query contract

* Keep every finite positive numeric `scale` value on the existing/manual code path.
* Treat the exact query value `scale=auto` as Auto Fit mode.
* Do not reinterpret missing or invalid values; retain the current legacy fallback.

## Required DOM integration

Use the existing element that contains the **entire** production card (header
through shift-flow footer); determine its actual selector from Hub source rather
than assuming a class name. Place it in a viewport/stage pair if the existing
numeric-scale implementation has no equivalent wrappers:

```html
<div id="production-fit-viewport">
  <div id="production-fit-stage"><!-- existing complete card root --></div>
</div>
```

The viewport fills the page, hides overflow, and centers the stage's **scaled
bounding box**. The stage retains its natural layout size and uses
`transform-origin: top left`. In Auto mode hide the stage until the first
successful measurement, then reveal it. Ensure `html`, `body`, and the viewport
have 100% width/height, zero margin, and `overflow: hidden`.

## Reference algorithm

Feed the computed number into the same mechanism already used for numeric
scales. Measure the stage/card with its transform temporarily set to `none` so
its natural width and height are CSS-pixel measurements.

```js
const SAFE_FIT_FACTOR = 0.97;
const RESIZE_DEBOUNCE_MS = 150;

function fitProductionCard() {
  stage.style.transform = 'none';
  const viewportRect = viewport.getBoundingClientRect();
  const naturalRect = stage.getBoundingClientRect();
  if (!viewportRect.width || !viewportRect.height ||
      !naturalRect.width || !naturalRect.height) return;

  const availableWidth = viewportRect.width - horizontalSafeSpace;
  const availableHeight = viewportRect.height - verticalSafeSpace;
  const effectiveScale = Math.min(
    availableWidth / naturalRect.width,
    availableHeight / naturalRect.height,
  ) * SAFE_FIT_FACTOR;

  applyExistingNumericScale(effectiveScale);
  stageWrapper.style.width = `${naturalRect.width * effectiveScale}px`;
  stageWrapper.style.height = `${naturalRect.height * effectiveScale}px`;
  stage.style.visibility = 'visible';
}
```

Center `stageWrapper` with flex/grid on both axes. Explicit scaled wrapper
width/height prevents transformed (visual) bounds from being confused with
untransformed layout bounds.

Observe both the viewport and natural card/stage with `ResizeObserver`. Also
listen for `window.resize` and `fullscreenchange`, funnelling all callbacks
through a 100–250 ms debounce into `fitProductionCard`. Recalculate styles only:
do not navigate, change the URL, replace the iframe, or call `location.reload()`.
Disconnect observers/listeners during the page/component cleanup lifecycle.

`window.innerWidth`, `clientWidth`, and card DOM measurements are already CSS
pixels. **Do not multiply or divide by `window.devicePixelRatio`**; DPR may be
logged for diagnostics only. The `0.97` safety factor covers subpixel, browser,
and WebView rounding.

## Acceptance matrix

Exercise 1, 2, 4, 6, 7, 9, and 12 cards at these physical-resolution/display
scale pairs: 1920x1080 @ 100/125/150%, 1366x768 @ 100%, 2560x1440 @ 100%, and
3840x2160 @ 150%. For every cell verify no horizontal/vertical clipping, no
scrollbar, all sections visible, and two-axis centering. Seven inventories must
produce a 3x3 Signage grid whose seven URLs have `scale=auto`.

During a live 1920x1080-equivalent to 1536x864-equivalent to smaller-cell resize,
assert scale recalculation occurs and iframe navigation count remains unchanged.
Also assert `scale=1.25` still invokes only the existing numeric/manual path.
