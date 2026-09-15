import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Bundles only what's needed to run (server + resolved deps) into
  // .next/standalone -- Dockerfile.prod copies that instead of shipping
  // the full node_modules into the production image.
  output: "standalone",
};

export default nextConfig;
