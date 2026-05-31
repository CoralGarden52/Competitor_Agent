/** @type {import('next').NextConfig} */
const nextConfig = {
  typedRoutes: true,
  async rewrites() {
    return [
      { source: '/runs/:path*', destination: 'http://127.0.0.1:8010/runs/:path*' },
      { source: '/collector/:path*', destination: 'http://127.0.0.1:8010/collector/:path*' },
      { source: '/schema/:path*', destination: 'http://127.0.0.1:8010/schema/:path*' },
      { source: '/healthz', destination: 'http://127.0.0.1:8010/healthz' },
    ];
  },
};

export default nextConfig;
