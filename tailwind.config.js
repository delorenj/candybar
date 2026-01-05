/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    "./src-next/pages/**/*.{js,ts,jsx,tsx,mdx}",
    "./src-next/components/**/*.{js,ts,jsx,tsx,mdx}",
    "./src-next/app/**/*.{js,ts,jsx,tsx,mdx}",
    "./src-next/containers/**/*.{js,ts,jsx,tsx,mdx}",
  ],
  theme: {
    extend: {},
  },
  plugins: [
    require('@tailwindcss/forms'),
  ],
}