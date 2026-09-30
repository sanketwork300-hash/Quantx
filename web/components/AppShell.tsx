"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useId, useMemo, useRef, useState } from "react";
import { displayKey } from "@/lib/display";

interface NavItem {
  href: string;
  label: string;
}

const NAV: { group: string | null; items: NavItem[] }[] = [
  { group: null, items: [{ href: "/", label: "Dashboard" }] },
  {
    group: "Markets",
    items: [
      { href: "/live", label: "Live market" },
      { href: "/live/options", label: "Live options" },
      { href: "/markets/chains", label: "Option chains" },
      { href: "/markets/analyses", label: "Volatility analyses" },
      { href: "/markets/surfaces", label: "Surfaces" },
      { href: "/markets/global-surfaces", label: "Global surfaces" },
      { href: "/markets/consensus", label: "Model consensus" },
    ],
  },
  {
    group: "Portfolio",
    items: [
      { href: "/portfolios", label: "Portfolios" },
      { href: "/portfolios/construct", label: "Construction" },
      { href: "/scenarios", label: "Scenarios" },
      { href: "/order-analysis", label: "Order analysis" },
    ],
  },
  {
    group: "Execution",
    items: [
      { href: "/execution", label: "Trade analysis" },
      { href: "/execution/simulate", label: "Simulation" },
      { href: "/microstructure", label: "Order book" },
      { href: "/trading", label: "Paper trading" },
    ],
  },
  {
    group: "Data",
    items: [
      { href: "/data", label: "Imports" },
      { href: "/warehouse", label: "Warehouse" },
    ],
  },
  { group: "Research", items: [{ href: "/research", label: "Backtests" }] },
  {
    group: "Account",
    items: [
      { href: "/login", label: "Sign in" },
      { href: "/connections", label: "Broker connections" },
    ],
  },
];

const PAGES = NAV.flatMap((section) =>
  section.items.map((item) => ({ ...item, group: section.group ?? "Home" })),
);

/** The nav entry a path belongs to: the longest href that contains it. */
function currentHref(pathname: string): string | null {
  let best: string | null = null;
  for (const { href } of PAGES) {
    const inside = href === "/" ? pathname === "/" : pathname === href || pathname.startsWith(`${href}/`);
    if (inside && (best === null || href.length > best.length)) best = href;
  }
  return best;
}

type Setting = "theme" | "contrast" | "text";

const SETTINGS: { key: Setting; legend: string; fallback: string; options: [string, string][] }[] = [
  { key: "text", legend: "Text size", fallback: "m", options: [["s", "Small"], ["m", "Medium"], ["l", "Large"], ["xl", "Largest"]] },
  { key: "contrast", legend: "Contrast", fallback: "standard", options: [["standard", "Standard"], ["high", "High"]] },
  { key: "theme", legend: "Background", fallback: "light", options: [["light", "White"], ["dark", "Dark grey"]] },
];

function DisplaySettings() {
  const [open, setOpen] = useState(false);
  const [values, setValues] = useState<Record<Setting, string>>({
    theme: "light",
    contrast: "standard",
    text: "m",
  });
  const panelId = useId();

  useEffect(() => {
    const root = document.documentElement;
    setValues({
      theme: root.getAttribute("data-theme") ?? "light",
      contrast: root.getAttribute("data-contrast") ?? "standard",
      text: root.getAttribute("data-text") ?? "m",
    });
  }, []);

  function choose(key: Setting, value: string) {
    setValues((previous) => ({ ...previous, [key]: value }));
    document.documentElement.setAttribute(`data-${key}`, value);
    try {
      localStorage.setItem(displayKey(key), value);
    } catch {
      // The choice still applies for this visit.
    }
  }

  return (
    <div>
      <button
        type="button"
        className="secondary display-toggle"
        aria-expanded={open}
        aria-controls={panelId}
        onClick={() => setOpen(!open)}
      >
        Display settings
        <span aria-hidden="true">{open ? "−" : "+"}</span>
      </button>
      <div id={panelId} className="display-panel" hidden={!open}>
        {SETTINGS.map((setting) => (
          <fieldset key={setting.key}>
            <legend>{setting.legend}</legend>
            <div className="segmented">
              {setting.options.map(([value, label]) => (
                <label key={value}>
                  <input
                    type="radio"
                    name={`display-${setting.key}`}
                    value={value}
                    checked={values[setting.key] === value}
                    onChange={() => choose(setting.key, value)}
                  />
                  {label}
                </label>
              ))}
            </div>
          </fieldset>
        ))}
      </div>
    </div>
  );
}

