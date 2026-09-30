/**
 * Runs before first paint so a saved display preference never flashes the
 * default. Storage can be unavailable (a private window), in which case the
 * defaults stand and the page still works.
 *
 * Kept out of any "use client" module so the server layout can inline it.
 */
export const DISPLAY_BOOTSTRAP = `try{var d=document.documentElement;["theme","contrast","text"].forEach(function(k){var v=localStorage.getItem("qip-display-"+k);if(v)d.setAttribute("data-"+k,v)})}catch(e){}`;

export function displayKey(setting: string): string {
  return `qip-display-${setting}`;
}
