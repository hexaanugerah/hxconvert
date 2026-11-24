import { fileURLToPath } from "node:url";

import { defineConfig } from "vitest/config";

export default defineConfig({
  resolve: {
    alias: { "@": fileURLToPath(new URL(".", import.meta.url)) },
  },
  // tsconfig keeps `jsx: "preserve"` for Next.js, so the test transform needs
  // an explicit JSX runtime.
  oxc: { jsx: "react-jsx" },
  test: {
    environment: "node",
    include: ["**/*.test.tsx"],
  },
});
