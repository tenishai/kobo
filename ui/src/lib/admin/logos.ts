// Vendor marks for the integration catalog. Simple Icons (CC0) supplies the
// brand paths it carries; the brands that asked to be removed from it (AWS,
// Azure, Wiz) get a tile in their brand colour with their initials instead
// of a hand-drawn copy of their logo.
import {
  siAtlassian,
  siDatadog,
  siFastly,
  siGithub,
  siGooglecloud,
  siOkta,
} from "simple-icons";

export type VendorMark =
  | { kind: "path"; path: string; hex: string; title: string }
  | { kind: "initials"; text: string; hex: string };

const PATHS: Record<string, { path: string; hex: string; title: string }> = {
  atlassian: siAtlassian,
  datadog: siDatadog,
  fastly: siFastly,
  // GitHub's brand colour is near-black; it is drawn in the ink colour so it
  // stays visible in dark mode.
  github: { ...siGithub, hex: "" },
  gcp: siGooglecloud,
  okta: siOkta,
};

const INITIALS: Record<string, { text: string; hex: string }> = {
  aws: { text: "aws", hex: "#FF9900" },
  azure: { text: "Az", hex: "#0078D4" },
  wiz: { text: "W", hex: "#0254EC" },
};

export function vendorMark(id: string, name: string): VendorMark {
  const p = PATHS[id];
  if (p) return { kind: "path", path: p.path, hex: p.hex, title: p.title };
  const i = INITIALS[id];
  if (i) return { kind: "initials", ...i };
  return { kind: "initials", text: name.slice(0, 1), hex: "" };
}
