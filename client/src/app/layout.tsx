import type { Metadata, Viewport } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Defiant FleetOps · Capture",
  description: "Scan, capture and keep physical operations moving.",
  applicationName: "Defiant FleetOps",
  manifest: "/manifest.webmanifest",
  icons: { icon: "/icons/icon.svg", apple: "/icons/icon-192.png" },
  appleWebApp: { capable: true, statusBarStyle: "black-translucent", title: "FleetOps" },
};
export const viewport: Viewport = { width: "device-width", initialScale: 1, themeColor: "#112b29" };

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
