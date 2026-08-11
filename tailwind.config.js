/** Dev-only build config -- see package.json's "build:css" script. Mirrors
 * the theme that used to live inline in base.html's `tailwind.config = {...}`
 * for the Play CDN script; kept here now that CSS is precompiled instead. */
module.exports = {
  content: ["./app/templates/**/*.html"],
  darkMode: "class",
  theme: {
    extend: {
      fontFamily: {
        sans: ['"Noto Sans JP"', "-apple-system", "BlinkMacSystemFont", '"Segoe UI"', "sans-serif"],
      },
      colors: {
        accent: { DEFAULT: "#0062CC", dark: "#409CFF" },
        danger: { DEFAULT: "#D70015", dark: "#FF6961" },
        success: { DEFAULT: "#1D7D34", dark: "#32D74B" },
      },
    },
  },
  // These 4 card-placeholder background images are assembled at runtime as
  // bg-[url('{{ placeholder_url }}')] (see items/_results_card.html) -- the
  // full class string never appears literally in any template, so the
  // content scanner above would never generate it without this safelist.
  safelist: [
    "bg-[url('/static/img/card-placeholder-blue.svg')]",
    "bg-[url('/static/img/card-placeholder-yellow.svg')]",
    "bg-[url('/static/img/card-placeholder-red.svg')]",
    "bg-[url('/static/img/card-placeholder-green.svg')]",
  ],
  plugins: [],
};
