import { defineConfig } from "vitest/config";

export default defineConfig({
  // tsconfig keeps `jsx: "preserve"` for Next.js, so the test transform needs
  // an explicit JSX runtime.
  oxc: { jsx: "react-jsx" },
  test: {
    environment: "node",
    include: ["**/*.test.tsx"],
  },
});
