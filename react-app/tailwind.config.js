/**
 * Copyright (C) 2026 Mugumya Zeal. All rights reserved.
 */

/** @type {import('tailwindcss').Config} */
export default {
  content: [
    './index.html',
    './src/**/*.{ts,tsx}',
  ],
  theme: {
    extend: {
      colors: {
        gray: {
          950: '#030712',
        },
      },
    },
  },
  plugins: [],
};
