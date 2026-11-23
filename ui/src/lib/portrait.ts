// Where a page reads an agent's picture (2026-11-20). The room keeps each
// picture with a version (a short digest of its bytes) and a 160px copy; a
// URL that names the version is one a browser keeps for good, since a new
// picture has a new version, and `small` asks for the copy the cards and the
// page header show instead of the full 1024px PNG.

export type PortraitSize = "small" | "full";

/** The picture's URL: by version when the room has one for it, small unless
 * the full picture is wanted (a download). */
export function portraitUrl(
  name: string,
  version: string | null | undefined,
  size: PortraitSize = "small",
): string {
  const params = new URLSearchParams();
  if (version) params.set("v", version);
  if (size === "small") params.set("size", "small");
  const query = params.toString();
  return `/api/agents/${encodeURIComponent(name)}/avatar${query ? `?${query}` : ""}`;
}
