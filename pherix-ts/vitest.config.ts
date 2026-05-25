import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    include: ["test/**/*.test.ts"],
    // The whole suite runs offline — no network, no API key, same discipline
    // as the Python suite. A single fork keeps the global tool REGISTRY from
    // being shared across parallel workers (it is module-level state, exactly
    // like Python's REGISTRY); each test file clears it in beforeEach.
    pool: "forks",
    environment: "node",
  },
});
