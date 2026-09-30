import type { Metadata, Viewport } from "next";
import "./globals.css";
import { AppShell } from "@/components/AppShell";
import { DISPLAY_BOOTSTRAP } from "@/lib/display";
import { Providers } from "./providers";

export const metadata: Metadata = {
  title: "Quant Intelligence Platform",
  description:
    "Derivatives valuation, portfolio risk and execution intelligence. Analytics, not advice.",
};

// Zoom is left to the reader: no maximum scale, no user-scalable=no.
export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    // The display bootstrap sets data-* attributes on <html> before hydration.
    <html lang="en" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: DISPLAY_BOOTSTRAP }} />
        <link rel="preconnect" href="https://fonts.googleapis.com" />
        <link rel="preconnect" href="https://fonts.gstatic.com" crossOrigin="anonymous" />
        {/* Atkinson Hyperlegible is drawn for low-vision legibility. If the
            fonts cannot be fetched the system faces in globals.css stand in. */}
        <link
          rel="stylesheet"
          href="https://fonts.googleapis.com/css2?family=Atkinson+Hyperlegible+Mono:wght@400;600&family=Atkinson+Hyperlegible+Next:wght@400;600;700&family=Michroma&display=swap"
        />
      </head>
      <body>
        <Providers>
          <AppShell>{children}</AppShell>
        </Providers>
      </body>
    </html>
  );
}
