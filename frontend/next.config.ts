import type { NextConfig } from "next";

// Set BACKEND_URL on Vercel (e.g. https://hxconvert.up.railway.app) to proxy
// /api through the frontend domain. The browser then stays same-origin, so
// guest/login cookies remain first-party and no CORS is involved. Leave it
// empty when NEXT_PUBLIC_API_URL points at the backend directly.
const backendUrl = (process.env.BACKEND_URL ?? "").trim().replace(/\/+$/, "");

const nextConfig: NextConfig = {
  reactStrictMode: true,
  output: "standalone",
  experimental: {
    optimizePackageImports: ["lucide-react"],
  },
  async rewrites() {
    if (!backendUrl) return [];
    return [
      {
        source: "/api/:path*",
        destination: `${backendUrl}/api/:path*`,
      },
    ];
  },
};

export default nextConfig;