/** Jump to any page by typing part of its name. Opens with Ctrl+K or ⌘K. */
function JumpDialog({ dialogRef }: { dialogRef: React.RefObject<HTMLDialogElement> }) {
  const router = useRouter();
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const listId = useId();

  const matches = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return PAGES;
    return PAGES.filter((page) => `${page.label} ${page.group}`.toLowerCase().includes(needle));
  }, [query]);

  useEffect(() => setActive(0), [query]);

  function go(href: string) {
    dialogRef.current?.close();
    router.push(href);
  }

  function onKeyDown(event: React.KeyboardEvent<HTMLInputElement>) {
    if (event.key === "ArrowDown") {
      event.preventDefault();
      setActive((index) => Math.min(index + 1, matches.length - 1));
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      setActive((index) => Math.max(index - 1, 0));
    } else if (event.key === "Enter" && matches[active]) {
      event.preventDefault();
      go(matches[active].href);
    }
  }

  return (
    <dialog
      ref={dialogRef}
      className="palette"
      aria-label="Jump to a page"
      onClose={() => setQuery("")}
      onClick={(event) => {
        // A click on the backdrop lands on the dialog element itself.
        if (event.target === dialogRef.current) dialogRef.current?.close();
      }}
    >
      <label className="visually-hidden" htmlFor={`${listId}-input`}>
        Page name
      </label>
      <input
        id={`${listId}-input`}
        type="text"
        role="combobox"
        aria-expanded="true"
        aria-controls={listId}
        aria-autocomplete="list"
        aria-activedescendant={matches[active] ? `${listId}-${active}` : undefined}
        placeholder="Type a page name"
        autoComplete="off"
        value={query}
        onChange={(event) => setQuery(event.target.value)}
        onKeyDown={onKeyDown}
      />
      <ul id={listId} role="listbox" aria-label="Pages">
        {matches.map((page, index) => (
          <li
            key={page.href}
            id={`${listId}-${index}`}
            role="option"
            aria-selected={index === active}
            onMouseEnter={() => setActive(index)}
            onClick={() => go(page.href)}
          >
            <span>{page.label}</span>
            <span className="where">{page.group}</span>
          </li>
        ))}
        {matches.length === 0 && (
          <li role="option" aria-selected="false" aria-disabled="true">
            No page matches “{query}”.
          </li>
        )}
      </ul>
      <p className="hint" aria-live="polite">
        {matches.length} page{matches.length === 1 ? "" : "s"}. Arrow keys to choose, Enter to
        open, Esc to close.
      </p>
    </dialog>
  );
}

/**
 * A scrolling table has to be reachable from the keyboard, or the columns off
 * to the right exist only for mouse users. Done here once rather than on each
 * of the pages that render one.
 */
function useKeyboardScrollableTables() {
  useEffect(() => {
    const mark = () => {
      document.querySelectorAll<HTMLElement>(".table-wrap:not([tabindex])").forEach((node) => {
        node.tabIndex = 0;
        node.setAttribute("role", "region");
        if (!node.hasAttribute("aria-label")) {
          node.setAttribute("aria-label", "Table, scrollable with the arrow keys");
        }
      });
    };
    mark();
    const observer = new MutationObserver(mark);
    observer.observe(document.body, { childList: true, subtree: true });
    return () => observer.disconnect();
  }, []);
}

export function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname() ?? "/";
  const current = currentHref(pathname);
  const [menuOpen, setMenuOpen] = useState(false);
  const dialogRef = useRef<HTMLDialogElement>(null);
  const navId = useId();

  useKeyboardScrollableTables();

  // A page was chosen: the small-screen menu has done its job.
  useEffect(() => setMenuOpen(false), [pathname]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        if (!dialogRef.current?.open) dialogRef.current?.showModal();
      } else if (event.key === "Escape") {
        setMenuOpen(false);
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  return (
    <>
      <a className="skip-link" href="#main">
        Skip to content
      </a>
      <div className="shell">
        <div className="topbar">
          <span className="name">Quant Intelligence</span>
          <button
            type="button"
            className="secondary"
            aria-expanded={menuOpen}
            aria-controls={navId}
            onClick={() => setMenuOpen(!menuOpen)}
          >
            {menuOpen ? "Close menu" : "Menu"}
          </button>
        </div>

        <aside id={navId} className="sidebar" data-open={menuOpen}>
          <div className="brand">
            <Link href="/">
              <span className="name">Quant Intelligence</span>
            </Link>
            <div className="tagline">Analytics, not advice</div>
          </div>

          <button type="button" className="jump" onClick={() => dialogRef.current?.showModal()}>
            Jump to a page
            <kbd>Ctrl K</kbd>
          </button>

          <nav aria-label="Main">
            {NAV.map((section) => (
              <div key={section.group ?? "home"}>
                {section.group && <h2 className="group">{section.group}</h2>}
                <ul>
                  {section.items.map((item) => (
                    <li key={item.href}>
                      <Link href={item.href} aria-current={item.href === current ? "page" : undefined}>
                        {item.label}
                      </Link>
                    </li>
                  ))}
                </ul>
              </div>
            ))}
          </nav>

          <DisplaySettings />
        </aside>

        <main id="main" className="main" tabIndex={-1}>
          {children}
        </main>
      </div>
      <JumpDialog dialogRef={dialogRef} />
    </>
  );
}
