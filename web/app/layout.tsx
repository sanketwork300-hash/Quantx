import type { Metadata } from "next";
import Link from "next/link";
import "./globals.css";
import { Providers } from "./providers";

export const metadata: Metadata = {
  title: "Quant Intelligence Platform",
  description:
    "Derivatives valuation, portfolio risk and execution intelligence. Analytics, not advice.",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body>
        <Providers>
          <div className="shell">
            <aside className="sidebar">
              <h1>Quant Intelligence</h1>
              <div className="tagline">Analytics, not advice</div>
              <nav>
                <Link href="/">Dashboard</Link>
                <div className="group">Markets</div>
                <Link href="/markets/chains">Option chains</Link>
                <Link href="/markets/analyses">Volatility analyses</Link>
                <Link href="/markets/surfaces">Surfaces</Link>
                <Link href="/markets/global-surfaces">Global surfaces</Link>
                <Link href="/markets/consensus">Model consensus</Link>
                <div className="group">Portfolio</div>
                <Link href="/portfolios">Portfolios</Link>
                <Link href="/scenarios">Scenarios</Link>
                <Link href="/order-analysis">Order analysis</Link>
                <div className="group">Execution</div>
                <Link href="/execution">Trade analysis</Link>
                <Link href="/execution/simulate">Simulation</Link>
                <Link href="/microstructure">Order book</Link>
                <div className="group">Data</div>
                <Link href="/data">Imports</Link>
                <div className="group">Account</div>
                <Link href="/login">Sign in</Link>
              </nav>
            </aside>
            <main className="main">{children}</main>
          </div>
        </Providers>
      </body>
    </html>
  );
}
