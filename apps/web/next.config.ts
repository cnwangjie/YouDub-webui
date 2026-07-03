import type { NextConfig } from "next";

function apiProxyTarget() {
  const configured =
    process.env.NEXT_SERVER_API_BASE_URL ||
    process.env.NEXT_PUBLIC_API_BASE_URL ||
    "http://127.0.0.1:8000";
  return configured.replace(/\/$/, "");
}

const nextConfig: NextConfig = {
  allowedDevOrigins: ["alex", "**.*"],
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: `${apiProxyTarget()}/api/:path*`,
      },
    ];
  },
};

export default nextConfig;
