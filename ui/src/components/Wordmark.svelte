<script lang="ts">
  // The KOBO wordmark: a dot-matrix face, each letter a hollow 5x7 grid of
  // square pixels. Drawn in currentColor, so it takes whatever ink it sits
  // on (light on the dark rail). public/kobo-wordmark.svg is the same mark
  // as a standalone file. The grid: 4-unit cells, a 3-unit square dot in
  // each, two empty columns between letters. The viewBox is 103x27, so a
  // height of 27 (or 54) puts every dot on whole pixels.
  let { height = 27, title = "KOBO" }: { height?: number; title?: string } = $props();
</script>

<svg
  class="wordmark"
  viewBox="0 0 103 27"
  height={height}
  width={(height * 103) / 27}
  fill="currentColor"
  role="img"
  aria-label={title}
>
  <rect x="0" y="0" width="3" height="3"/>
  <rect x="16" y="0" width="3" height="3"/>
  <rect x="0" y="4" width="3" height="3"/>
  <rect x="12" y="4" width="3" height="3"/>
  <rect x="0" y="8" width="3" height="3"/>
  <rect x="8" y="8" width="3" height="3"/>
  <rect x="0" y="12" width="3" height="3"/>
  <rect x="4" y="12" width="3" height="3"/>
  <rect x="0" y="16" width="3" height="3"/>
  <rect x="8" y="16" width="3" height="3"/>
  <rect x="0" y="20" width="3" height="3"/>
  <rect x="12" y="20" width="3" height="3"/>
  <rect x="0" y="24" width="3" height="3"/>
  <rect x="16" y="24" width="3" height="3"/>
  <rect x="32" y="0" width="3" height="3"/>
  <rect x="36" y="0" width="3" height="3"/>
  <rect x="40" y="0" width="3" height="3"/>
  <rect x="28" y="4" width="3" height="3"/>
  <rect x="44" y="4" width="3" height="3"/>
  <rect x="28" y="8" width="3" height="3"/>
  <rect x="44" y="8" width="3" height="3"/>
  <rect x="28" y="12" width="3" height="3"/>
  <rect x="44" y="12" width="3" height="3"/>
  <rect x="28" y="16" width="3" height="3"/>
  <rect x="44" y="16" width="3" height="3"/>
  <rect x="28" y="20" width="3" height="3"/>
  <rect x="44" y="20" width="3" height="3"/>
  <rect x="32" y="24" width="3" height="3"/>
  <rect x="36" y="24" width="3" height="3"/>
  <rect x="40" y="24" width="3" height="3"/>
  <rect x="56" y="0" width="3" height="3"/>
  <rect x="60" y="0" width="3" height="3"/>
  <rect x="64" y="0" width="3" height="3"/>
  <rect x="68" y="0" width="3" height="3"/>
  <rect x="56" y="4" width="3" height="3"/>
  <rect x="72" y="4" width="3" height="3"/>
  <rect x="56" y="8" width="3" height="3"/>
  <rect x="72" y="8" width="3" height="3"/>
  <rect x="56" y="12" width="3" height="3"/>
  <rect x="60" y="12" width="3" height="3"/>
  <rect x="64" y="12" width="3" height="3"/>
  <rect x="68" y="12" width="3" height="3"/>
  <rect x="56" y="16" width="3" height="3"/>
  <rect x="72" y="16" width="3" height="3"/>
  <rect x="56" y="20" width="3" height="3"/>
  <rect x="72" y="20" width="3" height="3"/>
  <rect x="56" y="24" width="3" height="3"/>
  <rect x="60" y="24" width="3" height="3"/>
  <rect x="64" y="24" width="3" height="3"/>
  <rect x="68" y="24" width="3" height="3"/>
  <rect x="88" y="0" width="3" height="3"/>
  <rect x="92" y="0" width="3" height="3"/>
  <rect x="96" y="0" width="3" height="3"/>
  <rect x="84" y="4" width="3" height="3"/>
  <rect x="100" y="4" width="3" height="3"/>
  <rect x="84" y="8" width="3" height="3"/>
  <rect x="100" y="8" width="3" height="3"/>
  <rect x="84" y="12" width="3" height="3"/>
  <rect x="100" y="12" width="3" height="3"/>
  <rect x="84" y="16" width="3" height="3"/>
  <rect x="100" y="16" width="3" height="3"/>
  <rect x="84" y="20" width="3" height="3"/>
  <rect x="100" y="20" width="3" height="3"/>
  <rect x="88" y="24" width="3" height="3"/>
  <rect x="92" y="24" width="3" height="3"/>
  <rect x="96" y="24" width="3" height="3"/>
</svg>

<style>
  .wordmark {
    display: block;
    flex-shrink: 0;
    shape-rendering: crispEdges;
  }
</style>
