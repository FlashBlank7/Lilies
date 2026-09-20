import type { NextConfig } from 'next'

const nextConfig: NextConfig = {
  output: 'standalone',
  allowedDevOrigins: [
    '127.0.0.1',
    ...(process.env.ALLOWED_DEV_ORIGINS || '').split(',').map((origin) => origin.trim()).filter(Boolean),
  ],
}

export default nextConfig
