/** @type {import('next').NextConfig} */
const nextConfig = {
  // A self-contained server for the production image (deploy/frontend.Dockerfile).
  output: "standalone",
  // The browser reaches the API at /api on this origin (src/lib/api.ts), so
  // the session cookie is first-party and no CORS is involved. Server
  // components call API_BASE directly (src/lib/server.ts).
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${process.env.API_BASE ?? "http://localhost:8000"}/:path*` }];
  },
};

module.exports = nextConfig;
