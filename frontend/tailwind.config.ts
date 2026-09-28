import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./src/**/*.{js,ts,jsx,tsx,mdx}"],
  theme: {
    extend: {
      colors: {
        // The accent: a light, fresh green. Fills use the light end (300-400)
        // with dark green text on them (brand-950 on brand-400 is ~9:1), so
        // they stay readable; text and links use the dark end (700-800).
        brand: {
          50: "#f1fdf4",
          100: "#dcfce6",
          200: "#bbf7d0",
          300: "#86efac",
          400: "#4ade80",
          500: "#22c55e",
          600: "#16a34a",
          700: "#15803d",
          800: "#166534",
          900: "#14532d",
          950: "#052e16",
        },
      },
    },
  },
  plugins: [],
};

export default config;
