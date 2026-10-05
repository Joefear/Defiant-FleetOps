import { build } from "esbuild";

// One bundle gives the service worker the exact same queue and sync logic as tests.
await build({
  entryPoints: ["src/worker/service-worker.ts"],
  outfile: "public/sw.js",
  bundle: true,
  format: "iife",
  platform: "browser",
  target: "es2022",
  minify: true,
});
