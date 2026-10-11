import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  turbopack: {
    root: __dirname,  // Lock workspace root to this dashboard dir (parent has unrelated package-lock.json)
  },
  // No output: "standalone": it is incompatible with `next start` (which is
  // how the dashboard service runs) and gives 500s on /_next/static chunks
  // after a rebuild. Use it only with `node .next/standalone/server.js`.
  images: {
    unoptimized: true,   // No external image optimizer needed
  },
  // Production optimizations
  poweredByHeader: false,
  compress: true,
  // Environment-based API URL. Default is the same-origin proxy so the
  // API key stays server-side and other LAN devices can use the dashboard.
  env: {
    NEXT_PUBLIC_API_URL: process.env.NEXT_PUBLIC_API_URL || "/api/pai",
  },
  // Three.js + TalkingHead loaded client-side via dynamic(() => import(), { ssr: false })
  // TalkingHead's dynamic lipsync imports are patched to static import map via postinstall.
};

export default nextConfig;
