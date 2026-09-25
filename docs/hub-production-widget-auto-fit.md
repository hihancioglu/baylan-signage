# Signage-side `production-widget` Auto Fit

No Baylan Hub change is required. Auto mode keeps the Hub at its natural scale
`1`. The Signage widget engine is the only scale authority and fits the entire
cross-origin iframe to each real grid cell.

## Payload contract

* Auto: `scale=1`, `reload_policy=stable`, `fit_mode=production_auto`, virtual
  dimensions 620×500, and safety factor 0.995.
* Manual: the configured numeric `scale` remains in the URL and no fit metadata
  is added.

## Runtime fit

The engine places the iframe in an absolutely positioned 620×500 virtual stage,
centered in its grid-cell wrapper. It evaluates every possible column count from
the real container size, selects the candidate with the largest card area, and
uses a centered wrapping flex stage with a 4px gap. For every wrapper measurement
it computes:

```js
const rect = wrapper.getBoundingClientRect();
const scaleX = rect.width / fitWidth;
const scaleY = rect.height / fitHeight;
const scale = Math.min(scaleX, scaleY) * fitSafety;
stage.style.transform = `translate(-50%, -50%) scale(${scale})`;
```

Scale is not capped at 1, so a single production card can grow. Each wrapper has
its own `ResizeObserver`; resizing changes only the stage transform and never the
iframe URL or DOM node. The observer is disconnected during widget cleanup.
`devicePixelRatio` is diagnostic telemetry only and is not part of the formula.

## Acceptance

Exercise 1, 2, 4, 6, 7, 8, 9, and 12 cards at 1920×1080 (100% and 125% display
scaling), 1366×768, and 2560×1440. In every cell verify two-axis centering,
unchanged aspect ratio, no clipping or scrollbar, and visibility of the complete
card. Seven inventories should select 4×2 at this aspect ratio instead of the old
3×3 layout. A live resize must optimize the grid and update the CSS transform
without causing iframe navigation.
