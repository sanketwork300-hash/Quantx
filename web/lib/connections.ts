/**
 * The provider redirect comes back with only a code and a state — not which
 * provider sent them — so the one being connected is remembered across the
 * round trip. Session storage rather than local storage: an abandoned handoff
 * should not outlive the tab.
 */
export const PENDING_PROVIDER_KEY = "qip.pending_provider";
