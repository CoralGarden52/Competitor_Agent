/** @type {import('next').NextConfig} */
const backendUrl = (process.env.BACKEND_URL || process.env.NEXT_PUBLIC_BACKEND_URL || 'http://127.0.0.1:8010').replace(/\/$/, '');

const nextConfig = {
  typedRoutes: true,
  async rewrites() {
    return [
      { source: '/runs/:path*', destination: `${backendUrl}/runs/:path*` },
      { source: '/collector/:path*', destination: `${backendUrl}/collector/:path*` },
      { source: '/schema/:path*', destination: `${backendUrl}/schema/:path*` },
      { source: '/healthz', destination: `${backendUrl}/healthz` },
    ];
  },
};

export default nextConfig;
